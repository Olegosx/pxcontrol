"""Тесты сервиса постов (fire-and-forget, ADR-0010) — без сети."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Channel
from pxcontrol.engine.services.posts import PostError, PostsService, ScheduledPostDto
from pxcontrol.engine.telegram.mtproto import UserbotUnavailable


class _FakeGateway:
	"""Подмена шлюза: фиксирует отправки и отложки."""

	def __init__(self) -> None:
		self.sent: list[tuple[str, str, str]] = []
		self.scheduled: list[tuple[str, str, datetime]] = []
		self.videos: list[tuple[str, str, str, datetime | None]] = []
		self.userbot_ok = True

	async def send_text(self, token: str, chat_id: str, text: str) -> int:
		self.sent.append((token, chat_id, text))
		return 42

	async def schedule_post(self, chat_id: str, text: str, when: datetime) -> None:
		if not self.userbot_ok:
			raise UserbotUnavailable("Userbot не подключён — войдите в аккаунт.")
		self.scheduled.append((chat_id, text, when))

	async def send_video(
		self, chat_id: str, video_path: str, caption: str,
		when: datetime | None, on_progress: object,
	) -> None:
		if not self.userbot_ok:
			raise UserbotUnavailable("Userbot не подключён — войдите в аккаунт.")
		if callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		self.videos.append((chat_id, video_path, caption, when))

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


async def test_schedule_via_userbot(db: Database) -> None:
	"""Отложка уходит через userbot с точным временем."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.schedule(channel_id, "позже", when)
	assert gateway.scheduled == [("-1001", "позже", when)]


async def test_schedule_requires_future(db: Database) -> None:
	"""Время «почти сейчас» отклоняется до похода в Telegram."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="в будущем"):
		await service.schedule(channel_id, "x", datetime.now(UTC))
	assert gateway.scheduled == []


async def test_schedule_userbot_unavailable(db: Database) -> None:
	"""Неподключённый userbot — ошибка с инструкцией, что делать."""
	gateway = _FakeGateway()
	gateway.userbot_ok = False
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	when = datetime.now(UTC) + timedelta(hours=1)
	with pytest.raises(UserbotUnavailable, match="войдите"):
		await service.schedule(channel_id, "x", when)


async def test_send_video_now_and_scheduled(db: Database, tmp_path: Path) -> None:
	"""Видео уходит через userbot: сразу (when=None) и отложенно."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	received: list[float] = []
	await service.send_video(
		channel_id, str(video), "подпись", on_progress=received.append
	)
	when = datetime.now(UTC) + timedelta(hours=2)
	await service.send_video(channel_id, str(video), "", when)
	assert gateway.videos == [
		("-1001", str(video), "подпись", None),
		("-1001", str(video), "", when),
	]
	assert received == [0.5, 1.0]


async def test_send_video_validations(db: Database, tmp_path: Path) -> None:
	"""Нет файла или время «почти сейчас» — ошибка до похода в Telegram."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="не найден"):
		await service.send_video(channel_id, str(tmp_path / "нет.mp4"))
	video = tmp_path / "есть.mp4"
	video.write_bytes(b"video")
	with pytest.raises(PostError, match="в будущем"):
		await service.send_video(channel_id, str(video), when=datetime.now(UTC))
	assert gateway.videos == []


async def test_list_scheduled_reads_from_telegram(db: Database) -> None:
	"""Список отложенных собирается из Telegram по активным каналам."""
	service = PostsService(db, _FakeGateway())
	await _add_channel(db)
	items = await service.list_scheduled()
	assert items == [ScheduledPostDto(
		"Канал", "Отложенный текст", datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
	)]
