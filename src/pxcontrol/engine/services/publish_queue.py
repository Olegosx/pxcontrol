"""Очередь отправки постов: последовательно, с прогрессом и отменой.

Очередь персистентная (ADR-0016): элементы хранятся в таблице
``publish_queue_items`` и переживают перезапуск приложения. Отложенный
черновик занимает слот отложек Telegram (лимит — 100 на канал,
:data:`TELEGRAM_MAX_SCHEDULED`); свободного слота нет — элемент ждёт
в состоянии WAITING, слоты перепроверяются раз в N минут
(настройка ``QUEUE_SLOT_POLL_MINUTES``). Пост «сейчас» слота не занимает
и не ждёт никогда. Файл ждущего поста переносится в папку очереди
(``stash_for_queue``) — его нельзя случайно удалить из «Готовых видео».

Все методы выполняются в цикле движка (вызовы — через мост интерфейса),
поэтому состояние в памяти не требует блокировок; таблица — снимок
для восстановления после перезапуска, истина по вышедшим постам
остаётся каналом (ADR-0010).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import PublishQueueItem
from pxcontrol.engine.jobs import Job, JobCancelled, JobDeferred, JobQueue, JobStatus
from pxcontrol.engine.services.posts import (
	MIN_SCHEDULE_AHEAD,
	PostDraft,
	PostError,
	PostsService,
	TextLimits,
	check_text_length,
	refresh_draft_media,
	text_preview,
)
from pxcontrol.engine.services.settings import QUEUE_SLOT_POLL_MINUTES, SettingsService
from pxcontrol.engine.telegram.mtproto import (
	UserbotScheduleFullError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.types import TELEGRAM_MAX_SCHEDULED, MediaKind, TelegramFloodError

logger = logging.getLogger(__name__)

#: Длина превью текста поста в заголовке элемента очереди.
_TITLE_PREVIEW_CHARS = 60


#: Статусы, которые хранятся в БД (RUNNING не пишется: падение во время
#: отправки при загрузке очереди выглядит как PENDING и уходит повторно).
_PERSISTED = (JobStatus.PENDING, JobStatus.WAITING, JobStatus.ERROR)

#: Статусы, в которых элемент правится (:meth:`PublishQueue.edit`): всё,
#: что ещё не ушло в Telegram. Набор совпадает с ``_PERSISTED`` не случайно
#: (в БД хранится именно неотправленное), но живёт отдельно: правила разные,
#: и расходиться им ничто не мешает. Публичный: по нему интерфейс решает,
#: раскрывать ли карточку формой правки, — правило одно на движок и окно.
EDITABLE_STATUSES = (
	JobStatus.PENDING,
	JobStatus.WAITING,
	JobStatus.ERROR,
)


@dataclass(frozen=True)
class QueueItemDto:
	"""Элемент очереди для интерфейса.

	Attributes:
		id: идентификатор элемента (для отмены и снятия с показа).
		title: человекочитаемо: имя файла или начало текста.
		community_id: id канала-получателя (фильтры и группировки: названия
			каналов не уникальны, идентичность — только по id).
		community_title: название канала-получателя.
		when: момент публикации (UTC); None — «сейчас». Задан — пост
			отложенный: после отправки станет записью в канале.
		status: текущий статус.
		progress: доля загрузки 0.0..1.0 (для отправляющегося).
		error: текст ошибки (для статуса ERROR).
		note: пометка состояния для карточки (флуд-пауза); None — нет.
		media_path: путь к вложению (None — пост без файла). Карточка
			даёт по нему посмотреть файл системным приложением: пока
			пост ждёт слота, это единственный способ увидеть, что
			именно уйдёт (файл уже уехал из «Готовых видео»).
	"""

	id: int
	title: str
	community_id: int
	community_title: str
	when: datetime | None
	status: JobStatus
	progress: float
	error: str | None
	note: str | None = None
	media_path: str | None = None

	@property
	def scheduled(self) -> bool:
		"""Пост отложенный (момент публикации задан)."""
		return self.when is not None


class _PublishJob(Job):
	"""Задание отправки: черновик поста и его канал.

	Общее состояние (статус, доля загрузки, ошибка, пометка, флаг
	отмены) живёт в базовом классе каркаса (ADR-0025); здесь —
	предметное: сам черновик и два признака, которых нет у других
	очередей.
	"""

	def __init__(self, item_id: int, draft: PostDraft, community_title: str) -> None:
		super().__init__(item_id)
		self.draft = draft
		self.community_title = community_title
		# идёт сохранение правки: рабочий цикл и дозор слотов такой
		# элемент не трогают, пока правка не завершится (см. edit)
		self.editing = False
		# канал удаляется: по исходу элемент снимается и с показа
		self.drop_on_finish = False

	def dto(self) -> QueueItemDto:
		"""Снимок элемента для интерфейса."""
		return QueueItemDto(
			id=self.id,
			title=_draft_title(self.draft),
			community_id=self.draft.community_id,
			community_title=self.community_title,
			when=self.draft.when,
			status=self.status,
			progress=self.progress,
			error=self.error,
			note=self.note,
			media_path=self.draft.media_path,
		)


def _draft_title(draft: PostDraft) -> str:
	"""Заголовок элемента: имя файла, иначе начало текста."""
	if draft.media_path is not None:
		return (draft.rename_to or Path(draft.media_path).name).strip()
	return text_preview(draft.text.strip(), _TITLE_PREVIEW_CHARS)


def _as_utc(moment: datetime | None) -> datetime | None:
	"""Момент из БД → aware-UTC (SQLite возвращает наивные значения)."""
	if moment is None or moment.tzinfo is not None:
		return moment
	return moment.replace(tzinfo=UTC)


def _expired(when: datetime | None, now: datetime) -> bool:
	"""Желаемый момент прошёл (или ближе минимального запаса)."""
	return when is not None and when <= now + MIN_SCHEDULE_AHEAD


#: Пауза между догоняющими постами (просроченное время → «сейчас»), секунды.
#: После простоя приложения очередь не строчит залпом: подписчики видят
#: «канал ожил», а не пулемётную ленту, и флуд-лимит не срабатывает.
CATCHUP_INTERVAL_S = 30

#: Страховка кооперативной остановки (ADR-0020): столько ждём каждую
#: фоновую задачу, затем — жёсткая отмена как последнее средство.
#: Страховка на задачу: штатно задачи выходят мгновенно (_wait_stop),
#: а в худшем случае сумма ожиданий может превысить 10 с ожидания
#: потока в EngineWorker.stop — тогда поток отцепится с предупреждением.
_SHUTDOWN_TIMEOUT_S = 10.0


class PublishQueue:
	"""Последовательная отправка постов с прогрессом, отменой и повтором.

	Пока элемент отправляется, новые свободно встают в хвост; ошибка
	или отмена одного элемента не трогает остальные. Элемент с ошибкой
	можно вернуть в очередь (:meth:`retry`). Отложенные без свободного
	слота ждут (WAITING) и публикуются только при запущенном приложении;
	внутри канала первым уходит элемент с ближайшей датой (ADR-0016).
	"""

	def __init__(
		self, posts: PostsService, db: Database, settings: SettingsService | None = None
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно)."""
		self._posts = posts
		self._db = db
		self._settings = settings if settings is not None else SettingsService(db)
		self._jobs: JobQueue[_PublishJob] = JobQueue(
			self._send,
			name="Отправка",
			# очередь персистентна (ADR-0016): ожидающие переживают
			# выход и уйдут после следующего запуска
			cancel_pending_on_shutdown=False,
			shutdown_timeout_s=_SHUTDOWN_TIMEOUT_S,
			# порядок «БД → память» держит каркас: статус, видный
			# в памяти, уже сохранён (см. _record_outcome)
			record=self._record_outcome,
			# щадящий догон просроченных постов (ADR-0016)
			cooldown=self._catchup_pause,
			# элемент, чья правка сохраняется, в отправку не берётся
			ready=lambda item: not item.editing,
			# точка подмены в тестах: настоящие паузы (флуд, догон)
			# растянули бы прогон на минуты
			sleep=lambda seconds: self._sleep(seconds),
		)
		self._sleep: Callable[[float], Coroutine[Any, Any, None]] = self._wait_stop
		# элементы, ушедшие догоном: им положена пауза перед следующим
		self._catchup: set[int] = set()
		# задача передачи активного элемента — единственное, что можно
		# рвать отменой: подготовка ходит в БД и обрываться не должна
		self._transmit: asyncio.Task[None] | None = None
		self._watcher: asyncio.Task[None] | None = None
		self._slot_check: asyncio.Task[None] | None = None

	async def load(self) -> None:
		"""Наполняет очередь из таблицы при старте движка (ADR-0016).

		Элемент, отправлявшийся в момент падения, хранится как PENDING
		и уйдёт повторно; черновик обновляется на уже выполненное
		переименование (:func:`refresh_draft_media`). Заодно запускается
		дозор слотов и немедленная проверка для ждущих.
		"""
		async with self._db.session_factory() as session:
			rows = (
				(await session.execute(select(PublishQueueItem).order_by(PublishQueueItem.id)))
				.scalars()
				.all()
			)
		titles: dict[int, str] = {}
		for row in rows:
			if row.community_id not in titles:
				titles[row.community_id] = await self._posts.community_title(row.community_id)
			draft = refresh_draft_media(
				PostDraft(
					community_id=row.community_id,
					text=row.text,
					media_path=row.media_path,
					media_kind=MediaKind(row.media_kind),
					when=_as_utc(row.when),
					rename_to=row.rename_to,
					topic_id=row.topic_id,
				)
			)
			item = _PublishJob(row.id, draft, titles[row.community_id])
			item.status = JobStatus(row.status)
			item.error = row.error
			self._jobs.add(item)
		if rows:
			logger.info("Очередь отправки восстановлена: элементов %d.", len(rows))
		self._jobs.ensure_worker()
		self._request_slot_check()

	async def enqueue(self, draft: PostDraft) -> int:
		"""Ставит черновик в очередь; проверки — сразу, отправка — по порядку.

		Returns:
			Идентификатор элемента очереди.

		Raises:
			PostError: Черновик не готов к отправке или канал не найден.
		"""
		return (await self.enqueue_many([draft]))[0]

	async def enqueue_many(self, drafts: list[PostDraft]) -> list[int]:
		"""Ставит пакет черновиков в очередь; проверки — до постановки.

		Постановка атомарна (ADR-0015): сначала проверяются все черновики
		и каналы, потом переносятся файлы и добавляются строки — негодный
		черновик в середине не оставляет пакет поставленным наполовину
		(перенесённые файлы возвращаются, строки откатываются). Файлы
		результатов переезжают в папку очереди (ADR-0016). Отложенные
		встают в WAITING — слоты проверяются сразу, фоновой задачей.

		Returns:
			Идентификаторы элементов в порядке черновиков.

		Raises:
			PostError: Список пуст, черновик не готов, канал не найден
				или файл-тёзка уже ждёт в папке очереди.
		"""
		if not drafts:
			raise PostError("Пакет пуст — отправлять нечего.")
		titles: dict[int, str] = {}
		# пределы длины — по одному чтению на канал, как и названия:
		# у пакета из полусотни строк канал обычно один (ADR-0015)
		limits: dict[int, TextLimits] = {}
		for draft in drafts:
			self._posts.validate_draft(draft)
			if draft.community_id not in titles:
				titles[draft.community_id] = await self._posts.community_title(draft.community_id)
				limits[draft.community_id] = await self._posts.text_limits(draft.community_id)
			check_text_length(
				draft.text,
				limits[draft.community_id].for_draft(draft),
				draft.media_path is not None,
			)
		stashed, moved = await self._stash_all(drafts)
		try:
			rows = [
				PublishQueueItem(
					community_id=draft.community_id,
					text=draft.text,
					media_path=draft.media_path,
					media_kind=str(draft.media_kind),
					when=draft.when,
					rename_to=draft.rename_to,
					topic_id=draft.topic_id,
					status=self._initial_status(draft).value,
				)
				for draft in stashed
			]
			async with self._db.session_factory() as session:
				session.add_all(rows)
				await session.commit()
		except BaseException:
			await self._unstash_moved(moved)
			raise
		ids: list[int] = []
		for row, draft in zip(rows, stashed, strict=True):
			item = _PublishJob(row.id, draft, titles[draft.community_id])
			item.status = JobStatus(row.status)
			self._jobs.add(item)
			ids.append(item.id)
			logger.info(
				"Пост «%s» → «%s»: %s (id=%s).",
				_draft_title(draft),
				item.community_title,
				"ждёт слота отложек" if item.status is JobStatus.WAITING else "в очереди",
				item.id,
			)
		self._jobs.ensure_worker()
		self._request_slot_check()
		return ids

	def _request_cancel(self, item: _PublishJob) -> None:
		"""Взводит отмену активного элемента; исход запишет ``_send``.

		Сеть обрывается отменой задачи передачи; если идёт ещё
		подготовка (задачи нет — ADR-0020), флаг увидит сам ``_send``
		сразу после неё. Общая точка для «Отмены» и ``drop_community``.
		"""
		self._jobs.request_cancel(item)
		if self._jobs.active_id == item.id and self._transmit is not None:
			self._transmit.cancel()

	async def cancel(self, item_id: int) -> None:
		"""Отменяет элемент: ожидающий убирается, отправляющийся обрывается."""
		cancellable = (JobStatus.PENDING, JobStatus.WAITING)
		for item in self._jobs.all():
			if item.id != item_id:
				continue
			if item.status is JobStatus.RUNNING:
				self._request_cancel(item)
				return
			if item.status in cancellable:
				item.status = JobStatus.CANCELLED
				await self._leave_queue(item)
				logger.info("Элемент очереди id=%s отменён (ждал).", item_id)
				return

	async def retry(self, item_id: int) -> None:
		"""Возвращает элемент с ошибкой в очередь на новую попытку.

		Черновик перепроверяется, как при постановке (файл мог исчезнуть);
		если прошлая попытка успела переименовать файл, черновик
		обновляется на новое имя. Отложенный с непрошедшим временем
		возвращается в ожидание слота (слоты перепроверятся); просроченное
		время снимается — пост уйдёт «сейчас», как при рестарте
		(ADR-0016, п. «просрочка → сейчас»); остальные — сразу в отправку.
		Элементы в других статусах не трогаются.

		Raises:
			PostError: Черновик больше не годен к отправке — элемент
				остаётся в ошибке с прежним текстом.
		"""
		for item in self._jobs.all():
			if item.id != item_id or item.status is not JobStatus.ERROR:
				continue
			item.draft = refresh_draft_media(item.draft)
			if _expired(item.draft.when, datetime.now(UTC)):
				# просрочка → «сейчас»: иначе validate_draft отверг бы
				# прошедшее время и повтор был бы невозможен
				item.draft = replace(item.draft, when=None)
			self._posts.validate_draft(item.draft)
			item.status = self._initial_status(item.draft)
			item.progress = 0.0
			item.error = None
			# флаг мог взвестись, если отмена совпала с ошибкой прошлой
			# попытки: не сбросить — остановка движка при следующей отправке
			# была бы принята за отмену пользователем (см. _send)
			item.cancel_requested = False
			await self._persist(item)
			self._jobs.ensure_worker()
			self._request_slot_check()
			logger.info("Элемент id=%s возвращён в очередь на повтор.", item_id)
			return

	async def get_draft(self, item_id: int) -> PostDraft:
		"""Черновик элемента для окна правки.

		Отдаётся с учётом уже выполненного переименования
		(:func:`refresh_draft_media`): неудачная попытка могла оставить
		файл под новым именем, и окно должно показывать тот путь,
		который есть на диске.

		Raises:
			PostError: Элемент не найден или его нельзя править
				(отправляется, уже ушёл).
		"""
		return refresh_draft_media(self._editable(item_id).draft)

	async def edit(self, item_id: int, draft: PostDraft) -> None:
		"""Заменяет черновик элемента очереди и возвращает его в работу.

		Правятся неотправленные элементы: ждущий слота, стоящий
		в очереди и остановленный ошибкой. Отправляющийся не правится — файл уже
		грузится в Telegram (сначала «Отмена»). Канал-получатель
		не меняется: у другого канала свои темы, лимит файла и права
		на отложенную публикацию — это была бы другая публикация.

		После сохранения элемент всегда возвращается в работу: «сейчас»
		уходит в отправку, отложенный — ждать слота (просроченное время
		снимается так же, как при повторе). Ошибка прошлой попытки
		забывается — иначе на карточке висел бы неактуальный текст.

		Файлы ездят по правилам ADR-0016: новое вложение из папки
		результатов переносится в папку очереди, прежнее — возвращается
		в результаты. Сбой на любом шаге откатывает перенос: элемент
		остаётся с прежним черновиком, файлы — на своих местах.

		Raises:
			PostError: Элемент не найден или не правится; черновик
				не годится к отправке; смена канала; файл конвейера
				обработки отправляется не как видео; перенос не удался.
		"""
		item = self._editable(item_id)
		current = refresh_draft_media(item.draft)
		if draft.community_id != current.community_id:
			raise PostError(
				"Канал поста в очереди не меняется — отмените его и создайте пост в нужном канале."
			)
		self._posts.validate_draft(draft)
		# точный предел канала: validate_draft знает только потолок Premium
		await self._posts.check_draft_limits(draft)
		self._check_pipeline_kind(draft)
		status = self._initial_status(draft)
		# флаг взводится до первого ожидания: воркер выбирает элемент
		# и помечает его SENDING без единой точки приостановки, поэтому
		# элемент, помеченный здесь, он уже не подхватит
		item.editing = True
		try:
			stashed_drafts, moved = await self._stash_all([draft])
			stashed = stashed_drafts[0]
			try:
				if item.status.left_queue() or self._jobs.get(item.id) is None:
					# пока переносили файл, элемент отменили или канал удалили
					raise PostError("Пост уже покинул очередь — правка не сохранена.")
				await self._persist_draft(item.id, stashed, status)
			except BaseException:
				await self._unstash_moved(moved)
				raise
			if current.media_path is not None and current.media_path != stashed.media_path:
				# прежнее вложение больше не принадлежит очереди
				await self._posts.unstash_from_queue(current.media_path)
			item.draft = stashed
			item.status = status
			item.progress = 0.0
			item.error = None
			# флаг мог взвестись отменой, совпавшей с ошибкой прошлой
			# попытки (та же причина, что в retry)
			item.cancel_requested = False
		finally:
			item.editing = False
		self._jobs.ensure_worker()
		self._request_slot_check()
		logger.info(
			"Элемент id=%s изменён: %s (%s).",
			item_id,
			_draft_title(stashed),
			"ждёт слота отложек" if status is JobStatus.WAITING else "в очереди",
		)

	def _editable(self, item_id: int) -> _PublishJob:
		"""Элемент, который можно править, — или понятный отказ.

		Raises:
			PostError: Элемент не найден, отправляется, уже завершён
				или прямо сейчас правится другим окном.
		"""
		for item in self._jobs.all():
			if item.id != item_id:
				continue
			if item.status is JobStatus.RUNNING:
				raise PostError("Пост уже отправляется — сначала отмените отправку, потом правьте.")
			if item.status not in EDITABLE_STATUSES:
				raise PostError("Пост уже покинул очередь — править нечего.")
			if item.editing:
				raise PostError("Пост правится в другом окне — дождитесь сохранения.")
			return item
		raise PostError("Элемент очереди не найден — обновите список.")

	def _check_pipeline_kind(self, draft: PostDraft) -> None:
		"""Отклоняет смену типа у файла конвейера обработки (ADR-0016).

		Маршрут ``processed → queued → published`` определён только для
		видео: тот же файл, объявленный фото или документом, после
		отправки остался бы в папке очереди навсегда и блокировал уборку
		её папок (инвариант зеркала). Постановка проверяет это со своей
		стороны (``stash_for_queue``), правка — со своей: её файл уже
		лежит в папке очереди, и переносить его никто не будет.

		Raises:
			PostError: Файл конвейера отправляется не как видео.
		"""
		if draft.media_path is None or draft.media_kind is MediaKind.VIDEO:
			return
		if self._posts.pipeline_file(draft.media_path):
			raise PostError(
				f"«{Path(draft.media_path).name}» — файл конвейера обработки видео, "
				"отправить его можно только видео. Чтобы отправить его как фото "
				"или документ, скопируйте файл в другую папку."
			)

	async def drop_community(self, community_id: int) -> None:
		"""Снимает все элементы канала из очереди (канал удаляется).

		Живая очередь сама не узнаёт об удалении канала: каскад БД
		убирает только строки, а элементы в памяти остались бы «зомби» —
		дозор вечно пропускал бы их с ошибкой «Канал не найден».
		Ожидающие снимаются как при «Отмене» (файлы возвращаются
		в результаты), активная отправка обрывается, завершённые
		уходят с показа.
		"""
		for item in self._jobs.all():
			if item.draft.community_id != community_id:
				continue
			if item.status is JobStatus.RUNNING:
				# исход запишет _send: CANCELLED, файл вернётся в результаты;
				# пометка drop_on_finish снимет элемент и с показа — гарантия
				# «зомби-элементов нет» держится движком, а не панелью
				self._request_cancel(item)
				item.drop_on_finish = True
				continue
			if not item.status.finished():
				item.status = JobStatus.CANCELLED
				await self._leave_queue(item)
			self._jobs.remove(item)
		logger.info("Элементы канала id=%s сняты из очереди перед удалением.", community_id)

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из списка (живые не трогаются).

		Снятая с показа ошибка покидает очередь навсегда: строка удаляется,
		файл возвращается из папки очереди в результаты.
		"""
		for item in self._jobs.all():
			if item.id == item_id and item.status is JobStatus.ERROR:
				await self._leave_queue(item)
				break
		for item in self._jobs.all():
			if item.id == item_id and item.status.finished():
				self._jobs.remove(item)

	async def state(self) -> list[QueueItemDto]:
		"""Снимок очереди для интерфейса.

		Отправка, готовые и ошибки — в порядке постановки; ждущие слота —
		после них, по возрастанию даты публикации (в этом порядке они
		и уйдут — интерфейс показывает ближайшие).
		"""
		fallback = datetime.max.replace(tzinfo=UTC)
		waiting = [item for item in self._jobs.all() if item.status is JobStatus.WAITING]
		others = [item for item in self._jobs.all() if item.status is not JobStatus.WAITING]
		waiting.sort(key=lambda item: item.draft.when or fallback)
		return [item.dto() for item in [*others, *waiting]]

	async def settle(self) -> None:
		"""Дожидается завершения внеплановой проверки слотов (ADR-0020).

		Детерминированная точка присоединения: после возврата фоновые
		тики, запущенные постановкой или загрузкой, завершены — снимок
		очереди отражает их результат. Нужна тестам вместо синхронизации
		сном настенного времени (гонка по построению).
		"""
		while (check := self._slot_check) is not None and not check.done():
			with suppress(asyncio.CancelledError):
				await check

	async def shutdown(self) -> None:
		"""Останавливает фоновые задачи кооперативно (ADR-0020).

		Взводится событие остановки: задачи выходят в безопасных точках,
		их запросы к БД не обрываются — иначе соединение aiosqlite
		бросалось бы с запросом «в полёте» и его поток стрелял бы
		в закрытый цикл событий. Жёстко отменяется только активная
		передача (обрыв недосланной загрузки — доменное поведение,
		сетевая операция без БД); статусы в БД дочищать не нужно:
		RUNNING не персистится, при следующем запуске элемент уйдёт
		повторно. Задача, не завершившаяся за страховочный таймаут,
		отменяется — последнее средство.
		"""
		if self._transmit is not None:
			self._transmit.cancel()
		await self._jobs.shutdown()
		for task in (self._watcher, self._slot_check):
			if task is not None:
				with suppress(asyncio.CancelledError, TimeoutError):
					await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT_S)
		self._watcher = None
		self._slot_check = None

	async def _wait_stop(self, seconds: float) -> None:
		"""Ждёт срок или остановку движка — смотря что наступит раньше.

		Паузы фоновых задач (дозор слотов) идут через это ожидание:
		остановка прерывает их немедленно, не оставляя `shutdown`
		ждать целый тик.
		"""
		await self._jobs.wait_stop(seconds)

	# --- слоты отложек (ADR-0016) --------------------------------------------

	@staticmethod
	def _initial_status(draft: PostDraft) -> JobStatus:
		"""Стартовый статус черновика: «сейчас» слота не ждёт."""
		if draft.when is None or _expired(draft.when, datetime.now(UTC)):
			return JobStatus.PENDING
		return JobStatus.WAITING

	def _ensure_watcher(self) -> None:
		"""Запускает периодическую проверку слотов, если она не крутится."""
		if self._watcher is None or self._watcher.done():
			self._watcher = asyncio.create_task(self._watch_slots())

	def _request_slot_check(self) -> None:
		"""Внеплановая проверка слотов (после постановки/загрузки).

		Дозор поднимается здесь же: пока ждущих нет, фоновая задача
		не нужна вовсе (и не мешает коротким жизням очереди в тестах).
		"""
		if not any(item.status is JobStatus.WAITING for item in self._jobs.all()):
			return
		self._ensure_watcher()
		if self._slot_check is None or self._slot_check.done():
			self._slot_check = asyncio.create_task(self._release_slots())

	async def _watch_slots(self) -> None:
		"""Дозор: раз в N минут проверяет слоты, пока есть ждущие.

		Ждущих не осталось — задача завершается: пока их нет, фоновой
		задачи нет вовсе; следующая постановка или возврат в ожидание
		поднимут её заново (``_ensure_watcher``). Остановка движка
		прерывает сон и выводит из цикла в безопасной точке (ADR-0020).
		"""
		while not self._jobs.stopping and any(
			item.status is JobStatus.WAITING for item in self._jobs.all()
		):
			minutes = await self._settings.get(QUEUE_SLOT_POLL_MINUTES)
			await self._wait_stop(max(1, minutes) * 60)
			if not self._jobs.stopping and any(
				item.status is JobStatus.WAITING for item in self._jobs.all()
			):
				await self._release_slots()

	async def _release_slots(self) -> None:
		"""Выпускает ждущих, на кого хватает свободных слотов отложек.

		Свободно = лимит − фактические отложки канала (чтение с сервера) −
		выпущенные, но ещё не отправленные отложенные этого канала.
		Внутри канала первым уходит ближайший по дате; просроченный слота
		не требует (он публикуется «сейчас», см. ``_send``). Недоступность
		userbot или канала не роняет дозор — канал пропускается до
		следующего тика.
		"""
		now = datetime.now(UTC)
		fallback = datetime.max.replace(tzinfo=UTC)
		communities = {
			item.draft.community_id for item in self._jobs.all() if item.status is JobStatus.WAITING
		}
		for community_id in communities:
			if self._jobs.stopping:
				# остановка движка: недопроверенные каналы подождут запуска —
				# дозор перепроверит слоты при восстановлении очереди
				return
			try:
				taken = len(await self._posts.scheduled_times(community_id))
			except TelegramFloodError as exc:
				# лимит держит дорожка аккаунта (ADR-0024): остальные его
				# каналы получат такой же мгновенный отказ, не обращаясь
				# к Telegram, а каналы других аккаунтов проверятся дальше
				logger.info(
					"Слоты канала id=%s не проверены: аккаунт под флуд-лимитом (%s).",
					community_id,
					exc,
				)
				continue
			except (PostError, UserbotUnavailableError) as exc:
				logger.warning("Слоты канала id=%s не прочитаны: %s", community_id, exc)
				continue
			except Exception:  # noqa: BLE001 — дозор не должен умирать
				logger.exception("Проверка слотов канала id=%s не удалась.", community_id)
				continue
			in_flight = sum(
				1
				for item in self._jobs.all()
				if item.draft.community_id == community_id
				and item.status in (JobStatus.PENDING, JobStatus.RUNNING)
				and item.draft.when is not None
				and not _expired(item.draft.when, now)
			)
			free = TELEGRAM_MAX_SCHEDULED - taken - in_flight
			waiting = sorted(
				(
					item
					for item in self._jobs.all()
					if item.status is JobStatus.WAITING
					and item.draft.community_id == community_id
					# правка сама поставит элементу статус по новому времени
					and not item.editing
				),
				key=lambda item: item.draft.when or fallback,
			)
			released = 0
			for item in waiting:
				if not _expired(item.draft.when, now):
					if free <= 0:
						break
					free -= 1
				item.status = JobStatus.PENDING
				await self._persist(item)
				released += 1
			if released:
				logger.info(
					"Канал id=%s: выпущено из ожидания %d (занято слотов %d).",
					community_id,
					released,
					taken,
				)
		self._jobs.ensure_worker()

	# --- персистентность и файлы ---------------------------------------------

	async def _stash_all(self, drafts: list[PostDraft]) -> tuple[list[PostDraft], list[str]]:
		"""Переносит файлы пакета в папку очереди; сбой откатывает всё.

		Returns:
			Черновики с путями в папке очереди и список перенесённых
			(для отката, если постановка сорвётся дальше).
		"""
		stashed: list[PostDraft] = []
		moved: list[str] = []
		try:
			for draft in drafts:
				if draft.media_path is None:
					stashed.append(draft)
					continue
				new_path = await self._posts.stash_for_queue(draft.media_path, draft.media_kind)
				if new_path != draft.media_path:
					moved.append(new_path)
				stashed.append(replace(draft, media_path=new_path))
		except BaseException:
			await self._unstash_moved(moved)
			raise
		return stashed, moved

	async def _unstash_moved(self, moved: list[str]) -> None:
		"""Возвращает перенесённые файлы обратно в результаты (откат)."""
		for path in moved:
			await self._posts.unstash_from_queue(path)

	async def _persist(self, item: _PublishJob) -> None:
		"""Пишет статус/ошибку элемента в таблицу (только хранимые статусы)."""
		if item.status not in _PERSISTED:
			return
		await self._persist_values(item.id, item.status, item.error)

	async def _persist_values(self, item_id: int, status: JobStatus, error: str | None) -> None:
		"""Пишет статус/ошибку строки элемента (до правки состояния в памяти)."""
		async with self._db.session_factory() as session:
			await session.execute(
				update(PublishQueueItem)
				.where(PublishQueueItem.id == item_id)
				.values(status=status.value, error=error)
			)
			await session.commit()

	async def _persist_draft(self, item_id: int, draft: PostDraft, status: JobStatus) -> None:
		"""Переписывает строку элемента новым черновиком и статусом.

		Ошибка прошлой попытки стирается вместе с черновиком: правка
		отменяет исход, к которому та ошибка относилась.
		"""
		async with self._db.session_factory() as session:
			await session.execute(
				update(PublishQueueItem)
				.where(PublishQueueItem.id == item_id)
				.values(
					text=draft.text,
					media_path=draft.media_path,
					media_kind=str(draft.media_kind),
					when=draft.when,
					rename_to=draft.rename_to,
					topic_id=draft.topic_id,
					status=status.value,
					error=None,
				)
			)
			await session.commit()

	async def _delete_row(self, item_id: int) -> None:
		"""Удаляет строку элемента (отправлен или покинул очередь)."""
		async with self._db.session_factory() as session:
			await session.execute(delete(PublishQueueItem).where(PublishQueueItem.id == item_id))
			await session.commit()

	async def _leave_queue(self, item: _PublishJob) -> None:
		"""Элемент покидает очередь без отправки: строка — долой, файл — назад."""
		await self._delete_row(item.id)
		if item.draft.media_path is not None:
			returned = await self._posts.unstash_from_queue(item.draft.media_path)
			item.draft = replace(item.draft, media_path=returned)

	# --- отправка -------------------------------------------------------------

	def _next_pending(self) -> _PublishJob | None:
		"""Первый готовый к отправке элемент (для проверки «есть ли ещё»).

		Пропускается тот, чья правка сейчас сохраняется (:meth:`edit`):
		забрать его в отправку значило бы отправить наполовину
		применённый черновик.
		"""
		for item in self._jobs.all():
			if item.status is JobStatus.PENDING and not item.editing:
				return item
		return None

	def _catchup_pause(self, item: _PublishJob) -> float:
		"""Пауза после догоняющего поста (ADR-0016): щадящий темп.

		После простоя приложения очередь не строчит залпом: подписчики
		видят «канал ожил», а не пулемётную ленту. Обычный пост паузы
		не требует.
		"""
		return CATCHUP_INTERVAL_S if item.id in self._catchup else 0.0

	async def _record_outcome(
		self, item: _PublishJob, status: JobStatus, error: str | None
	) -> None:
		"""Записывает исход элемента до того, как он появится в памяти.

		Порядок «БД → память» — инвариант очереди (ADR-0016):
		наблюдатель, увидевший исход, знает, что хранилище о нём уже
		знает; обрыв между шагами (остановка движка) оставляет в БД
		«не доделано», и элемент просто уйдёт после перезапуска.

		Исходы разные по существу: отправленный и отменённый покидают
		очередь (строка — долой, файл отменённого возвращается
		в результаты), ошибка и ожидание — остаются строкой.
		"""
		if status is JobStatus.DONE:
			await self._delete_row(item.id)
		elif status is JobStatus.CANCELLED:
			await self._leave_queue(item)
		else:
			await self._persist_values(item.id, status, error)
		if item.drop_on_finish and status.finished():
			# канал удалён (drop_community): исход записан — элемент
			# уходит и с показа, без участия панели интерфейса
			self._jobs.remove(item)

	async def _send(self, item: _PublishJob) -> None:
		"""Отправляет один пост; исход записывает каркас заданий.

		Подготовка (проверки, чтения БД, переименование файла) идёт
		здесь же, но **вне отменяемой части**: отменяется только задача
		передачи — сеть и файлы (ADR-0020). Жёсткая отмена не должна
		застать запрос к БД: соединение aiosqlite бросилось бы
		с запросом «в полёте».

		Raises:
			JobCancelled: Отправку отменил человек (или остановка
				движка застала её на подготовке).
			JobDeferred: Слоты отложек кончились (элемент ждёт снова)
				или Telegram просит подождать (повтор после паузы).
			PostError: Отправка не удалась — текст уйдёт на карточку.
		"""

		def _on_progress(fraction: float) -> None:
			item.progress = fraction

		draft = item.draft
		if _expired(draft.when, datetime.now(UTC)):
			# желаемый момент прошёл — это уже не отложка: публикуем
			# обычным сообщением, слота не занимая (ADR-0016)
			draft = replace(draft, when=None)
			# снимок для интерфейса честен: карточка и итоговая плашка
			# показывают «сейчас», а не несуществующую отложку
			item.draft = draft
			self._catchup.add(item.id)
		try:
			plan = await self._posts.prepare_publish(draft)
			if item.cancel_requested:
				# отмена пришла на подготовке: сети ещё не было,
				# обрывать нечего — исход тот же, что у обрыва передачи
				raise JobCancelled
			if self._jobs.stopping:
				# остановка движка — не исход элемента: он остаётся
				# PENDING в БД и уйдёт после перезапуска (ADR-0020)
				raise asyncio.CancelledError
			task = asyncio.create_task(self._posts.transmit(plan, on_progress=_on_progress))
			self._transmit = task
			try:
				await task
			finally:
				self._transmit = None
		except asyncio.CancelledError:
			# отменена задача передачи: человеком (`cancel`) или
			# остановкой движка (`shutdown`). Второе — не исход
			# элемента: он остаётся PENDING в БД и уйдёт после
			# перезапуска, поэтому отмену пробрасываем дальше
			if not item.cancel_requested:
				raise
			raise JobCancelled from None
		except UserbotScheduleFullError as exc:
			# гонка: слоты заняли руками из клиента Telegram между
			# проверкой и отправкой — не ошибка, элемент ждёт снова
			self._ensure_watcher()  # дозор вернёт его, когда слот освободится
			logger.info("Отправка id=%s: слоты кончились — элемент снова ждёт.", item.id)
			raise JobDeferred(JobStatus.WAITING) from exc
		except TelegramFloodError as exc:
			# флуд-лимит — временное состояние, не исход элемента:
			# сервер сам назвал срок повтора
			logger.warning(
				"Отправка id=%s: флуд-лимит — очередь ждёт %d с.", item.id, exc.retry_after_s
			)
			raise JobDeferred(
				JobStatus.PENDING,
				note=f"{exc} Очередь ждёт и повторит сама.",
				delay_s=exc.retry_after_s,
			) from exc
		except Exception as exc:
			if item.cancel_requested:
				# ошибка на фоне взведённой отмены — типовой случай:
				# drop_community уже удалил канал, и подготовка падает
				# «Канал не найден». Честный исход — отмена, не ошибка:
				# иначе файл застрял бы в папке очереди, а «Повторить»
				# вечно падал тем же текстом
				logger.info("Отправка id=%s отменена (ошибка на фоне отмены: %s).", item.id, exc)
				raise JobCancelled from exc
			raise
