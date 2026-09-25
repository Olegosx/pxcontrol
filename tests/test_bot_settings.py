"""Настройки сообщества через Bot API (ADR-0043): что бот читает и меняет — без сети."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetChatDescription
from aiogram.types import ChatPermissions, FSInputFile

from pxcontrol.engine.community_settings.catalog import SPECS
from pxcontrol.engine.community_settings.model import PhotoValue, SettingChange
from pxcontrol.engine.telegram import bot_api
from pxcontrol.engine.telegram.bot_api import BotError
from pxcontrol.engine.telegram.bot_settings import (
	BOT_READERS,
	BOT_WRITABLE,
	BOT_WRITERS,
	not_modified,
	read_bot_settings,
)
from pxcontrol.engine.telegram.rights import (
	ALL_MEMBER_RIGHTS,
	bot_default_permissions,
	bot_permission_flags,
)
from pxcontrol.engine.telegram.types import CommunityKind


class FakeBot:
	"""Бот aiogram: записывает вызовы, по заказу бросает ошибку сервера."""

	def __init__(self, error: BaseException | None = None) -> None:
		self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
		self.error = error

	def __getattr__(self, name: str) -> Any:
		async def call(*args: Any, **kwargs: Any) -> bool:
			self.calls.append((name, args, kwargs))
			if self.error is not None:
				raise self.error
			return True

		return call


def use_bot(monkeypatch: pytest.MonkeyPatch, bot: FakeBot) -> None:
	@asynccontextmanager
	async def client(_token: str) -> AsyncIterator[FakeBot]:
		yield bot

	monkeypatch.setattr(bot_api, "_bot_client", client)


# --- состав --------------------------------------------------------------------------


def test_bot_reads_exactly_what_it_writes() -> None:
	"""Бот читает только то, что меняет: прочее с его слов неверно (проба)."""
	assert set(BOT_READERS) == set(BOT_WRITERS) == BOT_WRITABLE
	assert {"title", "about", "photo", "permissions"} == BOT_WRITABLE
	assert set(SPECS) >= BOT_WRITABLE, "ключи бота — из каталога"


# --- чтение ----------------------------------------------------------------------------


def test_read_bot_settings() -> None:
	chat = SimpleNamespace(
		title="Группа",
		description=None,
		photo=object(),
		permissions=ChatPermissions(can_send_messages=True, can_send_polls=False),
		linked_chat_id=-1004344346478,
	)
	settings = read_bot_settings(chat, CommunityKind.GROUP)
	assert settings.values["title"] == "Группа" and settings.values["about"] == ""
	assert settings.values["photo"] == PhotoValue(True)
	permissions = settings.values["permissions"]
	assert permissions.send_plain is True
	assert permissions.send_polls is False
	assert permissions.send_photos is False, "поля нет — право не выдано"
	assert settings.context.linked_chat_id == "-1004344346478"
	assert settings.context.hidden_members_min is None, "пределов Bot API не отдаёт"


def test_bot_permissions_round_trip_and_other_messages() -> None:
	"""Разрешения туда и обратно; «прочее» — одним полем, разрешено только целиком."""
	allowed = replace(ALL_MEMBER_RIGHTS, send_polls=False)
	flags = bot_permission_flags(allowed)
	assert flags["can_send_polls"] is False and flags["can_send_messages"] is True
	assert bot_default_permissions(ChatPermissions(**flags)) == allowed
	one_other_banned = replace(ALL_MEMBER_RIGHTS, send_gifs=False)
	assert bot_permission_flags(one_other_banned)["can_send_other_messages"] is False
	assert bot_default_permissions(None) == ALL_MEMBER_RIGHTS, "нет слоя — нет запретов"


# --- запись ------------------------------------------------------------------------------


async def test_writers_call_bot_api() -> None:
	bot = FakeBot()
	await BOT_WRITERS["title"](bot, -100, "Новое")
	await BOT_WRITERS["about"](bot, -100, "")
	await BOT_WRITERS["photo"](bot, -100, PhotoValue(True, "/x/logo.jpg"))
	await BOT_WRITERS["photo"](bot, -100, PhotoValue(False))
	await BOT_WRITERS["permissions"](bot, -100, ALL_MEMBER_RIGHTS)
	names = [name for name, _args, _kwargs in bot.calls]
	assert names == [
		"set_chat_title",
		"set_chat_description",
		"set_chat_photo",
		"delete_chat_photo",
		"set_chat_permissions",
	]
	assert isinstance(bot.calls[2][1][1], FSInputFile)
	assert bot.calls[4][2] == {"use_independent_chat_permissions": True}


def _bad_request(text: str) -> TelegramBadRequest:
	return TelegramBadRequest(method=SetChatDescription(chat_id=1, description=""), message=text)


async def test_apply_same_value_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
	"""«is not modified» у Bot API — успех правки, а не отказ (живая проба)."""
	assert not_modified("Bad Request: chat description is not modified")
	use_bot(monkeypatch, FakeBot(_bad_request("Bad Request: chat description is not modified")))
	await bot_api.apply_community_setting("123:abc", "-100", SettingChange("about", "то же"))


async def test_apply_refusal_keeps_server_reason(monkeypatch: pytest.MonkeyPatch) -> None:
	use_bot(monkeypatch, FakeBot(_bad_request("Bad Request: not enough rights")))
	with pytest.raises(BotError, match="not enough rights"):
		await bot_api.apply_community_setting("123:abc", "-100", SettingChange("title", "Х"))


async def test_apply_setting_bot_cannot_write(monkeypatch: pytest.MonkeyPatch) -> None:
	use_bot(monkeypatch, FakeBot())
	with pytest.raises(BotError, match="userbot"):
		await bot_api.apply_community_setting("123:abc", "-100", SettingChange("slowmode", 10))
