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
from pxcontrol.engine.telegram.types import OutgoingPost


class _FakeGateway:
	"""Подмена шлюза: фиксирует отправки бота и публикации userbot."""

	def __init__(self) -> None:
		self.sent: list[tuple[str, str, str]] = []
		self.published: list[tuple[str, OutgoingPost]] = []
		self.userbot_ok = True

	async def send_text(self, token: str, chat_id: str, text: str) -> int:
		self.sent.append((token, chat_id, text))
		return 42

	async def publish(
		self, chat_id: str, post: OutgoingPost, on_progress: object
	) -> None:
		if not self.userbot_ok:
			raise UserbotUnavailable("Userbot не подключён — войдите в аккаунт.")
		if post.media_path is not None and callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		self.published.append((chat_id, post))

	def thumbs(self) -> list[str | None]:
		"""Миниатюры отправленных постов (в порядке отправки)."""
		return [post.thumb_path for _chat, post in self.published]

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
		("-1001", OutgoingPost(text="сразу")),
		("-1001", OutgoingPost(text="позже", when=when)),
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
	chat_id, post = gateway.published[0]
	assert (chat_id, post.text, post.media_path) == ("-1001", "подпись", str(video))
	assert post.media_kind == "video" and post.when is None
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


async def test_video_thumbnail_from_neighbor_preview(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Миниатюра видео режется из кадра-превью конвейера (сосед .png)."""
	sources: list[tuple[str, float]] = []

	def _fake_thumb(
		source: str, out: str, _bin: str = "ffmpeg", timestamp: float = 0.0
	) -> None:
		sources.append((source, timestamp))
		Path(out).write_bytes(b"jpg")

	monkeypatch.setattr(
		"pxcontrol.engine.services.posts.make_thumbnail", _fake_thumb
	)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	(tmp_path / "ролик.png").write_bytes(b"png")
	await service.publish(PostDraft(
		channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
	))
	assert sources == [(str(tmp_path / "ролик.png"), 0.0)]
	thumb = gateway.thumbs()[0]
	assert thumb is not None and thumb.endswith(".jpg")


async def test_video_thumbnail_random_middle_without_preview(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Без превью-соседа миниатюра — случайный кадр из середины видео."""
	from pxcontrol.engine.video.probe import VideoInfo

	def _fake_thumb(
		source: str, out: str, _bin: str = "ffmpeg", timestamp: float = 0.0
	) -> None:
		assert source.endswith(".mp4") and 25.0 <= timestamp <= 75.0
		Path(out).write_bytes(b"jpg")

	monkeypatch.setattr(
		"pxcontrol.engine.services.posts.make_thumbnail", _fake_thumb
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.posts.probe_video",
		lambda _p, _b: VideoInfo(1920, 1080, 100.0, 25.0, True),
	)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "чужой.mp4"
	video.write_bytes(b"video")
	await service.publish(PostDraft(
		channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
	))
	assert gateway.thumbs()[0] is not None


async def test_video_thumbnail_failure_does_not_block_publish(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Сбой миниатюры не мешает публикации: уходит без неё."""
	def _boom(*_args: object, **_kwargs: object) -> None:
		raise RuntimeError("ffmpeg сломался")

	monkeypatch.setattr("pxcontrol.engine.services.posts.make_thumbnail", _boom)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	(tmp_path / "ролик.png").write_bytes(b"png")
	await service.publish(PostDraft(
		channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
	))
	assert len(gateway.published) == 1 and gateway.thumbs() == [None]


async def test_publish_renames_file_and_preview(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""rename_to переименовывает файл и кадр-превью, уходит новый путь."""
	def _fake_thumb(
		source: str, out: str, _bin: str = "ffmpeg", timestamp: float = 0.0
	) -> None:
		assert source.endswith("Новое имя.png")  # превью ищется по новому имени
		Path(out).write_bytes(b"jpg")

	monkeypatch.setattr(
		"pxcontrol.engine.services.posts.make_thumbnail", _fake_thumb
	)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "старое_test_20260714-000000.mp4"
	video.write_bytes(b"video")
	(tmp_path / "старое_test_20260714-000000.png").write_bytes(b"png")
	await service.publish(PostDraft(
		channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
		rename_to="Новое имя.mp4",
	))
	_chat, post = gateway.published[0]
	assert post.media_path == str(tmp_path / "Новое имя.mp4")
	assert (tmp_path / "Новое имя.mp4").is_file()
	assert (tmp_path / "Новое имя.png").is_file()
	assert not video.exists()


async def test_publish_rename_validations(db: Database, tmp_path: Path) -> None:
	"""Имя с путём или занятое имя — ошибка до отправки."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "в.mp4"
	video.write_bytes(b"video")
	with pytest.raises(PostError, match="не должно содержать путь"):
		await service.publish(PostDraft(
			channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
			rename_to="a/b.mp4",
		))
	(tmp_path / "занято.mp4").write_bytes(b"x")
	with pytest.raises(PostError, match="уже существует"):
		await service.publish(PostDraft(
			channel_id, media_path=str(video), media_kind=MediaKind.VIDEO,
			rename_to="занято.mp4",
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
