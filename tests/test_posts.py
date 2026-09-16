"""Тесты сервиса постов (fire-and-forget, ADR-0010) — без сети."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, CommunityMember, TgAccount
from pxcontrol.engine.services.posts import (
	PostDraft,
	PostError,
	PostNotReadyError,
	PostsService,
	ScheduledDraft,
	ScheduledGoneError,
	ScheduledList,
	ScheduledPostDto,
	ScheduledRef,
)
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.bot_api import CommunityCheckError
from pxcontrol.engine.telegram.markup import (
	ButtonKind,
	MarkupError,
	PostButton,
	PostMarkup,
)
from pxcontrol.engine.telegram.mtproto import (
	UserbotFloodError,
	UserbotMessageGoneError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	BotRef,
	ForumTopicInfo,
	MediaKind,
	OutgoingPost,
	ScheduledMessage,
	TelegramFloodError,
)


class _FakeGateway:
	"""Подмена шлюза: фиксирует отправки бота и публикации userbot.

	Публикации и чтения приходят с id аккаунта (ADR-0019) — он пишется
	в журнал вызовов, тесты проверяют адресацию.
	"""

	def __init__(self) -> None:
		self.sent: list[tuple[str, str, str]] = []
		self.sent_topics: list[int | None] = []
		self.topics: list[ForumTopicInfo] = []
		self.media: list[tuple[str, str, str, str, str]] = []
		self.published: list[tuple[int, str, OutgoingPost]] = []
		self.userbot_ok = True
		self.premium_ids: set[int] = set()
		# отложки: чтение одной записи, журнал действий и признак «уже нет»
		self.scheduled_by_id: dict[int, ScheduledMessage] = {}
		self.scheduled_reads: list[tuple[int, str, int]] = []
		self.scheduled_edits: list[tuple[int, str, int, str, datetime]] = []
		self.scheduled_sent: list[tuple[int, str, tuple[int, ...]]] = []
		self.scheduled_deleted: list[tuple[int, str, tuple[int, ...]]] = []
		self.scheduled_gone = False
		# кнопки (ADR-0031): что ушло с постом и что дорисовано правкой
		self.sent_markups: list[object] = []
		self.markup_edits: list[tuple[str, int, object]] = []
		self.markup_edit_error: Exception | None = None

	def userbot_premium(self, account_id: int | None) -> bool:
		return account_id in self.premium_ids

	async def bot_send_text(
		self,
		bot: BotRef,
		chat_id: str,
		text: str,
		topic_id: int | None = None,
		markup: object = None,
	) -> int:
		self.sent.append((bot.token, chat_id, text))
		self.sent_topics.append(topic_id)
		self.sent_markups.append(markup)
		return 42

	async def bot_send_media(
		self,
		bot: BotRef,
		chat_id: str,
		kind: str,
		path: str,
		caption: str,
		topic_id: int | None = None,
		markup: object = None,
	) -> int:
		self.media.append((bot.token, chat_id, kind, path, caption))
		self.sent_markups.append(markup)
		return 43

	async def bot_edit_markup(
		self, bot: BotRef, chat_id: str, message_id: int, markup: object
	) -> None:
		if self.markup_edit_error is not None:
			raise self.markup_edit_error
		self.markup_edits.append((chat_id, message_id, markup))

	async def get_forum_topics(self, account_id: int, chat_id: str) -> list[ForumTopicInfo]:
		return list(self.topics)

	async def publish(
		self, account_id: int, chat_id: str, post: OutgoingPost, on_progress: object
	) -> int:
		if not self.userbot_ok:
			raise UserbotUnavailableError("Userbot не подключён — войдите в аккаунт.")
		if post.media_path is not None and callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		self.published.append((account_id, chat_id, post))
		return 100 + len(self.published)

	def sent_posts(self) -> list[tuple[str, OutgoingPost]]:
		"""Публикации без id аккаунта (для тестов, где адресация не важна)."""
		return [(chat, post) for _acc, chat, post in self.published]

	def thumbs(self) -> list[str | None]:
		"""Миниатюры отправленных постов (в порядке отправки)."""
		return [post.thumb_path for _acc, _chat, post in self.published]

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		return [
			ScheduledMessage(
				id=501,
				text="Отложенный текст",
				scheduled_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
			)
		]

	async def get_scheduled_message(
		self, account_id: int, chat_id: str, message_id: int
	) -> ScheduledMessage | None:
		self.scheduled_reads.append((account_id, chat_id, message_id))
		return self.scheduled_by_id.get(message_id)

	async def edit_scheduled(
		self, account_id: int, chat_id: str, message_id: int, text: str, when: datetime
	) -> None:
		if self.scheduled_gone:
			raise UserbotMessageGoneError("Этой записи в Telegram уже нет.")
		self.scheduled_edits.append((account_id, chat_id, message_id, text, when))

	async def send_scheduled_now(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> None:
		self.scheduled_sent.append((account_id, chat_id, tuple(message_ids)))

	async def delete_scheduled(self, account_id: int, chat_id: str, message_ids: list[int]) -> None:
		if self.scheduled_gone:
			raise UserbotMessageGoneError("Этой записи в Telegram уже нет.")
		self.scheduled_deleted.append((account_id, chat_id, tuple(message_ids)))


async def _add_account(db: Database, label: str = "@ub") -> int:
	"""Создаёт userbot-аккаунт с сессией, возвращает id."""
	async with db.session_factory() as session:
		account = TgAccount(label=label, phone="+7900", session="s")
		session.add(account)
		await session.commit()
		await session.refresh(account)
		return account.id


async def _bound_account(db: Database, community_id: int) -> int | None:
	"""Публикатор сообщества по умолчанию — прямо из БД.

	Сервис постов такого метода не имеет: единственным его потребителем
	был дозор слотов, а тот больше не ведёт своего списка замороженных
	аккаунтов — дисциплину держит дорожка шлюза (ADR-0024).
	"""
	async with db.session_factory() as session:
		community = await session.get(Community, community_id)
		assert community is not None
		return community.default_tg_account_id


async def _add_community(
	db: Database,
	with_bot: bool = True,
	userbot_assigned: bool = True,
	forum: bool = False,
	tg_chat_id: str = "-1001",
	bot_can_edit: bool = False,
) -> int:
	"""Создаёт сообщество (при нужде — бота и userbot-аккаунт), возвращает id."""
	async with db.session_factory() as session:
		bot_id = None
		if with_bot:
			bot = Bot(label="Паблишер", token="123:AAA", username="pub_bot")
			session.add(bot)
			await session.flush()
			bot_id = bot.id
		account_id = None
		if userbot_assigned:
			account = TgAccount(label="@ub", phone="+7900", session="s")
			session.add(account)
			await session.flush()
			account_id = account.id
		community = Community(
			title="Канал",
			tg_chat_id=tg_chat_id,
			kind="group" if forum else "channel",
			forum=forum,
			bot_id=bot_id,
			bot_can_edit=bot_can_edit,
			default_tg_account_id=account_id,
		)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def test_publish_text_now_and_scheduled(db: Database) -> None:
	"""Текст уходит через userbot: сразу (when=None) и отложенно."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	await service.publish(PostDraft(community_id, text="сразу"))
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.publish(PostDraft(community_id, text="позже", when=when))
	assert gateway.sent_posts() == [
		("-1001", OutgoingPost(text="сразу")),
		("-1001", OutgoingPost(text="позже", when=when)),
	]
	# публикация адресована аккаунту, привязанному к каналу (ADR-0019)
	bound = await _bound_account(db, community_id)
	assert [acc for acc, _chat, _post in gateway.published] == [bound, bound]


