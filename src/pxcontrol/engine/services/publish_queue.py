"""Очередь отправки постов: последовательно, с прогрессом и отменой.

Очередь живёт в памяти цикла событий движка (ADR-0012): пока один пост
загружается в Telegram, следующие ждут своей очереди, а форма публикации
свободна. Персистентного хранилища нет сознательно — источник истины
по постам остаётся каналом (ADR-0010); о непустой очереди при выходе
предупреждает интерфейс.

Все методы выполняются в цикле движка (вызовы — через мост интерфейса),
поэтому состояние не требует блокировок.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pxcontrol.engine.services.posts import PostDraft, PostsService, text_preview

logger = logging.getLogger(__name__)

#: Длина превью текста поста в заголовке элемента очереди.
_TITLE_PREVIEW_CHARS = 60


class QueueItemStatus(StrEnum):
	"""Статус элемента очереди отправки."""

	PENDING = "pending"  # ждёт своей очереди
	SENDING = "sending"  # загружается в Telegram
	DONE = "done"  # отправлен
	ERROR = "error"  # отправка не удалась (текст — в error)
	CANCELLED = "cancelled"  # отменён пользователем

	def finished(self) -> bool:
		"""Завершён ли элемент (в любом исходе)."""
		return self in (self.DONE, self.ERROR, self.CANCELLED)


@dataclass(frozen=True)
class QueueItemDto:
	"""Элемент очереди для интерфейса.

	Attributes:
		id: идентификатор элемента (для отмены и снятия с показа).
		title: человекочитаемо: имя файла или начало текста.
		channel_title: название канала-получателя.
		when: момент публикации (UTC); None — «сейчас». Задан — пост
			отложенный: после отправки станет записью в канале.
		status: текущий статус.
		progress: доля загрузки 0.0..1.0 (для отправляющегося).
		error: текст ошибки (для статуса ERROR).
	"""

	id: int
	title: str
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

	def __init__(
		self, item_id: int, draft: PostDraft, channel_title: str
	) -> None:
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


class PublishQueue:
	"""Последовательная отправка постов с прогрессом и отменой.

	Пока элемент отправляется, новые свободно встают в хвост; ошибка
	или отмена одного элемента не трогает остальные.
	"""

	def __init__(self, posts: PostsService) -> None:
		self._posts = posts
		self._items: list[_Item] = []
		self._next_id = 1
		self._worker: asyncio.Task[None] | None = None
		self._active: tuple[int, asyncio.Task[None]] | None = None

	async def enqueue(self, draft: PostDraft) -> int:
		"""Ставит черновик в очередь; проверки — сразу, отправка — по порядку.

		Returns:
			Идентификатор элемента очереди.

		Raises:
			PostError: Черновик не готов к отправке или канал не найден.
		"""
		self._posts.validate_draft(draft)
		channel_title = await self._posts.channel_title(draft.channel_id)
		item = _Item(self._next_id, draft, channel_title)
		self._next_id += 1
		self._items.append(item)
		self._ensure_worker()
		logger.info(
			"Пост «%s» → «%s»: в очереди (id=%s).",
			_draft_title(draft), channel_title, item.id,
		)
		return item.id

	async def cancel(self, item_id: int) -> None:
		"""Отменяет элемент: ожидающий убирается, отправляющийся обрывается."""
		if self._active is not None and self._active[0] == item_id:
			for item in self._items:
				if item.id == item_id:
					item.cancel_requested = True
			self._active[1].cancel()
			return
		for item in self._items:
			if item.id == item_id and item.status is QueueItemStatus.PENDING:
				item.status = QueueItemStatus.CANCELLED
				logger.info("Элемент очереди id=%s отменён (ждал).", item_id)
				return

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из списка (живые не трогаются)."""
		self._items = [
			item for item in self._items
			if not (item.id == item_id and item.status.finished())
		]

	async def state(self) -> list[QueueItemDto]:
		"""Снимок очереди для интерфейса (в порядке постановки)."""
		return [item.dto() for item in self._items]

	async def has_unfinished(self) -> bool:
		"""Есть ли элементы, которые ещё ждут или отправляются."""
		return any(not item.status.finished() for item in self._items)

	async def shutdown(self) -> None:
		"""Останавливает воркер очереди (при остановке движка)."""
		if self._worker is not None:
			self._worker.cancel()
			with suppress(asyncio.CancelledError):
				await self._worker
			self._worker = None

	# --- внутреннее ---------------------------------------------------------

	def _ensure_worker(self) -> None:
		"""Запускает фоновую задачу отправки, если она не крутится."""
		if self._worker is None or self._worker.done():
			self._worker = asyncio.create_task(self._run())

	async def _run(self) -> None:
		"""Отправляет элементы по одному, пока очередь не опустеет."""
		while (item := self._next_pending()) is not None:
			await self._send(item)

	def _next_pending(self) -> _Item | None:
		"""Первый ожидающий элемент (или None — очередь пуста)."""
		for item in self._items:
			if item.status is QueueItemStatus.PENDING:
				return item
		return None

	async def _send(self, item: _Item) -> None:
		"""Отправляет один элемент; исход пишется в его статус."""

		def _on_progress(fraction: float) -> None:
			item.progress = fraction

		item.status = QueueItemStatus.SENDING
		task = asyncio.create_task(
			self._posts.publish(item.draft, on_progress=_on_progress)
		)
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
			logger.info("Отправка id=%s отменена пользователем.", item.id)
		except Exception as exc:  # noqa: BLE001 — исход элемента, не очереди
			item.status = QueueItemStatus.ERROR
			item.error = str(exc)
			logger.exception("Отправка id=%s не удалась.", item.id)
		else:
			item.status = QueueItemStatus.DONE
			item.progress = 1.0
		finally:
			self._active = None
