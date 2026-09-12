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
from enum import StrEnum
from typing import Protocol

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.jobs import Job, JobCancelled, JobQueue, JobStatus
from pxcontrol.engine.services.communities import CommunitiesService, CommunityDto
from pxcontrol.engine.telegram.types import (
	CommunityInfo,
	ParticipantsPage,
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

#: Сколько участников читать одним запросом (предел Telegram — 200).
MEMBERS_PAGE_SIZE = 200

#: Сколько удалённых аккаунтов исключать за проход по умолчанию.
#: Умолчание намеренно скромное: число участников — видимая всем
#: величина, и резкое падение бьёт по охватам сообщества.
DEFAULT_KICK_LIMIT = 20

#: Пределы потолка исключений. Нижняя граница — 1: убрать один
#: мёртвый аккаунт должно быть можно.
KICK_LIMIT_RANGE = (1, 1000)


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

	async def participants_page(
		self, account_id: int, chat_id: str, offset: int, limit: int
	) -> ParticipantsPage: ...

	async def kick_participant(self, account_id: int, chat_id: str, user_id: int) -> int | None: ...


class MaintenanceTarget(StrEnum):
	"""Что обслуживаем (ADR-0026)."""

	#: служебные записи в ленте («вступил», «закреплено»)
	SERVICE_MESSAGES = "service_messages"
	#: удалённые аккаунты в списке участников
	DELETED_ACCOUNTS = "deleted_accounts"


@dataclass(frozen=True)
class ServiceReport:
	"""Итог прохода по служебным записям.

	Отчёт один на оба шага: просмотр — это тот же проход, просто
	без удаления, и у него честные нули в полях чистки.

	Attributes:
		found: сколько записей каждого вида найдено (пустых видов нет).
		scanned: сколько сообщений просмотрено — включая обычные.
		deleted: сколько записей удалено (просмотр — 0).
		skipped: сколько Telegram отказался удалять (защищённые им
			записи — отказ не считается сбоем).
		limited: чистка остановилась о потолок за проход.
		exhausted: история кончилась раньше глубины — дальше смотреть
			нечего.
		oldest_date: до какого числа дошёл проход; None — записей нет.
	"""

	found: dict[ServiceMessageKind, int] = field(default_factory=dict)
	scanned: int = 0
	deleted: int = 0
	skipped: int = 0
	limited: bool = False
	exhausted: bool = False
	oldest_date: datetime | None = None

	@property
	def removable(self) -> int:
		"""Сколько найденных записей вообще можно удалять."""
		return sum(count for kind, count in self.found.items() if kind.removable())


@dataclass(frozen=True)
class MembersReport:
	"""Итог прохода по участникам.

	Attributes:
		found: сколько удалённых аккаунтов найдено.
		scanned: сколько участников просмотрено.
		total: сколько участников всего, по мнению Telegram (None —
			не сказал).
		removed: сколько удалённых аккаунтов исключено (просмотр — 0).
		service_left: сколько служебных записей об исключении осталось
			в ленте — их не удалось убрать без права удалять сообщения.
		limited: чистка остановилась о потолок за проход.
		exhausted: список участников кончился.
		capped: Telegram перестал отдавать участников раньше, чем
			кончился список (``scanned`` меньше ``total``). Признак
			того самого предела выдачи, который обсуждался при
			проектировании (ADR-0026) — теперь он виден в отчёте,
			а не предполагается.
	"""

	found: int = 0
	scanned: int = 0
	total: int | None = None
	removed: int = 0
	service_left: int = 0
	limited: bool = False
	exhausted: bool = False
	capped: bool = False


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
		service: итог по служебным записям; None — задание про участников
			или ещё не закончено.
		members: итог по участникам; None — задание про записи или ещё
			не закончено.
	"""

	id: int
	title: str
	community_id: int
	status: JobStatus
	progress: float
	error: str | None
	note: str | None
	service: ServiceReport | None = None
	members: MembersReport | None = None


class _MaintenanceJob(Job):
	"""Задание обслуживания: что делаем, где и с какими границами."""

	def __init__(
		self,
		job_id: int,
		community: CommunityDto,
		account_id: int,
		*,
		target: MaintenanceTarget,
		clean: bool,
		kinds: tuple[ServiceMessageKind, ...] = (),
		depth: int = DEFAULT_DEPTH,
		limit: int = DEFAULT_DELETE_LIMIT,
		can_delete: bool = False,
	) -> None:
		super().__init__(job_id)
		self.community = community
		self.account_id = account_id
		self.target = target
		self.clean = clean
		self.kinds = kinds
		self.depth = depth
		self.limit = limit
		#: можно ли убрать служебные записи, которые породят исключения
		self.can_delete = can_delete
		self.service_report: ServiceReport | None = None
		self.members_report: MembersReport | None = None

	def dto(self) -> MaintenanceItemDto:
		"""Снимок задания для интерфейса."""
		what = "Чистка" if self.clean else "Просмотр"
		where = (
			"служебных записей"
			if self.target is MaintenanceTarget.SERVICE_MESSAGES
			else "удалённых аккаунтов"
		)
		return MaintenanceItemDto(
			id=self.id,
			title=f"{what} {where} · {self.community.title}",
			community_id=self.community.id,
			status=self.status,
			progress=self.progress,
			error=self.error,
			note=self.note,
			service=self.service_report,
			members=self.members_report,
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
		_check_range("Глубина просмотра", depth, DEPTH_RANGE)
		community, account_id = await self._target(community_id)
		return self._put(
			_MaintenanceJob(
				self._jobs.new_id(),
				community,
				account_id,
				target=MaintenanceTarget.SERVICE_MESSAGES,
				clean=False,
				depth=depth,
			)
		)

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
		_check_range("Глубина просмотра", depth, DEPTH_RANGE)
		_check_range("Предел удаления за проход", delete_limit, DELETE_LIMIT_RANGE)
		community, account_id = await self._target(community_id)
		rights = await self._rights(community, account_id)
		if not rights.can_delete:
			raise MaintenanceError(
				f"У публикатора «{community.title}» нет права удалять сообщения — "
				"выдайте аккаунту право «Удаление сообщений» в настройках "
				"администраторов Telegram."
			)
		return self._put(
			_MaintenanceJob(
				self._jobs.new_id(),
				community,
				account_id,
				target=MaintenanceTarget.SERVICE_MESSAGES,
				clean=True,
				kinds=chosen,
				depth=depth,
				limit=delete_limit,
			)
		)

	async def scan_deleted_accounts(self, community_id: int) -> int:
		"""Ставит задание «найти удалённые аккаунты» (никого не трогает).

		Список участников доступен только администратору, поэтому
		проход может упереться в отказ — тогда задание честно
		завершится ошибкой, а не пустым списком.

		Returns:
			Идентификатор задания очереди.

		Raises:
			MaintenanceError: Сообщество не найдено или нет публикатора.
		"""
		community, account_id = await self._target(community_id)
		return self._put(
			_MaintenanceJob(
				self._jobs.new_id(),
				community,
				account_id,
				target=MaintenanceTarget.DELETED_ACCOUNTS,
				clean=False,
			)
		)

	async def clean_deleted_accounts(
		self,
		community_id: int,
		*,
		limit: int = DEFAULT_KICK_LIMIT,
	) -> int:
		"""Ставит задание «исключить удалённые аккаунты».

		Потолок за проход обязателен и не может быть меньше единицы:
		резкое падение числа участников бьёт по охватам, и уменьшать
		его человек должен порциями, которые сам выбрал (ADR-0026).

		Исключение в супергруппе порождает служебную запись «X удалил
		Y» — чистка убирает их за собой, если у аккаунта есть право
		удалять сообщения; если права нет, записи остаются, и это
		сказано в отчёте.

		Returns:
			Идентификатор задания очереди.

		Raises:
			MaintenanceError: Сообщество не найдено, нет публикатора,
				потолок вне границ или у аккаунта нет права исключать.
			UserbotUnavailableError: Проверить права не удалось.
		"""
		_check_range("Предел исключений за проход", limit, KICK_LIMIT_RANGE)
		community, account_id = await self._target(community_id)
		rights = await self._rights(community, account_id)
		if not rights.can_ban:
			raise MaintenanceError(
				f"У публикатора «{community.title}» нет права исключать участников — "
				"выдайте аккаунту право «Блокировка пользователей» в настройках "
				"администраторов Telegram."
			)
		return self._put(
			_MaintenanceJob(
				self._jobs.new_id(),
				community,
				account_id,
				target=MaintenanceTarget.DELETED_ACCOUNTS,
				clean=True,
				limit=limit,
				can_delete=rights.can_delete,
			)
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

	async def drop_community(self, community_id: int) -> None:
		"""Снимает задания удалённого сообщества (ADR-0026).

		Задание держит снимок сообщества и работает по его
		``tg_chat_id``, от строки в БД не завися: без этого шага уборка
		продолжала бы удалять записи и исключать участников в Telegram
		для сущности, которой в приложении уже нет, а её окно закрыто.
		Ожидающие снимаются сразу, идущему взводится отмена — оно
		остановится между страницами (как и по кнопке «Отменить»).
		"""
		for job in self._jobs.all():
			if job.community.id != community_id:
				continue
			if job.status is JobStatus.PENDING:
				job.status = JobStatus.CANCELLED
			elif job.status is JobStatus.RUNNING:
				self._jobs.request_cancel(job)
			else:
				continue
			logger.info(
				"Обслуживание id=%s снято: сообщество «%s» удалено.",
				job.id,
				job.community.title,
			)

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

	def _put(self, job: _MaintenanceJob) -> int:
		"""Ставит готовое задание в очередь и будит воркера."""
		self._jobs.add(job)
		self._jobs.ensure_worker()
		logger.info(
			"Обслуживание «%s»: %s %s (id=%s).",
			job.community.title,
			"чистка" if job.clean else "просмотр",
			job.target,
			job.id,
		)
		return job.id

	async def _target(self, community_id: int) -> tuple[CommunityDto, int]:
		"""Сообщество и его аккаунт-публикатор — или понятный отказ.

		Raises:
			MaintenanceError: Сообщество не найдено или у него нет
				userbot-публикатора.
		"""
		try:
			community = await self._communities.get_community(community_id)
		except EngineError as exc:
			raise MaintenanceError(str(exc)) from exc
		if community.default_account_id is None:
			raise MaintenanceError(
				f"У «{community.title}» нет userbot-публикатора — обслуживание "
				"доступно только ему: список участников и чужую историю "
				"бот прочитать не может."
			)
		return community, community.default_account_id

	async def _rights(self, community: CommunityDto, account_id: int) -> CommunityInfo:
		"""Живой зонд прав аккаунта в сообществе.

		Права меняются в Telegram без нашего ведома, а начинать проход,
		который упрётся в отказ на первой же пачке, незачем.

		Raises:
			UserbotUnavailableError: Проверить не удалось (нет связи).
		"""
		return await self._gateway.check_community_userbot(account_id, community.tg_chat_id)

	# --- выполнение -----------------------------------------------------------

	async def _run_job(self, job: _MaintenanceJob) -> None:
		"""Выполняет задание; исход записывает каркас (ADR-0025).

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
			UserbotUnavailableError: Telegram отказал (в том числе
				флуд-лимитом) — проход прекращается, отчёт не сохраняется.
		"""
		if job.target is MaintenanceTarget.SERVICE_MESSAGES:
			await self._run_service_messages(job)
			return
		await self._run_deleted_accounts(job)

	async def _run_service_messages(self, job: _MaintenanceJob) -> None:
		"""Проход по истории: считает служебные записи, при чистке удаляет."""
		found: dict[ServiceMessageKind, int] = {}
		deleted = skipped = scanned = 0
		offset_id = 0
		oldest_date: datetime | None = None
		exhausted = False
		limited = False
		try:
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
					room = job.limit - deleted
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
				if job.clean and deleted >= job.limit:
					limited = True
					break
				offset_id = page.next_offset_id
		finally:
			# итог в журнал при любом исходе, включая отмену и флуд-лимит:
			# удаление необратимо, и разбирать инцидент по «ничего
			# не сохранилось» нечем. Отчёт при обрыве не строится
			# намеренно (ADR-0026, п. 7), но след обязан остаться
			logger.info(
				"Обслуживание id=%s: просмотрено %d, служебных %d, удалено %d, пропущено %d.",
				job.id,
				scanned,
				sum(found.values()),
				deleted,
				skipped,
			)
		job.note = None
		job.service_report = ServiceReport(
			found=found,
			scanned=scanned,
			deleted=deleted,
			skipped=skipped,
			limited=limited,
			exhausted=exhausted,
			oldest_date=oldest_date,
		)

	async def _run_deleted_accounts(self, job: _MaintenanceJob) -> None:
		"""Проход по участникам: ищет удалённые учётки, при чистке исключает.

		Исключение — блокировка со снятием; в супергруппе оно порождает
		служебную запись «X удалил Y». Записи копятся и убираются пачкой
		в конце: удалять их по одной — лишний запрос на каждого
		исключённого.
		"""
		found = removed = scanned = 0
		offset = 0
		total: int | None = None
		exhausted = False
		limited = False
		service_ids: list[int] = []
		completed = False
		try:
			while True:
				self._check_stop(job)
				page = await self._gateway.participants_page(
					job.account_id, job.community.tg_chat_id, offset, MEMBERS_PAGE_SIZE
				)
				scanned += page.scanned
				total = page.total if page.total is not None else total
				found += len(page.deleted_ids)
				if job.clean:
					for user_id in page.deleted_ids:
						if removed >= job.limit:
							limited = True
							break
						self._check_stop(job)
						service_id = await self._gateway.kick_participant(
							job.account_id, job.community.tg_chat_id, user_id
						)
						removed += 1
						if service_id is not None:
							service_ids.append(service_id)
				job.note = f"просмотрено участников {scanned}"
				if total:
					job.progress = min(1.0, scanned / total)
				if page.next_offset is None:
					exhausted = True
					break
				if job.clean and removed >= job.limit:
					limited = True
					break
				offset = page.next_offset
			completed = True
		finally:
			# исключение участника необратимо: итог в журнал при любом
			# исходе, включая отмену и флуд-лимит
			logger.info(
				"Обслуживание id=%s: участников просмотрено %d из %s, мёртвых %d, исключено %d.",
				job.id,
				scanned,
				total if total is not None else "?",
				found,
				removed,
			)
			if not completed and service_ids:
				# уборка за собой идёт после прохода, и обрыв её отменяет:
				# записи «X удалил Y» остаются в ленте. Молчать об этом
				# нельзя — обещание «чистка убирает за собой» не сбылось
				logger.warning(
					"Обслуживание id=%s прервано: %d служебных записей об исключении "
					"остались в ленте — уберите их чисткой служебных записей.",
					job.id,
					len(service_ids),
				)
		left = await self._sweep_service_notes(job, service_ids)
		job.note = None
		job.members_report = MembersReport(
			found=found,
			scanned=scanned,
			total=total,
			removed=removed,
			service_left=left,
			limited=limited,
			exhausted=exhausted,
			# Telegram перестал отдавать участников раньше конца списка
			capped=exhausted and total is not None and scanned < total,
		)

	async def _sweep_service_notes(self, job: _MaintenanceJob, ids: list[int]) -> int:
		"""Убирает служебные записи, которые породили исключения.

		Чистка мёртвых душ сама производит тот мусор, который убирает
		первый вид обслуживания, — оставлять его нечестно. Без права
		удалять сообщения записи остаются: сколько именно, скажет отчёт.

		Returns:
			Сколько записей осталось в ленте.
		"""
		if not ids:
			return 0
		if not job.can_delete:
			return len(ids)
		left = 0
		for start in range(0, len(ids), PAGE_SIZE):
			batch = ids[start : start + PAGE_SIZE]
			gone = await self._gateway.delete_messages(
				job.account_id, job.community.tg_chat_id, batch
			)
			left += len(batch) - gone
		return left

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
