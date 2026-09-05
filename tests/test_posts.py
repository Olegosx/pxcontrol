"""Тесты сервиса постов (fire-and-forget, ADR-0010) — без сети."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Channel, TgAccount
from pxcontrol.engine.services.posts import (
	PostDraft,
	PostError,
	PostsService,
	ScheduledPostDto,
)
from pxcontrol.engine.services.settings import CHANNEL_ENABLED, SettingsService
from pxcontrol.engine.telegram.mtproto import UserbotUnavailableError
from pxcontrol.engine.telegram.types import MediaKind, OutgoingPost, ScheduledMessage


class _FakeGateway:
	"""Подмена шлюза: фиксирует отправки бота и публикации userbot.

	Публикации и чтения приходят с id аккаунта (ADR-0019) — он пишется
	в журнал вызовов, тесты проверяют адресацию.
	"""

	def __init__(self) -> None:
		self.sent: list[tuple[str, str, str]] = []
		self.media: list[tuple[str, str, str, str, str]] = []
		self.published: list[tuple[int, str, OutgoingPost]] = []
		self.userbot_ok = True
		self.premium_ids: set[int] = set()

	def userbot_premium(self, account_id: int | None) -> bool:
		return account_id in self.premium_ids

	async def send_text(self, token: str, chat_id: str, text: str) -> int:
		self.sent.append((token, chat_id, text))
		return 42

	async def send_media(self, token: str, chat_id: str, kind: str, path: str, caption: str) -> int:
		self.media.append((token, chat_id, kind, path, caption))
		return 43

	async def publish(
		self, account_id: int, chat_id: str, post: OutgoingPost, on_progress: object
	) -> None:
		if not self.userbot_ok:
			raise UserbotUnavailableError("Userbot не подключён — войдите в аккаунт.")
		if post.media_path is not None and callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		self.published.append((account_id, chat_id, post))

	def sent_posts(self) -> list[tuple[str, OutgoingPost]]:
		"""Публикации без id аккаунта (для тестов, где адресация не важна)."""
		return [(chat, post) for _acc, chat, post in self.published]

	def thumbs(self) -> list[str | None]:
		"""Миниатюры отправленных постов (в порядке отправки)."""
		return [post.thumb_path for _acc, _chat, post in self.published]

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		return [
			ScheduledMessage(
				text="Отложенный текст",
				scheduled_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
			)
		]


async def _add_account(db: Database, label: str = "@ub") -> int:
	"""Создаёт userbot-аккаунт с сессией, возвращает id."""
	async with db.session_factory() as session:
		account = TgAccount(label=label, phone="+7900", session="s")
		session.add(account)
		await session.commit()
		await session.refresh(account)
		return account.id


async def _add_channel(db: Database, with_bot: bool = True, userbot_admin: bool = True) -> int:
	"""Создаёт канал (при нужде — бота и userbot-аккаунт), возвращает id."""
	async with db.session_factory() as session:
		bot_id = None
		if with_bot:
			bot = Bot(label="Паблишер", token="123:AAA", username="pub_bot")
			session.add(bot)
			await session.flush()
			bot_id = bot.id
		account_id = None
		if userbot_admin:
			account = TgAccount(label="@ub", phone="+7900", session="s")
			session.add(account)
			await session.flush()
			account_id = account.id
		channel = Channel(
			title="Канал",
			tg_chat_id="-1001",
			bot_id=bot_id,
			tg_account_id=account_id,
		)
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
		return channel.id


async def test_publish_text_now_and_scheduled(db: Database) -> None:
	"""Текст уходит через userbot: сразу (when=None) и отложенно."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	await service.publish(PostDraft(channel_id, text="сразу"))
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.publish(PostDraft(channel_id, text="позже", when=when))
	assert gateway.sent_posts() == [
		("-1001", OutgoingPost(text="сразу")),
		("-1001", OutgoingPost(text="позже", when=when)),
	]
	# публикация адресована аккаунту, привязанному к каналу (ADR-0019)
	bound = await service.account_for_channel(channel_id)
	assert [acc for acc, _chat, _post in gateway.published] == [bound, bound]