async def test_publish_media_with_progress(db: Database, tmp_path: Path) -> None:
	"""Медиа уходит с типом и подписью, прогресс пробрасывается."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	received: list[float] = []
	draft = PostDraft(
		community_id,
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
	community_id = await _add_community(db)
	with pytest.raises(PostError, match="пуст"):
		await service.publish(PostDraft(community_id))
	with pytest.raises(PostError, match="не указан тип"):
		await service.publish(PostDraft(community_id, media_path="x.bin"))
	# вид «прочее» приложение только читает (отложки из клиента Telegram)
	with pytest.raises(PostError, match="не отправляет"):
		await service.publish(
			PostDraft(community_id, media_path="x.bin", media_kind=MediaKind.OTHER)
		)
	with pytest.raises(PostError, match="не найден"):
		await service.publish(
			PostDraft(
				community_id,
				media_path=str(tmp_path / "нет.jpg"),
				media_kind=MediaKind.PHOTO,
			)
		)
	with pytest.raises(PostError, match="в будущем"):
		await service.publish(PostDraft(community_id, text="x", when=datetime.now(UTC)))
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
	community_id = await _add_community(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	(tmp_path / "ролик.png").write_bytes(b"png")
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	video = tmp_path / "чужой.mp4"
	video.write_bytes(b"video")
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	(tmp_path / "ролик.png").write_bytes(b"png")
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	video = tmp_path / "старое_test_20260714-000000.mp4"
	video.write_bytes(b"video")
	(tmp_path / "старое_test_20260714-000000.png").write_bytes(b"png")
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	video = tmp_path / "в.mp4"
	video.write_bytes(b"video")
	with pytest.raises(PostError, match="не должно содержать путь"):
		await service.publish(
			PostDraft(
				community_id,
				media_path=str(video),
				media_kind=MediaKind.VIDEO,
				rename_to="a/b.mp4",
			)
		)
	(tmp_path / "занято.mp4").write_bytes(b"x")
	with pytest.raises(PostError, match="уже существует"):
		await service.publish(
			PostDraft(
				community_id,
				media_path=str(video),
				media_kind=MediaKind.VIDEO,
				rename_to="занято.mp4",
			)
		)
	assert gateway.published == []


def test_publish_capabilities() -> None:
	"""Возможности из способов администрирования; userbot — приоритет."""
	from pxcontrol.engine.services.posts import publish_capabilities

	both = publish_capabilities(bot_assigned=True, userbot_assigned=True)
	assert both.userbot and both.bot
	bot_only = publish_capabilities(bot_assigned=True, userbot_assigned=False)
	assert not bot_only.userbot and bot_only.bot
	none = publish_capabilities(bot_assigned=False, userbot_assigned=False)
	assert not none.userbot and not none.bot


async def test_publish_bot_fallback_text_and_media(db: Database, tmp_path: Path) -> None:
	"""Канал «только бот»: текст и медиа ≤50 МБ уходят через Bot API."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, userbot_assigned=False)
	await service.publish(PostDraft(community_id, text="через бота"))
	assert gateway.sent == [("123:AAA", "-1001", "через бота")]
	photo = tmp_path / "фото.jpg"
	photo.write_bytes(b"jpg")
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db, userbot_assigned=False)
	when = datetime.now(UTC) + timedelta(hours=1)
	with pytest.raises(PostError, match="userbot-админа"):
		await service.publish(PostDraft(community_id, text="x", when=when))
	big = tmp_path / "большой.mp4"
	with big.open("wb") as handle:
		handle.truncate(51 * 1024 * 1024)  # разрежённый файл, диск не страдает
	with pytest.raises(PostError, match="50 МБ"):
		await service.publish(
			PostDraft(
				community_id,
				media_path=str(big),
				media_kind=MediaKind.VIDEO,
			)
		)
	assert gateway.sent == [] and gateway.media == []


