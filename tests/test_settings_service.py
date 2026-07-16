"""Тесты сервиса настроек (ADR-0013): реестр ключей, оба владельца, кэш."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AppSetting, Channel, ChannelSetting
from pxcontrol.engine.services.channels import ChannelsService
from pxcontrol.engine.services.settings import (
	CHANNEL_DEFAULT_PRESET,
	CHANNEL_ENABLED,
	FFMPEG_PATH,
	PUBLISH_LAST_CHANNEL_ID,
	PUBLISH_TIMES,
	THEME_DARK,
	SettingsError,
	SettingsService,
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
	await database.init()
	yield database
	await database.close()


async def _add_channel(db: Database, tg_chat_id: str = "-1001") -> int:
	"""Создаёт канал, возвращает id."""
	async with db.session_factory() as session:
		channel = Channel(title="Канал", tg_chat_id=tg_chat_id)
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
		return channel.id


async def test_app_defaults_and_roundtrip(db: Database) -> None:
	"""Не задано — умолчание из реестра; запись и чтение — круговой путь."""
	service = SettingsService(db)
	assert await service.get(THEME_DARK) is True
	assert await service.get(FFMPEG_PATH) == ""
	await service.set(THEME_DARK, False)
	await service.set(FFMPEG_PATH, "/opt/ffmpeg/ffmpeg")
	assert await service.get(THEME_DARK) is False
	assert await service.get(FFMPEG_PATH) == "/opt/ffmpeg/ffmpeg"
	# перезапись существующего значения
	await service.set(FFMPEG_PATH, "ffmpeg")
	assert await service.get(FFMPEG_PATH) == "ffmpeg"


async def test_none_resets_to_default(db: Database) -> None:
	"""Запись None удаляет строку — настройка возвращается к умолчанию."""
	service = SettingsService(db)
	await service.set(PUBLISH_LAST_CHANNEL_ID, 7)
	assert await service.get(PUBLISH_LAST_CHANNEL_ID) == 7
	await service.set(PUBLISH_LAST_CHANNEL_ID, None)
	assert await service.get(PUBLISH_LAST_CHANNEL_ID) is None


async def test_invalid_stored_value_falls_back(db: Database) -> None:
	"""Битое значение в БД не роняет чтение — умолчание с предупреждением."""
	async with db.session_factory() as session:
		session.add(AppSetting(name=THEME_DARK.name, value="не булево"))
		await session.commit()
	service = SettingsService(db)
	assert await service.get(THEME_DARK) is True  # умолчание


async def test_cached_and_prime(db: Database) -> None:
	"""prime() наполняет кэш; cached() — синхронно, обновляется записью."""
	service = SettingsService(db)
	assert service.cached(FFMPEG_PATH) == ""  # до prime — умолчание
	await service.set(FFMPEG_PATH, "/usr/bin/ffmpeg")
	assert service.cached(FFMPEG_PATH) == "/usr/bin/ffmpeg"
	fresh = SettingsService(db)  # новый экземпляр — как при старте движка
	await fresh.prime()
	assert fresh.cached(FFMPEG_PATH) == "/usr/bin/ffmpeg"


async def test_channel_scope_roundtrip_and_cleanup(db: Database) -> None:
	"""Настройка канала: круговой путь, сброс, чистка при удалении канала."""
	service = SettingsService(db)
	channel_id = await _add_channel(db)
	assert await service.get_for(CHANNEL_DEFAULT_PRESET, channel_id) is None
	await service.set_for(CHANNEL_DEFAULT_PRESET, channel_id, 5)
	assert await service.get_for(CHANNEL_DEFAULT_PRESET, channel_id) == 5
	await service.set_for(CHANNEL_DEFAULT_PRESET, channel_id, None)
	assert await service.get_for(CHANNEL_DEFAULT_PRESET, channel_id) is None
	# удаление канала уносит его настройки (страховка сервиса)
	await service.set_for(CHANNEL_DEFAULT_PRESET, channel_id, 5)

	class _Gateway:  # ChannelsService для удаления шлюз не использует
		pass

	channels = ChannelsService(db, _Gateway())  # type: ignore[arg-type]
	await channels.delete_channel(channel_id)
	assert await service.get_for(CHANNEL_DEFAULT_PRESET, channel_id) is None


async def test_channel_scope_requires_existing_channel(db: Database) -> None:
	"""Запись настройки несуществующему каналу — понятная ошибка."""
	service = SettingsService(db)
	with pytest.raises(SettingsError, match="Канал не найден"):
		await service.set_for(CHANNEL_DEFAULT_PRESET, 999, 1)


async def test_scope_mismatch_is_error(db: Database) -> None:
	"""Ключ приложения нельзя читать методами канала и наоборот."""
	service = SettingsService(db)
	with pytest.raises(SettingsError, match="принадлежит"):
		await service.get_for(THEME_DARK, 1)
	with pytest.raises(SettingsError, match="принадлежит"):
		await service.get(CHANNEL_DEFAULT_PRESET)


async def test_bool_does_not_pass_as_int(db: Database) -> None:
	"""True в БД не сходит за целое: у int-ключа откат к умолчанию."""
	async with db.session_factory() as session:
		session.add(AppSetting(name=PUBLISH_LAST_CHANNEL_ID.name, value=True))
		await session.commit()
	service = SettingsService(db)
	assert await service.get(PUBLISH_LAST_CHANNEL_ID) is None


async def test_set_rejects_wrong_type(db: Database) -> None:
	"""Запись значения не того типа отклоняется в обеих областях."""
	service = SettingsService(db)
	with pytest.raises(SettingsError, match="не подходит по типу"):
		await service.set(THEME_DARK, "тьма")  # type: ignore[arg-type]
	channel_id = await _add_channel(db)
	with pytest.raises(SettingsError, match="не подходит по типу"):
		# 1 — не bool: подкласс-ловушку проверяем в обе стороны
		await service.set_for(CHANNEL_ENABLED, channel_id, 1)  # type: ignore[arg-type]


async def test_get_for_all_returns_only_stored(db: Database) -> None:
	"""Пакетное чтение отдаёт строки только заданных каналов."""
	service = SettingsService(db)
	first = await _add_channel(db, "-1001")
	await _add_channel(db, "-1002")  # без настройки — читается умолчанием
	await service.set_for(CHANNEL_ENABLED, first, False)
	assert await service.get_for_all(CHANNEL_ENABLED) == {first: False}


async def test_list_setting_roundtrip_keeps_order(db: Database) -> None:
	"""Списковый ключ: круговой путь с сохранением порядка, [] по умолчанию."""
	service = SettingsService(db)
	channel_id = await _add_channel(db)
	assert await service.get_for(PUBLISH_TIMES, channel_id) == []
	await service.set_for(PUBLISH_TIMES, channel_id, ["18:30", "10:00"])
	assert await service.get_for(PUBLISH_TIMES, channel_id) == ["18:30", "10:00"]
	# не-список в БД → откат к умолчанию
	async with db.session_factory() as session:
		row = await session.get(ChannelSetting, (channel_id, PUBLISH_TIMES.name))
		assert row is not None
		row.value = "10:00"
		await session.commit()
	assert await service.get_for(PUBLISH_TIMES, channel_id) == []


async def test_drop_channel_value_removes_matching_refs(db: Database) -> None:
	"""Снятие настройки-ссылки задевает только каналы с этим значением."""
	service = SettingsService(db)
	first = await _add_channel(db, "-1001")
	second = await _add_channel(db, "-1002")
	await service.set_for(CHANNEL_DEFAULT_PRESET, first, 5)
	await service.set_for(CHANNEL_DEFAULT_PRESET, second, 7)
	await service.drop_channel_value(CHANNEL_DEFAULT_PRESET, 5)
	assert await service.get_for(CHANNEL_DEFAULT_PRESET, first) is None
	assert await service.get_for(CHANNEL_DEFAULT_PRESET, second) == 7