async def test_publish_media_with_progress(db: Database, tmp_path: Path) -> None:
	"""Медиа уходит с типом и подписью, прогресс пробрасывается."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	received: list[float] = []
	draft = PostDraft(
		channel_id,
		text="подпись",
		media_path=str(video),
		media_kind=MediaKind.VIDEO,
	)
	await service.publish(draft, on_progress=received.append)
	chat_id, post = gateway.sent_posts()[0]
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
		await service.publish(
			PostDraft(
				channel_id,
				media_path=str(tmp_path / "нет.jpg"),
				media_kind=MediaKind.PHOTO,
			)
		)
	with pytest.raises(PostError, match="в будущем"):
		await service.publish(PostDraft(channel_id, text="x", when=datetime.now(UTC)))
	assert gateway.published == []


async def test_video_thumbnail_from_neighbor_preview(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Миниатюра видео режется из кадра-превью конвейера (сосед .png)."""
	sources: list[tuple[str, float]] = []

	def _fake_thumb(source: str, out: str, _bin: str = "ffmpeg", timestamp: float = 0.0) -> None:
		sources.append((source, timestamp))
		Path(out).write_bytes(b"jpg")

	monkeypatch.setattr("pxcontrol.engine.services.posts._make_thumbnail", _fake_thumb)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	(tmp_path / "ролик.png").write_bytes(b"png")
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
		)
	)
	assert sources == [(str(tmp_path / "ролик.png"), 0.0)]
	thumb = gateway.thumbs()[0]
	assert thumb is not None and thumb.endswith(".jpg")


async def test_video_thumbnail_random_middle_without_preview(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Без превью-соседа миниатюра — случайный кадр из середины видео."""
	from pxcontrol.engine.video.probe import VideoInfo

	def _fake_thumb(source: str, out: str, _bin: str = "ffmpeg", timestamp: float = 0.0) -> None:
		assert source.endswith(".mp4") and 25.0 <= timestamp <= 75.0
		Path(out).write_bytes(b"jpg")

	monkeypatch.setattr("pxcontrol.engine.services.posts._make_thumbnail", _fake_thumb)
	monkeypatch.setattr(
		"pxcontrol.engine.services.posts.probe_video",
		lambda _p, _b: VideoInfo(1920, 1080, 100.0, 25.0, True),
	)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "чужой.mp4"
	video.write_bytes(b"video")
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
		)
	)
	assert gateway.thumbs()[0] is not None


async def test_video_thumbnail_failure_does_not_block_publish(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Сбой миниатюры не мешает публикации: уходит без неё."""

	def _boom(*_args: object, **_kwargs: object) -> None:
		raise RuntimeError("ffmpeg сломался")

	monkeypatch.setattr("pxcontrol.engine.services.posts._make_thumbnail", _boom)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	(tmp_path / "ролик.png").write_bytes(b"png")
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
		)
	)
	assert len(gateway.published) == 1 and gateway.thumbs() == [None]