async def test_publish_without_any_way(db: Database) -> None:
	"""Сообщество без публикатора — поправимое состояние, не дефект поста.

	Класс важен: по нему очередь придерживает пост, а не хоронит
	ошибкой (ADR-0016) — публикатор вернут, и пост уйдёт сам.
	"""
	service = PostsService(db, _FakeGateway())
	community_id = await _add_community(db, with_bot=False, userbot_assigned=False)
	with pytest.raises(PostNotReadyError, match="нет публикатора"):
		await service.publish(PostDraft(community_id, text="x"))


async def test_publish_userbot_unavailable(db: Database) -> None:
	"""Неподключённый userbot — ошибка с инструкцией, что делать."""
	gateway = _FakeGateway()
	gateway.userbot_ok = False
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	with pytest.raises(UserbotUnavailableError, match="войдите"):
		await service.publish(PostDraft(community_id, text="x"))


async def test_list_scheduled_reads_from_telegram(db: Database) -> None:
	"""Список отложенных собирается из Telegram по активным каналам."""
	service = PostsService(db, _FakeGateway())
	community_id = await _add_community(db)
	scheduled = await service.list_scheduled()
	assert scheduled.items == [
		ScheduledPostDto(
			community_id=community_id,
			community_title="Канал",
			account_id=await _bound_account(db, community_id) or 0,
			message_id=501,
			text_preview="Отложенный текст",
			scheduled_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
		)
	]
	assert scheduled.unread == ()  # все сообщества опрошены


async def test_list_scheduled_skips_disabled_community(db: Database) -> None:
	"""Выключенный канал (настройка enabled = False) не опрашивается."""
	service = PostsService(db, _FakeGateway())
	community_id = await _add_community(db)
	assert len((await service.list_scheduled()).items) == 1
	await SettingsService(db).set_for(COMMUNITY_ENABLED, community_id, False)
	# выключенный не опрашивают намеренно — это не «не удалось прочитать»
	assert await service.list_scheduled() == ScheduledList(items=[], unread=())


async def test_list_scheduled_skips_bot_only_community(db: Database) -> None:
	"""Бот-канал не опрашивается: Bot API отложенных не умеет.

	Раньше такой канал ронял всю страницу «Расписание» ошибкой userbot.
	"""
	service = PostsService(db, _FakeGateway())
	await _add_community(db, with_bot=True, userbot_assigned=False)
	assert await service.list_scheduled() == ScheduledList(items=[], unread=())


async def test_list_scheduled_isolates_community_failure(db: Database) -> None:
	"""Ошибка одного канала не роняет список: канал пропускается."""

	class _FlakyGateway(_FakeGateway):
		async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
			if chat_id == "-1001":
				raise UserbotUnavailableError("Telegram просит подождать 5 с.")
			return await super().get_scheduled(account_id, chat_id)

	service = PostsService(db, _FlakyGateway())
	await _add_community(db)  # tg_chat_id="-1001" — упадёт
	other_account = await _add_account(db, "@второй")
	async with db.session_factory() as session:
		session.add(
			Community(
				title="Второй",
				tg_chat_id="-1002",
				bot_id=None,
				default_tg_account_id=other_account,
			)
		)
		await session.commit()
	scheduled = await service.list_scheduled()
	assert [item.community_title for item in scheduled.items] == ["Второй"]
	# упавшее сообщество названо: «нет отложенных» о нём утверждать нельзя
	assert scheduled.unread == ("Канал",)


async def test_publish_rejects_disabled_community(db: Database) -> None:
	"""Выключенный канал не публикует — правило движка, не интерфейса."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	await SettingsService(db).set_for(COMMUNITY_ENABLED, community_id, False)
	with pytest.raises(PostError, match="выключен"):
		await service.publish(PostDraft(community_id, text="x"))
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
	community_id = await _add_community(db)
	draft = PostDraft(
		community_id,
		media_path=str(big),
		media_kind=MediaKind.DOCUMENT,
	)
	with pytest.raises(PostError, match="лимит"):
		await service.publish(draft)
	assert gateway.published == []
	# тот же файл у Premium-аккаунта канала проходит (лимит выше)
	bound = await _bound_account(db, community_id)
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
	community_id = await _add_community(db)
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	await service.publish(
		PostDraft(
			community_id,
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
	community_id = await _add_community(db)
	await service.publish(
		PostDraft(community_id, media_path=str(video), media_kind=MediaKind.VIDEO)
	)
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
	community_id = await _add_community(db)
	assert await service.scheduled_times(community_id) == [datetime(2026, 7, 13, 12, 0, tzinfo=UTC)]
	# канал без userbot-админа отложек иметь не может — пустой список
	async with db.session_factory() as session:
		other = Community(title="Бот-канал", tg_chat_id="-1002", default_tg_account_id=None)
		session.add(other)
		await session.commit()
		await session.refresh(other)
	assert await service.scheduled_times(other.id) == []


async def test_list_scheduled_isolates_flooded_account(db: Database) -> None:
	"""Флуд-лимит одного аккаунта не мешает читать отложки другого.

	Своего списка «провинившихся» проход не ведёт: лимит помнит дорожка
	аккаунта (ADR-0024) и отказывает его операциям сама, не тревожа
	Telegram. От чтения требуется пережить отказ — пропустить сообщество
	и собрать всё остальное.
	"""

	class _PartlyFloodedGateway(_FakeGateway):
		"""Первый аккаунт под лимитом, второй отвечает нормально."""

		def __init__(self, flooded_id: int) -> None:
			super().__init__()
			self.flooded_id = flooded_id

		async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
			if account_id == self.flooded_id:
				raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
			return [ScheduledMessage(id=1, text="жив", scheduled_at=datetime.now(UTC))]

	flooded_community = await _add_community(db)
	flooded_id = await _bound_account(db, flooded_community)
	assert flooded_id is not None
	async with db.session_factory() as session:  # сообщество другого аккаунта
		account = TgAccount(label="@ub2", phone="+7901", session="s")
		session.add(account)
		await session.flush()
		session.add(
			Community(title="Свободный", tg_chat_id="-1002", default_tg_account_id=account.id)
		)
		await session.commit()
	service = PostsService(db, _PartlyFloodedGateway(flooded_id))
	scheduled = await service.list_scheduled()
	assert [item.community_title for item in scheduled.items] == ["Свободный"]
	assert scheduled.unread == ("Канал",)  # про него честно сказано «не прочитано»


async def test_topic_requires_forum(db: Database) -> None:
	"""Тема при выключенном форуме отклоняется до любых побочных эффектов."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	with pytest.raises(PostError, match="нет тем"):
		await service.publish(PostDraft(community_id, text="в тему", topic_id=7))
	assert gateway.published == [] and gateway.sent == []


