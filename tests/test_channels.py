"""Тесты сервиса каналов и чистых функций проверки (без сети)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.channels import ChannelError, ChannelsService
from pxcontrol.engine.telegram.bot_api import (
	ChannelCheckError,
	ChannelInfo,
	ensure_bot_can_post,
	normalize_chat_ref,
)


class _FakeGateway:
	"""Подмена шлюза: токены и каналы проверяются без сети."""

	login = None  # вход userbot в этих тестах не используется

	async def check_bot_token(self, token: str) -> str:
		return "test_bot"

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo:
		if chat_ref == "@notfound":
			raise ChannelCheckError("Канал не найден — проверьте @имя или ID.")
		if chat_ref == "@noperm":
			raise ChannelCheckError(
				"У бота нет права публиковать сообщения в канале."
			)
		return ChannelInfo("-1001234", "Тестовый канал", "testchan")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'channels.db'}")
	await database.init()
	yield database
	await database.close()


async def _make_bot(db: Database) -> int:
	"""Создаёт бота и возвращает его id."""
	accounts = AccountsService(db, _FakeGateway())  # type: ignore[arg-type]
	bot = await accounts.add_bot("Публикатор", "123456:AAAbbb")
	return bot.id


async def test_channel_lifecycle(db: Database) -> None:
	"""Канал подключается с проверкой, виден в списке, удаляется."""
	bot_id = await _make_bot(db)
	service = ChannelsService(db, _FakeGateway())
	dto = await service.add_channel(bot_id, "@testchan")
	assert dto.title == "Тестовый канал"
	assert dto.tg_chat_id == "-1001234"
	assert dto.bot_label == "Публикатор"
	listed = await service.list_channels()
	assert [c.title for c in listed] == ["Тестовый канал"]
	assert listed[0].bot_label == "Публикатор"
	await service.delete_channel(dto.id)
	assert await service.list_channels() == []


async def test_failed_check_not_saved(db: Database) -> None:
	"""Не прошедший проверку канал не сохраняется."""
	bot_id = await _make_bot(db)
	service = ChannelsService(db, _FakeGateway())
	with pytest.raises(ChannelCheckError, match="не найден"):
		await service.add_channel(bot_id, "@notfound")
	with pytest.raises(ChannelCheckError, match="нет права"):
		await service.add_channel(bot_id, "@noperm")
	assert await service.list_channels() == []


async def test_duplicate_channel_rejected(db: Database) -> None:
	"""Повторное подключение того же канала — понятная ошибка."""
	bot_id = await _make_bot(db)
	service = ChannelsService(db, _FakeGateway())
	await service.add_channel(bot_id, "@testchan")
	with pytest.raises(ChannelError, match="уже подключён"):
		await service.add_channel(bot_id, "@testchan")


async def test_unknown_bot_rejected(db: Database) -> None:
	"""Подключение с несуществующим ботом — понятная ошибка."""
	service = ChannelsService(db, _FakeGateway())
	with pytest.raises(ChannelError, match="Бот не найден"):
		await service.add_channel(999, "@testchan")


def test_normalize_chat_ref() -> None:
	"""Все форматы ввода приводятся к виду для Bot API."""
	assert normalize_chat_ref("@mychannel") == "@mychannel"
	assert normalize_chat_ref("mychannel") == "@mychannel"
	assert normalize_chat_ref("https://t.me/mychannel") == "@mychannel"
	assert normalize_chat_ref("t.me/mychannel/") == "@mychannel"
	assert normalize_chat_ref("-1001234567") == -1001234567
	with pytest.raises(ChannelCheckError):
		normalize_chat_ref("   ")


def test_ensure_bot_can_post() -> None:
	"""Право публиковать: владелец и админ с правом проходят, прочие — нет."""
	ensure_bot_can_post(SimpleNamespace(status="creator"))
	ensure_bot_can_post(
		SimpleNamespace(status="administrator", can_post_messages=True)
	)
	with pytest.raises(ChannelCheckError, match="не администратор"):
		ensure_bot_can_post(SimpleNamespace(status="member"))
	with pytest.raises(ChannelCheckError, match="нет права"):
		ensure_bot_can_post(
			SimpleNamespace(status="administrator", can_post_messages=False)
		)