async def test_publish_renames_file_and_preview(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""rename_to переименовывает файл и кадр-превью, уходит новый путь."""

	def _fake_thumb(source: str, out: str, _bin: str = "ffmpeg", timestamp: float = 0.0) -> None:
		assert source.endswith("Новое имя.png")  # превью ищется по новому имени
		Path(out).write_bytes(b"jpg")

	monkeypatch.setattr("pxcontrol.engine.services.posts._make_thumbnail", _fake_thumb)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "старое_test_20260714-000000.mp4"
	video.write_bytes(b"video")
	(tmp_path / "старое_test_20260714-000000.png").write_bytes(b"png")
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
			rename_to="Новое имя.mp4",
		)
	)
	_chat, post = gateway.sent_posts()[0]
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
		await service.publish(
			PostDraft(
				channel_id,
				media_path=str(video),
				media_kind=MediaKind.VIDEO,
				rename_to="a/b.mp4",
			)
		)
	(tmp_path / "занято.mp4").write_bytes(b"x")
	with pytest.raises(PostError, match="уже существует"):
		await service.publish(
			PostDraft(
				channel_id,
				media_path=str(video),
				media_kind=MediaKind.VIDEO,
				rename_to="занято.mp4",
			)
		)
	assert gateway.published == []


def test_publish_capabilities() -> None:
	"""Возможности из способов администрирования; userbot — приоритет."""
	from pxcontrol.engine.services.posts import publish_capabilities

	both = publish_capabilities(bot_assigned=True, userbot_admin=True)
	assert both.userbot and both.bot
	bot_only = publish_capabilities(bot_assigned=True, userbot_admin=False)
	assert not bot_only.userbot and bot_only.bot
	none = publish_capabilities(bot_assigned=False, userbot_admin=False)
	assert not none.userbot and not none.bot


async def test_publish_bot_fallback_text_and_media(db: Database, tmp_path: Path) -> None:
	"""Канал «только бот»: текст и медиа ≤50 МБ уходят через Bot API."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db, userbot_admin=False)
	await service.publish(PostDraft(channel_id, text="через бота"))
	assert gateway.sent == [("123:AAA", "-1001", "через бота")]
	photo = tmp_path / "фото.jpg"
	photo.write_bytes(b"jpg")
	await service.publish(
		PostDraft(
			channel_id,
			text="подпись",
			media_path=str(photo),
			media_kind=MediaKind.PHOTO,
		)
	)
	assert gateway.media == [("123:AAA", "-1001", "photo", str(photo), "подпись")]
	assert gateway.published == []  # userbot-путь не задействован


async def test_publish_bot_limits(db: Database, tmp_path: Path) -> None:
	"""Канал «только бот»: отложка и файлы >50 МБ — ошибки до отправки."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db, userbot_admin=False)
	when = datetime.now(UTC) + timedelta(hours=1)
	with pytest.raises(PostError, match="userbot-админа"):
		await service.publish(PostDraft(channel_id, text="x", when=when))
	big = tmp_path / "большой.mp4"
	with big.open("wb") as handle:
		handle.truncate(51 * 1024 * 1024)  # разрежённый файл, диск не страдает
	with pytest.raises(PostError, match="50 МБ"):
		await service.publish(
			PostDraft(
				channel_id,
				media_path=str(big),
				media_kind=MediaKind.VIDEO,
			)
		)
	assert gateway.sent == [] and gateway.media == []


async def test_publish_without_any_way(db: Database) -> None:
	"""Канал без способов публикации — понятная ошибка."""
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db, with_bot=False, userbot_admin=False)
	with pytest.raises(PostError, match="нет способа публикации"):
		await service.publish(PostDraft(channel_id, text="x"))


async def test_publish_userbot_unavailable(db: Database) -> None:
	"""Неподключённый userbot — ошибка с инструкцией, что делать."""
	gateway = _FakeGateway()
	gateway.userbot_ok = False
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(UserbotUnavailableError, match="войдите"):
		await service.publish(PostDraft(channel_id, text="x"))


async def test_list_scheduled_reads_from_telegram(db: Database) -> None:
	"""Список отложенных собирается из Telegram по активным каналам."""
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db)
	items = await service.list_scheduled()
	assert items == [
		ScheduledPostDto(
			channel_id,
			"Канал",
			"Отложенный текст",
			datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
		)
	]


async def test_list_scheduled_skips_disabled_channel(db: Database) -> None:
	"""Выключенный канал (настройка enabled = False) не опрашивается."""
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db)
	assert len(await service.list_scheduled()) == 1
	await SettingsService(db).set_for(CHANNEL_ENABLED, channel_id, False)
	assert await service.list_scheduled() == []


async def test_list_scheduled_skips_bot_only_channel(db: Database) -> None:
	"""Бот-канал не опрашивается: Bot API отложенных не умеет.

	Раньше такой канал ронял всю страницу «Расписание» ошибкой userbot.
	"""
	service = PostsService(db, _FakeGateway())
	await _add_channel(db, with_bot=True, userbot_admin=False)
	assert await service.list_scheduled() == []


async def test_list_scheduled_isolates_channel_failure(db: Database) -> None:
	"""Ошибка одного канала не роняет список: канал пропускается."""

	class _FlakyGateway(_FakeGateway):
		async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
			if chat_id == "-1001":
				raise UserbotUnavailableError("Telegram просит подождать 5 с.")
			return await super().get_scheduled(account_id, chat_id)

	service = PostsService(db, _FlakyGateway())
	await _add_channel(db)  # tg_chat_id="-1001" — упадёт
	other_account = await _add_account(db, "@второй")
	async with db.session_factory() as session:
		session.add(
			Channel(
				title="Второй",
				tg_chat_id="-1002",
				bot_id=None,
				tg_account_id=other_account,
			)
		)
		await session.commit()
	items = await service.list_scheduled()
	assert [item.channel_title for item in items] == ["Второй"]


async def test_publish_rejects_disabled_channel(db: Database) -> None:
	"""Выключенный канал не публикует — правило движка, не интерфейса."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	await SettingsService(db).set_for(CHANNEL_ENABLED, channel_id, False)
	with pytest.raises(PostError, match="выключен"):
		await service.publish(PostDraft(channel_id, text="x"))
	assert gateway.published == []


async def test_publish_userbot_rejects_oversized_file(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Файл больше лимита аккаунта отклоняется до загрузки; Premium — щедрее."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.posts.userbot_max_file_bytes",
		lambda premium: 20 if premium else 10,
	)
	big = tmp_path / "big.bin"
	big.write_bytes(b"x" * 11)
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	draft = PostDraft(
		channel_id,
		media_path=str(big),
		media_kind=MediaKind.DOCUMENT,
	)
	with pytest.raises(PostError, match="лимит"):
		await service.publish(draft)
	assert gateway.published == []
	# тот же файл у Premium-аккаунта канала проходит (лимит выше)
	bound = await service.account_for_channel(channel_id)
	assert bound is not None
	gateway.premium_ids = {bound}
	await service.publish(draft)
	assert len(gateway.published) == 1


def test_userbot_limits_are_telegram_exact() -> None:
	"""Лимиты — точные значения Telegram (части по 512 КиБ), не «круглые» ГиБ."""
	from pxcontrol.engine.telegram.types import (
		USERBOT_MAX_FILE_BYTES,
		USERBOT_PREMIUM_MAX_FILE_BYTES,
		userbot_max_file_bytes,
	)

	assert USERBOT_MAX_FILE_BYTES == 2_097_152_000  # 2000 МиБ < 2 ГиБ
	assert USERBOT_PREMIUM_MAX_FILE_BYTES == 4_194_304_000  # 4000 МиБ
	assert userbot_max_file_bytes(False) == USERBOT_MAX_FILE_BYTES
	assert userbot_max_file_bytes(True) == USERBOT_PREMIUM_MAX_FILE_BYTES


async def test_published_video_moves_to_published_dir(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Видео из папки результатов переезжает в опубликованные (с превью)."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	processed = tmp_path / "media" / "processed" / "суб"
	processed.mkdir(parents=True)
	video = processed / "ролик.mp4"
	video.write_bytes(b"video")
	(processed / "ролик.png").write_bytes(b"png")
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db)
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
		)
	)
	published = tmp_path / "media" / "published" / "суб"
	assert (published / "ролик.mp4").is_file()
	assert (published / "ролик.png").is_file()
	assert not video.exists()


