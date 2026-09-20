"""Транспорт Bot API (через aiogram).

В первую очередь — проверки токена и прав бота и диагностика; публикация —
запасной путь (текст и файлы до 50 МБ, только «сейчас»), когда у канала
нет userbot-админа: основной транспорт публикации — MTProto (ADR-0011).
"""

from __future__ import annotations

import html
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from aiogram import Bot

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.markup import ButtonKind, PostMarkup
from pxcontrol.engine.telegram.poll import PollDraft
from pxcontrol.engine.telegram.refs import normalize_chat_ref, numeric_chat_id
from pxcontrol.engine.telegram.rich_text import RichText, TextEntity, TextStyle
from pxcontrol.engine.telegram.rights import (
	bot_rights,
)
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	CommunityInfo,
	CommunityKind,
	CommunityStatsInfo,
	LinkPreview,
	MediaKind,
	TelegramFloodError,
	limit_mb,
)

logger = logging.getLogger(__name__)


class InvalidBotTokenError(EngineError):
	"""Telegram отклонил токен бота (или токен неправильного формата)."""


class BotError(EngineError):
	"""Telegram отклонил операцию бота (с понятным человеку текстом).

	Базовая ошибка бот-пути: и проверка сообщества при подключении,
	и отправка, и правка клавиатуры. Прежнее имя обещало только
	проверку, а несли им два десятка разных отказов.

	Сообщество здесь — и канал, и группа (ADR-0021): бот штатно
	публикует и туда, и говорить человеку про канал там, где канала
	нет, нельзя.
	"""


class BotNotInCommunityError(BotError):
	"""Telegram не пускает бота в сообщество: не добавлен или выгнан (ADR-0035).

	Отдельный класс, потому что это **факт участия**, а не сбой: по нему
	перепроверка записывает боту «не состоит» — как пользователю по ответу
	«не участник». Без него выгнанный бот оставался бы в снимке
	администратором навсегда, и подбор продолжал бы его предлагать.

	Bot API сообщает об этом двумя ответами, и оба — этот класс
	(оба проверены живьём 20.09.2026, проба ``_misc/tg_kicked_bot_probe.py``):
	статусом 403 (``TelegramForbiddenError``: «bot was kicked from the
	channel chat», «bot is not a member of the supergroup chat»), который
	распознаётся по классу исключения, и статусом 400 с описанием
	«chat not found», когда бота в сообщество не добавляли вовсе: по
	числовому идентификатору сервер отвечает только участнику, а кода
	у такого отказа нет — только описание.
	"""


class BotMessageGoneError(BotError):
	"""Сообщения, которое бот собрался править или удалить, больше нет.

	Наследник базовой ошибки бот-пути: обработчики, ловящие ``BotError``,
	продолжают работать. Отдельный класс нужен дозору кнопок (ADR-0031):
	обещание на исчезнувший пост держать незачем — его надо отпускать
	сразу, а не ходить за ним сутки.
	"""


#: Что Telegram отвечает, когда правит или удаляет исчезнувшее сообщение.
#: Кода у этого отказа нет: Bot API отдаёт 400 и описание словами
#: (в отличие от MTProto, где есть типизованный MESSAGE_ID_INVALID).
#: Поэтому сверяем по описанию — список короткий, и он проверяется живьём.
_MESSAGE_GONE = ("message to edit not found", "message to delete not found", "message not found")


def _message_gone(description: str | None) -> bool:
	"""Отказ означает «сообщения больше нет», а не «нет прав»."""
	text = (description or "").lower()
	return any(mark in text for mark in _MESSAGE_GONE)


#: Что Telegram отвечает боту, которого в сообщество не добавляли:
#: по числовому идентификатору сервер отвечает только участнику,
#: и это 400 с описанием словами, а не 403 (тот приходит выгнанному).
#: Приём тот же, что у ``_MESSAGE_GONE``: кода нет, сверяем по описанию.
_CHAT_UNSEEN = ("chat not found",)


