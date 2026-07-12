"""Транспорт Bot API (через aiogram). В первую очередь — публикация постов."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class InvalidBotToken(Exception):
	"""Telegram отклонил токен бота (или токен неправильного формата)."""


class ChannelCheckError(Exception):
	"""Канал не прошёл проверку подключения (с понятным текстом)."""


@dataclass(frozen=True)
class ChannelInfo:
	"""Данные канала, полученные при проверке подключения."""

	chat_id: str
	title: str
	username: str | None


def normalize_chat_ref(chat_ref: str) -> str | int:
	"""Приводит ввод пользователя к виду для Bot API.

	Принимает ``@имя``, ``имя``, ссылки ``t.me/имя`` и ``t.me/c/<число>/…``,
	числовой ID (в том числе с пробелами внутри). Возвращает ``@имя``
	или число.

	Raises:
		ChannelCheckError: Пустая, инвайт- или неразборчивая ссылка.
	"""
	ref = chat_ref.strip()
	for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
		if ref.lower().startswith(prefix):
			ref = ref[len(prefix):]
			break
	ref = ref.strip("/")
	if ref.startswith("+"):
		raise ChannelCheckError(
			"Инвайт-ссылка (t.me/+…) не подходит — укажите @имя канала "
			"или его ID (начинается с -100)."
		)
	if ref.lower().startswith("c/"):
		internal = ref[2:].split("/", 1)[0]
		if internal.isdigit():
			return int(f"-100{internal}")
		raise ChannelCheckError(
			"Не удалось разобрать ссылку t.me/c/… — укажите ID канала (-100…)."
		)
	ref = ref.lstrip("@")
	digits = ref.replace(" ", "")
	if digits.lstrip("-").isdigit() and digits.lstrip("-"):
		return int(digits)
	if not ref:
		raise ChannelCheckError("Укажите @имя, ссылку t.me/… или ID канала.")
	return f"@{ref}"


def ensure_bot_can_post(member: Any) -> None:
	"""Проверяет, что бот — администратор канала с правом публиковать.

	Raises:
		ChannelCheckError: Бот не админ или без права публикации.
	"""
	status = getattr(member, "status", "")
	if status == "creator":
		return
	if status != "administrator":
		raise ChannelCheckError(
			"Бот не администратор канала — добавьте его администратором."
		)
	if getattr(member, "can_post_messages", None) is not True:
		raise ChannelCheckError("У бота нет права публиковать сообщения в канале.")


async def send_text(token: str, chat_id: str, text: str) -> int:
	"""Публикует текстовый пост в канал через Bot API («сейчас»).

	Returns:
		ID сообщения в Telegram.

	Raises:
		ChannelCheckError: Telegram отклонил отправку (нет прав и т.п.).
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram import Bot
	from aiogram.exceptions import (
		TelegramBadRequest,
		TelegramForbiddenError,
		TelegramNetworkError,
		TelegramUnauthorizedError,
	)

	bot = Bot(token)
	try:
		message = await bot.send_message(int(chat_id), text)
		return int(message.message_id)
	except TelegramUnauthorizedError as exc:
		raise ChannelCheckError("Telegram отклонил токен бота.") from exc
	except TelegramForbiddenError as exc:
		raise ChannelCheckError(
			f"Бот не может писать в канал. (Telegram: {exc.message})"
		) from exc
	except TelegramBadRequest as exc:
		raise ChannelCheckError(
			f"Telegram отклонил отправку. (Telegram: {exc.message})"
		) from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
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

	Raises:
		InvalidBotToken: Telegram отклонил токен.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram import Bot
	from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

	bot = Bot(token)
	try:
		updates = await bot.get_updates(timeout=1)
	except TelegramUnauthorizedError as exc:
		raise InvalidBotToken("Telegram отклонил токен (Unauthorized).") from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	finally:
		await bot.session.close()
	return [line for update in updates if (line := describe_update(update))]


async def check_channel(token: str, chat_ref: str) -> ChannelInfo:
	"""Проверяет канал: существует, бот в нём админ с правом публиковать.

	Raises:
		ChannelCheckError: Канал не найден / бот не добавлен / нет прав.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram import Bot
	from aiogram.exceptions import (
		TelegramBadRequest,
		TelegramForbiddenError,
		TelegramNetworkError,
		TelegramUnauthorizedError,
	)

	ref = normalize_chat_ref(chat_ref)
	logger.info("Проверка канала: ввод %r распознан как %r.", chat_ref, ref)
	bot = Bot(token)
	try:
		chat = await bot.get_chat(ref)
		me = await bot.get_me()
		member = await bot.get_chat_member(chat.id, me.id)
		ensure_bot_can_post(member)
		return ChannelInfo(str(chat.id), chat.title or str(ref), chat.username)
	except TelegramUnauthorizedError as exc:
		raise ChannelCheckError("Telegram отклонил токен бота.") from exc
	except TelegramForbiddenError as exc:
		raise ChannelCheckError(
			"Бот не добавлен в канал — добавьте его администратором. "
			f"(Telegram: {exc.message})"
		) from exc
	except TelegramBadRequest as exc:
		raise ChannelCheckError(
			"Канал не найден — проверьте @имя или ID; приватный канал "
			"виден боту только после добавления его администратором. "
			f"(Telegram: {exc.message})"
		) from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	finally:
		await bot.session.close()


async def check_token(token: str) -> str:
	"""Проверяет токен через метод getMe и возвращает @имя бота.

	Raises:
		InvalidBotToken: Токен неверного формата или отклонён Telegram.
		ConnectionError: Нет связи с серверами Telegram.
	"""
	from aiogram import Bot
	from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
	from aiogram.utils.token import TokenValidationError

	try:
		bot = Bot(token)
	except TokenValidationError as exc:
		raise InvalidBotToken("Строка не похожа на токен бота.") from exc
	try:
		me = await bot.get_me()
		return me.username or me.first_name
	except TelegramUnauthorizedError as exc:
		raise InvalidBotToken("Telegram отклонил токен (Unauthorized).") from exc
	except TelegramNetworkError as exc:
		raise ConnectionError("Нет связи с Telegram — проверьте сеть.") from exc
	finally:
		await bot.session.close()


class BotApiTransport:
	"""Обёртка над Bot API. Тяжёлая зависимость импортируется лениво."""

	def __init__(self, token: str | None) -> None:
		self._token = token
		self._bot: Any | None = None

	async def start(self) -> None:
		"""Создаёт клиента Bot API, если задан токен.

		Токены хранятся в БД (таблица ``bots``, ADR-0009); подключение
		конкретного бота — при реализации каналов.
		"""
		if not self._token:
			logger.info("Бот не настроен — Bot API отключён.")
			return
		from aiogram import Bot

		self._bot = Bot(self._token)
		logger.info("Bot API клиент создан.")

	async def stop(self) -> None:
		"""Закрывает сессию клиента Bot API."""
		if self._bot is not None:
			await self._bot.session.close()
			self._bot = None

	async def publish(self, chat_id: str, text: str, video_path: str | None = None) -> None:
		"""Публикует пост в канал.

		Каркас: реальная отправка добавляется при реализации модуля.
		"""
		raise NotImplementedError("Публикация через Bot API ещё не реализована")
