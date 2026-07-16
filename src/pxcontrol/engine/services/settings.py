"""Настройки в БД: реестр ключей и сервис (ADR-0013).

Хранение — «строка = имя → значение»: настройки приложения —
в ``app_settings``, настройки канала — в ``channel_settings`` (по строке
на канал × имя, с внешним ключом на канал). Состав, типы и умолчания
задаёт реестр ключей ниже; сервис принимает только объекты ключей,
поэтому мусорные имена не заводятся в принципе.

Значение хранится как JSON. Тип проверяется по ключу при чтении:
битое значение откатывается к умолчанию с предупреждением в логе.
Секретам здесь не место — они живут шифрованными колонками в таблицах
своих сущностей (ADR-0009).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar

from sqlalchemy import select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AppSetting, Channel, ChannelSetting

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class SettingsError(Exception):
	"""Ошибка работы с настройками (с понятным человеку текстом)."""


class SettingScope(StrEnum):
	"""Владелец настройки."""

	APP = "app"  # приложение в целом (одно значение)
	CHANNEL = "channel"  # конкретный канал (значение на канал)


@dataclass(frozen=True)
class SettingKey(Generic[_T]):
	"""Объявление настройки: имя, владелец, умолчание и тип значения.

	Единственный способ обратиться к настройке — объект ключа из реестра
	ниже; строковые имена наружу не выходят. ``value_type`` — допустимый
	тип хранимого значения (None допустим, если умолчание — None:
	запись значения None удаляет строку — «сброс к умолчанию»).
	"""

	name: str
	scope: SettingScope
	default: _T
	value_type: type | tuple[type, ...]


# --- реестр ключей -----------------------------------------------------------
# Добавить настройку = добавить ключ здесь (и потребителя).

#: Тёмная тема оформления.
THEME_DARK: SettingKey[bool] = SettingKey(
	"theme_dark", SettingScope.APP, True, bool
)

#: Состояние главного окна (Qt saveGeometry, base64); None — умолчания Qt.
WINDOW_GEOMETRY: SettingKey[str | None] = SettingKey(
	"window_geometry", SettingScope.APP, None, str
)

#: Канал, в который публиковали в прошлый раз (предвыбор на «Публикации»).
PUBLISH_LAST_CHANNEL_ID: SettingKey[int | None] = SettingKey(
	"publish_last_channel_id", SettingScope.APP, None, int
)

#: Путь к ffmpeg; пусто — бутстрап из .env / поиск в PATH.
FFMPEG_PATH: SettingKey[str] = SettingKey(
	"ffmpeg_path", SettingScope.APP, "", str
)

#: Пресет обработки видео по умолчанию для канала (id пресета).
CHANNEL_DEFAULT_PRESET: SettingKey[int | None] = SettingKey(
	"default_video_preset", SettingScope.CHANNEL, None, int
)

#: Стандартные времена публикации канала: список «ЧЧ:ММ», первое — по умолчанию.
#: Элементы валидирует интерфейс при сохранении; битые пропускаются при чтении.
PUBLISH_TIMES: SettingKey[list[str]] = SettingKey(
	"publish_times", SettingScope.CHANNEL, [], list
)

#: Папка исходных видео для обработки; пусто — media/source в папке приложения.
VIDEO_SOURCE_DIR: SettingKey[str] = SettingKey(
	"video_source_dir", SettingScope.APP, "", str
)

#: Папка результатов обработки; пусто — media/processed в папке приложения.
VIDEO_PROCESSED_DIR: SettingKey[str] = SettingKey(
	"video_processed_dir", SettingScope.APP, "", str
)

#: Папка опубликованных видео; пусто — media/published в папке приложения.
VIDEO_PUBLISHED_DIR: SettingKey[str] = SettingKey(
	"video_published_dir", SettingScope.APP, "", str
)

#: Канал активен: участвует в публикации и опросе расписания.
CHANNEL_ENABLED: SettingKey[bool] = SettingKey(
	"enabled", SettingScope.CHANNEL, True, bool
)


class SettingsService:
	"""Чтение и запись настроек приложения и каналов.

	Настройки приложения кэшируются в памяти движка (``prime()`` при
	старте): ``cached()`` даёт синхронный доступ для провайдеров —
	например, пути к ffmpeg внутри блокирующих участков сервисов.
	"""

	def __init__(self, db: Database) -> None:
		self._db = db
		self._cache: dict[str, Any] = {}

	# --- настройки приложения ------------------------------------------------

	async def prime(self) -> None:
		"""Загружает настройки приложения в кэш (вызывается при старте)."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(select(AppSetting))).scalars()
			self._cache = {row.name: row.value for row in rows}
		logger.info("Настройки приложения загружены (%d значений).", len(self._cache))

	async def get(self, key: SettingKey[_T]) -> _T:
		"""Возвращает настройку приложения (умолчание — если не задана)."""
		self._require_scope(key, SettingScope.APP)
		async with self._db.session_factory() as session:
			row = await session.get(AppSetting, key.name)
		if row is not None:
			self._cache[key.name] = row.value
		return self._validated(key, row.value if row is not None else None)

	async def set(self, key: SettingKey[_T], value: _T) -> None:
		"""Сохраняет настройку приложения (None — сброс к умолчанию).

		Raises:
			SettingsError: Значение не подходит ключу по типу.
		"""
		self._require_scope(key, SettingScope.APP)
		self._require_valid(key, value)
		async with self._db.session_factory() as session:
			row = await session.get(AppSetting, key.name)
			if value is None:
				if row is not None:
					await session.delete(row)
			elif row is None:
				session.add(AppSetting(name=key.name, value=value))
			else:
				row.value = value
			await session.commit()
		if value is None:
			self._cache.pop(key.name, None)
		else:
			self._cache[key.name] = value
		logger.info("Настройка %s сохранена.", key.name)

	def cached(self, key: SettingKey[_T]) -> _T:
		"""Настройка приложения из кэша (синхронно, без похода в БД).

		Кэш наполняется ``prime()`` при старте и обновляется записью;
		до ``prime()`` возвращаются умолчания.
		"""
		self._require_scope(key, SettingScope.APP)
		return self._validated(key, self._cache.get(key.name))

	# --- настройки каналов -----------------------------------------------------

	async def get_for(self, key: SettingKey[_T], channel_id: int) -> _T:
		"""Возвращает настройку канала (умолчание — если не задана)."""
		self._require_scope(key, SettingScope.CHANNEL)
		async with self._db.session_factory() as session:
			row = await session.get(ChannelSetting, (channel_id, key.name))
		return self._validated(key, row.value if row is not None else None)

	async def get_for_all(self, key: SettingKey[_T]) -> dict[int, _T]:
		"""Значения настройки по всем каналам одним запросом (для списков).

		Returns:
			Отображение «id канала → значение» только для каналов,
			у которых настройка задана; остальные — умолчание ключа.
		"""
		self._require_scope(key, SettingScope.CHANNEL)
		async with self._db.session_factory() as session:
			rows = (await session.execute(
				select(ChannelSetting).where(ChannelSetting.name == key.name)
			)).scalars()
			return {row.channel_id: self._validated(key, row.value) for row in rows}

	async def drop_channel_value(self, key: SettingKey[_T], value: _T) -> None:
		"""Снимает настройку со значением ``value`` у всех каналов.

		Целостность настроек-ссылок держит сервис (ADR-0013, вариант «а»):
		при удалении сущности, на которую ссылается настройка (например,
		пресета видео), ссылки чистятся этим методом. Сравнение — в коде:
		JSON-значения в SQL сравниваются ненадёжно, а строк немного.
		"""
		self._require_scope(key, SettingScope.CHANNEL)
		async with self._db.session_factory() as session:
			rows = (await session.execute(
				select(ChannelSetting).where(ChannelSetting.name == key.name)
			)).scalars().all()
			removed = 0
			for row in rows:
				if row.value == value:
					await session.delete(row)
					removed += 1
			await session.commit()
		if removed:
			logger.info(
				"Настройка %s со значением %r снята у %d канал(ов).",
				key.name, value, removed,
			)

	async def set_for(
		self, key: SettingKey[_T], channel_id: int, value: _T
	) -> None:
		"""Сохраняет настройку канала (None — сброс к умолчанию).

		Raises:
			SettingsError: Канал не найден или значение не подходит по типу.
		"""
		self._require_scope(key, SettingScope.CHANNEL)
		self._require_valid(key, value)
		async with self._db.session_factory() as session:
			if await session.get(Channel, channel_id) is None:
				raise SettingsError("Канал не найден — обновите список.")
			row = await session.get(ChannelSetting, (channel_id, key.name))
			if value is None:
				if row is not None:
					await session.delete(row)
			elif row is None:
				session.add(ChannelSetting(
					channel_id=channel_id, name=key.name, value=value
				))
			else:
				row.value = value
			await session.commit()
		logger.info("Настройка %s канала id=%s сохранена.", key.name, channel_id)

	# --- внутреннее -------------------------------------------------------------

	@staticmethod
	def _require_scope(key: SettingKey[Any], scope: SettingScope) -> None:
		"""Защита от вызова не того метода для ключа другого владельца."""
		if key.scope is not scope:
			raise SettingsError(
				f"Настройка «{key.name}» принадлежит {key.scope}, не {scope}."
			)

	@staticmethod
	def _matches(key: SettingKey[Any], value: Any) -> bool:
		"""Подходит ли значение ключу по типу.

		bool — подкласс int, поэтому проверяется отдельно: True не должен
		сходить за целое у int-ключа (и наоборот).
		"""
		if isinstance(value, bool):
			return key.value_type is bool
		return isinstance(value, key.value_type)

	@classmethod
	def _require_valid(cls, key: SettingKey[Any], value: Any) -> None:
		"""Отклоняет запись значения не того типа (None — сброс, допустим).

		Raises:
			SettingsError: Значение не подходит ключу по типу.
		"""
		if value is None or cls._matches(key, value):
			return
		raise SettingsError(
			f"Настройка «{key.name}»: значение {value!r} не подходит по типу."
		)

	@classmethod
	def _validated(cls, key: SettingKey[_T], raw: Any) -> _T:
		"""Проверяет тип значения из БД; битое — умолчание с предупреждением."""
		if raw is None:
			return key.default
		if cls._matches(key, raw):
			return raw  # type: ignore[no-any-return]  # тип проверен по ключу
		logger.warning(
			"Настройка %s: значение %r не подходит по типу — умолчание.",
			key.name, raw,
		)
		return key.default