def _chat_unseen(description: str | None) -> bool:
	"""Отказ означает «бот не видит это сообщество», а не «неверный запрос»."""
	text = (description or "").lower()
	return any(mark in text for mark in _CHAT_UNSEEN)


@asynccontextmanager
async def _bot_errors(forbidden: str, bad_request: str) -> AsyncIterator[None]:
	"""Переводит исключения aiogram в понятные человеку ошибки.

	Единый маппер для всех операций бота (парный ``_mtproto_errors``
	в mtproto): неверный токен и сеть переводятся одинаково, а тексты
	для «нет прав» (Forbidden) и «отклонено» (BadRequest) зависят
	от операции и передаются параметрами.

	Raises:
		InvalidBotTokenError: Telegram отклонил токен (Unauthorized).
		TelegramFloodError: Флуд-лимит — подождать и повторить.
		BotNotInCommunityError: Бота в сообществе нет (403).
		BotError: Telegram отклонил операцию (права, запрос).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.exceptions import (
		TelegramAPIError,
		TelegramBadRequest,
		TelegramEntityTooLarge,
		TelegramForbiddenError,
		TelegramNetworkError,
		TelegramRetryAfter,
		TelegramUnauthorizedError,
	)

	try:
		yield
	except TelegramUnauthorizedError as exc:
		raise InvalidBotTokenError("Telegram отклонил токен (Unauthorized).") from exc
	except TelegramForbiddenError as exc:
		# 403 у бота значит одно: в этом сообществе его нет (не добавлен,
		# выгнан, заблокирован) — факт участия, а не просто отказ
		raise BotNotInCommunityError(f"{forbidden} (Telegram: {exc.message})") from exc
	except TelegramRetryAfter as exc:
		# флуд-лимит (429) — временное состояние: очередь отправки ждёт
		# и повторяет (парный перевод — FloodWaitError в mtproto)
		raise TelegramFloodError(
			f"Telegram просит подождать {exc.retry_after} с.", retry_after_s=exc.retry_after
		) from exc
	except TelegramBadRequest as exc:
		if _message_gone(exc.message):
			raise BotMessageGoneError(
				"Сообщения уже нет — его удалили из другого клиента Telegram."
			) from exc
		if _chat_unseen(exc.message):
			# «chat not found» по идентификатору — факт участия, как и 403:
			# бота в сообществе нет (не добавляли), а не запрос кривой
			raise BotNotInCommunityError(f"{bad_request} (Telegram: {exc.message})") from exc
		raise BotError(f"{bad_request} (Telegram: {exc.message})") from exc
	except TelegramEntityTooLarge as exc:
		# наследует сетевую ошибку — ветка обязана стоять раньше неё,
		# иначе «файл велик» превратился бы в ложное «нет связи»
		raise BotError(
			f"Файл больше лимита Bot API ({limit_mb(BOT_MAX_FILE_BYTES)} МБ) — уменьшите файл."
		) from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	except TelegramAPIError as exc:
		# запасная ветка: серверные сбои (5xx) и прочие отказы API
		raise BotError(f"Telegram отклонил операцию: {exc}") from exc


def post_html(text: str, entities: tuple[TextEntity, ...] = ()) -> str:
	"""HTML поста для Bot API: текст как набран, оформление — сущностями.

	Истина об оформлении — сущности (ADR-0033); сама строка чистая,
	и разбирать в ней нечего: разделители (``**``, ``__``) — обычные
	символы, которые уходят подписчикам как есть. Разбор разделителей
	бот-путь вёл до перехода на визуальное поле; поколение таких текстов
	закрыто миграцией ``f9e2b47c3a81``, и ветки под него больше нет —
	иначе пост про ``__init__`` уходил бы курсивом.
	"""
	return html_from_rich(RichText(text, entities))


#: Наши виды разметки → пары HTML-тегов Bot API. Спойлер и цитата
#: здесь есть, а у HTML-парсера Telethon спойлера нет — потому оба
#: транспорта и получают своё представление, а не общую строку
#: (ADR-0033). Ссылка и блок кода собираются отдельно: у них атрибуты.
_STYLE_TAGS: dict[TextStyle, tuple[str, str]] = {
	TextStyle.BOLD: ("<b>", "</b>"),
	TextStyle.ITALIC: ("<i>", "</i>"),
	TextStyle.UNDERLINE: ("<u>", "</u>"),
	TextStyle.STRIKE: ("<s>", "</s>"),
	TextStyle.CODE: ("<code>", "</code>"),
	TextStyle.SPOILER: ("<tg-spoiler>", "</tg-spoiler>"),
	TextStyle.QUOTE: ("<blockquote>", "</blockquote>"),
	TextStyle.EXPANDABLE_QUOTE: ("<blockquote expandable>", "</blockquote>"),
}


def _style_tags(entity: TextEntity) -> tuple[str, str]:
	"""Пара тегов для куска разметки (у ссылки и блока кода — с атрибутом)."""
	if entity.style is TextStyle.LINK:
		return f'<a href="{html.escape(entity.value, quote=True)}">', "</a>"
	if entity.style is TextStyle.PRE:
		if entity.value:
			language = html.escape(entity.value, quote=True)
			return f'<pre><code class="language-{language}">', "</code></pre>"
		return "<pre>", "</pre>"
	return _STYLE_TAGS[entity.style]


def html_from_rich(rich: RichText) -> str:
	"""Переводит размеченный текст в HTML для Bot API (ADR-0033).

	Две тонкости, каждая — из живой ошибки, а не из теории.

	**Смещения считаются в кодовых единицах UTF-16**, поэтому текст
	режется по этим же единицам, но кусками между границами разметки,
	а не по одной единице: эмодзи — суррогатная пара, и раздельно её
	половинки даже не декодируются.

	**Куски разметки могут перекрываться** (жирный поверх спойлера
	и ссылки), а HTML требует правильной вложенности. Поэтому текст
	разбивается на отрезки с постоянным набором стилей: на границе
	отрезка лишние теги закрываются в обратном порядке, недостающие
	открываются. Наивное «открыть на начале, закрыть на конце» давало
	``<tg-spoiler><b>…</tg-spoiler>…</b>`` — сервер такого не примет.

	Чистая функция в одну сторону: обратного разбора HTML у нас нет
	и не нужно — истина живёт в сущностях.
	"""
	units = rich.text.encode("utf-16-le")
	total = len(units) // 2
	if not rich.entities:
		return html.escape(rich.text, quote=False)
	bounds = {0, total}
	for entity in rich.entities:
		bounds.add(entity.offset)
		bounds.add(entity.offset + entity.length)
	edges = sorted(bound for bound in bounds if 0 <= bound <= total)
	parts: list[str] = []
	open_stack: list[TextEntity] = []
	for index, start in enumerate(edges[:-1]):
		stop = edges[index + 1]
		active = [
			entity
			for entity in rich.entities
			if entity.offset <= start and entity.offset + entity.length >= stop
		]
		# порядок вложения устойчивый: сначала то, что началось раньше
		# и тянется дальше — иначе теги «мигали» бы на каждом отрезке
		active.sort(key=lambda entity: (entity.offset, -entity.length))
		shared = 0
		while (
			shared < len(open_stack)
			and shared < len(active)
			and open_stack[shared] is active[shared]
		):
			shared += 1
		for entity in reversed(open_stack[shared:]):
			parts.append(_style_tags(entity)[1])
		for entity in active[shared:]:
			parts.append(_style_tags(entity)[0])
		open_stack = active
		chunk = units[start * 2 : stop * 2].decode("utf-16-le")
		parts.append(html.escape(chunk, quote=False))
	for entity in reversed(open_stack):
		parts.append(_style_tags(entity)[1])
	return "".join(parts)


def _preview_options(preview: LinkPreview | None) -> Any:
	"""Настройки превью ссылки для Bot API (None — как решит Telegram).

	У Bot API все три вида штатные — в отличие от MTProto, где крупное
	превью и превью над текстом задаются только вместе с самой ссылкой
	(ADR-0033, подача C3).
	"""
	if preview is None or not preview:
		return None
	from aiogram.types import LinkPreviewOptions

	return LinkPreviewOptions(
		is_disabled=preview.disabled or None,
		url=preview.url or None,
		prefer_large_media=preview.large or None,
		show_above_text=preview.above or None,
	)


@asynccontextmanager
async def _bot_client(token: str) -> AsyncIterator[Bot]:
	"""Клиент Bot API на время одной операции — и гарантированное закрытие.

	Bot API работает по токену на операцию, постоянного соединения
	у него нет (ADR-0007): клиент создаётся, делает своё и закрывается.
	Связка «создать → try → finally: закрыть» была написана в каждой
	из девяти операций; забытый ``finally`` в новой утекал бы
	соединением, и ни одного следа в журнале это бы не оставило.

	Raises:
		InvalidBotTokenError: Строка не похожа на токен бота.
	"""
	bot = _make_bot(token)
	try:
		yield bot
	finally:
		await bot.session.close()


def _make_bot(token: str) -> Bot:
	"""Создаёт клиента Bot API с доменной ошибкой на битом токене.

	aiogram проверяет формат токена прямо в конструкторе ``Bot``. Токен
	операционных вызовов приходит из БД: повреждённая запись (например,
	после смены ключа шифрования) без перевода уходила бы в интерфейс
	сырой «внутренней ошибкой» вместо понятного текста.

	Raises:
		InvalidBotTokenError: Строка не похожа на токен бота.
	"""
	from aiogram import Bot
	from aiogram.utils.token import TokenValidationError

	try:
		return Bot(token)
	except TokenValidationError as exc:
		raise InvalidBotTokenError("Строка не похожа на токен бота.") from exc


def _chat_id(chat_id: str) -> int:
	"""Числовой ID из строки БД (:func:`refs.numeric_chat_id` с нашим классом)."""
	return numeric_chat_id(chat_id, BotError)


def community_kind_from_chat_type(chat_type: str) -> CommunityKind:
	"""Вид сообщества по типу чата Bot API (ADR-0021).

	Raises:
		BotError: Тип не подключается: малая группа —
			с подсказкой преобразовать в супергруппу, личный чат — с
			объяснением, что нужен канал или группа.
	"""
	if chat_type == "channel":
		return CommunityKind.CHANNEL
	if chat_type == "supergroup":
		return CommunityKind.GROUP
	if chat_type == "group":
		raise BotError(
			"Малые группы не подключаются — преобразуйте группу "
			"в супергруппу (в настройках группы) и повторите."
		)
	raise BotError("Это личный чат — укажите канал или группу.")


def to_reply_markup(markup: PostMarkup | None) -> Any | None:
	"""Переводит клавиатуру поста в разметку Bot API (None — кнопок нет).

	Перевод живёт здесь, а не в :mod:`markup`: тот модуль о самой
	клавиатуре и ни к одному транспорту не привязан, а формат кнопок —
	забота транспорта. Виды сознательно ограничены двумя (ADR-0031,
	п. 13): ссылка и «скопировать текст».
	"""
	from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

	if markup is None or not markup:
		return None
	rows = []
	for row in markup.rows:
		if not row:
			continue
		rows.append(
			[
				InlineKeyboardButton(text=button.text, url=button.value)
				if button.kind is ButtonKind.LINK
				else InlineKeyboardButton(
					text=button.text, copy_text=CopyTextButton(text=button.value)
				)
				for button in row
			]
		)
	return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_markup(token: str, chat_id: str, message_id: int, markup: PostMarkup | None) -> None:
	"""Ставит, меняет или снимает клавиатуру у поста (ADR-0031).

	Так бот дорисовывает кнопки к посту, который опубликовал
	публикатор: в канале это возможно, если у бота есть право изменять
	сообщения. Пустая клавиатура снимает кнопки — снять их может только
	бот, правка публикателя разметку не трогает (проверено опытом).

	Внимание вызывающему: **любая** правка поста ботом должна нести
	клавиатуру заново — иначе Telegram её стирает.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил правку (нет права
			изменять сообщения, пост не найден, разметка не годится).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	async with (
		_bot_client(token) as bot,
		_bot_errors(
			"У бота нет права изменять сообщения в этом сообществе.",
			"Telegram отклонил правку клавиатуры.",
		),
	):
		await bot.edit_message_reply_markup(
			chat_id=_chat_id(chat_id),
			message_id=message_id,
			reply_markup=to_reply_markup(markup),
		)


