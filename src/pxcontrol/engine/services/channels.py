"""Сервис каналов: подключение (бот или userbot), список, удаление."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Channel
from pxcontrol.engine.services.settings import CHANNEL_ENABLED, SettingsService
from pxcontrol.engine.telegram.mtproto import UserbotAccessError
from pxcontrol.engine.telegram.types import ChannelInfo

logger = logging.getLogger(__name__)


class ChannelError(Exception):
	"""Ошибка операций с каналами (с понятным человеку текстом)."""


class _ChannelChecker(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo: ...

	async def check_channel_userbot(self, chat_ref: str) -> ChannelInfo: ...


@dataclass(frozen=True)
class ChannelDto:
	"""Канал для показа в интерфейсе."""

	id: int
	title: str
	username: str | None
	tg_chat_id: str
	bot_id: int | None
	bot_label: str | None
	enabled: bool
	userbot_admin: bool = False


@dataclass(frozen=True)
class ChannelAccess:
	"""Итог перепроверки доступов канала.

	Attributes:
		channel: канал с обновлённым флагом userbot.
		userbot_ok: userbot — админ с правом публиковать
			(None — проверить не удалось: нет связи или userbot отключён).
		bot_ok: права бота на месте (None — бот не назначен).
	"""

	channel: ChannelDto
	userbot_ok: bool | None
	bot_ok: bool | None


class ChannelsService:
	"""Подключение каналов, проверка прав бота и хранение настроек."""

	def __init__(
		self,
		db: Database,
		gateway: _ChannelChecker,
		settings: SettingsService | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно:
		настройки каналов не кэшируются)."""
		self._db = db
		self._gateway = gateway
		self._settings = settings if settings is not None else SettingsService(db)

	async def list_channels(self) -> list[ChannelDto]:
		"""Возвращает все подключённые каналы (с именем бота)."""
		enabled = await self._settings.get_for_all(CHANNEL_ENABLED)
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(Channel)
					.options(selectinload(Channel.bot))
					.order_by(Channel.id)
				)
			).scalars()
			return [
				self._dto(ch, enabled=enabled.get(ch.id, CHANNEL_ENABLED.default))
				for ch in rows
			]

	async def add_channel(self, bot_id: int, chat_ref: str) -> ChannelDto:
		"""Подключает канал через бота (с попутной проверкой userbot).

		Порядок: бот существует → канал доступен и бот в нём админ
		с правом публикации → дубликата нет → сохранить. Попутно
		спрашиваем userbot: если он тоже админ — канал администрируется
		обоими способами.

		Raises:
			ChannelError: Бот не найден или канал уже подключён.
			ChannelCheckError: Канал не прошёл проверку Telegram.
			ConnectionError: Нет связи с Telegram.
		"""
		bot = await self._get_bot(bot_id)
		logger.info(
			"Подключаю канал: ввод %r, бот «%s» (@%s, id=%s).",
			chat_ref, bot.label, bot.username, bot.id,
		)
		info = await self._gateway.check_channel(bot.token, chat_ref)
		# при подключении «не удалось проверить» считаем отсутствием прав:
		# флаг поднимет перепроверка доступов, когда userbot появится
		userbot_admin = await self._probe_userbot(info.chat_id) is True
		channel = await self._store_channel(
			title=info.title, tg_chat_id=info.chat_id, username=info.username,
			bot_id=bot.id, userbot_admin=userbot_admin,
		)
		logger.info(
			"Подключён канал «%s» (бот %s, userbot админ: %s).",
			info.title, bot.label, userbot_admin,
		)
		return self._dto(channel, bot_label=bot.label)

	async def add_channel_via_userbot(self, chat_ref: str) -> ChannelDto:
		"""Подключает канал через userbot — бот в канале не нужен.

		Raises:
			ChannelError: Канал уже подключён.
			UserbotUnavailableError: Userbot не подключён, не админ или без
				права публиковать.
		"""
		logger.info("Подключаю канал через userbot: ввод %r.", chat_ref)
		info = await self._gateway.check_channel_userbot(chat_ref)
		channel = await self._store_channel(
			title=info.title, tg_chat_id=info.chat_id, username=info.username,
			bot_id=None, userbot_admin=True,
		)
		logger.info("Подключён канал «%s» (userbot).", info.title)
		return self._dto(channel)

	async def _probe_userbot(self, chat_id: str) -> bool | None:
		"""Попутно проверяет, админ ли userbot (сбой не мешает подключению).

		Returns:
			True/False — Telegram подтвердил наличие/отсутствие прав;
			None — проверить не удалось (нет связи, userbot отключён):
			это не знание о правах, менять хранимый флаг по нему нельзя.
		"""
		try:
			await self._gateway.check_channel_userbot(chat_id)
		except UserbotAccessError:
			logger.info("Userbot не админ канала %s.", chat_id)
			return False
		except Exception:  # noqa: BLE001 — вспомогательная проверка
			logger.info(
				"Проверка userbot канала %s не удалась (сеть или подключение).",
				chat_id,
			)
			return None
		return True

	async def _store_channel(
		self, *, title: str, tg_chat_id: str, username: str | None,
		bot_id: int | None, userbot_admin: bool,
	) -> Channel:
		"""Сохраняет канал, отклоняя дубликат.

		Raises:
			ChannelError: Канал уже подключён.
		"""
		async with self._db.session_factory() as session:
			existing = await session.execute(
				select(Channel.id).where(Channel.tg_chat_id == tg_chat_id)
			)
			if existing.scalar_one_or_none() is not None:
				raise ChannelError(f"Канал «{title}» уже подключён.")
			channel = Channel(
				title=title, tg_chat_id=tg_chat_id, username=username,
				bot_id=bot_id, userbot_admin=userbot_admin,
			)
			session.add(channel)
			await session.commit()
			await session.refresh(channel)
		return channel

	async def recheck_channel(self, channel_id: int) -> ChannelAccess:
		"""Перепроверяет оба способа администрирования канала.

		Флаг userbot обновляется в обе стороны (права могли отозвать),
		но только по подтверждённому ответу Telegram: сбой связи или
		отключённый userbot — не повод записывать «не админ» (иначе
		канал молча терял бы отложенные посты и большие файлы).
		Потеря прав бота его не отвязывает — только сообщается: бота
		могут вернуть в канал.

		Raises:
			ChannelError: Канал не найден.
		"""
		async with self._db.session_factory() as session:
			channel = (
				await session.execute(
					select(Channel).options(selectinload(Channel.bot))
					.where(Channel.id == channel_id)
				)
			).scalar_one_or_none()
			if channel is None:
				raise ChannelError("Канал не найден — обновите список.")
			bot_label = channel.bot.label if channel.bot is not None else None
			bot_token = channel.bot.token if channel.bot is not None else None
			userbot_ok = await self._probe_userbot(channel.tg_chat_id)
			bot_ok: bool | None = None
			if bot_token is not None:
				bot_ok = await self._probe_bot(bot_token, channel.tg_chat_id)
			if userbot_ok is not None:
				channel.userbot_admin = userbot_ok
			await session.commit()
			await session.refresh(channel)
			enabled = await self._settings.get_for(CHANNEL_ENABLED, channel_id)
			dto = self._dto(channel, bot_label=bot_label, enabled=enabled)
		logger.info(
			"Доступы канала «%s»: userbot=%s, бот=%s.",
			dto.title, userbot_ok, bot_ok,
		)
		return ChannelAccess(dto, userbot_ok, bot_ok)

	async def assign_bot(self, channel_id: int, bot_id: int) -> ChannelDto:
		"""Назначает каналу бота (с проверкой его прав в канале).

		Raises:
			ChannelError: Канал или бот не найдены.
			ChannelCheckError: Бот не админ канала / без права публиковать.
		"""
		bot = await self._get_bot(bot_id)
		async with self._db.session_factory() as session:
			channel = await session.get(Channel, channel_id)
			if channel is None:
				raise ChannelError("Канал не найден — обновите список.")
			chat_id = channel.tg_chat_id
		await self._gateway.check_channel(bot.token, chat_id)
		async with self._db.session_factory() as session:
			channel = await session.get(Channel, channel_id)
			if channel is None:
				raise ChannelError("Канал не найден — обновите список.")
			channel.bot_id = bot.id
			await session.commit()
			await session.refresh(channel)
			enabled = await self._settings.get_for(CHANNEL_ENABLED, channel_id)
			dto = self._dto(channel, bot_label=bot.label, enabled=enabled)
		logger.info("Каналу «%s» назначен бот «%s».", dto.title, bot.label)
		return dto

	async def unassign_bot(self, channel_id: int) -> ChannelDto:
		"""Отвязывает бота от канала (сам бот остаётся в приложении).

		Raises:
			ChannelError: Канал не найден.
		"""
		async with self._db.session_factory() as session:
			channel = await session.get(Channel, channel_id)
			if channel is None:
				raise ChannelError("Канал не найден — обновите список.")
			channel.bot_id = None
			await session.commit()
			await session.refresh(channel)
			enabled = await self._settings.get_for(CHANNEL_ENABLED, channel_id)
			dto = self._dto(channel, enabled=enabled)
		logger.info("От канала «%s» отвязан бот.", dto.title)
		return dto

	async def _probe_bot(self, token: str, chat_id: str) -> bool:
		"""Проверяет права бота, не роняя перепроверку."""
		try:
			await self._gateway.check_channel(token, chat_id)
		except Exception:  # noqa: BLE001 — итог отражается в ответе
			return False
		return True

	async def delete_channel(self, channel_id: int) -> None:
		"""Удаляет канал со всем хозяйством (из приложения, не из Telegram).

		Настройки и подписи канала убирают каскады внешних ключей:
		политики объявлены в схеме, проверку ключей включает ``Database``
		на каждом соединении.
		"""
		async with self._db.session_factory() as session:
			await session.execute(delete(Channel).where(Channel.id == channel_id))
			await session.commit()

	async def _get_bot(self, bot_id: int) -> Bot:
		"""Возвращает бота или объясняет, что он не найден."""
		async with self._db.session_factory() as session:
			bot = await session.get(Bot, bot_id)
		if bot is None:
			raise ChannelError("Бот не найден — добавьте его в Настройках.")
		return bot

	@staticmethod
	def _dto(
		channel: Channel, bot_label: str | None = None, enabled: bool = True
	) -> ChannelDto:
		"""Снимок канала; ``enabled`` — из настроек (у нового канала — True)."""
		if bot_label is None and channel.bot_id is not None and channel.bot is not None:
			bot_label = channel.bot.label
		return ChannelDto(
			channel.id,
			channel.title,
			channel.username,
			channel.tg_chat_id,
			channel.bot_id,
			bot_label,
			enabled,
			channel.userbot_admin,
		)
