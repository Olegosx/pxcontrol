"""Тесты сервиса каналов и чистых функций проверки (без сети)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.channels import ChannelError, ChannelsService
from pxcontrol.engine.services.settings import CHANNEL_ENABLED, SettingsService
from pxcontrol.engine.telegram.bot_api import ChannelCheckError, ensure_bot_can_post
from pxcontrol.engine.telegram.mtproto import (
	UserbotAccessError,
	UserbotNotConnectedError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.refs import ChatRefError, normalize_chat_ref
from pxcontrol.engine.telegram.types import ChannelInfo


class _FakeGateway:
	"""Подмена шлюза: токены и каналы проверяются без сети."""

	login = None  # вход userbot в этих тестах не используется
	userbot_is_admin = True  # ответ попутной/основной userbot-проверки
	bot_is_admin = True  # ответ проверки прав бота

	async def check_bot_token(self, token: str) -> str:
		return "test_bot"

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo:
		if chat_ref == "@notfound":
			raise ChannelCheckError("Канал не найден — проверьте @имя или ID.")
		if chat_ref == "@noperm" or not self.bot_is_admin:
			raise ChannelCheckError(
				"У бота нет права публиковать сообщения в канале."
			)
		return ChannelInfo("-1001234", "Тестовый канал", "testchan")

	async def check_channel_userbot(self, chat_ref: str) -> ChannelInfo:
		if not self.userbot_is_admin:
			raise UserbotAccessError(
				"Userbot не администратор канала — добавьте аккаунт "
				"администратором с правом публиковать."
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


async def test_bot_connect_probes_userbot(db: Database) -> None:
	"""Бот-путь попутно отмечает userbot-админа; сбой пробы не мешает."""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	service = ChannelsService(db, gateway)
	dto = await service.add_channel(bot_id, "@testchan")
	assert dto.userbot_admin is True
	await service.delete_channel(dto.id)
	gateway.userbot_is_admin = False
	dto = await service.add_channel(bot_id, "@testchan")
	assert dto.userbot_admin is False  # подключение состоялось без userbot


async def test_connect_via_userbot(db: Database) -> None:
	"""Через userbot канал подключается без бота; не админ — ошибка."""
	gateway = _FakeGateway()
	service = ChannelsService(db, gateway)
	dto = await service.add_channel_via_userbot("@testchan")
	assert dto.bot_id is None and dto.bot_label is None
	assert dto.userbot_admin is True
	listed = await service.list_channels()
	assert listed[0].userbot_admin is True
	with pytest.raises(ChannelError, match="уже подключён"):
		await service.add_channel_via_userbot("@testchan")
	await service.delete_channel(dto.id)
	gateway.userbot_is_admin = False
	with pytest.raises(UserbotUnavailableError, match="не администратор"):
		await service.add_channel_via_userbot("@testchan")
	assert await service.list_channels() == []


async def test_recheck_updates_flags_both_ways(db: Database) -> None:
	"""Перепроверка актуализирует userbot-флаг и сообщает о правах бота."""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	gateway.userbot_is_admin = False
	service = ChannelsService(db, gateway)
	dto = await service.add_channel(bot_id, "@testchan")
	assert dto.userbot_admin is False
	# userbot стал админом (например, добавили после подключения)
	gateway.userbot_is_admin = True
	access = await service.recheck_channel(dto.id)
	assert access.userbot_ok and access.channel.userbot_admin is True
	assert access.bot_ok is True
	# бота выгнали из канала: флаг userbot остаётся, бот — предупреждение
	gateway.bot_is_admin = False
	access = await service.recheck_channel(dto.id)
	assert access.bot_ok is False
	assert access.channel.bot_id is not None  # бот не отвязан молча


async def test_recheck_keeps_flag_when_userbot_unreachable(db: Database) -> None:
	"""Сбой связи/подключения userbot не сбрасывает сохранённые права.

	Иначе перепроверка при мигнувшей сети молча лишала бы канал
	отложенных постов и больших файлов (маршрутизация идёт по флагу).
	"""

	class _OfflineGateway(_FakeGateway):
		async def check_channel_userbot(self, chat_ref: str) -> ChannelInfo:
			raise UserbotNotConnectedError("Userbot не подключён — войдите.")

	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	service = ChannelsService(db, gateway)
	dto = await service.add_channel(bot_id, "@testchan")
	assert dto.userbot_admin is True
	offline = ChannelsService(db, _OfflineGateway())
	access = await offline.recheck_channel(dto.id)
	assert access.userbot_ok is None  # «не удалось проверить», не «не админ»
	assert access.channel.userbot_admin is True  # флаг не тронут


async def test_assign_and_unassign_bot(db: Database) -> None:
	"""Каналу без бота назначается бот (с проверкой прав) и отвязывается."""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	service = ChannelsService(db, gateway)
	dto = await service.add_channel_via_userbot("@testchan")
	assert dto.bot_id is None
	# без прав — не назначается
	gateway.bot_is_admin = False
	with pytest.raises(ChannelCheckError, match="нет права"):
		await service.assign_bot(dto.id, bot_id)
	# с правами — назначается
	gateway.bot_is_admin = True
	updated = await service.assign_bot(dto.id, bot_id)
	assert updated.bot_id == bot_id and updated.bot_label == "Публикатор"
	# отвязка: бот исчезает, userbot-флаг не трогается
	updated = await service.unassign_bot(dto.id)
	assert updated.bot_id is None and updated.userbot_admin is True


async def test_unknown_bot_rejected(db: Database) -> None:
	"""Подключение с несуществующим ботом — понятная ошибка."""
	service = ChannelsService(db, _FakeGateway())
	with pytest.raises(ChannelError, match="Бот не найден"):
		await service.add_channel(999, "@testchan")


def test_normalize_chat_ref() -> None:
	"""Все форматы ввода приводятся к виду для API Telegram."""
	assert normalize_chat_ref("@mychannel") == "@mychannel"
	assert normalize_chat_ref("mychannel") == "@mychannel"
	assert normalize_chat_ref("https://t.me/mychannel") == "@mychannel"
	assert normalize_chat_ref("t.me/mychannel/") == "@mychannel"
	assert normalize_chat_ref("-1001234567") == -1001234567
	with pytest.raises(ChatRefError):
		normalize_chat_ref("   ")


def test_normalize_chat_ref_hardened() -> None:
	"""Пробелы в ID, ссылки t.me/c/… и инвайт-ссылки (правки 2026-07-12)."""
	assert normalize_chat_ref("-100 2233 445 566") == -1002233445566
	assert normalize_chat_ref(" -1002233445566 ") == -1002233445566
	assert normalize_chat_ref("https://t.me/c/2233445566/5") == -1002233445566
	assert normalize_chat_ref("t.me/c/2233445566") == -1002233445566
	with pytest.raises(ChatRefError, match="Инвайт"):
		normalize_chat_ref("https://t.me/+AbCdEfGh123")
	with pytest.raises(ChatRefError, match="t.me/c"):
		normalize_chat_ref("t.me/c/abc/5")


def test_describe_update() -> None:
	"""Описание событий бота: членство, пост в канале, прочее — None."""
	from datetime import datetime

	from pxcontrol.engine.telegram.bot_api import describe_update

	membership = SimpleNamespace(
		date=datetime(2026, 7, 12, 16, 30),
		chat=SimpleNamespace(title="Мой канал", type="channel", id=-1004344346478),
		new_chat_member=SimpleNamespace(
			status="administrator", can_post_messages=True
		),
	)
	line = describe_update(SimpleNamespace(my_chat_member=membership, channel_post=None))
	assert line is not None
	assert "Мой канал" in line and "administrator" in line and "есть" in line

	post = SimpleNamespace(
		date=datetime(2026, 7, 12, 16, 31),
		chat=SimpleNamespace(title="Мой канал", id=-1004344346478),
	)
	line = describe_update(SimpleNamespace(my_chat_member=None, channel_post=post))
	assert line is not None and "пост в канале" in line

	assert describe_update(SimpleNamespace(my_chat_member=None, channel_post=None)) is None


async def test_channel_enabled_comes_from_settings(db: Database) -> None:
	"""Флаг активности канала в DTO читается из настроек (умолчание — True)."""
	service = ChannelsService(db, _FakeGateway())
	channel = await service.add_channel_via_userbot("@chan")
	assert channel.enabled is True
	await SettingsService(db).set_for(CHANNEL_ENABLED, channel.id, False)
	listed = await service.list_channels()
	assert [ch.enabled for ch in listed] == [False]


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
