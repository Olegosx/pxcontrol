"""Сервис постов: fire-and-forget, источник истины — сам канал (ADR-0010).

Публикация любого контента — единой сущностью ``PostDraft``. Транспорт
выбирается по возможностям канала: userbot в приоритете (ADR-0011),
бот — запасной путь. «Сейчас» — обычная отправка, отложенно — запись
прямо в канале (её хранит и публикует сервер Telegram). Локальной
таблицы постов нет; экран «Отложено» читает отложенные из Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, CommunityMember
from pxcontrol.engine.errors import EngineError, user_message
from pxcontrol.engine.services.captions import filename_complaint
from pxcontrol.engine.services.publish_route import (
	PublishCapabilities,
	PublishRoute,
	choose_route,
	markup_blocker,
	poll_blocker,
	post_markup_blocker,
	publish_capabilities,
	route_uses_userbot,
)
from pxcontrol.engine.services.settings import (
	COMMUNITY_ENABLED,
	VIDEO_PROCESSED_DIR,
	VIDEO_PUBLISHED_DIR,
	VIDEO_QUEUED_DIR,
	SettingKey,
	SettingsService,
)
from pxcontrol.engine.services.video import prune_empty_dirs, video_base_dir
from pxcontrol.engine.telegram.bot_api import BotMessageGoneError
from pxcontrol.engine.telegram.markup import PostMarkup, validate_markup
from pxcontrol.engine.telegram.mtproto import UserbotMessageGoneError, UserbotUnavailableError
from pxcontrol.engine.telegram.poll import PollDraft, validate_poll
from pxcontrol.engine.telegram.rich_text import (
	RichText,
	TextEntity,
	first_link,
	keep_entities,
	trimmed,
	validate_rich_text,
)
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	CAPTION_LENGTH_LIMIT,
	TEXT_LENGTH_LIMIT,
	BotRef,
	CommunityKind,
	ForumTopicInfo,
	LinkPreview,
	MediaKind,
	OutgoingFile,
	OutgoingPost,
	PublishedMessage,
	PublishedPage,
	ScheduledMessage,
	TelegramFloodError,
	limit_gb,
	limit_mb,
	telegram_text_length,
	text_length_limit,
	userbot_max_file_bytes,
)
from pxcontrol.engine.video.constants import preview_path
from pxcontrol.engine.video.ffmpeg import (
	FfmpegSource,
	ProgressCallback,
	ffmpeg_source,
	run_tool,
)
from pxcontrol.engine.video.frames import resolve_timestamp
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video

logger = logging.getLogger(__name__)

#: Минимальный запас до времени публикации (Telegram не берёт «почти сейчас»).
MIN_SCHEDULE_AHEAD = timedelta(seconds=60)


#: Миниатюра видео для Telegram: вписывается в квадрат, JPEG-качество ffmpeg.
_THUMB_BOX_PX = 320
_THUMB_JPEG_QUALITY = "4"

#: Предел ожидания ffmpeg на миниатюру: один кадр — секунды, предел
#: с большим запасом ловит зависший процесс (например, на битом файле).
_THUMBNAIL_TIMEOUT_S = 120.0

#: Длина превью текста отложенной записи на экране «Отложено».
_SCHEDULED_PREVIEW_CHARS = 80

#: Сколько постов ленты читать за один запрос (Telegram отдаёт до 100).
#: Полсотни — страница показа списков приложения (``list_view.PAGE_SIZE``):
#: одна страница экрана — один запрос к Telegram.
PUBLISHED_PAGE_SIZE = 50

#: Единый текст исхода «поста уже нет»: чтение и действие звучат одинаково.
_PUBLISHED_GONE_TEXT = "Этого поста в Telegram уже нет — он удалён. Обновите ленту сообщества."

#: Единый текст исхода «записи уже нет»: пустой ответ на чтение
#: и отказ Telegram на действие звучат одинаково.
_SCHEDULED_GONE_TEXT = (
	"Этой записи в Telegram уже нет — она опубликована или удалена. Обновите список."
)

_T = TypeVar("_T")


def community_capabilities(community: Community) -> PublishCapabilities:
	"""Возможности публикации строки сообщества (связи должны быть подгружены).

	Приостановленный публикатор (ADR-0029) не считается: приложение его
	не использует, и пост через него не пойдёт. Одна точка на подготовку
	публикации и проверку препятствий — прежде правило «назначен ли»
	было написано в обеих.
	"""
	bot = community.bot
	account = community.default_account
	bot_ready = bot is not None and not bot.paused
	return publish_capabilities(
		bot_ready,
		account is not None and not account.paused,
		markup_edit=bot_ready and community.bot_can_edit,
	)


def publisher_paused(community: Community) -> bool:
	"""Есть ли у сообщества **приостановленный** публикатор (ADR-0029).

	Не путать с одноимённым свойством DTO сообщества: там вопрос другой —
	«публиковать некому именно из-за паузы», и оно ложно, пока есть хоть
	один действующий публикатор. Здесь — просто «среди назначенных есть
	приостановленный», и зовут это только из ветки «публиковать некем»,
	чтобы отличить «нет публикатора» от «публикатор на паузе».
	"""
	bot = community.bot
	account = community.default_account
	return (bot is not None and bot.paused) or (account is not None and account.paused)


def _dedup_scheduled(items: list[ScheduledPostDto]) -> list[ScheduledPostDto]:
	"""Схлопывает дубли отложек (страховка ADR-0022).

	Ожидаемо каждый аккаунт группы видит только свои отложки и дублей
	нет; если видимость окажется шире (например, у админов), одна
	запись пришла бы от нескольких читателей. Тождество — по id записи
	в очереди сообщества: два разных поста с одинаковым текстом
	и временем — это два поста, а не дубль. Остаётся первый читатель.
	"""
	seen: set[tuple[int, int]] = set()
	result: list[ScheduledPostDto] = []
	for item in items:
		key = (item.community_id, item.message_id)
		if key not in seen:
			seen.add(key)
			result.append(item)
	return result


class PostError(EngineError):
	"""Ошибка создания/отправки поста (с понятным человеку текстом)."""


class PostNotReadyError(PostError):
	"""Сообщество сейчас не может принять пост, но это поправимо.

	Не дефект черновика и не отказ Telegram, а состояние сообщества,
	которое меняет сам человек: сообщество выключено переключателем
	либо у него не осталось публикатора. Очередь отправки по этому
	классу возвращает пост в ожидание, а не хоронит ошибкой
	(ADR-0016): иначе одно нажатие «выключить» превращало бы всю
	накопленную очередь канала в десятки карточек с ошибкой, которые
	пришлось бы перебирать руками.
	"""


class PublishedGoneError(PostError):
	"""Вышедшего поста в Telegram уже нет (удалён из другого клиента).

	Штатная гонка с сервером-истиной (ADR-0010), как
	:class:`ScheduledGoneError` у отложенной записи: не сбой приложения,
	а повод перечитать ленту. Класс — ради одного текста на всех путях
	(правка текста и правка кнопок идут разными транспортами, а причина
	у человека одна); до формы доходит сообщение, а не тип.
	"""


class ScheduledGoneError(PostError):
	"""Отложенной записи в Telegram уже нет — опубликована или удалена.

	Штатная гонка: список читается снимком, а истина живёт на сервере
	(ADR-0010), и между чтением и действием запись могла уйти из другого
	клиента. Отдельный класс — ради одного текста на всех путях: мост
	интерфейса доносит наружу только сообщение, тип до формы не доходит.
	Список при этом перечитывается **после любого исхода** действия
	(ADR-0028, п. 3), поэтому отличать эту причину от прочих форме
	не нужно — исчезнувшая запись исчезнет и из списка.
	"""


@dataclass(frozen=True)
class TextLimits:
	"""Пределы длины текста, действующие в конкретном сообществе.

	Зависят от того, кто публикует (ADR-0011/0019): у Premium-аккаунта
	пределы выше, у бота — всегда базовые. Пара отдаётся интерфейсу
	одним запросом, чтобы переключение типа контента не ходило в движок
	за каждым новым пределом.

	Attributes:
		text: предел для поста без вложения.
		caption: предел для подписи к файлу (он меньше).
	"""

	text: int
	caption: int

	def on_route(self, route: PublishRoute) -> TextLimits:
		"""Пределы, действующие на этом маршруте отправки.

		Пост, который отправляет бот, ограничен базовыми пределами
		Telegram: подписки у ботов не бывает, и Premium-пределы
		публикатора к нему не относятся. Правило одно на троих —
		постановку в очередь (отказ обязан всплыть под рукой у человека,
		а не через час), выбор транспорта при отправке и счётчик
		символов в форме, чтобы тот не обещал больше, чем пройдёт.
		"""
		if route is not PublishRoute.BOT:
			return self
		return TextLimits(text=TEXT_LENGTH_LIMIT, caption=CAPTION_LENGTH_LIMIT)

	def for_draft(self, draft: PostDraft) -> int:
		"""Предел, действующий для этого черновика."""
		return self.caption if draft.with_media else self.text


def check_schedule_ahead(when: datetime | None) -> None:
	"""Отклоняет время публикации, до которого меньше минуты.

	Отложенную запись хранит сервер Telegram (ADR-0010), и «через
	пару секунд» он не примет: пока пост дойдёт до него, названное время
	уже пройдёт. ``None`` — публикация «сейчас», проверять нечего.

	Raises:
		PostError: До названного времени меньше минуты.
	"""
	if when is not None and when.astimezone(UTC) - datetime.now(UTC) < MIN_SCHEDULE_AHEAD:
		raise PostError("Время публикации должно быть хотя бы на минуту в будущем.")


def check_text_length(text: str, limit: int, with_media: bool) -> None:
	"""Отклоняет текст длиннее предела Telegram.

	Длина считается так же, как её считает Telegram
	(:func:`telegram_text_length`) — иначе счётчик в интерфейсе
	и проверка расходились бы на эмодзи.

	Args:
		text: текст поста или подпись к файлу.
		limit: предел для этого поста (:func:`text_length_limit`).
		with_media: пост с вложением — от этого зависит формулировка.

	Raises:
		PostError: Текст длиннее предела.
	"""
	length = telegram_text_length(text)
	if length <= limit:
		return
	if with_media:
		raise PostError(
			f"Подпись к файлу длиннее предела Telegram: {length} символов "
			f"при {limit}. Сократите подпись или отправьте текст "
			"отдельным постом."
		)
	raise PostError(
		f"Текст поста длиннее предела Telegram: {length} символов при {limit}. "
		"Сократите текст или разбейте его на несколько постов."
	)


def text_preview(text: str, limit: int) -> str:
	"""Обрезает текст до ``limit`` символов, длинный — с «…» на конце.

	Общий помощник коротких заголовков/превью (очередь отправки,
	список отложенных): лимит зависит от места показа.
	"""
	if len(text) <= limit:
		return text
	return f"{text[: limit - 1]}…"


#: Сколько файлов Telegram принимает одним альбомом (ADR-0033, C4).
MAX_ALBUM_FILES = 10


@dataclass(frozen=True)
class MediaFile:
	"""Один файл поста: путь, вид и имя, под которым он уйдёт.

	Пост — это текст и **список** файлов: ноль (текстовый пост), один
	(обычное вложение) или несколько (альбом, ADR-0033 подача C4).
	Прежде файл был один и жил тремя полями черновика; список сделал
	альбом выразимым, а одиночный пост — его частным случаем.

	Attributes:
		path: путь к файлу на диске.
		kind: вид вложения.
		rename_to: новое имя файла (без пути) перед отправкой; вместе
			с файлом переименовывается его кадр-превью (сосед ``.png``).
	"""

	path: str
	kind: MediaKind
	rename_to: str | None = None


@dataclass(frozen=True)
class PostDraft:
	"""Черновик публикации — единая сущность для всех типов контента.

	Attributes:
		community_id: подключённый канал (id в нашей БД).
		text: текст поста или подпись к медиа.
		media: файлы поста: пусто — текст, один — обычное вложение,
			несколько — альбом (ADR-0033, подача C4).
		poll: опрос (None — обычный пост). Опрос исключает текст
			и файлы: у Telegram это самостоятельное вложение, его
			содержимое — вопрос и варианты (ADR-0033, подача C5).
		when: момент публикации (None — «сейчас»).
		topic_id: тема форума (id корневого сообщения; None — общая
			лента; допустима только у сообществ с включёнными темами).
		markup: клавиатура под постом (None — кнопок нет). Ставит её
			только бот (ADR-0031), поэтому её наличие влияет на выбор
			маршрута отправки; у альбома кнопок не бывает вовсе.
		markup_first: режим «кнопки важнее» у отложенного поста
			(ADR-0031, п. 4): вместо серверной отложки пост ждёт
			в нашей очереди и уходит ботом в назначенную минуту — кнопки
			видны с первой секунды, но приложение в это время должно
			работать. По умолчанию важнее публикация: отложку держит
			сервер Telegram, а кнопки бот дорисует после выхода.
	"""

	community_id: int
	text: str = ""
	media: tuple[MediaFile, ...] = ()
	poll: PollDraft | None = None
	when: datetime | None = None
	topic_id: int | None = None
	markup: PostMarkup | None = None
	markup_first: bool = False
	#: разметка текста (ADR-0033): пусто — обычный текст, и транспорт
	#: разбирает строку по-старому (так уходят старые элементы очереди)
	entities: tuple[TextEntity, ...] = ()
	#: превью ссылки у текстового поста (ADR-0033, подача C3):
	#: выключить, крупное, над текстом. У поста с вложением его не бывает
	preview: LinkPreview = field(default_factory=LinkPreview)

	@property
	def rich(self) -> RichText:
		"""Текст поста вместе с его разметкой — как его видит Telegram."""
		return RichText(self.text, self.entities)

	@property
	def with_media(self) -> bool:
		"""Есть ли у поста вложение (хоть одно)."""
		return bool(self.media)

	@property
	def is_album(self) -> bool:
		"""Пост — альбом: несколько файлов одной записью."""
		return len(self.media) > 1

	@property
	def media_kind(self) -> MediaKind:
		"""Вид вложения поста (``NONE`` — текстовый пост).

		У альбома вид первого файла задаёт его характер для показа:
		мешать документы с фото и видео Telegram всё равно не даёт
		(:func:`album_blocker`). У опроса файла нет, но вид есть —
		``POLL``: списки и карточки показывают его наравне с прочими.
		"""
		if self.poll is not None:
			return MediaKind.POLL
		return self.media[0].kind if self.media else MediaKind.NONE


def media_to_json(media: Sequence[MediaFile]) -> list[dict[str, str | None]] | None:
	"""Файлы поста в JSON для колонки БД (None — текстовый пост)."""
	if not media:
		return None
	return [
		{"path": file.path, "kind": str(file.kind), "rename_to": file.rename_to} for file in media
	]


def media_from_json(raw: object) -> tuple[MediaFile, ...]:
	"""Собирает файлы поста из значения колонки БД.

	Повреждённая запись не роняет восстановление очереди: такой элемент
	станет текстовым постом и будет отвергнут проверкой при отправке —
	с понятной причиной, а не падением при старте.
	"""
	if not raw:
		return ()
	try:
		items: list[Any] = list(raw)  # type: ignore[call-overload]
		return tuple(
			MediaFile(
				path=str(item["path"]),
				kind=MediaKind(item["kind"]),
				rename_to=item.get("rename_to") or None,
			)
			for item in items
		)
	except (TypeError, KeyError, ValueError):
		logger.warning("Файлы поста в БД не разобрались — элемент остался без вложений.")
		return ()


def album_blocker(media: Sequence[MediaFile], *, with_markup: bool) -> str | None:
	"""Что мешает отправить эти файлы одним постом (None — ничего).

	Правила Telegram, а не наши: альбом — до десяти файлов; фото и видео
	группируются вместе, документы — только с документами, музыка —
	только с музыкой; кнопок у альбома не бывает вовсе (проверено
	живьём 15.09.2026, ADR-0031). Чистая функция: то же правило
	показывает форма и проверяет движок.
	"""
	if len(media) <= 1:
		return None
	if len(media) > MAX_ALBUM_FILES:
		return (
			f"В альбоме не больше {MAX_ALBUM_FILES} файлов, а выбрано {len(media)} — "
			"уберите лишние или отправьте двумя постами."
		)
	if with_markup:
		return "У альбома не бывает кнопок — снимите их или отправьте файлы по одному."
	groups = {_album_group(file.kind) for file in media}
	if len(groups) > 1:
		return (
			"Telegram не смешивает в альбоме фото и видео с документами и музыкой — "
			"разложите такие файлы по разным постам."
		)
	return None


def _album_group(kind: MediaKind) -> str:
	"""Группа совместимости вложения внутри альбома."""
	if kind in (MediaKind.PHOTO, MediaKind.VIDEO):
		return "visual"
	return str(kind)


def _media_note(draft: PostDraft) -> str:
	"""Чем пост наполнен — строкой для журнала («текст», «видео», «альбом…»)."""
	if draft.poll is not None:
		return "викторина" if draft.poll.quiz else "опрос"
	if not draft.media:
		return "текст"
	if draft.is_album:
		return f"альбом из {len(draft.media)}"
	return str(draft.media[0].kind)


def _free_name(target: Path) -> Path:
	"""Свободное имя рядом с занятым: «имя (2).mp4», «имя (3).mp4»…"""
	if not target.exists():
		return target
	counter = 2
	while True:
		candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
		if not candidate.exists():
			return candidate
		counter += 1


def resolve_preview(preview: LinkPreview, rich: RichText) -> LinkPreview:
	"""Подставляет ссылку превью, если человек её не называл (ADR-0033).

	Крупное превью и превью над текстом Telegram строит **по адресу**,
	а не «по первой ссылке сам»: обычной отправке адрес не нужен,
	сырому запросу — обязателен. Поэтому недостающий адрес берём из
	текста тем же правилом, каким его выбрал бы сам Telegram.
	"""
	if preview.url or not preview.needs_media:
		return preview
	return replace(preview, url=first_link(rich))


def refresh_draft_media(draft: PostDraft) -> PostDraft:
	"""Возвращает черновик с учётом уже выполненного переименования файла.

	Нужен при повторной отправке после ошибки: переименование выполняется
	до загрузки, поэтому неудачная попытка могла оставить файл уже под
	новым именем. Если исходного пути больше нет, а файл с именем
	``rename_to`` в той же папке есть — черновик указывает на него,
	и повторное переименование снимается.
	"""
	updated: list[MediaFile] = []
	changed = False
	for file in draft.media:
		source = Path(file.path)
		if not file.rename_to or source.is_file():
			updated.append(file)
			continue
		target = source.with_name(file.rename_to)
		if target.is_file():
			updated.append(replace(file, path=str(target), rename_to=None))
			changed = True
		else:
			updated.append(file)
	return replace(draft, media=tuple(updated)) if changed else draft


class _PostPort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	def userbot_premium(self, account_id: int | None) -> bool: ...

	async def bot_send_text(
		self,
		bot: BotRef,
		chat_id: str,
		text: str,
		topic_id: int | None = None,
		markup: PostMarkup | None = None,
		entities: tuple[TextEntity, ...] = (),
		preview: LinkPreview | None = None,
	) -> int: ...

	async def bot_send_poll(
		self,
		bot: BotRef,
		chat_id: str,
		poll: PollDraft,
		topic_id: int | None = None,
		markup: PostMarkup | None = None,
	) -> int: ...

	async def userbot_get_forum_topics(
		self, account_id: int, chat_id: str
	) -> list[ForumTopicInfo]: ...

	async def userbot_publish(
		self,
		account_id: int,
		chat_id: str,
		post: OutgoingPost,
		on_progress: ProgressCallback | None,
	) -> int: ...

	async def bot_edit_markup(
		self, bot: BotRef, chat_id: str, message_id: int, markup: PostMarkup | None
	) -> None: ...

	async def bot_send_media(
		self,
		bot: BotRef,
		chat_id: str,
		kind: MediaKind,
		path: str,
		caption: str,
		topic_id: int | None = None,
		markup: PostMarkup | None = None,
		entities: tuple[TextEntity, ...] = (),
	) -> int: ...

	async def bot_send_album(
		self,
		bot: BotRef,
		chat_id: str,
		files: Sequence[tuple[MediaKind, str]],
		caption: str,
		topic_id: int | None = None,
		entities: tuple[TextEntity, ...] = (),
	) -> int: ...

	async def userbot_history_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> PublishedPage: ...

	async def userbot_get_post(
		self, account_id: int, chat_id: str, message_id: int
	) -> PublishedMessage | None: ...

	async def userbot_edit_post(
		self,
		account_id: int,
		chat_id: str,
		message_id: int,
		text: str,
		entities: tuple[TextEntity, ...] = (),
	) -> None: ...

	async def userbot_delete_messages(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> int: ...

	async def userbot_get_scheduled(
		self, account_id: int, chat_id: str
	) -> list[ScheduledMessage]: ...

	async def userbot_get_scheduled_message(
		self, account_id: int, chat_id: str, message_id: int
	) -> ScheduledMessage | None: ...

	async def userbot_edit_scheduled(
		self,
		account_id: int,
		chat_id: str,
		message_id: int,
		text: str,
		when: datetime,
		entities: tuple[TextEntity, ...] = (),
	) -> None: ...

	async def userbot_send_scheduled_now(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> None: ...

	async def userbot_delete_scheduled(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> None: ...


#: Крючок «обещанные кнопки едут за отложкой»: сообщество, номер записи,
#: новое время и новый текст (None — текст не менялся).
MarkupMoved = Callable[[int, int, datetime, str | None], Awaitable[None]]

#: Крючок «отложек больше нет»: сообщество и номера исчезнувших записей.
MarkupGone = Callable[[int, list[int]], Awaitable[None]]

#: Крючок «каким отложкам сообщества обещаны кнопки» (номера записей).
PromisedMarkups = Callable[[int], Awaitable[set[int]]]

#: Крючок «каким вышедшим постам кнопки обещаны, но не стоят»:
#: номер поста → текст последней неудачи (пустая строка — не пытались).
PostPromises = Callable[[int], Awaitable[dict[int, str]]]

#: Крючок «освежи права бота»: сообщество, чьи доступы надо перепроверить
#: живым зондом. Снимок прав в базе стареет — право «изменять сообщения»
#: человек выдаёт в Telegram, а приложение о том не знает (ADR-0031);
#: отказывать по устаревшей записи нечестно, поэтому перед отказом
#: сервис просит движок перепроверить и считает правило заново.
RefreshRights = Callable[[int], Awaitable[None]]

#: Крючок «с кнопками поста разобрались сами»: сообщество и номер поста.
#: Человек поставил или снял клавиатуру руками (или удалил пост) —
#: обещание дозору больше не нужно.
MarkupSettled = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True)
class PublishPlan:
	"""Подготовленная публикация: проверки и чтения БД уже выполнены.

	Разделение подготовки и передачи — ADR-0020: отменяемая задача
	отправки работает только с сетью и файлами, жёсткая отмена
	не застаёт запрос к БД (иначе соединение aiosqlite бросалось бы
	с запросом «в полёте»).

	Attributes:
		draft: черновик (после снятия просрочки, если она была).
		community: канал-получатель (строка БД с привязками).
		files: файлы поста после переименования (пусто — текст;
			несколько — альбом).
		route: каким путём уходит пост (ADR-0031): публикатор, бот
			или «публикатор отправил — бот дорисовал кнопки».
	"""

	draft: PostDraft
	community: Community
	files: tuple[MediaFile, ...]
	route: PublishRoute

	@property
	def single_path(self) -> str | None:
		"""Путь единственного файла (None — текст или альбом)."""
		return self.files[0].path if len(self.files) == 1 else None


@dataclass(frozen=True)
class PublishOutcome:
	"""Чем закончилась передача поста.

	Attributes:
		message_id: номер вышедшего поста (у отложенного — номер записи
			в очереди отложенных сервера).
		markup_error: кнопки обещали, но поставить не удалось — текст
			причины для человека. Пост при этом опубликован: это исход,
			а не сбой поста (ADR-0031, п. 12), и очередь сохраняет
			обещание, чтобы попытку можно было повторить.
		markup_pending: кнопки обещаны посту, которого ещё нет в канале
			(отложенная запись): применить их можно только после выхода,
			поэтому очередь сохраняет обещание, а дозор применит его сам
			(ADR-0031, п. 9).
	"""

	message_id: int | None = None
	markup_error: str | None = None
	markup_pending: bool = False


@dataclass(frozen=True)
class ScheduledRef:
	"""Адрес отложенной записи для действий над ней.

	Записи не хранятся в БД (ADR-0010), поэтому адрес — тройка:
	сообщество, аккаунт, чьим чтением запись попала в список (в группе
	отложку видит и правит только её создатель, ADR-0022), и id записи
	в очереди отложенных сообщества.
	"""

	community_id: int
	account_id: int
	message_id: int


@dataclass(frozen=True)
class ScheduledPostDto:
	"""Отложенная запись канала (прочитана из Telegram) для интерфейса.

	``community_id`` — id канала в нашей БД: по нему интерфейс фильтрует
	список по каналам (название для этого не годится — не уникально).

	Attributes:
		community_id: сообщество (id в нашей БД).
		community_title: название сообщества.
		account_id: аккаунт, чьим чтением запись попала в список — им же
			она правится, публикуется сейчас и удаляется.
		message_id: id записи в очереди отложенных сообщества.
		text_preview: начало текста для карточки.
		scheduled_at: момент публикации (UTC).
		media_kind: вид вложения (``NONE`` — текст).
		topic_id: тема форума (None — общая лента).
		markup_promised: кнопки этой записи обещаны и будут поставлены
			после её выхода (ADR-0031) — человек должен знать, что их
			отсутствие сейчас не потеря.
	"""

	community_id: int
	community_title: str
	account_id: int
	message_id: int
	text_preview: str
	scheduled_at: datetime
	media_kind: MediaKind = MediaKind.NONE
	topic_id: int | None = None
	markup_promised: bool = False

	@property
	def ref(self) -> ScheduledRef:
		"""Адрес записи для действий над ней."""
		return ScheduledRef(self.community_id, self.account_id, self.message_id)

	@property
	def when(self) -> datetime:
		"""Момент публикации — под тем же именем, что у элемента очереди.

		Общие правила показа списков (сортировка «ближайшие сначала»,
		фильтр по слоту времени) читают момент одним именем у обоих
		видов элементов; у отложенной записи он есть всегда.
		"""
		return self.scheduled_at

	@property
	def title(self) -> str:
		"""Заголовок карточки — начало текста (общее имя с элементом очереди)."""
		return self.text_preview


@dataclass(frozen=True)
class ScheduledDraft:
	"""Отложенная запись целиком — для формы правки.

	Читается с сервера при открытии формы, а не из снимка списка: запись
	могли изменить из другого клиента Telegram. Что можно менять —
	текст и время; вложение и тема показываются, но не правятся
	(у ``messages.editMessage`` нет адресата темы, а замена файла —
	загрузка с прогрессом, то есть задание очереди, а не правка).

	Attributes:
		ref: адрес записи.
		community_title: название сообщества.
		text: полный текст (у записи с вложением — подпись).
		entities: разметка текста (ADR-0033) — её надо передать серверу
			заново, иначе правка сотрёт оформление.
		when: момент публикации (UTC).
		media_kind: вид вложения (``NONE`` — текст, ``OTHER`` — вложение,
			которого приложение не создаёт: у него правится только время).
		topic_id: тема форума (None — общая лента).
		text_limit: предел длины текста для этой записи — по Premium
			аккаунта, который её правит, и по наличию вложения.
	"""

	ref: ScheduledRef
	community_title: str
	text: str
	when: datetime
	media_kind: MediaKind
	topic_id: int | None
	text_limit: int
	entities: tuple[TextEntity, ...] = ()

	@property
	def rich(self) -> RichText:
		"""Текст записи вместе с разметкой — им наполняется форма."""
		return RichText(self.text, self.entities)

	@property
	def text_editable(self) -> bool:
		"""Есть ли у записи текст, который можно править.

		У опроса и у вложений не наших видов (геопозиция) подписи нет —
		Telegram отвечал бы отказом на любую правку текста.
		"""
		return self.media_kind.has_caption


@dataclass(frozen=True)
class PublishedPostDto:
	"""Вышедший пост сообщества для экрана «Опубликовано» (ADR-0032).

	Своей таблицы постов нет (ADR-0010): всё, что здесь есть, прочитано
	из самого сообщества, кроме состояния обещанных кнопок — его знает
	только приложение.

	Attributes:
		community_id: сообщество (id в нашей БД).
		community_title: название сообщества.
		message_id: номер поста в ленте.
		text_preview: начало текста для карточки.
		published_at: когда пост вышел (UTC).
		media_kind: вид вложения (``NONE`` — текст).
		topic_id: тема форума (None — общая лента).
		buttons: сколько кнопок стоит под постом (0 — их нет).
		views: сколько раз пост просмотрели (None — Telegram не сказал).
		markup_error: кнопки обещаны, но не стоят — текст последней
			неудачи (пустая строка — попыток ещё не было); None —
			обещания нет.
		link: ссылка на пост (None — у сообщества нет @имени).
		group_id: номер группы альбома, каким его дал Telegram
			(None — обычный пост). По нему записи альбома узнают
			друг друга (ADR-0033, C4).
		album_ids: номера **всех** записей альбома, по возрастанию
			(пусто — пост из одной записи). Читателю альбом виден
			одной записью, и карточка у него тоже одна — но удалять
			приходится все.
	"""

	community_id: int
	community_title: str
	message_id: int
	text_preview: str
	published_at: datetime
	media_kind: MediaKind = MediaKind.NONE
	topic_id: int | None = None
	buttons: int = 0
	views: int | None = None
	markup_error: str | None = None
	link: str | None = None
	group_id: int | None = None
	album_ids: tuple[int, ...] = ()

	@property
	def message_ids(self) -> tuple[int, ...]:
		"""Записи поста: у альбома их несколько, у обычного поста одна."""
		return self.album_ids or (self.message_id,)

	@property
	def album_size(self) -> int:
		"""Сколько файлов в альбоме (1 — обычный пост)."""
		return len(self.message_ids)

	@property
	def is_album(self) -> bool:
		"""Альбом ли это (несколько записей одной публикацией)."""
		return self.album_size > 1

	@property
	def when(self) -> datetime:
		"""Момент выхода — под тем же именем, что у элемента очереди."""
		return self.published_at

	@property
	def title(self) -> str:
		"""Заголовок карточки — начало текста (общее имя со списками)."""
		return self.text_preview


def group_albums(items: Sequence[PublishedPostDto]) -> list[PublishedPostDto]:
	"""Схлопывает записи одного альбома в одну карточку (ADR-0033, C4).

	Telegram отдаёт альбом **несколькими записями подряд** с общим
	номером группы, а читателю показывает его одной публикацией.
	Лента приложения обязана показывать то же: десять карточек вместо
	одной — это не лента, а список файлов.

	Карточкой становится запись, где живёт **подпись** (первая
	по номеру с текстом; текста нет ни у одной — просто первая): её же
	правит форма, на неё ведёт ссылка. Просмотры берутся наибольшие
	из записей — Telegram считает их у каждой отдельно.

	Функция чистая и **повторимая**: схлопнутую карточку можно подать
	ей снова вместе с дочитанным хвостом альбома — записи сложатся,
	а не задвоятся. Это и нужно ленте: альбом попадает на границу
	страниц, и целым он становится только после дочитывания.

	Args:
		items: посты страницы в порядке ленты (новые сначала); записи
			альбома идут подряд — так их отдаёт Telegram.

	Returns:
		Тот же список, где каждый альбом занимает одно место.
	"""
	grouped: list[PublishedPostDto] = []
	run: list[PublishedPostDto] = []
	for item in items:
		if run and item.group_id is not None and item.group_id == run[0].group_id:
			run.append(item)
			continue
		if run:
			grouped.append(_merge_album(run))
		run = [item]
	if run:
		grouped.append(_merge_album(run))
	return grouped


def _merge_album(run: Sequence[PublishedPostDto]) -> PublishedPostDto:
	"""Собирает карточку альбома из его записей (см. :func:`group_albums`)."""
	if len(run) == 1 and not run[0].album_ids:
		return run[0]
	ids = sorted({message_id for item in run for message_id in item.message_ids})
	with_text = [item for item in run if item.text_preview]
	base = min(with_text or list(run), key=lambda item: item.message_id)
	views = [item.views for item in run if item.views is not None]
	return replace(
		base,
		album_ids=tuple(ids),
		views=max(views) if views else None,
	)


@dataclass(frozen=True)
class PublishedRef:
	"""Адрес вышедшего поста для действий над ним.

	Пары хватает: пост живёт в ленте сообщества, и номер в ней
	уникален — в отличие от отложенной записи, у которой свой номер
	в очереди отложенных и свой читатель (ADR-0022).
	"""

	community_id: int
	message_id: int


@dataclass(frozen=True)
class PublishedDraft:
	"""Вышедший пост, прочитанный с сервера для формы правки.

	Attributes:
		ref: адрес поста.
		community_title: название сообщества.
		text: текст поста (у поста с вложением — подпись).
		media_kind: вид вложения (``NONE`` — текст).
		topic_id: тема форума (None — общая лента).
		entities: разметка текста поста (ADR-0033) — её надо передать
			серверу заново, иначе правка сотрёт оформление.
		buttons: сколько кнопок стоит под постом сейчас.
		markup: клавиатура поста в нашем виде — ею наполняется форма.
			None при ненулевом ``buttons`` означает «клавиатура не наших
			видов»: её можно заменить целиком, но не показать по кнопкам.
		text_limit: предел длины текста по Premium публикатора.
		markup_blocker: почему кнопки этого поста изменить нельзя
			(None — можно; текст — человеку).
	"""

	ref: PublishedRef
	community_title: str
	text: str
	media_kind: MediaKind
	topic_id: int | None
	buttons: int
	markup: PostMarkup | None
	text_limit: int
	markup_blocker: str | None
	entities: tuple[TextEntity, ...] = ()

	@property
	def markup_ours(self) -> bool:
		"""Можно ли показать клавиатуру поста кнопками в форме."""
		return not self.buttons or self.markup is not None

	@property
	def rich(self) -> RichText:
		"""Текст поста вместе с разметкой — им наполняется форма."""
		return RichText(self.text, self.entities)

	@property
	def text_editable(self) -> bool:
		"""Есть ли у поста текст, который вообще можно править.

		У опроса текста нет: Telegram не даёт менять ни вопрос,
		ни варианты — такому посту доступны только кнопки и удаление.
		То же у чужих видов вложений (геопозиция, контакт).
		"""
		return self.media_kind.has_caption


@dataclass(frozen=True)
class PublishedList:
	"""Страница ленты сообщества и место, с которого читать дальше.

	Attributes:
		items: посты страницы, новые сначала.
		next_offset_id: номер, с которого продолжать чтение;
			None — лента кончилась.
	"""

	items: list[PublishedPostDto]
	next_offset_id: int | None


@dataclass(frozen=True, slots=True)
class UnreadCommunity:
	"""Сообщество, чьи отложенные прочитать не удалось.

	Идентификатор, а не одно название: названия сообществ не уникальны
	(тот же урок, что у фильтров очереди), а показ сверяет по нему,
	какое сообщество перечитано удачно и пометку с него пора снять.

	Attributes:
		id: сообщество.
		title: название — им зовут сообщество в тексте для человека.
	"""

	id: int
	title: str


@dataclass(frozen=True)
class ScheduledList:
	"""Отложенные записи и сообщества, которые прочитать не удалось.

	Истина об отложенных живёт на сервере Telegram (ADR-0010), и обход
	сообществ может оказаться неполным: аккаунт под флуд-лимитом,
	userbot отвалился. Молчать об этом нельзя — пустой список тогда
	читался бы как «отложенных нет», хотя их просто не спросили.

	Attributes:
		items: сами записи, отсортированные по времени публикации.
		unread: сообщества, чьи отложенные прочитать не удалось.
	"""

	items: list[ScheduledPostDto]
	unread: tuple[UnreadCommunity, ...] = ()


class PostsService:
	"""Публикация постов: userbot в приоритете, бот — запасной путь."""

	def __init__(
		self,
		db: Database,
		gateway: _PostPort,
		ffmpeg_path: FfmpegSource = "ffmpeg",
		settings: SettingsService | None = None,
		markup_moved: MarkupMoved | None = None,
		markup_gone: MarkupGone | None = None,
		promised_markups: PromisedMarkups | None = None,
		post_promises: PostPromises | None = None,
		markup_settled: MarkupSettled | None = None,
		refresh_rights: RefreshRights | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно:
		настройки каналов не кэшируются).

		``markup_moved`` и ``markup_gone`` — крючки судьбы обещанных
		кнопок (ADR-0031): правка отложенной записи уводит обещание
		за собой, удаление — снимает. Связка крючками, а не ссылкой
		на сервис: посты не должны знать про хранилище клавиатур,
		а движок и так собирает такие связи (как у сообществ)."""
		self._db = db
		self._gateway = gateway
		self._ffmpeg = ffmpeg_source(ffmpeg_path)  # провайдер пути (настройки)
		self._settings = settings if settings is not None else SettingsService(db)
		self._markup_moved = markup_moved
		self._markup_gone = markup_gone
		self._promised_markups = promised_markups
		self._post_promises = post_promises
		self._markup_settled = markup_settled
		self._refresh_rights = refresh_rights

	async def publish(
		self, draft: PostDraft, on_progress: ProgressCallback | None = None
	) -> PublishOutcome:
		"""Публикует черновик: userbot в приоритете, бот — запасной путь.

		Единый вход для всех типов контента. Транспорт выбирается по
		возможностям канала (:func:`publish_capabilities`): userbot —
		полный набор; только бот — текст и медиа до 50 МБ, «сейчас».
		``on_progress`` получает долю загрузки файла 0.0..1.0
		(бот-путь прогресс не отдаёт). Композиция трёх фаз (ADR-0020):
		очередь отправки вызывает их раздельно, чтобы отменяемой была
		только передача, а раскладка файлов шла уже вне отмены.

		В работающем приложении этот вход не используется: публикация
		идёт через очередь, то есть фазами. Он остаётся публичным
		контрактом (ADR-0011, п. 1) и прямым входом для будущих
		вызывающих — автопостинга из источников.

		Raises:
			PostError: Черновик/канал/файл не годятся, канал выключен
				или у сообщества нет способа публикации.
			UserbotUnavailableError: Userbot отвалился по дороге.
		"""
		plan = await self.prepare_publish(draft)
		outcome = await self.transmit(plan, on_progress)
		await self.settle_published(plan)
		return outcome

	async def publish_blocker(self, community_id: int) -> str | None:
		"""Что мешает сообществу принять пост прямо сейчас (None — ничего).

		Одна точка правды на два пути: подготовку публикации (она
		по этой причине отклоняет черновик) и дозор слотов очереди
		(он такие сообщества пропускает, а не выпускает их ждущие
		посты навстречу отказу).

		Returns:
			Текст причины для человека или None, если препятствий нет.
		"""
		return await self._publish_blocker(await self._get_community(community_id))

	async def _publish_blocker(self, community: Community) -> str | None:
		"""То же правило, но на уже прочитанной строке сообщества.

		Подготовка публикации читает сообщество для себя и не должна
		читать его второй раз ради этой проверки: два чтения — это ещё
		и два разных снимка одной строки в одной операции.
		"""
		if not await self._settings.get_for(COMMUNITY_ENABLED, community.id):
			return f"Сообщество «{community.title}» выключено — пост ждёт, пока его включат."
		caps = community_capabilities(community)
		if not caps.userbot and not caps.bot:
			if publisher_paused(community):
				return (
					f"Публикатор «{community.title}» приостановлен — пост ждёт, "
					"пока его возобновят в разделе «Пользователи и боты»."
				)
			return (
				f"У «{community.title}» нет публикатора — пост ждёт, "
				"пока аккаунт или бот вернётся в доступы."
			)
		return None

	async def prepare_publish(self, draft: PostDraft) -> PublishPlan:
		"""Подготовка публикации: проверки, чтения БД, переименование.

		Сетевых операций нет — фаза выполняется вне отменяемой задачи
		отправки (ADR-0020): жёсткая отмена загрузки не может застать
		запрос к БД. Побочный эффект один — переименование файла, и оно
		идёт после всех проверок, способных отклонить черновик:
		отклонённая публикация не должна оставлять файл переименованным.

		Raises:
			PostError: Черновик/канал/файл не годятся, канал выключен
				или у сообщества нет способа публикации.
		"""
		self.validate_draft(draft)
		community = await self._get_community(draft.community_id)
		# правило системы, не интерфейса: любой будущий вход в публикацию
		# (автопостинг из источников) не должен писать в выключенное
		# сообщество или в сообщество без публикатора
		blocker = await self._publish_blocker(community)
		if blocker is not None:
			raise PostNotReadyError(blocker)
		if draft.topic_id is not None and not community.forum:
			# тема живёт только в форуме: без проверки пост с темой улетел бы
			# в Telegram и вернулся сырой ошибкой TOPIC_ID_INVALID
			raise PostError(
				f"У «{community.title}» нет тем (форум выключен) — "
				"обновите выбор темы или перепроверьте доступы."
			)
		over_bot_limit = self._over_bot_limit(draft)
		community, blocker = await self._fresh_blocker(
			community, lambda item: self._markup_blocker(item, draft, over_bot_limit)
		)
		if blocker is not None:
			raise PostError(blocker)
		caps = community_capabilities(community)
		blocker = self._poll_blocker(community, draft)
		if blocker is not None:
			raise PostError(blocker)
		route = choose_route(
			caps,
			with_markup=bool(draft.markup),
			media_over_bot_limit=over_bot_limit,
			scheduled=draft.when is not None,
			markup_first=draft.markup_first,
		)
		self._check_transport(route, draft, community.default_tg_account_id)
		files = tuple(
			replace(file, path=self._apply_rename(file.path, file.rename_to), rename_to=None)
			if file.rename_to
			else file
			for file in draft.media
		)
		return PublishPlan(draft=draft, community=community, files=files, route=route)

	@staticmethod
	def _markup_blocker(community: Community, draft: PostDraft, over_bot_limit: bool) -> str | None:
		"""Что мешает кнопкам этого черновика в этом сообществе (None — ничего)."""
		if not draft.markup:
			return None
		return markup_blocker(
			community_capabilities(community),
			title=community.title,
			kind=CommunityKind(community.kind),
			scheduled=draft.when is not None,
			media_over_bot_limit=over_bot_limit,
			markup_first=draft.markup_first,
			poll=draft.poll is not None,
		)

	@staticmethod
	def _rights_may_be_stale(community: Community) -> bool:
		"""Может ли отказ по кнопкам объясняться устаревшим снимком прав.

		Право «изменять сообщения» живёт в Telegram, а у нас — снимком
		(колонка ``bot_can_edit``, ADR-0031): человек выдаёт право
		в настройках канала, приложение об этом не узнаёт до следующей
		перепроверки доступов. Значит отказ «у бота нет права» может
		быть не правдой, а устаревшей записью — и прежде чем отказать,
		её стоит освежить.

		Случай узкий: канал, бот назначен и не приостановлен, а права
		правки в снимке нет. Всё прочее (бота нет вовсе, группа) зондом
		не лечится — там отказ окончателен.
		"""
		bot = community.bot
		return (
			CommunityKind(community.kind) is CommunityKind.CHANNEL
			and bot is not None
			and not bot.paused
			and not community.bot_can_edit
		)

	async def _fresh_community(self, community: Community) -> Community:
		"""Перепроверяет доступы сообщества и возвращает свежую запись.

		Зонд — живой запрос ботом, поэтому зовётся он **только** там,
		где отказ иначе был бы ложным (:meth:`_rights_may_be_stale`),
		и ровно один раз на операцию: человек нажал кнопку и ждёт,
		а не получает отказ по памяти недельной давности.

		Сбой зонда не превращается в сбой операции: не удалось
		перепроверить — остаётся прежний снимок и прежний отказ,
		а причина уходит в журнал.
		"""
		if self._refresh_rights is None:
			return community
		try:
			await self._refresh_rights(community.id)
		except Exception as exc:  # noqa: BLE001 — зонд вспомогательный
			logger.warning(
				"Не удалось перепроверить права бота в «%s»: %s",
				community.title,
				user_message(exc),
			)
			return community
		return await self._get_community(community.id)

	@staticmethod
	def _poll_blocker(community: Community, draft: PostDraft) -> str | None:
		"""Что мешает опросу этого черновика в этом сообществе (None — ничего)."""
		if draft.poll is None:
			return None
		return poll_blocker(
			draft.poll.anonymous,
			title=community.title,
			kind=CommunityKind(community.kind),
		)

	async def _fresh_blocker(
		self, community: Community, rule: Callable[[Community], str | None]
	) -> tuple[Community, str | None]:
		"""Причина отказа по правилу — с живой перепроверкой прав, если надо.

		Снимок прав бота стареет, и отказ по памяти может оказаться
		неправдой: право могли выдать в Telegram уже после нашей
		последней перепроверки. Спрашиваем живьём узко — только когда
		отказ вообще мог возникнуть из-за снимка, и один раз на операцию;
		сбой зонда оставляет прежний отказ.

		Returns:
			Пара «сообщество (возможно, перечитанное) и причина или None».
		"""
		blocker = rule(community)
		if blocker is not None and self._rights_may_be_stale(community):
			community = await self._fresh_community(community)
			blocker = rule(community)
		return community, blocker

	def _base_limits(self, community: Community) -> TextLimits:
		"""Пределы длины публикатора сообщества (с учётом его Premium)."""
		premium = community.default_tg_account_id is not None and self._gateway.userbot_premium(
			community.default_tg_account_id
		)
		return TextLimits(
			text=text_length_limit(premium, with_media=False),
			caption=text_length_limit(premium, with_media=True),
		)

	def _draft_limits(
		self, community: Community, draft: PostDraft, over_bot_limit: bool
	) -> TextLimits:
		"""Пределы длины, действующие **на этом черновике**.

		Не «пределы сообщества»: пост с кнопками уходит ботом даже там,
		где у публикатора Premium, и предел у него базовый. Считать
		по сообществу значило бы принять в очередь пост, который упадёт
		при отправке, — а у отложенного это случится часы спустя,
		карточкой с ошибкой.
		"""
		route = choose_route(
			community_capabilities(community),
			with_markup=bool(draft.markup),
			media_over_bot_limit=over_bot_limit,
			scheduled=draft.when is not None,
			markup_first=draft.markup_first,
		)
		return self._base_limits(community).on_route(route)

	async def check_draft_rules(self, draft: PostDraft) -> None:
		"""Проверяет черновик по правилам его сообщества — до постановки.

		Одна точка на три правила, которые зависят от сообщества и от
		того, кто повезёт пост: предел длины текста (по маршруту),
		кнопки и опрос. Отказ обязан всплыть под рукой у человека,
		при нажатии «Отправить», а не через час, когда пост дождётся
		своей минуты.

		Raises:
			PostError: Сообщество не найдено, текст длиннее предела
				маршрута, кнопки или опрос этому сообществу недоступны.
		"""
		community = await self._get_community(draft.community_id)
		over_bot_limit = self._over_bot_limit(draft)
		if draft.markup or draft.poll is not None:
			community, blocker = await self._fresh_blocker(
				community, lambda item: self._markup_blocker(item, draft, over_bot_limit)
			)
			if blocker is None:
				# опрос проверяется здесь же: правило у него тоже
				# от сообщества, и отказ обязан всплыть при постановке
				blocker = self._poll_blocker(community, draft)
			if blocker is not None:
				raise PostError(blocker)
		limits = self._draft_limits(community, draft, over_bot_limit)
		check_text_length(draft.text, limits.for_draft(draft), draft.with_media)

	def _over_bot_limit(self, draft: PostDraft) -> bool:
		"""Файл черновика не по силам боту (лимит заливки — 50 МБ).

		Размер решает выбор маршрута: пост, который бот может отправить
		сам, уходит одним вызовом и с кнопками сразу (ADR-0031, п. 2a).
		"""
		return any(self._file_size(file.path) > BOT_MAX_FILE_BYTES for file in draft.media)

	async def transmit(
		self, plan: PublishPlan, on_progress: ProgressCallback | None = None
	) -> PublishOutcome:
		"""Передача подготовленного поста: только сеть, без запросов к БД.

		Отменяемая фаза публикации (ADR-0020): обрыв здесь безопасен —
		недосланное Telegram не публикует, соединений с БД в полёте нет.
		Раскладку файлов после удачной отправки делает
		:meth:`settle_published` — отдельно и вне отмены: пост к тому
		моменту уже опубликован, и обрывать перекладывание гигабайтов
		посреди работы нельзя.

		Raises:
			PostError: Транспорт отклонил отправку.
			UserbotUnavailableError: Userbot отвалился по дороге.
		"""
		draft = plan.draft
		markup_pending = False
		if plan.route is PublishRoute.BOT:
			# бот отправляет сам — кнопки уходят вместе с постом
			message_id = await self._publish_bot(plan.community, draft, plan.files)
			markup_error = None
		else:
			message_id = await self._publish_userbot(plan.community, draft, plan.files, on_progress)
			markup_error = None
			if plan.route is PublishRoute.USERBOT_MARKUP and draft.when is not None:
				# поста ещё нет в канале: его опубликует сервер Telegram,
				# и кнопки применит дозор после выхода (ADR-0031, п. 9)
				markup_pending = bool(draft.markup)
			else:
				markup_pending = False
				markup_error = await self._apply_markup(plan, message_id)
		logger.info(
			"Пост (%s) → «%s» (%s, %s).",
			_media_note(draft),
			plan.community.title,
			plan.route,
			f"отложено на {draft.when}" if draft.when else "опубликовано",
		)
		return PublishOutcome(
			message_id=message_id, markup_error=markup_error, markup_pending=markup_pending
		)

	async def _apply_markup(self, plan: PublishPlan, message_id: int | None) -> str | None:
		"""Дорисовывает кнопки к посту публикателя (маршрут Р2, ADR-0031).

		Зовётся сразу после публикации: номер поста известен из ответа,
		поэтому опознавать его не нужно — это дешёвая половина маршрута.

		Неудача кнопок **не отменяет пост**: он уже в канале, и обрывать
		отправку ошибкой значило бы предложить человеку повтор, который
		опубликовал бы пост второй раз. Поэтому причина возвращается
		текстом — очередь покажет её пометкой и сохранит обещание
		(ADR-0031, п. 12).

		Returns:
			Текст причины, по которой кнопок нет, или None при удаче.
		"""
		if plan.route is not PublishRoute.USERBOT_MARKUP or not plan.draft.markup:
			return None
		bot = plan.community.bot
		if bot is None or message_id is None:
			return "Кнопки не поставлены: пост ушёл, но бота для разметки не оказалось."
		try:
			await self._gateway.bot_edit_markup(
				BotRef(bot.id, bot.token),
				plan.community.tg_chat_id,
				message_id,
				plan.draft.markup,
			)
		except Exception as exc:  # noqa: BLE001 — исход кнопок, а не поста
			logger.warning(
				"Пост id=%s в «%s» опубликован, но кнопки не поставлены.",
				message_id,
				plan.community.title,
				exc_info=True,
			)
			return f"Кнопки не поставлены: {user_message(exc)}"
		return None

	async def settle_published(self, plan: PublishPlan) -> None:
		"""Раскладывает файлы после состоявшейся отправки.

		Третья фаза публикации, неотменяемая: пост уже в канале, и
		вопрос лишь в том, где лежит его видео. Раньше этот шаг жил
		внутри :meth:`transmit`, то есть внутри отменяемой задачи —
		отмена, пришедшая в момент переноса, объявляла опубликованный
		пост отменённым и возвращала файл в результаты навстречу
		копирующему потоку.

		Сбой переноса публикацию не отменяет: он вспомогательный,
		и след о нём остаётся в журнале.
		"""
		for file in plan.files:
			if file.kind is MediaKind.VIDEO:
				await self._move_to_published(file.path)

	def _check_transport(
		self, route: PublishRoute, draft: PostDraft, account_id: int | None
	) -> None:
		"""Проверки транспорта, способные отклонить черновик.

		Выполняются до побочных эффектов публикации (переименование файла):
		отклонённый черновик не должен менять ничего на диске.
		``account_id`` — привязанный userbot-аккаунт канала (ADR-0019):
		лимит файла зависит от Premium именно этого аккаунта. Пределы
		берутся по **маршруту**, а не по возможностям сообщества: пост
		с кнопками может уйти ботом даже там, где есть публикатор,
		и тогда действуют базовые пределы бота (ADR-0031).

		Наличие публикатора здесь не проверяется: это свойство
		сообщества, а не черновика, и живёт оно в одной точке —
		:meth:`publish_blocker` (её зовёт подготовка до этих проверок).

		Raises:
			PostNotReadyError: Отложенный пост, а userbot-публикатора нет.
			PostError: Текст длиннее предела или файл больше лимита
				выбранного транспорта.
		"""
		with_media = draft.with_media
		biggest = max((self._file_size(file.path) for file in draft.media), default=0)
		premium = route_uses_userbot(route) and self._gateway.userbot_premium(account_id)
		# длина — по тому же правилу, что при постановке: у бота подписки
		# не бывает, и `on_route` сводит его к базовым пределам
		limits = TextLimits(
			text=text_length_limit(premium, with_media=False),
			caption=text_length_limit(premium, with_media=True),
		).on_route(route)
		check_text_length(draft.text, limits.for_draft(draft), with_media)
		if route_uses_userbot(route):
			limit = userbot_max_file_bytes(premium)
			if biggest > limit:
				raise PostError(
					f"Файл больше {limit_gb(limit)} ГБ — лимит Telegram на файл "
					"для этого аккаунта. Уменьшите файл (например, битрейтом "
					"на странице «Видео»)."
				)
			return
		if draft.when is not None:
			# поправимо человеком (вернуть userbot в доступы), поэтому
			# очередь такой пост придержит, а не похоронит ошибкой
			raise PostNotReadyError(
				"Отложенные посты требуют userbot-админа в сообществе — "
				"через бота доступно только «сейчас»."
			)
		if biggest > BOT_MAX_FILE_BYTES:
			raise PostError(
				f"Файл больше {limit_mb(BOT_MAX_FILE_BYTES)} МБ — лимит "
				"отправки ботом. Добавьте userbot администратором канала "
				"или уменьшите файл."
			)

	@staticmethod
	def _file_size(media_path: str) -> int:
		"""Размер файла для проверки лимитов транспорта.

		Raises:
			PostError: Файл исчез или недоступен (сетевой диск, права) —
				доменный текст вместо сырой «внутренней ошибки» в очереди.
		"""
		try:
			return Path(media_path).stat().st_size
		except OSError as exc:
			raise PostError(f"Файл недоступен: {exc.strerror or exc} — {media_path}") from exc

	async def _publish_userbot(
		self,
		community: Community,
		draft: PostDraft,
		files: tuple[MediaFile, ...],
		on_progress: ProgressCallback | None,
	) -> int:
		"""Полный путь через userbot: из сессии аккаунта канала (ADR-0019).

		Лимит размера файла проверен раньше (:meth:`_check_transport`);
		сюда канал приходит только с привязкой (маршрутизация ``publish``).

		Returns:
			Номер вышедшего поста (у отложенного — номер записи
			в очереди отложенных сервера): по нему бот дорисовывает
			кнопки (ADR-0031).
		"""
		if community.default_tg_account_id is None:  # publish() сюда без умолчания не приводит
			raise PostError("У сообщества нет userbot-публикатора — проверьте доступы.")
		with tempfile.TemporaryDirectory() as tmp:
			# миниатюра — только у одиночного видео: в альбоме Telegram
			# берёт обложки из самих файлов, а класть десяток временных
			# кадров ради этого незачем
			thumb: str | None = None
			single = files[0] if len(files) == 1 else None
			if single is not None and single.kind is MediaKind.VIDEO:
				thumb = await asyncio.to_thread(self._video_thumbnail, single.path, tmp)
			post = OutgoingPost(
				text=draft.text,
				entities=draft.entities,
				poll=draft.poll,
				# ссылку для превью выбираем здесь: транспорту нужен
				# конкретный адрес, а человек обычно его не называет
				preview=resolve_preview(draft.preview, draft.rich),
				files=tuple(
					OutgoingFile(file.path, file.kind, thumb if file is single else None)
					for file in files
				),
				when=draft.when,
				topic_id=draft.topic_id,
			)
			return await self._gateway.userbot_publish(
				community.default_tg_account_id, community.tg_chat_id, post, on_progress
			)

	async def _publish_bot(
		self, community: Community, draft: PostDraft, files: tuple[MediaFile, ...]
	) -> int:
		"""Путь через бота: текст и медиа до 50 МБ, только «сейчас».

		Он же — путь поста с кнопками (ADR-0031): бот ставит их своему
		посту сам, одним вызовом и с первой секунды. Отложенность
		и лимит размера проверены раньше (:meth:`_check_transport`).

		Returns:
			Номер вышедшего поста.
		"""
		if community.bot is None:  # publish() сюда без бота не приводит
			raise PostError("У сообщества не назначен бот — переподключите его.")
		bot = BotRef(community.bot.id, community.bot.token)
		if draft.poll is not None:
			return await self._gateway.bot_send_poll(
				bot, community.tg_chat_id, draft.poll, draft.topic_id, draft.markup
			)
		if len(files) > 1:
			return await self._gateway.bot_send_album(
				bot,
				community.tg_chat_id,
				[(file.kind, file.path) for file in files],
				draft.text,
				draft.topic_id,
				entities=draft.entities,
			)
		if not files:
			return await self._gateway.bot_send_text(
				bot,
				community.tg_chat_id,
				draft.text,
				draft.topic_id,
				markup=draft.markup,
				entities=draft.entities,
				preview=draft.preview,
			)
		return await self._gateway.bot_send_media(
			bot,
			community.tg_chat_id,
			files[0].kind,
			files[0].path,
			draft.text,
			draft.topic_id,
			markup=draft.markup,
			entities=draft.entities,
		)

	async def list_topics(self, community_id: int) -> list[ForumTopicInfo]:
		"""Читает темы форума сообщества живьём (истина — Telegram).

		Только userbot: у Bot API метода перечисления тем нет — форум
		с одним ботом публикует в общую ленту (ограничение зафиксировано
		в ADR-0021).

		Raises:
			PostError: Сообщество не найдено, темы выключены или нет
				userbot-привязки.
			UserbotUnavailableError: Аккаунт недоступен или Telegram
				отказал.
		"""
		community = await self._get_community(community_id)
		if not community.forum:
			raise PostError(f"У «{community.title}» темы (форум) не включены.")
		if community.default_tg_account_id is None:
			raise PostError(
				f"Темы «{community.title}» может прочитать только userbot — "
				"привяжите аккаунт на странице сообщества → «Участники…»."
			)
		return await self._gateway.userbot_get_forum_topics(
			community.default_tg_account_id, community.tg_chat_id
		)

	async def _move_to_published(self, media_path: str) -> None:
		"""Переносит опубликованное видео из результатов в опубликованные.

		Правило — «зеркалим относительный путь»: файл из
		``<результаты>/<подпапка>/…`` переезжает в
		``<опубликованные>/<подпапка>/…`` (вместе с соседом-превью ``.png``).
		Файл вне папки результатов не трогается. Перенос вспомогательный:
		любой сбой — предупреждение в лог, публикацию не роняет (пост уже
		ушёл; у отложенных файл уже загружен на сервер Telegram).
		"""
		source = Path(media_path)
		# основной путь — из папки очереди (ADR-0016: постановка перенесла
		# файл туда); прямой вызов publish() минуя очередь — из результатов
		rel = self._relative_to_root(source, VIDEO_QUEUED_DIR) or self._relative_to_root(
			source, VIDEO_PROCESSED_DIR
		)
		if rel is None:
			return  # видео не из наших папок — оставляем на месте
		target = video_base_dir(self._settings, VIDEO_PUBLISHED_DIR) / rel
		try:
			# перенос между дисками — это копирование гигабайтов: в отдельном
			# потоке, чтобы не останавливать цикл событий движка
			await asyncio.to_thread(self._move_with_preview_and_prune, source, target)
			logger.info("Опубликованное видео перенесено: %s → %s", source, target)
		except OSError:
			logger.warning(
				"Не удалось перенести опубликованное видео %s — файл остался в папке результатов.",
				media_path,
				exc_info=True,
			)

	@staticmethod
	def _move_with_preview(source: Path, target: Path) -> None:
		"""Блокирующий перенос файла с соседом-превью ``.png`` (в потоке)."""
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.move(str(source), str(target))
		preview = preview_path(source)
		if preview.is_file():
			shutil.move(str(preview), str(preview_path(target)))

	def _move_with_preview_and_prune(self, source: Path, target: Path) -> None:
		"""Перенос с уборкой опустевших папок за источником (в потоке).

		Для постановки в очередь (``stash_for_queue``) не используется:
		опустевшая папка результатов сохраняется, пока элементы очереди
		могут вернуть в неё файлы (правила — ADR-0016).
		"""
		self._move_with_preview(source, target)
		self._prune_source_dirs(source.parent)

	def _prune_source_dirs(self, source_parent: Path) -> None:
		"""Блокирующая уборка после ухода файла (правила — ADR-0016).

		Уход из папки очереди (публикация, возврат при отмене): опустевшие
		папки дерева очереди убираются безусловно, затем — их опустевшие
		зеркала в результатах (файлы в них уже не вернутся). Уход
		из результатов (публикация мимо очереди): уровень убирается,
		только если его зеркало в очереди пусто или отсутствует — иначе
		отмена элемента ещё может вернуть файлы.
		"""
		queued_root = video_base_dir(self._settings, VIDEO_QUEUED_DIR)
		processed_root = video_base_dir(self._settings, VIDEO_PROCESSED_DIR)
		rel = self._relative_to_root(source_parent, VIDEO_QUEUED_DIR)
		if rel is not None:
			prune_empty_dirs(source_parent, queued_root)
			prune_empty_dirs(processed_root / rel, processed_root, mirror_root=queued_root)
			return
		if self._relative_to_root(source_parent, VIDEO_PROCESSED_DIR) is not None:
			prune_empty_dirs(source_parent, processed_root, mirror_root=queued_root)

	def pipeline_file(self, media_path: str) -> bool:
		"""Файл принадлежит конвейеру обработки видео (ADR-0016).

		True — файл лежит в папке результатов или в папке очереди, то есть
		движок водит его по маршруту ``processed → queued → published``.
		Маршрут определён только для видео, поэтому такой файл нельзя
		отправить фото или документом: правка очереди сверяется с этим
		признаком до переноса файла (``stash_for_queue`` — та же проверка
		на своей стороне, для постановки).
		"""
		path = Path(media_path)
		return (
			self._relative_to_root(path, VIDEO_PROCESSED_DIR) is not None
			or self._relative_to_root(path, VIDEO_QUEUED_DIR) is not None
		)

	def _relative_to_root(self, path: Path, key: SettingKey[str]) -> Path | None:
		"""Путь относительно корня папки видео; None — файл вне корня."""
		root = video_base_dir(self._settings, key)
		try:
			return path.resolve().relative_to(root.resolve())
		except ValueError:
			return None

	async def stash_for_queue(self, media_path: str, media_kind: MediaKind) -> str:
		"""Переносит файл результата в папку очереди отправки (ADR-0016).

		Зеркалит относительный путь (``queued/<подпапка>/<файл>``), вместе
		с файлом переезжает кадр-превью. Файл ждущего поста уходит
		из «Готовых видео» — его нельзя случайно удалить или поставить
		повторно. Файл вне папки результатов (произвольное вложение
		с диска) не трогается.

		Жизненный цикл processed → queued → published определён только
		для видео: не-видео из папки результатов отклоняется — такой файл
		принадлежит конвейеру обработки и как фото/документ не уходит.

		Returns:
			Путь файла в папке очереди (или исходный, если файл не наш).

		Raises:
			PostError: Файл из папки результатов — не видео; в папке
				очереди уже есть файл с таким относительным именем;
				перенос не удался (права, диск).
		"""
		source = Path(media_path)
		rel = self._relative_to_root(source, VIDEO_PROCESSED_DIR)
		if rel is None:
			return media_path
		if media_kind is not MediaKind.VIDEO:
			raise PostError(
				f"Из папки результатов можно отправлять только видео: «{rel}» — "
				"часть конвейера обработки. Чтобы отправить его как фото или "
				"документ, скопируйте файл в другую папку."
			)
		target = video_base_dir(self._settings, VIDEO_QUEUED_DIR) / rel
		if target.exists():
			raise PostError(
				f"В папке очереди уже есть файл «{rel}» — переименуйте "
				"результат или дождитесь отправки тёзки."
			)
		try:
			await asyncio.to_thread(self._move_with_preview, source, target)
		except OSError as exc:
			raise PostError(
				f"Не удалось перенести файл в папку очереди: {exc.strerror or exc}"
			) from exc
		return str(target)

	async def unstash_from_queue(self, media_path: str) -> str:
		"""Возвращает файл из папки очереди в результаты (ADR-0016).

		Вызывается при отмене или снятии элемента без отправки: файл снова
		«готовый». Коллизия имён (результат с тем же именем появился
		заново) решается суффиксом « (2)» — возврат не должен падать;
		сбой переноса не роняет операцию (файл остаётся в папке очереди,
		предупреждение в лог). Файл вне папки очереди не трогается.

		Returns:
			Путь файла в папке результатов (или исходный).
		"""
		source = Path(media_path)
		rel = self._relative_to_root(source, VIDEO_QUEUED_DIR)
		if rel is None:
			return media_path
		target = _free_name(video_base_dir(self._settings, VIDEO_PROCESSED_DIR) / rel)
		try:
			await asyncio.to_thread(self._move_with_preview_and_prune, source, target)
		except OSError:
			logger.warning(
				"Не удалось вернуть файл %s из папки очереди — он остался там.",
				media_path,
				exc_info=True,
			)
			return media_path
		return str(target)

	async def sweep_queue_dirs(self) -> None:
		"""Разовая уборка при старте: пустые папки дерева очереди и зеркала.

		Пустая папка в дереве очереди на старте не принадлежит ни одному
		элементу — файлы ждущих лежат физически (ADR-0016), значит это
		остатки отработанных пакетов. Вместе с каждой убранной убирается
		и её опустевшее зеркало в результатах. Папка результатов сама
		по себе не метётся: пустые подпапки там бывают законными
		(рабочие папки пресетов, свои папки пользователя). Сбой уборки
		не мешает запуску — предупреждение в лог.
		"""
		try:
			await asyncio.to_thread(self._sweep_queue_dirs)
		except OSError:
			logger.warning("Уборка папки очереди при старте не удалась.", exc_info=True)

	def _sweep_queue_dirs(self) -> None:
		"""Блокирующий обход дерева очереди снизу вверх (в потоке)."""
		queued_root = video_base_dir(self._settings, VIDEO_QUEUED_DIR)
		processed_root = video_base_dir(self._settings, VIDEO_PROCESSED_DIR)
		if not queued_root.is_dir():
			return
		removed = 0
		# сортировка в обратном порядке ставит вложенные папки раньше
		# родителей — родитель к своей очереди уже может опустеть
		for path in sorted(queued_root.rglob("*"), reverse=True):
			if not path.is_dir():
				continue
			try:
				path.rmdir()
			except OSError:
				continue  # непуста — живёт своей жизнью
			removed += 1
			rel = path.relative_to(queued_root)
			prune_empty_dirs(processed_root / rel, processed_root, mirror_root=queued_root)
		if removed:
			logger.info("Уборка при старте: удалено пустых папок очереди — %d.", removed)

	@staticmethod
	def _apply_rename(media_path: str, rename_to: str) -> str:
		"""Переименовывает файл (и его кадр-превью) перед отправкой.

		Returns:
			Путь к файлу под новым именем (папка не меняется).

		Raises:
			PostError: Имя содержит путь, целевое имя занято или
				переименовать не удалось (права, диск).
		"""
		PostsService.check_rename_name(rename_to)
		source = Path(media_path)
		try:
			target = source.with_name(rename_to)
		except ValueError as exc:  # страховка: Path строже наших проверок
			raise PostError(f"Имя «{rename_to}» не годится для файла.") from exc
		if target == source:
			return str(source)
		if target.exists():
			raise PostError(f"Файл «{rename_to}» уже существует — смените имя.")
		try:
			source.rename(target)
		except OSError as exc:
			raise PostError(
				f"Не удалось переименовать файл в «{rename_to}»: {exc.strerror or exc}"
			) from exc
		preview = preview_path(source)
		try:
			if preview.is_file():
				preview.rename(preview_path(target))
		except OSError as exc:
			# пара «файл + превью» переименовывается атомарно: без отката
			# превью осталось бы под старым стемом и потерялось бы при
			# переносе в «опубликованные» (поиск соседа идёт по новому)
			try:
				target.rename(source)
			except OSError:
				# откат не удался — говорить «переименование отменено»
				# было бы неправдой: файл остался под новым именем
				logger.warning(
					"Откат переименования %s → %s не удался.",
					target.name,
					source.name,
					exc_info=True,
				)
				raise PostError(
					f"Не удалось переименовать превью файла «{rename_to}», "
					f"а вернуть прежнее имя не вышло — файл называется "
					f"«{target.name}»: {exc.strerror or exc}"
				) from exc
			raise PostError(
				f"Не удалось переименовать превью файла «{rename_to}» — "
				f"переименование отменено: {exc.strerror or exc}"
			) from exc
		logger.info("Файл переименован: %s → %s", source.name, target.name)
		return str(target)

	def _video_thumbnail(self, video_path: str, tmp_dir: str) -> str | None:
		"""Готовит JPEG-миниатюру видео для Telegram (вписана в 320×320).

		Источник: кадр-превью конвейера (сосед видео с расширением .png),
		а без него — случайный кадр из середины видео. Миниатюра —
		вспомогательная: любой сбой не мешает публикации (None + лог).
		"""
		thumb = str(Path(tmp_dir) / "thumb.jpg")
		preview = preview_path(video_path)
		try:
			if preview.is_file():
				_make_thumbnail(str(preview), thumb, self._ffmpeg())
			else:
				info = probe_video(video_path, ffprobe_bin_for(self._ffmpeg()))
				timestamp = resolve_timestamp("random-middle", info)
				_make_thumbnail(video_path, thumb, self._ffmpeg(), timestamp)
		except (OSError, RuntimeError, ValueError):
			logger.warning(
				"Миниатюра для %s не получилась — публикуем без неё.",
				video_path,
				exc_info=True,
			)
			return None
		return thumb

	async def userbot_limit_gb(self, community_id: int) -> int:
		"""Лимит на файл канала в целых ГБ — для подсказок интерфейса.

		Зависит от Premium аккаунта, привязанного к каналу (ADR-0019);
		канал без привязки — меньший, безопасный лимит.

		Raises:
			PostError: Сообщество не найдено.
		"""
		return limit_gb(await self.userbot_limit_bytes(community_id))

	async def userbot_limit_bytes(self, community_id: int) -> int:
		"""Точный лимит на файл канала в байтах (2000/4000 МиБ по Premium).

		Для пометки «больше лимита канала» в пакете отправки (ADR-0015):
		округление до целых ГБ здесь дало бы ложные пометки у файлов
		между 2 ГБ и фактическими 2000 МиБ.

		Raises:
			PostError: Сообщество не найдено.
		"""
		community = await self._get_community(community_id)
		return userbot_max_file_bytes(
			self._gateway.userbot_premium(community.default_tg_account_id)
		)

	async def text_limits(self, community_id: int) -> TextLimits:
		"""Пределы длины текста, действующие в сообществе.

		Зависят от публикатора: userbot с Premium — вчетверо больший
		предел подписи, бот — всегда базовые (ADR-0011). Интерфейс
		берёт пару разом: переключение типа контента не должно ходить
		в движок за каждым новым пределом.

		Raises:
			PostError: Сообщество не найдено.
		"""
		return self._base_limits(await self._get_community(community_id))

	async def community_title(self, community_id: int) -> str:
		"""Название канала (для заголовков элементов очереди отправки).

		Raises:
			PostError: Сообщество не найдено.
		"""
		return (await self._get_community(community_id)).title

	@staticmethod
	def _validate_poll_draft(draft: PostDraft) -> None:
		"""Проверяет черновик-опрос (ADR-0033, C5).

		У опроса свои правила и свой набор полей: ни подписи, ни файлов,
		ни превью ссылки. Смешанный черновик — не «почти опрос», а знак
		того, что форма собрала его неверно: молча отбрасывать лишнее
		нельзя, человек увидел бы в канале не то, что собирал.

		Raises:
			PostError: Опрос смешан с текстом, файлом или превью.
			PollError: Сам опрос не проходит пределы Telegram.
			MarkupError: Клавиатура не проходит пределы (ADR-0031).
		"""
		assert draft.poll is not None  # ветка выбрана по его наличию
		if draft.text or draft.with_media:
			raise PostError("Опрос — самостоятельный пост: ни подписи, ни файла у него не бывает.")
		if draft.preview:
			raise PostError("У опроса превью ссылки не бывает — ссылке негде показаться.")
		validate_poll(draft.poll)
		if draft.markup is not None:
			validate_markup(draft.markup)
		when = draft.when
		check_schedule_ahead(when)

	@staticmethod
	def check_rename_name(rename_to: str) -> None:
		"""Отклоняет негодное имя для «переименовать при отправке».

		Проверка та же, что и у имени, собранного по шаблону подписи
		(:func:`filename_complaint`): один набор запрещённых символов,
		один предел файловых систем, один предел Telegram на стем.
		Раньше набранное человеком имя проходило мягче — только
		проверка на путь, — и Telegram молча урезал его на сервере,
		а переименование длинного имени падало сырой ошибкой
		файловой системы.

		Raises:
			PostError: Имя содержит путь, служебное («.», «..»),
				запрещённые символы или не проходит по длине.
		"""
		if "/" in rename_to or "\\" in rename_to:
			raise PostError("Новое имя файла не должно содержать путь.")
		if rename_to in (".", ".."):
			raise PostError("Укажите настоящее имя файла («.» и «..» — служебные).")
		complaint = filename_complaint(rename_to)
		if complaint is not None:
			raise PostError(complaint)

	@staticmethod
	def validate_draft(draft: PostDraft) -> None:
		"""Отклоняет пустой черновик, битый путь, негодное имя переименования,
		заведомо непроходимую длину текста и время «почти сейчас».

		Публичная: очередь отправки проверяет черновик при постановке,
		чтобы ошибка всплыла сразу, а не при отправке.

		Длина проверяется по **потолку** — пределам Premium-аккаунта:
		здесь нет ни канала, ни его публикатора, а отвергать по базовому
		пределу значило бы запрещать то, что Telegram разрешает владельцу
		Premium. Точный предел канала проверяют :meth:`check_draft_limits`
		(постановка в очередь) и :meth:`_check_transport` (отправка).

		Raises:
			PostError: Черновик не готов к отправке.
			MarkupError: Клавиатура не проходит пределы Telegram (ADR-0031).
			RichTextError: Разметка текста разъехалась (ADR-0033).
		"""
		if draft.poll is not None:
			PostsService._validate_poll_draft(draft)
			return
		if not draft.text and not draft.with_media:
			raise PostError("Пост пуст — добавьте текст или файл.")
		album_problem = album_blocker(draft.media, with_markup=draft.markup is not None)
		if album_problem is not None:
			raise PostError(album_problem)
		if draft.markup is not None:
			# пределы клавиатуры Telegram не объявляет и молча обрезает
			# лишнее (ADR-0031, п. 11) — проверяем до отправки
			validate_markup(draft.markup)
		# разъехавшуюся разметку сервер отвергает невнятной ошибкой
		# разбора, а то и молча теряет оформление (ADR-0033)
		validate_rich_text(draft.rich)
		if draft.with_media and draft.preview:
			raise PostError("У поста с вложением превью ссылки не бывает — место занято файлом.")
		if draft.preview.needs_media and not (draft.preview.url or first_link(draft.rich)):
			raise PostError(
				"Крупное превью и превью над текстом строятся по ссылке, "
				"а в тексте поста ссылки нет."
			)
		with_media = draft.with_media
		check_text_length(draft.text, text_length_limit(True, with_media), with_media)
		for file in draft.media:
			if file.rename_to:
				PostsService.check_rename_name(file.rename_to)
			if file.kind is MediaKind.NONE:
				raise PostError("У вложения не указан тип контента.")
			if not file.kind.creatable:
				raise PostError("Вложения такого вида приложение не отправляет.")
			if not Path(file.path).is_file():
				raise PostError(f"Файл не найден: {file.path}")
		when = draft.when
		check_schedule_ahead(when)

	async def list_scheduled(self, community_id: int | None = None) -> ScheduledList:
		"""Собирает отложенные записи активных userbot-сообществ из Telegram.

		``community_id`` — только это сообщество (вкладка «Отложено»
		на его странице); None — все активные (экран «Отложено»).

		Канал опрашивается аккаунтом-умолчанием (все админы видят одни
		и те же отложки), группа — **всеми участниками** (ADR-0022:
		отложку в группе видит только её создатель — подтверждено живым
		прогоном 2026-09-06; запрос на аккаунт, темп держит дорожка
		аккаунта — ADR-0024).
		У бот-сообщества отложенных быть не может (Bot API их не умеет,
		ADR-0010/0011). Выключенные (``enabled`` = False)
		не опрашиваются. Ошибка одного опроса не роняет весь список —
		сообщество пропускается, его название попадает в ``unread``,
		а подробности — в лог. Пустой список с непустым ``unread``
		означает «не спросили», а не «отложенных нет»: истина живёт
		на сервере Telegram (ADR-0010), и выдавать одно за другое
		нельзя.

		Returns:
			Записи всех опрошенных сообществ и названия тех, чьи
			отложенные прочитать не удалось.
		"""
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		async with self._db.session_factory() as session:
			communities = (
				(
					await session.execute(
						select(Community)
						.options(
							selectinload(Community.members).selectinload(
								CommunityMember.tg_account
							),
							selectinload(Community.default_account),
						)
						.order_by(Community.id)
					)
				)
				.scalars()
				.all()
			)
		items: list[ScheduledPostDto] = []
		unread: list[UnreadCommunity] = []
		for community in communities:
			if community_id is not None and community.id != community_id:
				continue
			if not enabled.get(community.id, COMMUNITY_ENABLED.default):
				continue
			for account_id in self._scheduled_readers(community):
				try:
					messages = await self._gateway.userbot_get_scheduled(
						account_id, community.tg_chat_id
					)
				except TelegramFloodError as exc:
					# флуд-лимит действует на аккаунт целиком, и помнит об этом
					# дорожка аккаунта (ADR-0024): остальные его сообщества
					# получат такой же мгновенный отказ, не тревожа Telegram, —
					# своего списка «провинившихся» проходу вести не нужно
					logger.info(
						"Отложенные «%s» пропущены: аккаунт id=%s под флуд-лимитом (%s).",
						community.title,
						account_id,
						exc,
					)
					unread.append(UnreadCommunity(community.id, community.title))
					continue
				except UserbotUnavailableError as exc:
					logger.warning(
						"Отложенные «%s» не прочитаны аккаунтом id=%s: %s",
						community.title,
						account_id,
						exc,
					)
					unread.append(UnreadCommunity(community.id, community.title))
					continue
				promised = (
					await self._promised_markups(community.id)
					if self._promised_markups is not None
					else set()
				)
				for message in messages:
					items.append(
						self._dto(
							community,
							account_id,
							message,
							markup_promised=message.id in promised,
						)
					)
		items = _dedup_scheduled(items)
		items.sort(key=lambda item: item.scheduled_at)
		# сообщество группы опрашивают несколько участников — в списке
		# непрочитанных оно должно встретиться один раз
		return ScheduledList(items=items, unread=tuple(dict.fromkeys(unread)))

	async def list_published(
		self, community_id: int, offset_id: int = 0, limit: int = PUBLISHED_PAGE_SIZE
	) -> PublishedList:
		"""Читает страницу ленты сообщества: что уже вышло (ADR-0032).

		Ленту читает **публикатор** сообщества — аккаунт по умолчанию
		(ADR-0022): он же её и наполняет, и от его имени доступны посты
		закрытых сообществ. Бот здесь не годится: своей истории через
		Bot API не прочитать.

		Страница — единица работы: один запрос через дорожку аккаунта
		с интерактивным приоритетом (человек ждёт на экране), следующая
		страница читается по требованию, а не «вся лента сразу».

		Args:
			community_id: сообщество.
			offset_id: читать посты старше этого номера (0 — с самых новых).
			limit: сколько постов прочитать за раз.

		Returns:
			Посты страницы (новые сначала) и номер, с которого читать
			дальше. Альбом занимает одно место (:func:`group_albums`);
			попавший на границу страниц складывается при дочитывании.

		Raises:
			PostError: Сообщества нет или у него нет публикатора.
			UserbotUnavailableError: Аккаунт не подключён, приостановлен,
				под флуд-лимитом или Telegram отказал.
		"""
		community = await self._get_community(community_id)
		account_id = self._published_reader(community)
		page = await self._gateway.userbot_history_page(
			account_id, community.tg_chat_id, offset_id, limit
		)
		promises = (
			await self._post_promises(community_id) if self._post_promises is not None else {}
		)
		items = [
			PublishedPostDto(
				community_id=community.id,
				community_title=community.title,
				message_id=message.id,
				text_preview=text_preview(message.text, _SCHEDULED_PREVIEW_CHARS),
				published_at=message.date,
				media_kind=message.media_kind,
				topic_id=message.topic_id,
				buttons=message.buttons,
				views=message.views,
				# обещание живёт, только пока кнопок под постом нет:
				# поставились — обещание снято, и пометка солгала бы
				markup_error=promises.get(message.id) if not message.buttons else None,
				link=(
					f"https://t.me/{community.username}/{message.id}"
					if community.username
					else None
				),
				group_id=message.group_id,
			)
			for message in page.messages
		]
		return PublishedList(items=group_albums(items), next_offset_id=page.next_offset_id)

	async def published_draft(self, ref: PublishedRef) -> PublishedDraft:
		"""Читает вышедший пост с сервера для формы правки (ADR-0032, A4).

		Пост берётся **с сервера целиком**, а не из снимка ленты: его
		могли поправить из другого клиента Telegram, а истина живёт
		в самом сообществе (ADR-0010).

		Returns:
			Пост и правила его правки: предел длины текста по Premium
			публикатора и причина, по которой кнопки изменить нельзя.

		Raises:
			PostError: Сообщества нет или у него нет публикатора.
			PublishedGoneError: Поста в ленте уже нет.
			UserbotUnavailableError: Аккаунт недоступен или Telegram отказал.
		"""
		community = await self._get_community(ref.community_id)
		account_id = self._published_reader(community)
		message = await self._gateway.userbot_get_post(
			account_id, community.tg_chat_id, ref.message_id
		)
		if message is None:
			raise PublishedGoneError(_PUBLISHED_GONE_TEXT)
		premium = self._gateway.userbot_premium(account_id)
		with_media = message.media_kind is not MediaKind.NONE
		return PublishedDraft(
			ref=ref,
			community_title=community.title,
			text=message.text,
			entities=message.entities,
			media_kind=message.media_kind,
			topic_id=message.topic_id,
			buttons=message.buttons,
			markup=message.markup,
			text_limit=text_length_limit(premium, with_media),
			markup_blocker=post_markup_blocker(
				community_capabilities(community),
				title=community.title,
				kind=CommunityKind(community.kind),
			),
		)

	async def edit_published(
		self,
		draft: PublishedDraft,
		text: str,
		entities: tuple[TextEntity, ...] | None = None,
	) -> None:
		"""Меняет текст и оформление вышедшего поста — публикатором.

		Проверки те же, что у постановки поста: у поста без вложения
		текст не может быть пустым, длина — в пределе публикатора.
		``entities`` — разметка нового текста (ADR-0033); она передаётся
		серверу заново, иначе он сотрёт оформление. None означает «форма
		правит только буквы»: разметка берётся у черновика и снимается,
		если текст изменили (:func:`keep_entities`).

		Кнопки правка не трогает: под постом они остаются как были
		(ADR-0031) — менять их может только бот
		(:meth:`set_published_markup`).

		Raises:
			PostError: Пустой текст у поста без вложения, текст длиннее
				предела или у поста нет правимого текста (опрос).
			PublishedGoneError: Поста в ленте уже нет.
			UserbotUnavailableError: Нет права править (в группе правит
				только автор), аккаунт недоступен или Telegram отказал.
		"""
		if not draft.text_editable:
			raise PostError(
				"У этого поста нет правимого текста: вопрос и варианты опроса "
				"Telegram менять не даёт."
			)
		with_media = draft.media_kind is not MediaKind.NONE
		# обрезка краёв — только вместе со смещениями: простой strip()
		# оставлял бы разметку на прежних местах, и оформление наезжало
		# бы на чужие буквы (для того `trimmed` и заведена, ADR-0033)
		rich = trimmed(RichText(text, entities or ()))
		cleaned = rich.text
		if not cleaned and not with_media:
			raise PostError("Текст поста пуст — у поста без вложения он обязателен.")
		check_text_length(cleaned, draft.text_limit, with_media)
		if entities is not None:
			validate_rich_text(rich)
		community = await self._get_community(draft.ref.community_id)
		account_id = self._published_reader(community)
		try:
			await self._gateway.userbot_edit_post(
				account_id,
				community.tg_chat_id,
				draft.ref.message_id,
				cleaned,
				rich.entities
				if entities is not None
				else keep_entities(draft.text, cleaned, draft.entities),
			)
		except UserbotMessageGoneError as exc:
			raise PublishedGoneError(_PUBLISHED_GONE_TEXT) from exc

	async def set_published_markup(self, ref: PublishedRef, markup: PostMarkup | None) -> None:
		"""Ставит, меняет или снимает кнопки вышедшего поста — ботом.

		Клавиатуру под постом трогает только бот (ADR-0031, проверено
		живьём): правка публикатора её не касается, а пустая разметка
		снимает кнопки совсем. Ограничения — в
		:func:`post_markup_blocker`: в группе такой правки не бывает,
		в канале боту нужно право «изменять сообщения».

		После удачи обещание дозору снимается (крючок ``markup_settled``):
		с кнопками этого поста разобрались руками.

		Raises:
			PostError: Сообщества нет, кнопки этого поста изменить нельзя
				или клавиатура не проходит пределы Telegram.
			PublishedGoneError: Поста в ленте уже нет.
			BotError: Telegram отказал боту (нет права, отклонил правку).
			InvalidBotTokenError: Токен бота в базе повреждён.
			TelegramFloodError: Флуд-лимит — подождать и повторить.
			ConnectionError: Нет связи с серверами Telegram.
		"""
		community = await self._get_community(ref.community_id)

		def blocked(item: Community) -> str | None:
			return post_markup_blocker(
				community_capabilities(item),
				title=item.title,
				kind=CommunityKind(item.kind),
			)

		community, blocker = await self._fresh_blocker(community, blocked)
		if blocker is not None:
			raise PostError(blocker)
		if markup is not None:
			validate_markup(markup)
		bot = community.bot
		if bot is None:  # проверка выше уже это исключила — страховка контракта
			raise PostError(f"У «{community.title}» нет бота — кнопки ставить некому.")
		try:
			await self._gateway.bot_edit_markup(
				BotRef(bot.id, bot.token), community.tg_chat_id, ref.message_id, markup
			)
		except BotMessageGoneError as exc:
			# та же гонка, что и у правки текста: пост удалили из другого
			# клиента между чтением ленты и действием. Исход должен
			# звучать одинаково на обеих ветках формы — иначе человек
			# на одну и ту же причину получает два разных совета
			raise PublishedGoneError(_PUBLISHED_GONE_TEXT) from exc
		logger.info(
			"Кнопки поста %s в «%s»: %s.",
			ref.message_id,
			community.title,
			f"{len(markup.buttons)} шт." if markup else "сняты",
		)
		await self._settle_markup(ref)

	async def delete_published(self, ref: PublishedRef, ids: Sequence[int] = ()) -> None:
		"""Удаляет вышедший пост — публикатором. Необратимо.

		Бот для этого не годится: ему Telegram разрешает удалять только
		сообщения моложе 48 часов, у публикатора-администратора такого
		ограничения нет.

		Args:
			ref: адрес поста (у альбома — запись с подписью).
			ids: все записи поста одним запросом — у альбома их
				несколько (ADR-0033, C4). Пусто — удалить одну
				запись ``ref``. Половина удалённого альбома хуже
				целого, поэтому записи уходят вместе, а не по одной.

		Raises:
			PostError: Сообщества нет, нет публикатора или Telegram
				не дал удалить пост.
			UserbotUnavailableError: Аккаунт недоступен или Telegram отказал.
		"""
		community = await self._get_community(ref.community_id)
		account_id = self._published_reader(community)
		targets = sorted(set(ids) | {ref.message_id})
		deleted = await self._gateway.userbot_delete_messages(
			account_id, community.tg_chat_id, targets
		)
		if not deleted:
			raise PostError(
				"Telegram не дал удалить этот пост. Обычно так отвечают "
				"на защищённые записи и на посты, которые аккаунт удалять "
				"не вправе."
			)
		logger.info(
			"Пост %s удалён из «%s» (записей: %s).",
			ref.message_id,
			community.title,
			len(targets),
		)
		await self._settle_markup(ref)

	async def _settle_markup(self, ref: PublishedRef) -> None:
		"""Снимает обещание кнопок этого поста (крючок движка)."""
		if self._markup_settled is not None:
			await self._markup_settled(ref.community_id, ref.message_id)

	@staticmethod
	def _published_reader(community: Community) -> int:
		"""Аккаунт, читающий ленту сообщества (публикатор по умолчанию).

		Raises:
			PostError: Публикатора нет или он приостановлен — читать
				ленту нечем, и притвориться пустой лентой нельзя.
		"""
		account = community.default_account
		if account is None:
			raise PostError(
				f"У «{community.title}» нет публикатора — ленту читать нечем. "
				"Назначьте публикатора на странице сообщества."
			)
		if account.paused:
			raise PostError(
				f"Публикатор «{community.title}» приостановлен — ленту читать нечем. "
				"Возобновите его в разделе «Пользователи и боты»."
			)
		return int(account.id)

	@staticmethod
	def _scheduled_readers(community: Community) -> list[int]:
		"""Аккаунты для чтения отложек сообщества (ADR-0022).

		Канал — только умолчание: отложки канала общие для админов,
		опрос каждого дал бы одни и те же записи. Группа — все участники
		(отложку видит создатель) плюс умолчание, если оно вне списка
		(страховка рассинхрона инварианта). Приостановленные аккаунты
		(ADR-0029) не спрашиваются: обращений к ним нет, а их сообщество
		в «непрочитанные» не попадает — его и не пытались читать.
		Связи ``members → tg_account`` и ``default_account`` должны быть
		подгружены.
		"""
		default = community.default_account
		default_id = default.id if default is not None and not default.paused else None
		if community.kind != "group":
			return [default_id] if default_id is not None else []
		readers = [
			member.tg_account_id for member in community.members if not member.tg_account.paused
		]
		if default_id is not None and default_id not in readers:
			readers.append(default_id)
		return readers

	async def scheduled_times(self, community_id: int) -> list[datetime]:
		"""Моменты существующих отложек канала (для раскладки пакета).

		Пакетная отправка (ADR-0015) пропускает занятые слоты — сюда
		отдаются времена уже созданных в Telegram отложенных записей;
		читает их аккаунт, привязанный к каналу (ADR-0019). Канал без
		userbot-админа отложек иметь не может — пустой список.

		Raises:
			PostError: Сообщество не найдено.
			UserbotUnavailableError: Отложки прочитать не удалось —
				вызывающая сторона решает, продолжать ли без них.
		"""
		community = await self._get_community(community_id)
		if community.default_tg_account_id is None:
			return []
		messages = await self._gateway.userbot_get_scheduled(
			community.default_tg_account_id, community.tg_chat_id
		)
		return [message.scheduled_at for message in messages]

	async def _get_community(self, community_id: int) -> Community:
		"""Возвращает сообщество с публикаторами или объясняет, что оно не найдено.

		Бот и аккаунт-умолчание подгружаются сразу: у обоих есть признак
		паузы (ADR-0029), по которому решается, кто публикует.
		"""
		async with self._db.session_factory() as session:
			community = (
				await session.execute(
					select(Community)
					.options(selectinload(Community.bot), selectinload(Community.default_account))
					.where(Community.id == community_id)
				)
			).scalar_one_or_none()
		if community is None:
			raise PostError("Сообщество не найдено — обновите список.")
		return community

	@staticmethod
	def _dto(
		community: Community,
		account_id: int,
		message: ScheduledMessage,
		*,
		markup_promised: bool = False,
	) -> ScheduledPostDto:
		"""Готовит запись для интерфейса: канал, читатель, короткий текст, время."""
		text = message.text or "(медиа без текста)"
		preview = text_preview(text, _SCHEDULED_PREVIEW_CHARS)
		return ScheduledPostDto(
			community_id=community.id,
			community_title=community.title,
			account_id=account_id,
			message_id=message.id,
			text_preview=preview,
			scheduled_at=message.scheduled_at,
			media_kind=message.media_kind,
			topic_id=message.topic_id,
			markup_promised=markup_promised,
		)

	# --- действия над отложенными (истина — сервер Telegram, ADR-0010) --------

	async def scheduled_draft(self, ref: ScheduledRef) -> ScheduledDraft:
		"""Читает отложенную запись целиком для формы правки.

		Свежее состояние с сервера, а не снимок списка: запись могли
		изменить из другого клиента. Предел текста считается по Premium
		аккаунта-читателя — им же запись и правится.

		Raises:
			ScheduledGoneError: Записи в очереди отложенных уже нет.
			PostError: Сообщество не найдено.
			UserbotUnavailableError: Аккаунт недоступен или Telegram отказал.
		"""
		community = await self._get_community(ref.community_id)
		message = await self._scheduled_call(
			self._gateway.userbot_get_scheduled_message(
				ref.account_id, community.tg_chat_id, ref.message_id
			)
		)
		if message is None:
			raise ScheduledGoneError(_SCHEDULED_GONE_TEXT)
		with_media = message.media_kind is not MediaKind.NONE
		premium = self._gateway.userbot_premium(ref.account_id)
		return ScheduledDraft(
			ref=ref,
			community_title=community.title,
			text=message.text,
			when=message.scheduled_at,
			media_kind=message.media_kind,
			topic_id=message.topic_id,
			text_limit=text_length_limit(premium, with_media),
			entities=message.entities,
		)

	async def edit_scheduled(
		self,
		draft: ScheduledDraft,
		text: str,
		when: datetime,
		entities: tuple[TextEntity, ...] | None = None,
	) -> None:
		"""Меняет текст, оформление и/или время отложенной записи на сервере.

		``draft`` — запись, как её показала форма (:meth:`scheduled_draft`):
		по нему известно, есть ли у записи вложение (от этого зависит
		предел текста и можно ли текст трогать вовсе). Проверки те же,
		что у постановки поста: непустой текст у записи без вложения,
		предел длины по Premium аккаунта, время не ближе минуты.
		``entities`` — разметка нового текста (ADR-0033): передаётся
		заново, иначе сервер сотрёт оформление. None — форма правит
		только буквы, и разметка берётся у черновика
		(:func:`keep_entities`).

		Сообщество не меняется — это была бы другая публикация.

		Raises:
			PostError: Текст пуст или длиннее предела; время слишком
				близко; у записи нет правимого текста.
			ScheduledGoneError: Записи в очереди отложенных уже нет.
			UserbotUnavailableError: Аккаунт недоступен или Telegram
				отказал (в том числе отклонил время).
		"""
		with_media = draft.media_kind is not MediaKind.NONE
		if not draft.text_editable and text != draft.text:
			raise PostError(
				"У этой записи нет текста, который можно править, — меняется только время."
			)
		if not text and not with_media:
			raise PostError("Пост пуст — добавьте текст.")
		premium = self._gateway.userbot_premium(draft.ref.account_id)
		check_text_length(text, text_length_limit(premium, with_media), with_media)
		if entities:
			validate_rich_text(RichText(text, entities))
		check_schedule_ahead(when)
		community = await self._get_community(draft.ref.community_id)
		await self._scheduled_call(
			self._gateway.userbot_edit_scheduled(
				draft.ref.account_id,
				community.tg_chat_id,
				draft.ref.message_id,
				text,
				when,
				# разметку передаём заново, иначе сервер сотрёт оформление
				entities
				if entities is not None
				else keep_entities(draft.text, text, draft.entities),
			)
		)
		# обещанные кнопки едут за постом: дозор опознаёт вышедший пост
		# по тексту и времени (ADR-0031, п. 9), и старые ему не годятся
		if self._markup_moved is not None:
			await self._markup_moved(draft.ref.community_id, draft.ref.message_id, when, text)

	async def send_scheduled_now(self, ref: ScheduledRef) -> None:
		"""Публикует отложенную запись немедленно (она уходит в ленту).

		Raises:
			ScheduledGoneError: Записи в очереди отложенных уже нет.
			PostError: Сообщество не найдено.
			UserbotUnavailableError: Аккаунт недоступен или Telegram отказал.
		"""
		community = await self._get_community(ref.community_id)
		await self._scheduled_call(
			self._gateway.userbot_send_scheduled_now(
				ref.account_id, community.tg_chat_id, [ref.message_id]
			)
		)
		# пост выходит сейчас — обещание должно стать «пора» (у вышедшего
		# поста будет новый номер, дозор опознает его по тексту)
		if self._markup_moved is not None:
			await self._markup_moved(ref.community_id, ref.message_id, datetime.now(UTC), None)

	async def delete_scheduled(self, ref: ScheduledRef) -> None:
		"""Удаляет отложенную запись, не публикуя.

		Raises:
			ScheduledGoneError: Записи в очереди отложенных уже нет.
			PostError: Сообщество не найдено.
			UserbotUnavailableError: Аккаунт недоступен или Telegram отказал.
		"""
		community = await self._get_community(ref.community_id)
		await self._scheduled_call(
			self._gateway.userbot_delete_scheduled(
				ref.account_id, community.tg_chat_id, [ref.message_id]
			)
		)
		# записи больше нет — обещанным кнопкам некуда ехать
		if self._markup_gone is not None:
			await self._markup_gone(ref.community_id, [ref.message_id])

	@staticmethod
	async def _scheduled_call(call: Awaitable[_T]) -> _T:
		"""Выполняет обращение к отложке, переводя «записи уже нет» в исход сервиса.

		Транспорт сообщает об исчезнувшей записи своим классом; наружу
		сервис отдаёт один класс на оба случая — пустой ответ и отказ
		Telegram, — чтобы интерфейсу не различать источники.
		"""
		try:
			return await call
		except UserbotMessageGoneError as exc:
			raise ScheduledGoneError(str(exc)) from exc