async def test_topic_passes_to_userbot(db: Database) -> None:
	"""Тема форума доезжает до userbot-транспорта в исходящем посте."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, forum=True)
	await service.publish(PostDraft(community_id, text="в тему", topic_id=7))
	assert [post.topic_id for _chat, post in gateway.sent_posts()] == [7]


async def test_topic_passes_to_bot(db: Database) -> None:
	"""Бот умеет отправлять в тему (message_thread_id) — тема доезжает."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, userbot_assigned=False, forum=True)
	await service.publish(PostDraft(community_id, text="в тему", topic_id=7))
	assert gateway.sent_topics == [7]


async def test_list_topics_guards_and_reads(db: Database) -> None:
	"""Список тем: только форум и только userbot; чтение — живьём из шлюза."""
	gateway = _FakeGateway()
	gateway.topics = [ForumTopicInfo(1, "General"), ForumTopicInfo(7, "Новости")]
	service = PostsService(db, gateway)
	plain = await _add_community(db)
	with pytest.raises(PostError, match="не включены"):
		await service.list_topics(plain)
	# сообщество-форум только с ботом: перечислить темы нечем (Bot API не умеет)
	bot_only = await _add_community(db, userbot_assigned=False, forum=True, tg_chat_id="-1002")
	with pytest.raises(PostError, match="userbot"):
		await service.list_topics(bot_only)


async def test_list_topics_returns_from_gateway(db: Database) -> None:
	"""Темы форума с userbot-привязкой читаются через шлюз."""
	gateway = _FakeGateway()
	gateway.topics = [ForumTopicInfo(1, "General"), ForumTopicInfo(7, "Новости")]
	service = PostsService(db, gateway)
	community_id = await _add_community(db, with_bot=False, forum=True)
	topics = await service.list_topics(community_id)
	assert [(t.id, t.title) for t in topics] == [(1, "General"), (7, "Новости")]


async def _add_group_with_members(db: Database, count: int = 2) -> tuple[int, list[int]]:
	"""Группа с пулом участников (первый — умолчание); id группы и аккаунтов."""
	async with db.session_factory() as session:
		accounts = [TgAccount(label=f"@ub{i}", phone=f"+790{i}", session="s") for i in range(count)]
		session.add_all(accounts)
		await session.flush()
		community = Community(
			title="Группа",
			tg_chat_id="-1005",
			kind="group",
			forum=False,
			default_tg_account_id=accounts[0].id,
		)
		session.add(community)
		await session.flush()
		session.add_all(
			CommunityMember(
				community_id=community.id,
				tg_account_id=account.id,
				role="admin" if account is accounts[0] else "member",
			)
			for account in accounts
		)
		await session.commit()
		return community.id, [account.id for account in accounts]


class _PerAccountGateway(_FakeGateway):
	"""Отложки — свои у каждого аккаунта (как в группах Telegram)."""

	def __init__(self) -> None:
		super().__init__()
		self.per_account: dict[int, list[ScheduledMessage]] = {}
		self.polled: list[int] = []
		self.flooded_accounts: set[int] = set()

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		self.polled.append(account_id)
		if account_id in self.flooded_accounts:
			raise TelegramFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
		return self.per_account.get(account_id, [])


async def test_list_scheduled_group_reads_all_members(db: Database) -> None:
	"""Отложки группы собираются всеми участниками (ADR-0022).

	Живой прогон 2026-09-06 подтвердил: отложку в группе видит только
	её создатель — опрос одним умолчанием терял бы записи остальных.
	"""
	gateway = _PerAccountGateway()
	service = PostsService(db, gateway)
	_community_id, (first, second) = await _add_group_with_members(db)
	gateway.per_account = {
		first: [ScheduledMessage(1, "от первого", datetime(2026, 7, 13, 12, 0, tzinfo=UTC))],
		second: [ScheduledMessage(2, "от второго", datetime(2026, 7, 13, 11, 0, tzinfo=UTC))],
	}
	scheduled = await service.list_scheduled()
	assert [item.text_preview for item in scheduled.items] == ["от второго", "от первого"]
	assert sorted(gateway.polled) == sorted([first, second])
	assert scheduled.unread == ()


