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
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select, update

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import PublishQueueItem
from pxcontrol.engine.errors import user_message
from pxcontrol.engine.services.posts import (
	MIN_SCHEDULE_AHEAD,
	PostDraft,
	PostError,
	PostsService,
	refresh_draft_media,
	text_preview,
)
from pxcontrol.engine.services.settings import QUEUE_SLOT_POLL_MINUTES, SettingsService
from pxcontrol.engine.telegram.mtproto import (
	UserbotScheduleFullError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.types import TELEGRAM_MAX_SCHEDULED, MediaKind

logger = logging.getLogger(__name__)

#: Длина превью текста поста в заголовке элемента очереди.
_TITLE_PREVIEW_CHARS = 60


class QueueItemStatus(StrEnum):
	"""Статус элемента очереди отправки."""

	PENDING = "pending"  # ждёт своей очереди на отправку
	WAITING = "waiting"  # ждёт свободного слота отложек канала (ADR-0016)
	SENDING = "sending"  # загружается в Telegram
	DONE = "done"  # отправлен
	ERROR = "error"  # отправка не удалась (текст — в error)
	CANCELLED = "cancelled"  # отменён пользователем

	def finished(self) -> bool:
		"""Завершён ли элемент (в любом исходе)."""
		return self in (self.DONE, self.ERROR, self.CANCELLED)


#: Статусы, которые хранятся в БД (SENDING не пишется: падение во время
#: отправки при загрузке очереди выглядит как PENDING и уходит повторно).
_PERSISTED = (QueueItemStatus.PENDING, QueueItemStatus.WAITING, QueueItemStatus.ERROR)


@dataclass(frozen=True)
class QueueItemDto:
	"""Элемент очереди для интерфейса.

	Attributes:
		id: идентификатор элемента (для отмены и снятия с показа).
		title: человекочитаемо: имя файла или начало текста.
		channel_id: id канала-получателя (фильтры и группировки: названия
			каналов не уникальны, идентичность — только по id).
		channel_title: название канала-получателя.
		when: момент публикации (UTC); None — «сейчас». Задан — пост
			отложенный: после отправки станет записью в канале.
		status: текущий статус.
		progress: доля загрузки 0.0..1.0 (для отправляющегося).
		error: текст ошибки (для статуса ERROR).
	"""

	id: int
	title: str
	channel_id: int
	channel_title: str
	when: datetime | None
	status: QueueItemStatus
	progress: float
	error: str | None

	@property
	def scheduled(self) -> bool:
		"""Пост отложенный (момент публикации задан)."""
		return self.when is not None


class _Item:
	"""Внутреннее состояние элемента очереди (изменяемое)."""

	def __init__(self, item_id: int, draft: PostDraft, channel_title: str) -> None:
		self.id = item_id
		self.draft = draft
		self.channel_title = channel_title
		self.status = QueueItemStatus.PENDING
		self.progress = 0.0
		self.error: str | None = None
		# отмену запросил пользователь (отличает её от остановки движка)
		self.cancel_requested = False

	def dto(self) -> QueueItemDto:
		"""Снимок элемента для интерфейса."""
		return QueueItemDto(
			id=self.id,
			title=_draft_title(self.draft),
			channel_id=self.draft.channel_id,
			channel_title=self.channel_title,
			when=self.draft.when,
			status=self.status,
			progress=self.progress,
			error=self.error,
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
		self._items: list[_Item] = []
		self._worker: asyncio.Task[None] | None = None
		self._active: tuple[int, asyncio.Task[None]] | None = None
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
			if row.channel_id not in titles:
				titles[row.channel_id] = await self._posts.channel_title(row.channel_id)
			draft = refresh_draft_media(
				PostDraft(
					channel_id=row.channel_id,
					text=row.text,
					media_path=row.media_path,
					media_kind=MediaKind(row.media_kind),
					when=_as_utc(row.when),
					rename_to=row.rename_to,
				)
			)
			item = _Item(row.id, draft, titles[row.channel_id])
			item.status = QueueItemStatus(row.status)
			item.error = row.error
			self._items.append(item)
		if rows:
			logger.info("Очередь отправки восстановлена: элементов %d.", len(rows))
		self._ensure_worker()
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
		for draft in drafts:
			self._posts.validate_draft(draft)
			if draft.channel_id not in titles:
				titles[draft.channel_id] = await self._posts.channel_title(draft.channel_id)
		stashed, moved = await self._stash_all(drafts)
		try:
			rows = [
				PublishQueueItem(
					channel_id=draft.channel_id,
					text=draft.text,
					media_path=draft.media_path,
					media_kind=str(draft.media_kind),
					when=draft.when,
					rename_to=draft.rename_to,
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
			item = _Item(row.id, draft, titles[draft.channel_id])
			item.status = QueueItemStatus(row.status)
			self._items.append(item)
			ids.append(item.id)
			logger.info(
				"Пост «%s» → «%s»: %s (id=%s).",
				_draft_title(draft),
				item.channel_title,
				"ждёт слота отложек" if item.status is QueueItemStatus.WAITING else "в очереди",
				item.id,
			)
		self._ensure_worker()
		self._request_slot_check()
		return ids

	async def cancel(self, item_id: int) -> None:
		"""Отменяет элемент: ожидающий убирается, отправляющийся обрывается."""
		if self._active is not None and self._active[0] == item_id:
			for item in self._items:
				if item.id == item_id:
					item.cancel_requested = True
			self._active[1].cancel()
			return
		cancellable = (QueueItemStatus.PENDING, QueueItemStatus.WAITING)
		for item in self._items:
			if item.id == item_id and item.status in cancellable:
				item.status = QueueItemStatus.CANCELLED
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
		for item in self._items:
			if item.id != item_id or item.status is not QueueItemStatus.ERROR:
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
			self._ensure_worker()
			self._request_slot_check()
			logger.info("Элемент id=%s возвращён в очередь на повтор.", item_id)
			return

	async def drop_channel(self, channel_id: int) -> None:
		"""Снимает все элементы канала из очереди (канал удаляется).

		Живая очередь сама не узнаёт об удалении канала: каскад БД
		убирает только строки, а элементы в памяти остались бы «зомби» —
		дозор вечно пропускал бы их с ошибкой «Канал не найден».
		Ожидающие снимаются как при «Отмене» (файлы возвращаются
		в результаты), активная отправка обрывается, завершённые
		уходят с показа.
		"""
		for item in list(self._items):
			if item.draft.channel_id != channel_id:
				continue
			if self._active is not None and self._active[0] == item.id:
				# исход запишет _send: CANCELLED, файл вернётся в результаты
				item.cancel_requested = True
				self._active[1].cancel()
				continue
			if not item.status.finished():
				item.status = QueueItemStatus.CANCELLED
				await self._leave_queue(item)
			self._items.remove(item)
		logger.info("Элементы канала id=%s сняты из очереди перед удалением.", channel_id)

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из списка (живые не трогаются).

		Снятая с показа ошибка покидает очередь навсегда: строка удаляется,
		файл возвращается из папки очереди в результаты.
		"""
		for item in self._items:
			if item.id == item_id and item.status is QueueItemStatus.ERROR:
				await self._leave_queue(item)
				break
		self._items = [
			item for item in self._items if not (item.id == item_id and item.status.finished())
		]

	async def state(self) -> list[QueueItemDto]:
		"""Снимок очереди для интерфейса.

		Отправка, готовые и ошибки — в порядке постановки; ждущие слота —
		после них, по возрастанию даты публикации (в этом порядке они
		и уйдут — интерфейс показывает ближайшие).
		"""
		fallback = datetime.max.replace(tzinfo=UTC)
		waiting = [item for item in self._items if item.status is QueueItemStatus.WAITING]
		others = [item for item in self._items if item.status is not QueueItemStatus.WAITING]
		waiting.sort(key=lambda item: item.draft.when or fallback)
		return [item.dto() for item in [*others, *waiting]]

	async def has_unfinished(self) -> bool:
		"""Идёт ли сейчас отправка (для подтверждения выхода).

		Ожидающие и ждущие слота выход не задерживают: очередь
		персистентная (ADR-0016) и продолжится при следующем запуске;
		вопрос заслуживает только обрыв активной загрузки.
		"""
		return any(item.status is QueueItemStatus.SENDING for item in self._items)

	async def shutdown(self) -> None:
		"""Останавливает воркер и дозор слотов (при остановке движка).

		Статусы в БД дочищать не нужно: SENDING не персистится, при
		следующем запуске элемент уйдёт повторно; отмена задачи обрывает
		активную отправку (недосланное Telegram не публикует).
		"""
		for task in (self._worker, self._watcher, self._slot_check):
			if task is not None:
				task.cancel()
				with suppress(asyncio.CancelledError):
					await task
		self._worker = None
		self._watcher = None
		self._slot_check = None

	# --- слоты отложек (ADR-0016) --------------------------------------------

	@staticmethod
	def _initial_status(draft: PostDraft) -> QueueItemStatus:
		"""Стартовый статус черновика: «сейчас» слота не ждёт."""
		if draft.when is None or _expired(draft.when, datetime.now(UTC)):
			return QueueItemStatus.PENDING
		return QueueItemStatus.WAITING

	def _ensure_watcher(self) -> None:
		"""Запускает периодическую проверку слотов, если она не крутится."""
		if self._watcher is None or self._watcher.done():
			self._watcher = asyncio.create_task(self._watch_slots())

	def _request_slot_check(self) -> None:
		"""Внеплановая проверка слотов (после постановки/загрузки).

		Дозор поднимается здесь же: пока ждущих нет, фоновая задача
		не нужна вовсе (и не мешает коротким жизням очереди в тестах).
		"""
		if not any(item.status is QueueItemStatus.WAITING for item in self._items):
			return
		self._ensure_watcher()
		if self._slot_check is None or self._slot_check.done():
			self._slot_check = asyncio.create_task(self._release_slots())

	async def _watch_slots(self) -> None:
		"""Дозор: раз в N минут проверяет слоты, пока есть ждущие.

		Ждущих не осталось — задача завершается: пока их нет, фоновой
		задачи нет вовсе; следующая постановка или возврат в ожидание
		поднимут её заново (``_ensure_watcher``).
		"""
		while any(item.status is QueueItemStatus.WAITING for item in self._items):
			minutes = await self._settings.get(QUEUE_SLOT_POLL_MINUTES)
			await asyncio.sleep(max(1, minutes) * 60)
			if any(item.status is QueueItemStatus.WAITING for item in self._items):
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
		channels = {
			item.draft.channel_id for item in self._items if item.status is QueueItemStatus.WAITING
		}
		for channel_id in channels:
			try:
				taken = len(await self._posts.scheduled_times(channel_id))
			except (PostError, UserbotUnavailableError) as exc:
				logger.warning("Слоты канала id=%s не прочитаны: %s", channel_id, exc)
				continue
			except Exception:  # noqa: BLE001 — дозор не должен умирать
				logger.exception("Проверка слотов канала id=%s не удалась.", channel_id)
				continue
			in_flight = sum(
				1
				for item in self._items
				if item.draft.channel_id == channel_id
				and item.status in (QueueItemStatus.PENDING, QueueItemStatus.SENDING)
				and item.draft.when is not None
				and not _expired(item.draft.when, now)
			)
			free = TELEGRAM_MAX_SCHEDULED - taken - in_flight
			waiting = sorted(
				(
					item
					for item in self._items
					if item.status is QueueItemStatus.WAITING
					and item.draft.channel_id == channel_id
				),
				key=lambda item: item.draft.when or fallback,
			)
			released = 0
			for item in waiting:
				if not _expired(item.draft.when, now):
					if free <= 0:
						break
					free -= 1
				item.status = QueueItemStatus.PENDING
				await self._persist(item)
				released += 1
			if released:
				logger.info(
					"Канал id=%s: выпущено из ожидания %d (занято слотов %d).",
					channel_id,
					released,
					taken,
				)
		self._ensure_worker()

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

	async def _persist(self, item: _Item) -> None:
		"""Пишет статус/ошибку элемента в таблицу (только хранимые статусы)."""
		if item.status not in _PERSISTED:
			return
		await self._persist_values(item.id, item.status, item.error)

	async def _persist_values(
		self, item_id: int, status: QueueItemStatus, error: str | None
	) -> None:
		"""Пишет статус/ошибку строки элемента (до правки состояния в памяти)."""
		async with self._db.session_factory() as session:
			await session.execute(
				update(PublishQueueItem)
				.where(PublishQueueItem.id == item_id)
				.values(status=status.value, error=error)
			)
			await session.commit()

	async def _delete_row(self, item_id: int) -> None:
		"""Удаляет строку элемента (отправлен или покинул очередь)."""
		async with self._db.session_factory() as session:
			await session.execute(delete(PublishQueueItem).where(PublishQueueItem.id == item_id))
			await session.commit()

	async def _leave_queue(self, item: _Item) -> None:
		"""Элемент покидает очередь без отправки: строка — долой, файл — назад."""
		await self._delete_row(item.id)
		if item.draft.media_path is not None:
			returned = await self._posts.unstash_from_queue(item.draft.media_path)
			item.draft = replace(item.draft, media_path=returned)

	# --- отправка -------------------------------------------------------------

	def _ensure_worker(self) -> None:
		"""Запускает фоновую задачу отправки, если она не крутится."""
		if self._worker is None or self._worker.done():
			self._worker = asyncio.create_task(self._run())

	async def _run(self) -> None:
		"""Отправляет элементы по одному, пока есть готовые к отправке."""
		while (item := self._next_pending()) is not None:
			await self._send(item)

	def _next_pending(self) -> _Item | None:
		"""Первый готовый к отправке элемент (ждущие слота пропускаются)."""
		for item in self._items:
			if item.status is QueueItemStatus.PENDING:
				return item
		return None

	async def _send(self, item: _Item) -> None:
		"""Отправляет один элемент; исход пишется в его статус."""

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
		item.status = QueueItemStatus.SENDING
		task = asyncio.create_task(self._posts.publish(draft, on_progress=_on_progress))
		self._active = (item.id, task)
		try:
			await task
		except asyncio.CancelledError:
			if not item.cancel_requested:
				# отменили сам воркер (остановка движка): гасим отправку
				# и пробрасываем отмену дальше — очередь не продолжается.
				# task.cancelled() здесь не годится: при остановке цикла
				# отменяются обе задачи, и по нему не отличить пользователя.
				task.cancel()
				raise
			item.status = QueueItemStatus.CANCELLED
			await self._leave_queue(item)
			logger.info("Отправка id=%s отменена пользователем.", item.id)
		except UserbotScheduleFullError:
			# гонка: слоты заняли руками из клиента Telegram между проверкой
			# и отправкой — не ошибка, элемент возвращается ждать (ADR-0016).
			# Сначала БД, потом память: обрыв между шагами (остановка движка)
			# оставит в БД pending — при перезапуске элемент просто уйдёт снова
			await self._persist_values(item.id, QueueItemStatus.WAITING, None)
			item.status = QueueItemStatus.WAITING
			item.progress = 0.0
			self._ensure_watcher()  # дозор вернёт элемент, когда слот освободится
			logger.info("Отправка id=%s: слоты кончились — элемент снова ждёт.", item.id)
		except Exception as exc:  # noqa: BLE001 — исход элемента, не очереди
			# карточка очереди показывает этот текст как есть — сворачиваем
			# недоменные исключения, как мост интерфейса (контракт errors.py).
			# Порядок «БД → память» — как у ветки выше
			message = user_message(exc)
			await self._persist_values(item.id, QueueItemStatus.ERROR, message)
			item.status = QueueItemStatus.ERROR
			item.error = message
			logger.exception("Отправка id=%s не удалась.", item.id)
		else:
			item.status = QueueItemStatus.DONE
			item.progress = 1.0
			await self._delete_row(item.id)
		finally:
			self._active = None
