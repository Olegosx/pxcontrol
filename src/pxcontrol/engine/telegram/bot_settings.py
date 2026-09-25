"""Настройки сообщества через Bot API: что бот читает и меняет (ADR-0043).

Бот — запасной исполнитель экрана «Настройки › В Telegram»: он меняет
лишь то, для чего у Bot API есть метод, — название, описание, фото
и общие права участников. Остальное Bot API либо не меняет вовсе, либо
читает не так, как userbot (живая проба 25.09.2026: «писать только
вступившим» бот сообщает по факту, а не по переключателю, реакции
канала — пустым списком). Поэтому бот и читает только эти четыре
настройки: показывать с его слов остальное значило бы показывать
неправду.

Таблицы — по ключам каталога, как у userbot (:mod:`mtproto_settings`):
добавить настройку, которую научится менять бот, = строка в каждой.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pxcontrol.engine.community_settings.model import (
	CommunitySettings,
	PhotoValue,
	SettingsContext,
	SettingValue,
)
from pxcontrol.engine.telegram.rights import (
	MemberRights,
	bot_default_permissions,
	bot_permission_flags,
)
from pxcontrol.engine.telegram.types import CommunityKind

BotReader = Callable[[Any], SettingValue]
BotWriter = Callable[[Any, int, SettingValue], Awaitable[None]]


BOT_READERS: dict[str, BotReader] = {
	"photo": lambda chat: PhotoValue(present=getattr(chat, "photo", None) is not None),
	"title": lambda chat: str(getattr(chat, "title", "") or ""),
	"about": lambda chat: str(getattr(chat, "description", "") or ""),
	"permissions": lambda chat: bot_default_permissions(getattr(chat, "permissions", None)),
}


def read_bot_settings(chat: Any, kind: CommunityKind) -> CommunitySettings:
	"""Снимок настроек по ответу ``getChat``: только то, что бот меняет.

	Отбор по виду сообщества делает сервис (как у userbot). Контекст
	беднее, чем у userbot: пределов сервера Bot API не отдаёт.
	"""
	values = {key: read(chat) for key, read in BOT_READERS.items()}
	linked = getattr(chat, "linked_chat_id", None)
	context = SettingsContext(kind=kind, linked_chat_id=str(linked) if linked else None)
	return CommunitySettings(values, context)


def _text(value: SettingValue) -> str:
	if not isinstance(value, str):
		raise TypeError(f"ожидался текст, пришло {value!r}")
	return value


async def _write_title(bot: Any, chat_id: int, value: SettingValue) -> None:
	await bot.set_chat_title(chat_id, _text(value))


async def _write_about(bot: Any, chat_id: int, value: SettingValue) -> None:
	await bot.set_chat_description(chat_id, description=_text(value))


async def _write_photo(bot: Any, chat_id: int, value: SettingValue) -> None:
	from aiogram.types import FSInputFile

	if not isinstance(value, PhotoValue):
		raise TypeError(f"ожидалось фото, пришло {value!r}")
	if value.upload is None:
		await bot.delete_chat_photo(chat_id)
		return
	await bot.set_chat_photo(chat_id, FSInputFile(value.upload))


async def _write_permissions(bot: Any, chat_id: int, value: SettingValue) -> None:
	"""Общие права: поля по видам независимы (``use_independent_chat_permissions``)."""
	from aiogram.types import ChatPermissions

	if not isinstance(value, MemberRights):
		raise TypeError(f"ожидались разрешения, пришло {value!r}")
	permissions = ChatPermissions(**bot_permission_flags(value))
	await bot.set_chat_permissions(chat_id, permissions, use_independent_chat_permissions=True)


BOT_WRITERS: dict[str, BotWriter] = {
	"photo": _write_photo,
	"title": _write_title,
	"about": _write_about,
	"permissions": _write_permissions,
}

#: Что бот умеет менять — для правила доступности (экран и сервис).
BOT_WRITABLE: frozenset[str] = frozenset(BOT_WRITERS)

#: Как Bot API отвечает на правку тем же значением: кода у ответа нет,
#: только описание (живая проба 25.09.2026: «chat description is not
#: modified»). Для правки это успех — цель достигнута.
_NOT_MODIFIED_MARK = "is not modified"


def not_modified(description: str | None) -> bool:
	"""Отказ означает «ничего не изменилось», а не настоящий отказ."""
	return _NOT_MODIFIED_MARK in (description or "").lower()