async def test_list_scheduled_channel_polls_only_default(db: Database) -> None:
	"""Отложки канала читает только умолчание: у админов они общие."""
	gateway = _PerAccountGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, tg_chat_id="-1006")
	async with db.session_factory() as session:
		community = await session.get(Community, community_id)
		assert community is not None
		default_id = community.default_tg_account_id
		assert default_id is not None
		# второй админ-участник канала: опрос обоих дал бы дубли
		extra = TgAccount(label="@extra", phone="+7999", session="s")
		session.add(extra)
		await session.flush()
		session.add_all(
			CommunityMember(community_id=community_id, tg_account_id=acc_id, role="admin")
			for acc_id in (default_id, extra.id)
		)
		await session.commit()
	await service.list_scheduled()
	assert gateway.polled == [default_id]


async def test_list_scheduled_flood_of_member_spares_others(db: Database) -> None:
	"""Флуд-лимит одного участника не мешает опросу остальных."""
	gateway = _PerAccountGateway()
	service = PostsService(db, gateway)
	_community_id, (first, second) = await _add_group_with_members(db)
	gateway.flooded_accounts = {first}
	gateway.per_account = {
		second: [ScheduledMessage(3, "живой", datetime(2026, 7, 13, 12, 0, tzinfo=UTC))],
	}
	scheduled = await service.list_scheduled()
	assert [item.text_preview for item in scheduled.items] == ["живой"]
	# группу опрашивают двое: непрочитанной она названа один раз
	assert scheduled.unread == ("Группа",)


async def test_list_scheduled_dedups_identical(db: Database) -> None:
	"""Одинаковая запись от двух читателей показывается один раз."""
	gateway = _PerAccountGateway()
	service = PostsService(db, gateway)
	_community_id, (first, second) = await _add_group_with_members(db)
	same = ScheduledMessage(7, "общая", datetime(2026, 7, 13, 12, 0, tzinfo=UTC))
	gateway.per_account = {first: [same], second: [same]}
	scheduled = await service.list_scheduled()
	assert [item.text_preview for item in scheduled.items] == ["общая"]
	assert scheduled.items[0].account_id == first  # остаётся первый читатель


async def test_list_scheduled_keeps_twins_with_different_ids(db: Database) -> None:
	"""Два поста с одинаковым текстом и временем — два поста, а не дубль.

	Тождество записи — её id в очереди сообщества, а не содержимое:
	раньше схлопывание по «текст + время» прятало бы второй из двух
	одинаковых постов, и его нельзя было бы ни увидеть, ни удалить.
	"""
	gateway = _PerAccountGateway()
	service = PostsService(db, gateway)
	_community_id, (first, _second) = await _add_group_with_members(db)
	when = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
	gateway.per_account = {
		first: [ScheduledMessage(1, "близнец", when), ScheduledMessage(2, "близнец", when)]
	}
	scheduled = await service.list_scheduled()
	assert [item.message_id for item in scheduled.items] == [1, 2]


# --- действия над отложенными (истина — сервер Telegram) --------------------


def _scheduled_service(db: Database) -> tuple[PostsService, _FakeGateway]:
	gateway = _FakeGateway()
	return PostsService(db, gateway), gateway


async def _scheduled_ref(db: Database, community_id: int, message_id: int = 501) -> ScheduledRef:
	account_id = await _bound_account(db, community_id)
	assert account_id is not None
	return ScheduledRef(community_id, account_id, message_id)


async def test_scheduled_draft_reads_fresh_record_with_limit(db: Database) -> None:
	"""Форма правки получает запись с сервера и предел текста по аккаунту."""
	service, gateway = _scheduled_service(db)
	community_id = await _add_community(db)
	ref = await _scheduled_ref(db, community_id)
	when = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
	gateway.scheduled_by_id[501] = ScheduledMessage(
		501, "полный текст подписи", when, media_kind=MediaKind.PHOTO, topic_id=None
	)
	draft = await service.scheduled_draft(ref)
	assert draft.ref == ref
	assert draft.community_title == "Канал"
	assert draft.text == "полный текст подписи"
	assert draft.when == when
	assert draft.media_kind is MediaKind.PHOTO
	assert draft.text_limit == 1024  # подпись, аккаунт без Premium
	assert draft.text_editable is True
	assert gateway.scheduled_reads == [(ref.account_id, "-1001", 501)]

	gateway.premium_ids.add(ref.account_id)
	assert (await service.scheduled_draft(ref)).text_limit == 4096


async def test_scheduled_draft_of_gone_record(db: Database) -> None:
	"""Пустой ответ на чтение — «записи уже нет», а не пустая форма."""
	service, _gateway = _scheduled_service(db)
	community_id = await _add_community(db)
	with pytest.raises(ScheduledGoneError, match="уже нет"):
		await service.scheduled_draft(await _scheduled_ref(db, community_id))


def _draft(ref: ScheduledRef, **overrides: Any) -> ScheduledDraft:
	fields: dict[str, Any] = {
		"ref": ref,
		"community_title": "Канал",
		"text": "было",
		"when": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
		"media_kind": MediaKind.NONE,
		"topic_id": None,
		"text_limit": 4096,
	}
	fields.update(overrides)
	return ScheduledDraft(**fields)