async def send_media(
	token: str,
	chat_id: str,
	kind: MediaKind,
	path: str,
	caption: str,
	topic_id: int | None = None,
	markup: PostMarkup | None = None,
	entities: tuple[TextEntity, ...] = (),
) -> int:
	"""Отправляет медиа через Bot API (лимит — 50 МБ на файл).

	``topic_id`` — тема форума (``message_thread_id``); None — общая
	лента (для каналов и обычных групп всегда None).

	Returns:
		ID сообщения в Telegram.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил отправку (нет прав, размер и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.types import FSInputFile

	file = FSInputFile(path)
	text = post_html(caption, entities) if caption else None
	mode = "HTML"
	keyboard = to_reply_markup(markup)
	async with (
		_bot_client(token) as bot,
		_bot_errors("Бот не может писать в сообщество.", "Telegram отклонил отправку."),
	):
		if kind is MediaKind.PHOTO:
			message = await bot.send_photo(
				_chat_id(chat_id),
				file,
				caption=text,
				parse_mode=mode,
				reply_markup=keyboard,
				message_thread_id=topic_id,
			)
		elif kind is MediaKind.VIDEO:
			message = await bot.send_video(
				_chat_id(chat_id),
				file,
				caption=text,
				parse_mode=mode,
				reply_markup=keyboard,
				supports_streaming=True,
				message_thread_id=topic_id,
			)
		elif kind is MediaKind.AUDIO:
			message = await bot.send_audio(
				_chat_id(chat_id),
				file,
				caption=text,
				parse_mode=mode,
				reply_markup=keyboard,
				message_thread_id=topic_id,
			)
		else:
			message = await bot.send_document(
				_chat_id(chat_id),
				file,
				caption=text,
				parse_mode=mode,
				reply_markup=keyboard,
				message_thread_id=topic_id,
			)
		return int(message.message_id)


async def send_album(
	token: str,
	chat_id: str,
	files: Sequence[tuple[MediaKind, str]],
	caption: str,
	topic_id: int | None = None,
	entities: tuple[TextEntity, ...] = (),
) -> int:
	"""Отправляет альбом через Bot API (лимит — 50 МБ на файл).

	Подпись достаётся первому файлу — так альбом устроен у самого
	Telegram. Кнопок у альбома не бывает (ADR-0031), поэтому клавиатуры
	здесь нет вовсе.

	Returns:
		ID первого сообщения альбома.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил отправку (нет прав, размер).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.types import (
		FSInputFile,
		InputMediaAudio,
		InputMediaDocument,
		InputMediaPhoto,
		InputMediaVideo,
	)

	builders: dict[MediaKind, Any] = {
		MediaKind.PHOTO: InputMediaPhoto,
		MediaKind.VIDEO: InputMediaVideo,
		MediaKind.AUDIO: InputMediaAudio,
		MediaKind.DOCUMENT: InputMediaDocument,
	}
	text = post_html(caption, entities) if caption else None
	group = [
		builders[kind](
			media=FSInputFile(path),
			# подпись — только у первого: остальные идут без неё
			caption=text if index == 0 else None,
			parse_mode="HTML" if index == 0 else None,
		)
		for index, (kind, path) in enumerate(files)
	]
	async with (
		_bot_client(token) as bot,
		_bot_errors("Бот не может писать в сообщество.", "Telegram отклонил отправку."),
	):
		messages = await bot.send_media_group(_chat_id(chat_id), group, message_thread_id=topic_id)
		return int(messages[0].message_id)