async def test_video_outside_processed_dir_stays(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Видео не из папки результатов после публикации остаётся на месте."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	video = tmp_path / "чужое.mp4"
	video.write_bytes(b"video")
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db)
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
		)
	)
	assert video.is_file()


async def test_move_failure_does_not_break_publish(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Сбой переезда — предупреждение в лог, публикация считается успешной."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")

	def _boom(*_args: object, **_kwargs: object) -> None:
		raise OSError("диск переполнен")

	monkeypatch.setattr("pxcontrol.engine.services.posts.shutil.move", _boom)
	processed = tmp_path / "media" / "processed"
	processed.mkdir(parents=True)
	video = processed / "ролик.mp4"
	video.write_bytes(b"video")
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	channel_id = await _add_channel(db)
	await service.publish(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
		)
	)
	assert len(gateway.published) == 1  # пост ушёл, несмотря на сбой переезда


# --- уборка опустевших папок (ADR-0016, «Уборка опустевших папок») ------------


async def test_publish_from_queue_prunes_emptied_batch_dirs(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Уход последнего файла пакета убирает его папки в очереди и результатах."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	processed = tmp_path / "media" / "processed" / "пакет"
	processed.mkdir(parents=True)  # опустела при постановке: файл уже в очереди
	queued = tmp_path / "media" / "queued" / "пакет"
	queued.mkdir(parents=True)
	video = queued / "ролик.mp4"
	video.write_bytes(b"video")
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db)
	await service.publish(PostDraft(channel_id, media_path=str(video), media_kind=MediaKind.VIDEO))
	assert (tmp_path / "media" / "published" / "пакет" / "ролик.mp4").is_file()
	assert not queued.exists()  # очередь по папке отработана
	assert not processed.exists()  # зеркало опустело — файлы уже не вернутся


