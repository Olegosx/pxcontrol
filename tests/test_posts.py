"""Тесты сервиса постов (fire-and-forget, ADR-0010) — без сети."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Channel
from pxcontrol.engine.services.posts import (
	MediaKind,
	PostDraft,
	PostError,
	PostsService,
	ScheduledPostDto,
)
from pxcontrol.engine.telegram.mtproto import UserbotUnavailable


class _FakeGateway:
	"""Подмена шлюза: фиксирует отправки бота и публикации userbot."""

	def __init__(self) -> None:
		self.sent: list[tuple[str, str, str]] = []
		self.published: list[tuple[str, str, str | None, str, datetime | None]] = []
		self.userbot_ok = True

	async def send_text(self, token: str, chat_id: str, text: str) -> int:
		self.sent.append((token, chat_id, text))
		return 42

	async def publish(
		self, chat_id: str, text: str, media_path: str | None,
		media_kind: str, when: datetime | None, on_progress: object,
	) -> None:
		if not self.userbot_ok:
			raise UserbotUnavailable("Userbot не подключён — войдите в аккаунт.")
		if media_path is not None and callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		self.published.append((chat_id, text, media_path, media_kind, when))

	async def get_scheduled(self, chat_id: str) -> list[object]:
		return [SimpleNamespace(
			message="Отложенный текст",
			date=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
		)]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'posts.db'}")
	await database.init()
	yield database
	await database.close()


async def _add_channel(db: Database, with_bot: bool = True) -> int:
	"""Создаёт канал (и бота при необходимости), возвращает id канала."""
	async with db.session_factory() as session:
		bot_id = None
		if with_bot:
			bot = Bot(label="Паблишер", token="123:AAA", username="pub_bot")
			session.add(bot)
			await session.flush()
			bot_id = bot.id
		channel = Channel(title="Канал", tg_chat_id="-1001", bot_id=bot_id)
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
		return channel.id


async def test_send_now_via_bot(db: Database) -> None:
	"""«Сейчас» уходит через бота канала с расшифрованным токеном."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	assert await service.send_now(channel_id, "привет") == 42
	token, chat_id, text = gateway.sent[0]
	assert (token, chat_id, text) == ("123:AAA", "-1001", "привет")


async def test_send_now_requires_bot(db: Database) -> None:
	"""Канал без бота — понятная ошибка, отправки нет."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db, with_bot=False)
	with pytest.raises(PostError, match="не назначен бот"):
		await service.send_now(channel_id, "x")
	assert gateway.sent == []


async def test_publish_text_now_and_scheduled(db: Database) -> None:
	"""Текст уходит через userbot: сразу (when=None) и отложенно."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	await service.publish(PostDraft(channel_id, text="сразу"))
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.publish(PostDraft(channel_id, text="позже", when=when))
	assert gateway.published == [
		("-1001", "сразу", None, "none", None),
		("-1001", "позже", None, "none", when),
	]


async def test_publish_media_with_progress(db: Database, tmp_path: Path) -> None:
	"""Медиа уходит с типом и подписью, прогресс пробрасывается."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	received: list[float] = []
	draft = PostDraft(
		channel_id, text="подпись", media_path=str(video),
		media_kind=MediaKind.VIDEO,
	)
	await service.publish(draft, on_progress=received.append)
	assert gateway.published == [("-1001", "подпись", str(video), "video", None)]
	assert received == [0.5, 1.0]


async def test_publish_validations(db: Database, tmp_path: Path) -> None:
	"""Пустой черновик, битый путь, тип, «почти сейчас» — до похода в Telegram."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="пуст"):
		await service.publish(PostDraft(channel_id))
	with pytest.raises(PostError, match="не указан тип"):
		await service.publish(PostDraft(channel_id, media_path="x.bin"))
	with pytest.raises(PostError, match="не найден"):
		await service.publish(PostDraft(
			channel_id, media_path=str(tmp_path / "нет.jpg"),
			media_kind=MediaKind.PHOTO,
		))
	with pytest.raises(PostError, match="в будущем"):
		await service.publish(PostDraft(
			channel_id, text="x", when=datetime.now(UTC)
		))
	assert gateway.published == []


async def test_publish_userbot_unavailable(db: Database) -> None:
	"""Неподключённый userbot — ошибка с инструкцией, что делать."""
	gateway = _FakeGateway()
	gateway.userbot_ok = False
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(UserbotUnavailable, match="войдите"):
		await service.publish(PostDraft(channel_id, text="x"))


async def test_list_scheduled_reads_from_telegram(db: Database) -> None:
	"""Список отложенных собирается из Telegram по активным каналам."""
	service = PostsService(db, _FakeGateway())
	await _add_channel(db)
	items = await service.list_scheduled()
	assert items == [ScheduledPostDto(
		"Канал", "Отложенный текст", datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
	)]
