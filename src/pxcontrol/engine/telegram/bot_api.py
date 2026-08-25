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
from pxcontrol.engine.telegram.refs import normalize_chat_ref
from pxcontrol.engine.telegram.types import ChannelInfo, MediaKind

logger = logging.getLogger(__name__)


class InvalidBotTokenError(EngineError):
	"""Telegram отклонил токен бота (или токен неправильного формата)."""


class ChannelCheckError(EngineError):
	"""Канал не прошёл проверку подключения (с понятным текстом)."""


@asynccontextmanager
async def _bot_errors(forbidden: str, bad_request: str) -> AsyncIterator[None]:
	"""Переводит исключения aiogram в понятные человеку ошибки.

	Единый маппер для всех операций бота (аналог ``_map_post_error``
	в mtproto): неверный токен и сеть переводятся одинаково, а тексты
	для «нет прав» (Forbidden) и «отклонено» (BadRequest) зависят
	от операции и передаются параметрами.

	Raises:
		ChannelCheckError: Telegram отклонил операцию (токен, права, запрос).
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
		raise ChannelCheckError("Telegram отклонил токен бота.") from exc
	except TelegramForbiddenError as exc:
		raise ChannelCheckError(f"{forbidden} (Telegram: {exc.message})") from exc
	except TelegramRetryAfter as exc:
		# лимит частоты (429) — парный текст в _translate_error (mtproto)
		raise ChannelCheckError(f"Telegram просит подождать {exc.retry_after} с.") from exc
	except TelegramBadRequest as exc:
		raise ChannelCheckError(f"{bad_request} (Telegram: {exc.message})") from exc
	except TelegramEntityTooLarge as exc:
		# наследует сетевую ошибку — ветка обязана стоять раньше неё,
		# иначе «файл велик» превратился бы в ложное «нет связи»
		raise ChannelCheckError("Файл больше лимита Bot API (50 МБ) — уменьшите файл.") from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	except TelegramAPIError as exc:
		# запасная ветка: серверные сбои (5xx) и прочие отказы API
		raise ChannelCheckError(f"Telegram отклонил операцию: {exc}") from exc


#: Разметка поля текста поста (её разбирает Telethon) → HTML-теги Bot API.
_MARKUP: list[tuple[re.Pattern[str], str]] = [
	(re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"<b>\1</b>"),
	(re.compile(r"__(.+?)__", re.DOTALL), r"<i>\1</i>"),
	(re.compile(r"~~(.+?)~~", re.DOTALL), r"<s>\1</s>"),
	(re.compile(r"`(.+?)`", re.DOTALL), r"<code>\1</code>"),
]


def to_html(text: str) -> str:
	"""Переводит текст поста из разметки поля ввода в HTML для Bot API.

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
	"""Числовой ID канала из строки БД (контракт ``ChannelInfo.chat_id``).

	Парный помощник ``_peer_id`` в mtproto: нечисловая строка — повреждённая
	запись, а не сетевой сбой, и заслуживает понятного текста.

	Raises:
		ChannelCheckError: В БД оказался нечисловой ID.
	"""
	try:
		return int(chat_id)
	except ValueError as exc:
		raise ChannelCheckError(
			f"Некорректный ID канала в базе: {chat_id!r} — переподключите "
			"канал на странице «Каналы»."
		) from exc


def ensure_bot_can_post(member: Any) -> None:
	"""Проверяет, что бот — администратор канала с правом публиковать.

	Raises:
		ChannelCheckError: Бот не админ или без права публикации.
	"""
	status = getattr(member, "status", "")
	if status == "creator":
		return
	if status != "administrator":
		raise ChannelCheckError("Бот не администратор канала — добавьте его администратором.")
	if getattr(member, "can_post_messages", None) is not True:
		raise ChannelCheckError("У бота нет права публиковать сообщения в канале.")


