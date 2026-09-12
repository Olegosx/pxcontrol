"""Обслуживание сообществ: чистка служебных записей (ADR-0026).

Служебные записи («такой-то вступил», «сообщение закреплено») копятся
в ленте активной группы и мешают читать. Серверного фильтра «только
служебные» у Telegram нет, поэтому история просматривается страницами
по сотне, а отбор идёт у нас — цена работы линейна по длине истории,
и потому у неё две обязательные границы: **глубина просмотра**
и **потолок удаления за проход**.

Работа идёт двумя шагами: «просмотреть» ничего не меняет и отвечает,
сколько чего нашлось; «почистить» удаляет выбранные виды. Удаление
необратимо, а ошибиться в наборе видов легко — поэтому разделение
не удобство, а правило (ADR-0026).

Жизненный цикл заданий держит общий каркас (:mod:`pxcontrol.engine.jobs`,
ADR-0025), темп обращений к Telegram — дорожка аккаунта (ADR-0024):
между страницами она пропускает вперёд публикацию, а флуд-лимит
останавливает проход целиком. Только userbot: список участников и
история чужими глазами боту недоступны, а удалять чужие сообщения
может лишь администратор с правом «удалять сообщения».
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.jobs import Job, JobCancelled, JobQueue, JobStatus
from pxcontrol.engine.services.communities import CommunitiesService, CommunityDto
from pxcontrol.engine.telegram.types import (
	CommunityInfo,
	ServiceMessageKind,
	ServiceMessagesPage,
)

logger = logging.getLogger(__name__)

#: Сколько сообщений читать одним запросом (предел Telegram — 100).
PAGE_SIZE = 100

#: Сколько последних сообщений просматривать по умолчанию: два десятка
#: запросов к Telegram. Серверного фильтра нет, и цена просмотра линейна
#: по длине истории — бесконтрольный проход по каналу в десятки тысяч
#: постов упёрся бы во флуд-лимит (ADR-0026).
DEFAULT_DEPTH = 2000

#: Пределы глубины просмотра: правило одно на движок и интерфейс.
DEPTH_RANGE = (PAGE_SIZE, 100_000)

#: Сколько записей удалять за один проход по умолчанию.
DEFAULT_DELETE_LIMIT = 500

#: Пределы потолка удаления. Нижняя граница — 1: удалить одну запись
#: должно быть можно (то же правило у чистки мёртвых аккаунтов).
DELETE_LIMIT_RANGE = (1, 10_000)

#: Виды записей, предлагаемые к чистке по умолчанию: шум от участников.
DEFAULT_KINDS = (ServiceMessageKind.MEMBERS,)


class MaintenanceError(EngineError):
	"""Ошибка обслуживания сообщества (с понятным человеку текстом)."""


class _MaintenancePort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def check_community_userbot(self, account_id: int, chat_ref: str) -> CommunityInfo: ...

	async def service_messages_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage: ...

	async def delete_messages(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> int: ...


@dataclass(frozen=True)
class ServiceScanReport:
	"""Что нашёл просмотр истории.

	Attributes:
		found: сколько записей каждого вида найдено (пустых видов нет).
		scanned: сколько сообщений просмотрено — включая обычные.
		oldest_date: до какого числа дошёл просмотр; None — записей нет.
		exhausted: история кончилась раньше, чем глубина просмотра
			(значит, просмотрено всё — дальше смотреть нечего).
	"""

	found: dict[ServiceMessageKind, int] = field(default_factory=dict)
	scanned: int = 0
	oldest_date: datetime | None = None
	exhausted: bool = False

	@property
	def removable(self) -> int:
		"""Сколько найденных записей вообще можно удалять."""
		return sum(count for kind, count in self.found.items() if kind.removable())


@dataclass(frozen=True)
class ServiceCleanReport:
	"""Чем кончилась чистка.

	Attributes:
		deleted: сколько записей удалено.
		skipped: сколько Telegram отказался удалять (защищённые им
			служебные записи — отказ не считается сбоем, ADR-0026).
		scanned: сколько сообщений просмотрено по пути.
		limited: чистка остановилась о потолок за проход (осталось ещё).
	"""

	deleted: int = 0
	skipped: int = 0
	scanned: int = 0
	limited: bool = False


@dataclass(frozen=True)
class MaintenanceItemDto:
	"""Задание обслуживания для интерфейса.

	Attributes:
		id: идентификатор задания (для отмены и снятия с показа).
		title: что и где делается («Просмотр · Мой канал»).
		community_id: сообщество-цель.
		status: состояние задания (общий набор очередей, ADR-0025).
		progress: доля выполнения 0.0..1.0 (по глубине просмотра).
		error: текст ошибки (для статуса ERROR).
		note: пометка состояния (ход просмотра).
		scan: итог просмотра; None — задание другое или не закончено.
		clean: итог чистки; None — задание другое или не закончено.
	"""

	id: int
	title: str
	community_id: int
	status: JobStatus
	progress: float
	error: str | None
	note: str | None
	scan: ServiceScanReport | None = None
	clean: ServiceCleanReport | None = None


class _MaintenanceJob(Job):
	"""Задание обслуживания: что делаем, где и с какими границами."""

	def __init__(
		self,
		job_id: int,
		community: CommunityDto,
		account_id: int,
		*,
		clean: bool,
		kinds: tuple[ServiceMessageKind, ...],
		depth: int,
		delete_limit: int,
	) -> None:
		super().__init__(job_id)
		self.community = community
		self.account_id = account_id
		self.clean = clean
		self.kinds = kinds
		self.depth = depth
		self.delete_limit = delete_limit
		self.scan_report: ServiceScanReport | None = None
		self.clean_report: ServiceCleanReport | None = None

	def dto(self) -> MaintenanceItemDto:
		"""Снимок задания для интерфейса."""
		what = "Чистка" if self.clean else "Просмотр"
		return MaintenanceItemDto(
			id=self.id,
			title=f"{what} · {self.community.title}",
			community_id=self.community.id,
			status=self.status,
			progress=self.progress,
			error=self.error,
			note=self.note,
			scan=self.scan_report,
			clean=self.clean_report,
		)


def selectable_kinds(kinds: Sequence[ServiceMessageKind]) -> tuple[ServiceMessageKind, ...]:
	"""Отсеивает виды, которые удалять нельзя (ADR-0026).

	Защищённые записи не должны попадать в чистку ни при каком наборе
	галочек: корень темы форума — это сама тема, а создание сообщества
	и передача владения — его история. Правило движка, а не интерфейса:
	будущий вход в обслуживание (расписание уборки) обязан подчиняться
	ему так же.
	"""
	return tuple(kind for kind in dict.fromkeys(kinds) if kind.removable())


class MaintenanceService:
	"""Обслуживание сообществ: просмотр и чистка служебных записей."""

	def __init__(
		self,
		gateway: _MaintenancePort,
		communities: CommunitiesService,
	) -> None:
		self._gateway = gateway
		self._communities = communities
		self._jobs: JobQueue[_MaintenanceJob] = JobQueue(
			self._run_job,
			name="Обслуживание",
			# задания не переживают перезапуск: истина — сам Telegram,
			# повторный проход даст ту же картину (как у обработки видео)
			cancel_pending_on_shutdown=True,
		)

	async def scan_service_messages(
		self,
		community_id: int,
		*,
		depth: int = DEFAULT_DEPTH,
	) -> int:
		"""Ставит задание «просмотреть служебные записи» (ничего не меняет).

		Просмотр считает все виды разом — выбирать их человеку предстоит
		уже по результату, зная числа.

		Returns:
			Идентификатор задания очереди.

		Raises:
			MaintenanceError: Сообщество не найдено, у него нет
				userbot-публикатора или глубина вне допустимых границ.
		"""
		return await self._enqueue(community_id, clean=False, kinds=(), depth=depth)

	async def clean_service_messages(
		self,
		community_id: int,
		kinds: Sequence[ServiceMessageKind],
		*,
		depth: int = DEFAULT_DEPTH,
		delete_limit: int = DEFAULT_DELETE_LIMIT,
	) -> int:
		"""Ставит задание «удалить служебные записи выбранных видов».

		Права проверяются здесь же, живым зондом: удалять чужие
		сообщения может только администратор с правом «удалять
		сообщения», и роль сама по себе его не гарантирует.

		Returns:
			Идентификатор задания очереди.

		Raises:
			MaintenanceError: Сообщество не найдено, нет публикатора,
				не выбран ни один вид, границы негодны или у аккаунта
				нет права удалять.
			UserbotUnavailableError: Проверить права не удалось.
		"""
		chosen = selectable_kinds(kinds)
		if not chosen:
			raise MaintenanceError("Не выбрано ни одного вида записей — чистить нечего.")
		return await self._enqueue(
			community_id,
			clean=True,
			kinds=chosen,
			depth=depth,
			delete_limit=delete_limit,
		)

	async def state(self) -> list[MaintenanceItemDto]:
		"""Снимок очереди обслуживания для интерфейса."""
		return [job.dto() for job in self._jobs.all()]

	async def cancel(self, item_id: int) -> None:
		"""Отменяет задание: ожидающее убирается, идущее — прекращается.

		Идущее задание останавливается между страницами: рвать
		обращение к Telegram посреди удаления незачем — часть записей
		уже удалена, и честный отчёт дороже мгновенной остановки.
		"""
		job = self._jobs.get(item_id)
		if job is None:
			return
		if job.status is JobStatus.PENDING:
			job.status = JobStatus.CANCELLED
			logger.info("Обслуживание id=%s отменено (ждало).", item_id)
		elif job.status is JobStatus.RUNNING:
			self._jobs.request_cancel(job)

	async def retry(self, item_id: int) -> None:
		"""Повторяет задание с ошибкой (границы и виды — прежние)."""
		job = self._jobs.get(item_id)
		if job is None or job.status is not JobStatus.ERROR:
			return
		job.status = JobStatus.PENDING
		job.progress = 0.0
		job.error = None
		job.note = None
		job.cancel_requested = False
		self._jobs.ensure_worker()
		logger.info("Обслуживание id=%s возвращено в очередь на повтор.", item_id)

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённое задание из списка (живые не трогаются)."""
		job = self._jobs.get(item_id)
		if job is not None and job.status.finished():
			self._jobs.remove(job)

	async def settle(self) -> None:
		"""Дожидается простоя очереди (детерминированная точка для тестов)."""
		await self._jobs.wait_idle()

	async def shutdown(self) -> None:
		"""Гасит очередь при остановке движка (ADR-0020)."""
		await self._jobs.shutdown()

	# --- постановка -----------------------------------------------------------

	async def _enqueue(
		self,
		community_id: int,
		*,
		clean: bool,
		kinds: tuple[ServiceMessageKind, ...],
		depth: int,
		delete_limit: int = DEFAULT_DELETE_LIMIT,
	) -> int:
		"""Общая постановка задания: проверки, затем очередь."""
		_check_range("Глубина просмотра", depth, DEPTH_RANGE)
		if clean:
			_check_range("Предел удаления за проход", delete_limit, DELETE_LIMIT_RANGE)
		community = await self._community(community_id)
		account_id = community.default_account_id
		if account_id is None:
			raise MaintenanceError(
				f"У «{community.title}» нет userbot-публикатора — обслуживание "
				"доступно только ему: список участников и чужую историю "
				"бот прочитать не может."
			)
		if clean:
			await self._require_delete_right(community, account_id)
		job = _MaintenanceJob(
			self._jobs.new_id(),
			community,
			account_id,
			clean=clean,
			kinds=kinds,
			depth=depth,
			delete_limit=delete_limit,
		)
		self._jobs.add(job)
		self._jobs.ensure_worker()
		logger.info(
			"Обслуживание «%s»: %s (id=%s, глубина %d%s).",
			community.title,
			"чистка " + ", ".join(kinds) if clean else "просмотр",
			job.id,
			depth,
			f", не больше {delete_limit}" if clean else "",
		)
		return job.id

	async def _community(self, community_id: int) -> CommunityDto:
		"""Сообщество по id — или понятный отказ.

		Raises:
			MaintenanceError: Сообщество не найдено (например, удалено).
		"""
		try:
			return await self._communities.get_community(community_id)
		except EngineError as exc:
			raise MaintenanceError(str(exc)) from exc

	async def _require_delete_right(self, community: CommunityDto, account_id: int) -> None:
		"""Требует у аккаунта право удалять чужие сообщения.

		Проверка живая: права меняются в Telegram без нашего ведома,
		а начинать проход, который упрётся в отказ на первой же пачке,
		незачем.

		Raises:
			MaintenanceError: Права нет.
			UserbotUnavailableError: Проверить не удалось (нет связи).
		"""
		info = await self._gateway.check_community_userbot(account_id, community.tg_chat_id)
		if not info.can_delete:
			raise MaintenanceError(
				f"У публикатора «{community.title}» нет права удалять сообщения — "
				"выдайте аккаунту право «Удаление сообщений» в настройках "
				"администраторов Telegram."
			)

	# --- выполнение -----------------------------------------------------------

	async def _run_job(self, job: _MaintenanceJob) -> None:
		"""Выполняет задание: проход по истории, при чистке — удаление.

		Исход записывает каркас (ADR-0025); здесь — предметное.

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
			UserbotUnavailableError: Telegram отказал (в том числе
				флуд-лимитом) — проход прекращается, отчёт не сохраняется.
		"""
		found: dict[ServiceMessageKind, int] = {}
		deleted = skipped = scanned = 0
		offset_id = 0
		oldest_date: datetime | None = None
		exhausted = False
		limited = False
		while scanned < job.depth:
			self._check_stop(job)
			page = await self._gateway.service_messages_page(
				job.account_id,
				job.community.tg_chat_id,
				offset_id,
				min(PAGE_SIZE, job.depth - scanned),
			)
			scanned += page.scanned
			oldest_date = page.oldest_date or oldest_date
			for message in page.messages:
				found[message.kind] = found.get(message.kind, 0) + 1
			if job.clean:
				batch = [m.id for m in page.messages if m.kind in job.kinds]
				room = job.delete_limit - deleted
				if len(batch) > room:
					batch = batch[:room]
					limited = True
				if batch:
					gone = await self._gateway.delete_messages(
						job.account_id, job.community.tg_chat_id, batch
					)
					deleted += gone
					skipped += len(batch) - gone
			job.progress = min(1.0, scanned / job.depth)
			job.note = f"просмотрено {scanned}"
			if page.next_offset_id is None:
				exhausted = True
				break
			if job.clean and deleted >= job.delete_limit:
				limited = True
				break
			offset_id = page.next_offset_id
		job.note = None
		if job.clean:
			job.clean_report = ServiceCleanReport(
				deleted=deleted, skipped=skipped, scanned=scanned, limited=limited
			)
			logger.info(
				"Обслуживание id=%s: удалено %d, пропущено %d, просмотрено %d.",
				job.id,
				deleted,
				skipped,
				scanned,
			)
			return
		job.scan_report = ServiceScanReport(
			found=found, scanned=scanned, oldest_date=oldest_date, exhausted=exhausted
		)
		logger.info(
			"Обслуживание id=%s: просмотрено %d, служебных найдено %d.",
			job.id,
			scanned,
			sum(found.values()),
		)

	def _check_stop(self, job: _MaintenanceJob) -> None:
		"""Прерывает проход между страницами по отмене или остановке.

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
		"""
		if job.cancel_requested or self._jobs.stopping:
			raise JobCancelled


def _check_range(what: str, value: int, limits: tuple[int, int]) -> None:
	"""Проверяет, что число в допустимых границах.

	Raises:
		MaintenanceError: Значение вне границ.
	"""
	low, high = limits
	if not low <= value <= high:
		raise MaintenanceError(f"{what}: допустимо от {low} до {high}, а указано {value}.")