async def test_stash_keeps_emptied_processed_dir(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Постановка в очередь папку результатов не трогает: файл может вернуться."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	processed = tmp_path / "media" / "processed" / "пакет"
	processed.mkdir(parents=True)
	video = processed / "ролик.mp4"
	video.write_bytes(b"video")
	service = PostsService(db, _FakeGateway())
	stashed = await service.stash_for_queue(str(video), MediaKind.VIDEO)
	assert Path(stashed).is_file()
	assert processed.is_dir()  # пустая, но стоит — ждёт возможного возврата


async def test_unstash_prunes_emptied_queue_dir(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Возврат последнего файла при отмене убирает папку пакета в очереди."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	queued = tmp_path / "media" / "queued" / "пакет"
	queued.mkdir(parents=True)
	video = queued / "ролик.mp4"
	video.write_bytes(b"video")
	service = PostsService(db, _FakeGateway())
	returned = await service.unstash_from_queue(str(video))
	assert Path(returned) == tmp_path / "media" / "processed" / "пакет" / "ролик.mp4"
	assert Path(returned).is_file()
	assert not queued.exists()  # папка пакета в очереди опустела и убрана


async def test_sweep_queue_dirs_removes_leftovers_only(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Уборка при старте метёт пустые папки очереди и зеркала, чужое не трогает."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	stale_queued = tmp_path / "media" / "queued" / "старый" / "вложенный"
	stale_queued.mkdir(parents=True)
	live_queued = tmp_path / "media" / "queued" / "живой"
	live_queued.mkdir(parents=True)
	(live_queued / "ждёт.mp4").write_bytes(b"video")
	stale_processed = tmp_path / "media" / "processed" / "старый" / "вложенный"
	stale_processed.mkdir(parents=True)
	preset_dir = tmp_path / "media" / "processed" / "пресет"
	preset_dir.mkdir(parents=True)  # рабочая папка без зеркала — не мусор
	service = PostsService(db, _FakeGateway())
	await service.sweep_queue_dirs()
	assert not (tmp_path / "media" / "queued" / "старый").exists()
	assert (live_queued / "ждёт.mp4").is_file()  # непустая папка живёт
	assert not (tmp_path / "media" / "processed" / "старый").exists()
	assert preset_dir.is_dir()  # папка результатов сама по себе не метётся


def test_validate_draft_checks_rename_early(tmp_path: Path) -> None:
	"""Негодное имя переименования ловится при постановке в очередь.

	Докстринг validate_draft обещает раннюю ошибку; раньше «.» и «..»
	доходили до отправки и роняли её сырым ValueError из Path.with_name.
	"""
	media = tmp_path / "clip.mp4"
	media.write_bytes(b"x")
	for bad_name in (".", "..", "a/b.mp4", "a\\b.mp4"):
		draft = PostDraft(
			1,
			media_path=str(media),
			media_kind=MediaKind.VIDEO,
			rename_to=bad_name,
		)
		with pytest.raises(PostError):
			PostsService.validate_draft(draft)


async def test_scheduled_times_for_batch_planning(db: Database) -> None:
	"""Времена отложек канала — для пропуска занятых слотов (ADR-0015)."""
	service = PostsService(db, _FakeGateway())
	channel_id = await _add_channel(db)
	assert await service.scheduled_times(channel_id) == [datetime(2026, 7, 13, 12, 0, tzinfo=UTC)]
	# канал без userbot-админа отложек иметь не может — пустой список
	async with db.session_factory() as session:
		other = Channel(title="Бот-канал", tg_chat_id="-1002", tg_account_id=None)
		session.add(other)
		await session.commit()
		await session.refresh(other)
	assert await service.scheduled_times(other.id) == []