async def send_poll(
	token: str,
	chat_id: str,
	poll: PollDraft,
	topic_id: int | None = None,
	markup: PostMarkup | None = None,
) -> int:
	"""Публикует опрос через Bot API («сейчас», ADR-0033, C5).

	Разметки в вопросе и вариантах нет осознанно: Bot API принимает там
	только кастомные эмодзи, которых приложение не умеет, — см. модуль
	``telegram/poll.py``. Викторина уходит номером правильного варианта
	(``correct_option_id``), пояснение — обычным текстом.

	Returns:
		ID сообщения в Telegram.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил отправку (нет прав и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.types import InputPollOption

	async with (
		_bot_client(token) as bot,
		_bot_errors("Бот не может писать в сообщество.", "Telegram отклонил отправку."),
	):
		message = await bot.send_poll(
			_chat_id(chat_id),
			question=poll.question,
			options=[InputPollOption(text=option) for option in poll.options],
			is_anonymous=poll.anonymous,
			type="quiz" if poll.quiz else "regular",
			allows_multiple_answers=poll.multiple,
			correct_option_id=poll.correct_option if poll.quiz else None,
			explanation=poll.explanation or None,
			message_thread_id=topic_id,
			reply_markup=to_reply_markup(markup),
		)
		return int(message.message_id)


async def send_text(
	token: str,
	chat_id: str,
	text: str,
	topic_id: int | None = None,
	markup: PostMarkup | None = None,
	entities: tuple[TextEntity, ...] = (),
	preview: LinkPreview | None = None,
) -> int:
	"""Публикует текстовый пост через Bot API («сейчас»).

	``topic_id`` — тема форума (``message_thread_id``); None — общая лента.

	Returns:
		ID сообщения в Telegram.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил отправку (нет прав и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	async with (
		_bot_client(token) as bot,
		_bot_errors("Бот не может писать в сообщество.", "Telegram отклонил отправку."),
	):
		message = await bot.send_message(
			_chat_id(chat_id),
			post_html(text, entities),
			parse_mode="HTML",
			message_thread_id=topic_id,
			reply_markup=to_reply_markup(markup),
			link_preview_options=_preview_options(preview),
		)
		return int(message.message_id)


def describe_update(update: Any) -> str | None:
	"""Человекочитаемое описание события бота (для лога и диагностики).

	Понимает изменение статуса бота в чате (``my_chat_member``) и посты
	в каналах (``channel_post``); прочие события пропускает.
	"""
	membership = getattr(update, "my_chat_member", None)
	if membership is not None:
		chat = membership.chat
		new = membership.new_chat_member
		rights = getattr(new, "can_post_messages", None)
		rights_text = "—" if rights is None else ("есть" if rights else "нет")
		return (
			f"{membership.date:%d.%m %H:%M} — «{chat.title}» "
			f"({chat.type}, id={chat.id}): статус бота «{new.status}», "
			f"право публиковать: {rights_text}"
		)
	post = getattr(update, "channel_post", None)
	if post is not None:
		chat = post.chat
		return f"{post.date:%d.%m %H:%M} — пост в канале «{chat.title}» (id={chat.id})"
	return None


async def get_bot_events(token: str) -> list[str]:
	"""Читает необработанные события бота (getUpdates), не удаляя их.

	Telegram хранит события 24 часа. По ним видно, в какие каналы/группы
	бота добавляли и с какими правами — диагностика «бот не тот / не там».
	Возвращаются первые ~100 необработанных событий (лимит одного запроса
	getUpdates): пагинация подтверждала бы (удаляла) прочитанное, а
	диагностика события не трогает — у активного бота свежие добавления
	могут не попасть в выдачу.

	Raises:
		InvalidBotTokenError: Telegram отклонил токен.
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил запрос (вебхук, параллельный опрос).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.exceptions import TelegramConflictError

	async with (
		_bot_client(token) as bot,
		_bot_errors("Telegram отклонил запрос событий.", "Telegram отклонил запрос событий."),
	):
		try:
			# timeout=1 — короткий long-poll: диагностике нужен снимок,
			# не ожидание
			updates = await bot.get_updates(timeout=1)
		except TelegramConflictError as exc:
			# точный совет ценнее запасной ветки единого маппера
			raise BotError(
				"События недоступны: у бота включён вебхук или его опрашивает другое приложение."
			) from exc
	return [line for update in updates if (line := describe_update(update))]


async def check_community(token: str, chat_ref: str) -> CommunityInfo:
	"""Читает сообщество и права бота в нём (ADR-0021, ADR-0035).

	Возвращает **факт**, а не приговор (ADR-0035, п. 7): участие и полный
	снимок прав. Нехватка прав отказом не является — «может ли бот
	публиковать» решает правило над снимком (:func:`abilities.can`).
	Отказ остаётся там, где дело не в правах: Telegram не показал боту
	сообщество, это малая группа или личный чат.

	Raises:
		ChatRefError: Введённую ссылку/имя не удалось разобрать.
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotNotInCommunityError: Бота в сообществе нет — выгнан (403)
			или не добавляли и оно ему не видно (400 «chat not found»).
		BotError: Прочие отказы Telegram и вид, который не подключается
			(малая группа, личный чат).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	ref = normalize_chat_ref(chat_ref)
	logger.info("Проверка сообщества: ввод %r распознан как %r.", chat_ref, ref)
	# тексты нейтральны к сценарию: та же проверка служит и подключению
	# по вводу человека, и вводу бота в известное сообщество, где ID
	# взят из базы и «проверьте ID» человеку не помог бы
	async with (
		_bot_client(token) as bot,
		_bot_errors(
			"Бот не добавлен в сообщество — добавьте его (в канал — администратором).",
			"Бот не видит это сообщество: по числовому ID Telegram отвечает только "
			"участнику. Добавьте бота в сообщество (в канал — администратором) "
			"или укажите публичное @имя.",
		),
	):
		chat = await bot.get_chat(ref)
		kind = community_kind_from_chat_type(str(chat.type))
		me = await bot.get_me()
		member = await bot.get_chat_member(chat.id, me.id)
		# снимок прав бота — из того же ответа (ADR-0035); в нём и право
		# править чужие сообщения, от которого зависят кнопки поверх
		# поста публикателя (ADR-0031): подключению оно не требуется
		rights = bot_rights(member, chat.permissions)
		return CommunityInfo(
			str(chat.id),
			chat.title or str(ref),
			chat.username,
			kind=kind,
			rights=rights,
			forum=bool(chat.is_forum),
		)


async def get_community_stats(token: str, chat_id: str) -> CommunityStatsInfo:
	"""Статистика сообщества через бота: число участников и связанный чат.

	Два запроса Bot API — ``getChat`` (связанное сообщество) и
	``getChatMemberCount``. Больше Bot API о сообществе не расскажет:
	ни онлайна, ни отложенных, ни встроенной статистики у него нет —
	это путь дешёвого частого опроса, полная картина — за userbot.

	Raises:
		InvalidBotTokenError: Токен отклонён Telegram.
		TelegramFloodError: Флуд-лимит — подождать и повторить.
		BotError: Бот не видит сообщество или запрос отклонён.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	async with (
		_bot_client(token) as bot,
		_bot_errors(
			"Бот не видит сообщество — его могли исключить.",
			"Telegram отклонил запрос сведений о сообществе.",
		),
	):
		numeric = _chat_id(chat_id)
		chat = await bot.get_chat(numeric)
		count = await bot.get_chat_member_count(numeric)
		linked = getattr(chat, "linked_chat_id", None)
		return CommunityStatsInfo(
			participants=count,
			online=None,
			linked_chat_id=str(linked) if linked is not None else None,
		)


async def check_token(token: str) -> str:
	"""Проверяет токен через метод getMe и возвращает @имя бота.

	Raises:
		InvalidBotTokenError: Токен неверного формата или отклонён Telegram.
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		BotError: Telegram отклонил запрос getMe (практически
			не случается — запасные ветки единого маппера).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	async with _bot_client(token) as bot:
		text = "Telegram отклонил запрос getMe."
		async with _bot_errors(text, text):
			me = await bot.get_me()
			return me.username or me.first_name
