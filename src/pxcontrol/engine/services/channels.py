"""Сервис каналов: подключение с проверкой прав бота, список, удаление."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Channel
from pxcontrol.engine.telegram.bot_api import ChannelInfo

logger = logging.getLogger(__name__)


class ChannelError(Exception):
	"""Ошибка операций с каналами (с понятным человеку текстом)."""


class _ChannelChecker(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo: ...


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


class ChannelsService:
	"""Подключение каналов, проверка прав бота и хранение настроек."""

	def __init__(self, db: Database, gateway: _ChannelChecker) -> None:
		self._db = db
		self._gateway = gateway

	async def list_channels(self) -> list[ChannelDto]:
		"""Возвращает все подключённые каналы (с именем бота)."""
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(Channel)
					.options(selectinload(Channel.bot))
					.order_by(Channel.id)
				)
			).scalars()
			return [self._dto(ch) for ch in rows]

	async def add_channel(self, bot_id: int, chat_ref: str) -> ChannelDto:
		"""Проверяет канал через Telegram и сохраняет его.

		Порядок: бот существует → канал доступен и бот в нём админ
		с правом публикации → дубликата нет → сохранить.

		Raises:
			ChannelError: Бот не найден или канал уже подключён.
			ChannelCheckError: Канал не прошёл проверку Telegram.
			ConnectionError: Нет связи с Telegram.
		"""
		bot = await self._get_bot(bot_id)
		info = await self._gateway.check_channel(bot.token, chat_ref)
		async with self._db.session_factory() as session:
			existing = await session.execute(
				select(Channel.id).where(Channel.tg_chat_id == info.chat_id)
			)
			if existing.scalar_one_or_none() is not None:
				raise ChannelError(f"Канал «{info.title}» уже подключён.")
			channel = Channel(
				title=info.title,
				tg_chat_id=info.chat_id,
				username=info.username,
				bot_id=bot.id,
			)
			session.add(channel)
			await session.commit()
			await session.refresh(channel)
		logger.info("Подключён канал «%s» (бот %s).", info.title, bot.label)
		return self._dto(channel, bot_label=bot.label)

	async def delete_channel(self, channel_id: int) -> None:
		"""Удаляет канал по идентификатору (из приложения, не из Telegram)."""
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
	def _dto(channel: Channel, bot_label: str | None = None) -> ChannelDto:
		if bot_label is None and channel.bot is not None:
			bot_label = channel.bot.label
		return ChannelDto(
			channel.id,
			channel.title,
			channel.username,
			channel.tg_chat_id,
			channel.bot_id,
			bot_label,
			channel.enabled,
		)