async def test_edit_scheduled_passes_to_reader_account(db: Database) -> None:
	"""Правка уходит тем аккаунтом, чьим чтением запись попала в список."""
	service, gateway = _scheduled_service(db)
	community_id = await _add_community(db)
	ref = await _scheduled_ref(db, community_id)
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.edit_scheduled(_draft(ref), "стало", when)
	assert gateway.scheduled_edits == [(ref.account_id, "-1001", 501, "стало", when)]


async def test_edit_scheduled_rejects_bad_input(db: Database) -> None:
	"""Пустой текст у записи без вложения, длина и близкое время — отказ до сети."""
	service, gateway = _scheduled_service(db)
	community_id = await _add_community(db)
	ref = await _scheduled_ref(db, community_id)
	soon = datetime.now(UTC) + timedelta(seconds=10)
	later = datetime.now(UTC) + timedelta(hours=1)
	with pytest.raises(PostError, match="пуст"):
		await service.edit_scheduled(_draft(ref), "", later)
	with pytest.raises(PostError, match="минуту"):
		await service.edit_scheduled(_draft(ref), "текст", soon)
	# предел подписи (1024) у записи с вложением, аккаунт без Premium
	with pytest.raises(PostError, match="1024"):
		await service.edit_scheduled(_draft(ref, media_kind=MediaKind.VIDEO), "я" * 1025, later)
	# у вложения не наших видов текста нет — правится только время
	with pytest.raises(PostError, match="только время"):
		await service.edit_scheduled(_draft(ref, media_kind=MediaKind.OTHER), "текст", later)
	assert gateway.scheduled_edits == []
	# а пустая подпись у вложения и время у «прочего» — законны
	await service.edit_scheduled(_draft(ref, media_kind=MediaKind.VIDEO), "", later)
	await service.edit_scheduled(_draft(ref, media_kind=MediaKind.OTHER), "было", later)
	assert [edit[3] for edit in gateway.scheduled_edits] == ["", "было"]


async def test_send_now_and_delete_scheduled(db: Database) -> None:
	"""«Сейчас» и «Удалить» адресуются аккаунтом читателя и id записи."""
	service, gateway = _scheduled_service(db)
	community_id = await _add_community(db)
	ref = await _scheduled_ref(db, community_id, 77)
	await service.send_scheduled_now(ref)
	await service.delete_scheduled(ref)
	assert gateway.scheduled_sent == [(ref.account_id, "-1001", (77,))]
	assert gateway.scheduled_deleted == [(ref.account_id, "-1001", (77,))]


async def test_scheduled_actions_translate_gone_record(db: Database) -> None:
	"""Отказ Telegram «записи нет» приходит одним классом с пустым чтением."""
	service, gateway = _scheduled_service(db)
	community_id = await _add_community(db)
	ref = await _scheduled_ref(db, community_id)
	gateway.scheduled_gone = True
	with pytest.raises(ScheduledGoneError):
		await service.delete_scheduled(ref)
	with pytest.raises(ScheduledGoneError):
		await service.edit_scheduled(_draft(ref), "x", datetime.now(UTC) + timedelta(hours=1))


# --- пределы длины текста ---------------------------------------------------


def _media_file(tmp_path: Path) -> str:
	"""Маленький файл-вложение (проверки длины от размера не зависят)."""
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"video")
	return str(video)


def test_telegram_text_length_counts_utf16_units() -> None:
	"""Длина считается в кодовых единицах UTF-16, как смещения Telegram."""
	from pxcontrol.engine.telegram.types import telegram_text_length

	assert telegram_text_length("абв") == 3  # кириллица — по одной единице
	assert telegram_text_length("a" * 100) == 100
	assert telegram_text_length("🙂") == 2  # эмодзи вне основной таблицы — две
	assert telegram_text_length("") == 0


def test_text_length_limits_match_telegram() -> None:
	"""Пределы — значения Telegram; Premium поднимает оба."""
	from pxcontrol.engine.telegram.types import (
		CAPTION_LENGTH_LIMIT,
		CAPTION_LENGTH_LIMIT_PREMIUM,
		TEXT_LENGTH_LIMIT,
		TEXT_LENGTH_LIMIT_PREMIUM,
		text_length_limit,
	)

	assert (TEXT_LENGTH_LIMIT, TEXT_LENGTH_LIMIT_PREMIUM) == (4096, 8192)
	assert (CAPTION_LENGTH_LIMIT, CAPTION_LENGTH_LIMIT_PREMIUM) == (1024, 4096)
	assert text_length_limit(premium=False, with_media=False) == TEXT_LENGTH_LIMIT
	assert text_length_limit(premium=True, with_media=False) == TEXT_LENGTH_LIMIT_PREMIUM
	assert text_length_limit(premium=False, with_media=True) == CAPTION_LENGTH_LIMIT
	assert text_length_limit(premium=True, with_media=True) == CAPTION_LENGTH_LIMIT_PREMIUM


def test_validate_draft_allows_premium_ceiling_and_rejects_above() -> None:
	"""Общая проверка знает только потолок: 8192 проходит, 8193 — нет.

	Канала здесь нет, поэтому отвергать по базовому пределу нельзя —
	у владельца Premium такой пост законен.
	"""
	PostsService.validate_draft(PostDraft(1, text="я" * 8192))
	with pytest.raises(PostError, match="Текст поста длиннее"):
		PostsService.validate_draft(PostDraft(1, text="я" * 8193))


