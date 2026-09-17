"""Транспорт Bot API (через aiogram).

В первую очередь — проверки токена и прав бота и диагностика; публикация —
запасной путь (текст и файлы до 50 МБ, только «сейчас»), когда у канала
нет userbot-админа: основной транспорт публикации — MTProto (ADR-0011).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from aiogram import Bot

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.markup import ButtonKind, PostMarkup
from pxcontrol.engine.telegram.refs import normalize_chat_ref, numeric_chat_id
from pxcontrol.engine.telegram.rich_text import RichText, TextEntity, TextStyle
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	CommunityInfo,
	CommunityKind,
	CommunityStatsInfo,
	MediaKind,
	TelegramFloodError,
	limit_mb,
)

logger = logging.getLogger(__name__)


class InvalidBotTokenError(EngineError):
	"""Telegram отклонил токен бота (или токен неправильного формата)."""


class CommunityCheckError(EngineError):
	"""Канал не прошёл проверку подключения (с понятным текстом)."""


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
		CommunityCheckError: Telegram отклонил операцию (права, запрос).
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
		raise CommunityCheckError(f"{forbidden} (Telegram: {exc.message})") from exc
	except TelegramRetryAfter as exc:
		# флуд-лимит (429) — временное состояние: очередь отправки ждёт
		# и повторяет (парный перевод — FloodWaitError в mtproto)
		raise TelegramFloodError(
			f"Telegram просит подождать {exc.retry_after} с.", retry_after_s=exc.retry_after
		) from exc
	except TelegramBadRequest as exc:
		raise CommunityCheckError(f"{bad_request} (Telegram: {exc.message})") from exc
	except TelegramEntityTooLarge as exc:
		# наследует сетевую ошибку — ветка обязана стоять раньше неё,
		# иначе «файл велик» превратился бы в ложное «нет связи»
		raise CommunityCheckError(
			f"Файл больше лимита Bot API ({limit_mb(BOT_MAX_FILE_BYTES)} МБ) — уменьшите файл."
		) from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	except TelegramAPIError as exc:
		# запасная ветка: серверные сбои (5xx) и прочие отказы API
		raise CommunityCheckError(f"Telegram отклонил операцию: {exc}") from exc


#: Разметка поля текста поста (её разбирает Telethon) → HTML-теги Bot API.
_MARKUP: list[tuple[re.Pattern[str], str]] = [
	(re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"<b>\1</b>"),
	(re.compile(r"__(.+?)__", re.DOTALL), r"<i>\1</i>"),
	(re.compile(r"~~(.+?)~~", re.DOTALL), r"<s>\1</s>"),
	(re.compile(r"`(.+?)`", re.DOTALL), r"<code>\1</code>"),
]


def post_html(text: str, entities: tuple[TextEntity, ...] = ()) -> str:
	"""HTML поста для Bot API: из сущностей, а без них — из старой разметки.

	Одна точка на оба случая (ADR-0033). У размеченного текста истина —
	сущности, и строка уже чистая: её переводит :func:`html_from_rich`.
	У текста без разметки (старые элементы очереди и всё, что пока
	пишет форма) остаётся прежний путь — разбор разделителей строки,
	чтобы бот-канал выглядел так же, как userbot-канал.
	"""
	if entities:
		return html_from_rich(RichText(text, entities))
	return to_html(text)


def to_html(text: str) -> str:
	"""Переводит текст поста из старой разметки поля ввода в HTML.

	Поле текста поста живёт в разметке, которую Telethon (основной путь,
	ADR-0011) разбирает сам: ``**жирный**``, ``__курсив__``,
	``~~зачёркнутый~~``, `` `код` ``. Bot API без ``parse_mode``
	не разбирает ничего, а его Markdown-режимы с двойными звёздочками
	несовместимы — единственный совместимый режим ``HTML``. Экранируем
	служебные символы HTML и переводим пары в теги — бот-канал выглядит
	так же, как userbot-канал. Ссылки ``[текст](url)`` намеренно
	не переводятся (редки в подписях; кривой URL сломал бы весь пост).
	"""
	escaped = html.escape(text, quote=False)
	for pattern, replacement in _MARKUP:
		escaped = pattern.sub(replacement, escaped)
	return escaped


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
	return numeric_chat_id(chat_id, CommunityCheckError)


def community_kind_from_chat_type(chat_type: str) -> CommunityKind:
	"""Вид сообщества по типу чата Bot API (ADR-0021).

	Raises:
		CommunityCheckError: Тип не подключается: малая группа —
			с подсказкой преобразовать в супергруппу, личный чат — с
			объяснением, что нужен канал или группа.
	"""
	if chat_type == "channel":
		return CommunityKind.CHANNEL
	if chat_type == "supergroup":
		return CommunityKind.GROUP
	if chat_type == "group":
		raise CommunityCheckError(
			"Малые группы не подключаются — преобразуйте группу "
			"в супергруппу (в настройках группы) и повторите."
		)
	raise CommunityCheckError("Это личный чат — укажите канал или группу.")


def ensure_bot_can_post(member: Any) -> None:
	"""Проверяет, что бот — администратор канала с правом публиковать.

	Raises:
		CommunityCheckError: Бот не админ или без права публикации.
	"""
	status = getattr(member, "status", "")
	if status == "creator":
		return
	if status != "administrator":
		raise CommunityCheckError("Бот не администратор канала — добавьте его администратором.")
	if getattr(member, "can_post_messages", None) is not True:
		raise CommunityCheckError("У бота нет права публиковать сообщения в канале.")


def bot_can_edit_messages(member: Any) -> bool:
	"""Может ли бот править чужие сообщения в канале (ADR-0031).

	Право ``can_edit_messages`` существует только у каналов; у владельца
	оно есть всегда, у группы его не бывает вовсе (там каждый правит
	только своё — проверено опытом). Отсутствие права не мешает
	подключению сообщества: без него просто недоступен маршрут, в котором
	бот дорисовывает кнопки к посту публикателя.

	Args:
		member: ответ ``getChatMember`` для самого бота.
	"""
	status = getattr(member, "status", "")
	if status == "creator":
		return True
	return status == "administrator" and getattr(member, "can_edit_messages", None) is True


def ensure_bot_can_send_in_group(member: Any, default_permissions: Any) -> None:
	"""Проверяет, что бот может писать в группе (ADR-0021).

	В группах права ``post_messages`` нет: писать может любой участник,
	которого не ограничили. Админам (и создателю) ограничения группы
	не мешают; обычный участник упирается в общие права группы
	(``chat.permissions``), ограниченный — ещё и в свои.

	Args:
		member: ответ ``getChatMember`` для самого бота.
		default_permissions: общие права группы (``chat.permissions``).

	Raises:
		CommunityCheckError: Бот не участник или не может писать.
	"""
	status = getattr(member, "status", "")
	if status in ("creator", "administrator"):
		return
	if status == "restricted":
		if getattr(member, "is_member", None) is not True:
			raise CommunityCheckError("Бот не участник группы — добавьте его в группу.")
		if getattr(member, "can_send_messages", None) is not True:
			raise CommunityCheckError("Бот ограничен в отправке сообщений в этой группе.")
	elif status != "member":
		raise CommunityCheckError("Бот не участник группы — добавьте его в группу.")
	if getattr(default_permissions, "can_send_messages", None) is False:
		raise CommunityCheckError(
			"В группе писать могут только администраторы — назначьте бота администратором."
		)


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
		CommunityCheckError: Telegram отклонил правку (нет права
			изменять сообщения, пост не найден, разметка не годится).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	bot = _make_bot(token)
	try:
		async with _bot_errors(
			"У бота нет права изменять сообщения в этом сообществе.",
			"Telegram отклонил правку клавиатуры.",
		):
			await bot.edit_message_reply_markup(
				chat_id=_chat_id(chat_id),
				message_id=message_id,
				reply_markup=to_reply_markup(markup),
			)
	finally:
		await bot.session.close()


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
		CommunityCheckError: Telegram отклонил отправку (нет прав, размер и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.types import FSInputFile

	bot = _make_bot(token)
	file = FSInputFile(path)
	text = post_html(caption, entities) if caption else None
	mode = "HTML"
	keyboard = to_reply_markup(markup)
	try:
		async with _bot_errors("Бот не может писать в канал.", "Telegram отклонил отправку."):
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
	finally:
		await bot.session.close()


async def send_text(
	token: str,
	chat_id: str,
	text: str,
	topic_id: int | None = None,
	markup: PostMarkup | None = None,
	entities: tuple[TextEntity, ...] = (),
) -> int:
	"""Публикует текстовый пост через Bot API («сейчас»).

	``topic_id`` — тема форума (``message_thread_id``); None — общая лента.

	Returns:
		ID сообщения в Telegram.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		CommunityCheckError: Telegram отклонил отправку (нет прав и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	bot = _make_bot(token)
	try:
		async with _bot_errors("Бот не может писать в канал.", "Telegram отклонил отправку."):
			message = await bot.send_message(
				_chat_id(chat_id),
				post_html(text, entities),
				parse_mode="HTML",
				message_thread_id=topic_id,
				reply_markup=to_reply_markup(markup),
			)
			return int(message.message_id)
	finally:
		await bot.session.close()


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
		CommunityCheckError: Telegram отклонил запрос (вебхук, параллельный опрос).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.exceptions import TelegramConflictError

	bot = _make_bot(token)
	try:
		async with _bot_errors(
			"Telegram отклонил запрос событий.", "Telegram отклонил запрос событий."
		):
			try:
				# timeout=1 — короткий long-poll: диагностике нужен снимок,
				# не ожидание
				updates = await bot.get_updates(timeout=1)
			except TelegramConflictError as exc:
				# точный совет ценнее запасной ветки единого маппера
				raise CommunityCheckError(
					"События недоступны: у бота включён вебхук или его "
					"опрашивает другое приложение."
				) from exc
	finally:
		await bot.session.close()
	return [line for update in updates if (line := describe_update(update))]


async def check_community(token: str, chat_ref: str) -> CommunityInfo:
	"""Проверяет сообщество и права бота по его виду (ADR-0021).

	Канал: бот — админ с правом публиковать. Группа (супергруппа):
	бот — участник, не ограниченный в отправке. Малая группа и личный
	чат не подключаются. Вид и признак форума возвращаются в
	:class:`CommunityInfo`.

	Raises:
		ChatRefError: Введённую ссылку/имя не удалось разобрать.
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		CommunityCheckError: Сообщество не найдено / бот не добавлен /
			нет прав / вид не подключается (малая группа, личный чат).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	ref = normalize_chat_ref(chat_ref)
	logger.info("Проверка сообщества: ввод %r распознан как %r.", chat_ref, ref)
	bot = _make_bot(token)
	try:
		async with _bot_errors(
			"Бот не добавлен в сообщество — добавьте его (в канал — администратором).",
			"Канал или группа не найдены — проверьте @имя или ID; приватное "
			"сообщество видно боту только после добавления его участником.",
		):
			chat = await bot.get_chat(ref)
			kind = community_kind_from_chat_type(str(chat.type))
			me = await bot.get_me()
			member = await bot.get_chat_member(chat.id, me.id)
			if kind is CommunityKind.CHANNEL:
				ensure_bot_can_post(member)
			else:
				ensure_bot_can_send_in_group(member, chat.permissions)
			return CommunityInfo(
				str(chat.id),
				chat.title or str(ref),
				chat.username,
				kind=kind,
				forum=bool(chat.is_forum),
				# право правки не требуется для подключения — оно решает
				# только, доступны ли кнопки поверх поста публикателя
				can_edit=bot_can_edit_messages(member),
			)
	finally:
		await bot.session.close()


async def get_community_stats(token: str, chat_id: str) -> CommunityStatsInfo:
	"""Статистика сообщества через бота: число участников и связанный чат.

	Два запроса Bot API — ``getChat`` (связанное сообщество) и
	``getChatMemberCount``. Больше Bot API о сообществе не расскажет:
	ни онлайна, ни отложенных, ни встроенной статистики у него нет —
	это путь дешёвого частого опроса, полная картина — за userbot.

	Raises:
		InvalidBotTokenError: Токен отклонён Telegram.
		TelegramFloodError: Флуд-лимит — подождать и повторить.
		CommunityCheckError: Бот не видит сообщество или запрос отклонён.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	bot = _make_bot(token)
	try:
		async with _bot_errors(
			"Бот не видит сообщество — его могли исключить.",
			"Telegram отклонил запрос сведений о сообществе.",
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
	finally:
		await bot.session.close()


async def check_token(token: str) -> str:
	"""Проверяет токен через метод getMe и возвращает @имя бота.

	Raises:
		InvalidBotTokenError: Токен неверного формата или отклонён Telegram.
		TelegramFloodError: Флуд-лимит — очередь ждёт и повторяет сама.
		CommunityCheckError: Telegram отклонил запрос getMe (практически
			не случается — запасные ветки единого маппера).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	bot = _make_bot(token)
	try:
		text = "Telegram отклонил запрос getMe."
		async with _bot_errors(text, text):
			me = await bot.get_me()
			return me.username or me.first_name
	finally:
		await bot.session.close()
