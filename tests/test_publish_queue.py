"""Тесты очереди отправки: порядок, отмена, ошибки — без сети."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Channel
from pxcontrol.engine.services.posts import (
	PostDraft,
	PostError,
	PostsService,
	ProgressCallback,
)
from pxcontrol.engine.services.publish_queue import (
	PublishQueue,
	QueueItemDto,
	QueueItemStatus,
)
from pxcontrol.engine.telegram.types import MediaKind, OutgoingPost


class _SlowGateway:
	"""Подмена шлюза: отправка ждёт отмашки — как долгая загрузка видео."""

	def __init__(self) -> None:
		self.release = asyncio.Event()
		self.published: list[OutgoingPost] = []
		self.fail_texts: set[str] = set()

	async def publish(
		self, chat_id: str, post: OutgoingPost,
		on_progress: ProgressCallback | None = None,
	) -> None:
		if on_progress is not None:
			on_progress(0.5)
		await self.release.wait()
		if post.text in self.fail_texts:
			raise PostError("Telegram отклонил отправку.")
		self.published.append(post)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
	await database.init()
	yield database
	await database.close()


async def _add_channel(db: Database) -> int:
	"""Создаёт канал с userbot-админом, возвращает id."""
	async with db.session_factory() as session:
		channel = Channel(title="Канал", tg_chat_id="-1001", userbot_admin=True)
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
		return channel.id


def _queue(db: Database, gateway: _SlowGateway) -> PublishQueue:
	return PublishQueue(PostsService(db, gateway))


async def _wait_status(
	queue: PublishQueue, item_id: int, status: QueueItemStatus, tries: int = 500
) -> QueueItemDto:
	"""Ждёт, пока элемент дойдёт до статуса (максимум ~5 секунд).

	Пауза настоящая (не ``sleep(0)``): запросы к SQLite выполняет
	поток aiosqlite, ему нужно реальное время.
	"""
	for _ in range(tries):
		items = {item.id: item for item in await queue.state()}
		if item_id in items and items[item_id].status is status:
			return items[item_id]
		await asyncio.sleep(0.01)
	raise AssertionError(f"элемент {item_id} не достиг статуса {status}")


async def _wait_progress(
	queue: PublishQueue, item_id: int, expected: float, tries: int = 500
) -> None:
	"""Ждёт, пока до элемента доедет доля прогресса загрузки."""
	for _ in range(tries):
		items = {item.id: item for item in await queue.state()}
		if item_id in items and items[item_id].progress == expected:
			return
		await asyncio.sleep(0.01)
	raise AssertionError(f"элемент {item_id} не получил прогресс {expected}")


async def test_enqueue_during_send_keeps_order(db: Database) -> None:
	"""Пока первый уходит, второй свободно встаёт в хвост; порядок сохраняется."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	first = await queue.enqueue(PostDraft(channel_id, text="первый"))
	second = await queue.enqueue(PostDraft(channel_id, text="второй"))
	await _wait_status(queue, first, QueueItemStatus.SENDING)
	await _wait_progress(queue, first, 0.5)  # прогресс доехал до состояния
	items = {item.id: item for item in await queue.state()}
	assert items[second].status is QueueItemStatus.PENDING
	gateway.release.set()
	await _wait_status(queue, first, QueueItemStatus.DONE)
	await _wait_status(queue, second, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["первый", "второй"]
	assert not await queue.has_unfinished()


async def test_cancel_pending_skips_send(db: Database) -> None:
	"""Отмена ожидающего: он не отправляется, остальные — по плану."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	first = await queue.enqueue(PostDraft(channel_id, text="первый"))
	second = await queue.enqueue(PostDraft(channel_id, text="второй"))
	await _wait_status(queue, first, QueueItemStatus.SENDING)
	await queue.cancel(second)
	gateway.release.set()
	await _wait_status(queue, first, QueueItemStatus.DONE)
	cancelled = await _wait_status(queue, second, QueueItemStatus.CANCELLED)
	assert cancelled.status is QueueItemStatus.CANCELLED
	assert [post.text for post in gateway.published] == ["первый"]


async def test_cancel_active_moves_to_next(db: Database) -> None:
	"""Отмена отправляющегося обрывает загрузку; очередь идёт дальше."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	first = await queue.enqueue(PostDraft(channel_id, text="первый"))
	second = await queue.enqueue(PostDraft(channel_id, text="второй"))
	await _wait_status(queue, first, QueueItemStatus.SENDING)
	await queue.cancel(first)
	await _wait_status(queue, first, QueueItemStatus.CANCELLED)
	await _wait_status(queue, second, QueueItemStatus.SENDING)
	gateway.release.set()
	await _wait_status(queue, second, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["второй"]


async def test_error_does_not_stop_queue(db: Database) -> None:
	"""Ошибка одного элемента фиксируется в нём и не роняет следующие."""
	gateway = _SlowGateway()
	gateway.release.set()  # отправка без задержки
	gateway.fail_texts = {"сбойный"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	bad = await queue.enqueue(PostDraft(channel_id, text="сбойный"))
	good = await queue.enqueue(PostDraft(channel_id, text="целый"))
	failed = await _wait_status(queue, bad, QueueItemStatus.ERROR)
	assert failed.error is not None and "отклонил" in failed.error
	await _wait_status(queue, good, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["целый"]
	# ошибка висит в списке, пока её не уберут явно
	await queue.dismiss(bad)
	assert [item.id for item in await queue.state()] == [good]


async def test_enqueue_validates_immediately(db: Database) -> None:
	"""Негодный черновик отклоняется при постановке, а не при отправке."""
	queue = _queue(db, _SlowGateway())
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="пуст"):
		await queue.enqueue(PostDraft(channel_id))
	with pytest.raises(PostError, match="Канал не найден"):
		await queue.enqueue(PostDraft(999, text="x"))
	assert await queue.state() == []


async def test_dto_titles_and_flags(db: Database, tmp_path: Path) -> None:
	"""Заголовок — имя файла (учитывая переименование) или начало текста."""
	gateway = _SlowGateway()  # отмашки нет — всё висит, удобно смотреть
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"v")
	await queue.enqueue(PostDraft(
		channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
		rename_to="Новое имя.mp4",
	))
	when = datetime.now(UTC) + timedelta(hours=1)
	await queue.enqueue(PostDraft(channel_id, text="о" * 100, when=when))
	first, second = await queue.state()
	assert first.title == "Новое имя.mp4" and not first.scheduled
	assert first.when is None  # «сейчас» — интерфейс покажет это словом
	assert second.title == "о" * 59 + "…" and second.scheduled
	assert second.when == when  # момент публикации виден в карточке очереди
	assert second.channel_title == "Канал"
	assert await queue.has_unfinished()
	await queue.shutdown()  # гасим воркер с висящей отправкой


async def test_dismiss_ignores_unfinished(db: Database) -> None:
	"""Снять с показа можно только завершённый элемент."""
	queue = _queue(db, _SlowGateway())
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="живой"))
	await queue.dismiss(item)
	assert [i.id for i in await queue.state()] == [item]
	await queue.shutdown()