def test_validate_draft_caption_ceiling_is_lower(tmp_path: Path) -> None:
	"""У поста с вложением предел другой: потолок подписи — 4096."""
	media = _media_file(tmp_path)
	PostsService.validate_draft(
		PostDraft(1, text="я" * 4096, media_path=media, media_kind=MediaKind.VIDEO)
	)
	with pytest.raises(PostError, match="Подпись к файлу длиннее"):
		PostsService.validate_draft(
			PostDraft(1, text="я" * 4097, media_path=media, media_kind=MediaKind.VIDEO)
		)


async def test_text_limits_reflect_publisher_premium(db: Database) -> None:
	"""Пределы канала зависят от Premium его публикатора."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	limits = await service.text_limits(community_id)
	assert (limits.text, limits.caption) == (4096, 1024)
	bound = await _bound_account(db, community_id)
	assert bound is not None
	gateway.premium_ids = {bound}
	premium_limits = await service.text_limits(community_id)
	assert (premium_limits.text, premium_limits.caption) == (8192, 4096)


async def test_text_limits_for_draft_picks_caption_with_media(db: Database, tmp_path: Path) -> None:
	"""Предел выбирается по наличию вложения у черновика."""
	service = PostsService(db, _FakeGateway())
	community_id = await _add_community(db)
	limits = await service.text_limits(community_id)
	assert limits.for_draft(PostDraft(community_id, text="текст")) == limits.text
	with_media = PostDraft(
		community_id, media_path=_media_file(tmp_path), media_kind=MediaKind.VIDEO
	)
	assert limits.for_draft(with_media) == limits.caption


async def test_publish_rejects_caption_over_channel_limit(db: Database, tmp_path: Path) -> None:
	"""Подпись длиннее предела канала не уходит; у Premium та же проходит."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	draft = PostDraft(
		community_id,
		text="я" * 1025,
		media_path=_media_file(tmp_path),
		media_kind=MediaKind.VIDEO,
	)
	with pytest.raises(PostError, match="Подпись к файлу длиннее"):
		await service.publish(draft)
	assert gateway.published == []
	bound = await _bound_account(db, community_id)
	assert bound is not None
	gateway.premium_ids = {bound}
	await service.publish(draft)
	assert len(gateway.published) == 1


async def test_check_draft_limits_rejects_before_sending(db: Database) -> None:
	"""Точная проверка канала доступна отдельно — очередь зовёт её при постановке."""
	service = PostsService(db, _FakeGateway())
	community_id = await _add_community(db)
	await service.check_draft_limits(PostDraft(community_id, text="я" * 4096))
	with pytest.raises(PostError, match="Текст поста длиннее"):
		await service.check_draft_limits(PostDraft(community_id, text="я" * 4097))


async def test_bot_path_uses_base_limits(db: Database) -> None:
	"""Бот-путь всегда базовый: подписки у ботов не бывает."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, userbot_assigned=False)
	await service.publish(PostDraft(community_id, text="я" * 4096))
	assert len(gateway.sent) == 1
	with pytest.raises(PostError, match="Текст поста длиннее"):
		await service.publish(PostDraft(community_id, text="я" * 4097))
	assert len(gateway.sent) == 1


# --- приостановленные публикаторы (ADR-0029) --------------------------------------


async def _set_paused(db: Database, community_id: int, *, account: bool, bot: bool) -> None:
	"""Ставит на паузу публикаторов сообщества прямо в БД."""
	async with db.session_factory() as session:
		community = (
			await session.execute(
				select(Community)
				.options(selectinload(Community.bot), selectinload(Community.default_account))
				.where(Community.id == community_id)
			)
		).scalar_one()
		if community.default_account is not None:
			community.default_account.paused = account
		if community.bot is not None:
			community.bot.paused = bot
		await session.commit()


async def test_publish_waits_while_publishers_paused(db: Database) -> None:
	"""Оба публикатора на паузе — пост ждёт (поправимо), а не падает ошибкой.

	Класс тот же, что у выключенного сообщества: очередь придерживает
	пост, пока аккаунт или бота не возобновят.
	"""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	await _set_paused(db, community_id, account=True, bot=True)
	with pytest.raises(PostNotReadyError, match="приостановлен"):
		await service.publish(PostDraft(community_id, text="x"))
	assert gateway.published == [] and gateway.sent == []


async def test_paused_userbot_falls_back_to_bot(db: Database) -> None:
	"""Userbot на паузе — «сейчас» уходит ботом, отложка ждёт userbot."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	await _set_paused(db, community_id, account=True, bot=False)
	await service.publish(PostDraft(community_id, text="сейчас"))
	assert gateway.published == [], "приостановленным аккаунтом не публикуем"
	assert [text for _t, _c, text in gateway.sent] == ["сейчас"]
	when = datetime.now(UTC) + timedelta(hours=1)
	with pytest.raises(PostNotReadyError, match="userbot-админа"):
		await service.publish(PostDraft(community_id, text="позже", when=when))


async def test_list_scheduled_skips_paused_readers(db: Database) -> None:
	"""Приостановленный аккаунт отложки не читает; сообщество — не «непрочитанное»."""
	service = PostsService(db, _FakeGateway())
	community_id = await _add_community(db, with_bot=False)
	assert len((await service.list_scheduled()).items) == 1
	await _set_paused(db, community_id, account=True, bot=False)
	assert await service.list_scheduled() == ScheduledList(items=[], unread=())


# --- кнопки под постом (ADR-0031, этап 2) --------------------------------


def _markup(text: str = "Смотреть") -> PostMarkup:
	"""Клавиатура из одной кнопки-ссылки."""
	return PostMarkup(((PostButton(ButtonKind.LINK, text, "https://telegram.org"),),))