async def send_media(token: str, chat_id: str, kind: MediaKind, path: str, caption: str) -> int:
	"""Отправляет медиа в канал через Bot API (лимит — 50 МБ на файл).

	Returns:
		ID сообщения в Telegram.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		ChannelCheckError: Telegram отклонил отправку (нет прав, размер и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.types import FSInputFile

	bot = _make_bot(token)
	file = FSInputFile(path)
	text = to_html(caption) if caption else None
	mode = "HTML"
	try:
		async with _bot_errors("Бот не может писать в канал.", "Telegram отклонил отправку."):
			if kind is MediaKind.PHOTO:
				message = await bot.send_photo(
					_chat_id(chat_id), file, caption=text, parse_mode=mode
				)
			elif kind is MediaKind.VIDEO:
				message = await bot.send_video(
					_chat_id(chat_id), file, caption=text, parse_mode=mode, supports_streaming=True
				)
			elif kind is MediaKind.AUDIO:
				message = await bot.send_audio(
					_chat_id(chat_id), file, caption=text, parse_mode=mode
				)
			else:
				message = await bot.send_document(
					_chat_id(chat_id), file, caption=text, parse_mode=mode
				)
			return int(message.message_id)
	finally:
		await bot.session.close()


async def send_text(token: str, chat_id: str, text: str) -> int:
	"""Публикует текстовый пост в канал через Bot API («сейчас»).

	Returns:
		ID сообщения в Telegram.

	Raises:
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		ChannelCheckError: Telegram отклонил отправку (нет прав и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	bot = _make_bot(token)
	try:
		async with _bot_errors("Бот не может писать в канал.", "Telegram отклонил отправку."):
			message = await bot.send_message(_chat_id(chat_id), to_html(text), parse_mode="HTML")
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
		ChannelCheckError: Telegram отклонил запрос (вебхук, параллельный опрос).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.exceptions import (
		TelegramBadRequest,
		TelegramConflictError,
		TelegramNetworkError,
		TelegramUnauthorizedError,
	)

	bot = _make_bot(token)
	try:
		# timeout=1 — короткий long-poll: диагностике нужен снимок, не ожидание
		updates = await bot.get_updates(timeout=1)
	except TelegramUnauthorizedError as exc:
		raise InvalidBotTokenError("Telegram отклонил токен (Unauthorized).") from exc
	except TelegramConflictError as exc:
		raise ChannelCheckError(
			"События недоступны: у бота включён вебхук или его опрашивает другое приложение."
		) from exc
	except TelegramBadRequest as exc:
		raise ChannelCheckError(f"Telegram отклонил запрос событий. ({exc.message})") from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	finally:
		await bot.session.close()
	return [line for update in updates if (line := describe_update(update))]


async def check_channel(token: str, chat_ref: str) -> ChannelInfo:
	"""Проверяет канал: существует, бот в нём админ с правом публиковать.

	Raises:
		ChatRefError: Введённую ссылку/имя не удалось разобрать.
		InvalidBotTokenError: Токен в БД повреждён (не похож на токен).
		ChannelCheckError: Канал не найден / бот не добавлен / нет прав.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	ref = normalize_chat_ref(chat_ref)
	logger.info("Проверка канала: ввод %r распознан как %r.", chat_ref, ref)
	bot = _make_bot(token)
	try:
		async with _bot_errors(
			"Бот не добавлен в канал — добавьте его администратором.",
			"Канал не найден — проверьте @имя или ID; приватный канал "
			"виден боту только после добавления его администратором.",
		):
			chat = await bot.get_chat(ref)
			me = await bot.get_me()
			member = await bot.get_chat_member(chat.id, me.id)
			ensure_bot_can_post(member)
			return ChannelInfo(str(chat.id), chat.title or str(ref), chat.username)
	finally:
		await bot.session.close()


async def check_token(token: str) -> str:
	"""Проверяет токен через метод getMe и возвращает @имя бота.

	Raises:
		InvalidBotTokenError: Токен неверного формата или отклонён Telegram.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

	bot = _make_bot(token)
	try:
		me = await bot.get_me()
		return me.username or me.first_name
	except TelegramUnauthorizedError as exc:
		raise InvalidBotTokenError("Telegram отклонил токен (Unauthorized).") from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	finally:
		await bot.session.close()