def _make_thumbnail(
	source_path: str,
	output_jpg: str,
	ffmpeg_bin: str = "ffmpeg",
	timestamp: float = 0.0,
) -> None:
	"""Делает JPEG-миниатюру для Telegram: кадр, вписанный в 320×320.

	Пропорции кадра сохраняются (Telegram растягивает миниатюру до
	пропорций видео — квадратный кроп исказил бы картинку). Источник —
	картинка или видео (кадр берётся в момент ``timestamp``). Живёт
	на слое публикации: чистый модуль ``engine/video`` про Telegram
	не знает.

	Raises:
		RuntimeError: Если ffmpeg не смог сделать миниатюру.
	"""
	box = _THUMB_BOX_PX
	cmd = [
		ffmpeg_bin,
		"-y",
		"-ss",
		f"{timestamp:.3f}",
		"-i",
		source_path,
		"-frames:v",
		"1",
		"-vf",
		f"scale={box}:{box}:force_original_aspect_ratio=decrease",
		"-q:v",
		_THUMB_JPEG_QUALITY,
		output_jpg,
	]
	# один кадр — секунды; предел ловит зависший ffmpeg (недоступный диск)
	run_tool(cmd, "миниатюра видео", timeout=_THUMBNAIL_TIMEOUT_S)