async def test_buttons_now_go_by_bot_even_with_publisher(db: Database) -> None:
	"""Пост «сейчас» с кнопками отправляет бот — простейшим маршрутом.

	Публикатор у сообщества есть, но дорисовка поверх его поста — это
	лишний вызов и окно без кнопок там, где хватает одного вызова
	(ADR-0031, п. 2a).
	"""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	markup = _markup()
	outcome = await service.publish(PostDraft(community_id, text="с кнопками", markup=markup))
	assert gateway.sent == [("123:AAA", "-1001", "с кнопками")]
	assert gateway.sent_markups == [markup]
	assert gateway.published == []  # публикатор не задействован
	assert gateway.markup_edits == []  # дорисовывать нечего
	assert outcome.message_id == 42 and outcome.markup_error is None


async def test_big_file_with_buttons_publishes_then_bot_draws(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Файл не по силам боту: публикатор отправил — бот дорисовал кнопки."""
	monkeypatch.setattr(PostsService, "_file_size", lambda self, path: BOT_MAX_FILE_BYTES + 1)
	video = tmp_path / "большое.mp4"
	video.write_bytes(b"video")
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, bot_can_edit=True)
	markup = _markup()
	outcome = await service.publish(
		PostDraft(
			community_id,
			text="подпись",
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
			markup=markup,
		)
	)
	assert len(gateway.published) == 1  # ушло публикателем
	assert gateway.media == []  # бот файл не заливал
	assert gateway.markup_edits == [("-1001", outcome.message_id, markup)]
	assert outcome.markup_error is None


async def test_markup_failure_keeps_post_and_reports(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Кнопки не поставились — пост всё равно опубликован, причина названа.

	Обратное было бы опаснее: ошибка отправки предложила бы повтор,
	а повтор опубликовал бы пост второй раз (ADR-0031, п. 12).
	"""
	monkeypatch.setattr(PostsService, "_file_size", lambda self, path: BOT_MAX_FILE_BYTES + 1)
	video = tmp_path / "большое.mp4"
	video.write_bytes(b"video")
	gateway = _FakeGateway()
	gateway.markup_edit_error = CommunityCheckError(
		"У бота нет права изменять сообщения в этом сообществе."
	)
	service = PostsService(db, gateway)
	community_id = await _add_community(db, bot_can_edit=True)
	outcome = await service.publish(
		PostDraft(
			community_id,
			text="подпись",
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
			markup=_markup(),
		)
	)
	assert len(gateway.published) == 1  # пост в канале
	assert outcome.markup_error is not None
	assert "Кнопки не поставлены" in outcome.markup_error
	assert "права изменять сообщения" in outcome.markup_error


async def test_scheduled_post_with_buttons_waits_for_publication(db: Database) -> None:
	"""Отложенный пост с кнопками уходит публикателем, кнопки — потом.

	Применить их сейчас невозможно: поста в канале ещё нет, его
	опубликует сервер Telegram. Исход так и говорит — «кнопки обещаны»,
	и очередь сохраняет обещание (ADR-0031, п. 9).
	"""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, bot_can_edit=True)
	when = datetime.now(UTC) + timedelta(hours=1)
	outcome = await service.publish(
		PostDraft(community_id, text="позже", when=when, markup=_markup())
	)
	assert len(gateway.published) == 1  # отложка создана публикателем
	assert gateway.markup_edits == []  # дорисовывать пока нечего
	assert outcome.markup_pending is True
	assert outcome.markup_error is None


async def test_scheduled_buttons_need_edit_right(db: Database) -> None:
	"""Без права изменять сообщения отложенный пост с кнопками отклоняется."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)  # право не выдано
	when = datetime.now(UTC) + timedelta(hours=1)
	with pytest.raises(PostError, match="нет права изменять"):
		await service.publish(PostDraft(community_id, text="позже", when=when, markup=_markup()))
	assert gateway.published == []


async def test_buttons_without_bot_refused(db: Database) -> None:
	"""Кнопки ставит только бот: сообщество без бота получает отказ с причиной."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db, with_bot=False)
	with pytest.raises(PostError, match="Кнопки ставит только бот"):
		await service.publish(PostDraft(community_id, text="текст", markup=_markup()))


async def test_bad_markup_rejected_before_send(db: Database) -> None:
	"""Клавиатура сверх пределов Telegram не уходит: он обрезал бы её молча."""
	gateway = _FakeGateway()
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	wide = PostMarkup(
		(tuple(PostButton(ButtonKind.LINK, f"к{i}", "https://telegram.org") for i in range(9)),)
	)
	with pytest.raises(MarkupError, match="предел Telegram"):
		await service.publish(PostDraft(community_id, text="текст", markup=wide))
	assert gateway.sent == [] and gateway.published == []


async def test_bot_route_uses_base_text_limits(db: Database) -> None:
	"""Пост с кнопками уходит ботом — значит и пределы текста бота.

	У публикатора Premium, но пост идёт не им: подписки у ботов
	не бывает, и предел подписи остаётся базовым.
	"""
	gateway = _FakeGateway()
	gateway.premium_ids = {1}
	service = PostsService(db, gateway)
	community_id = await _add_community(db)
	long_text = "я" * 5000  # больше базовых 4096, но меньше Premium-8192
	with pytest.raises(PostError, match="длиннее"):
		await service.publish(PostDraft(community_id, text=long_text, markup=_markup()))
	# без кнопок тот же текст уходит публикателем с Premium-пределом
	await service.publish(PostDraft(community_id, text=long_text))
	assert len(gateway.published) == 1
