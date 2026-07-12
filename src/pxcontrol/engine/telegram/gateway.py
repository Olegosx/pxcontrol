"""Единая точка доступа к Telegram поверх двух транспортов (ADR-0007).

Остальной код не знает, каким транспортом выполнена операция. Ориентир:
публикация — Bot API, чтение каналов — MTProto; жёсткой границы нет.
"""

from __future__ import annotations

import logging
from typing import Any

from pxcontrol.config import Settings
from pxcontrol.engine.telegram.bot_api import (
	BotApiTransport,
	ChannelInfo,
	check_channel,
	check_token,
	get_bot_events,
)
from pxcontrol.engine.telegram.mtproto import MtprotoLoginManager, MtprotoTransport

logger = logging.getLogger(__name__)


class TelegramGateway:
	"""Объединяет транспорты Bot API и MTProto за общим интерфейсом."""

	def __init__(self, settings: Settings) -> None:
		# Реквизиты берутся из БД (таблицы bots / tg_accounts, ADR-0009);
		# подключение конкретных бота и аккаунта — при реализации каналов.
		self._settings = settings
		self.bot_api = BotApiTransport(token=None)
		self.mtproto = MtprotoTransport()
		self.login = MtprotoLoginManager()

	async def start(self) -> None:
		"""Запускает оба транспорта."""
		await self.bot_api.start()
		await self.mtproto.start()

	async def stop(self) -> None:
		"""Останавливает оба транспорта (в обратном порядке)."""
		await self.mtproto.stop()
		await self.bot_api.stop()

	async def check_bot_token(self, token: str) -> str:
		"""Проверяет токен бота через getMe и возвращает его @имя."""
		return await check_token(token)

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo:
		"""Проверяет канал и права бота в нём (getChat + getChatMember)."""
		return await check_channel(token, chat_ref)

	async def bot_events(self, token: str) -> list[str]:
		"""Диагностика: события бота за 24 ч (getUpdates, без удаления)."""
		return await get_bot_events(token)

	async def publish(self, chat_id: str, text: str, video_path: str | None = None) -> None:
		"""Публикует пост (по умолчанию через Bot API)."""
		await self.bot_api.publish(chat_id, text, video_path)

	async def read_channel(self, username: str, limit: int = 20) -> list[Any]:
		"""Читает канал-источник (через MTProto)."""
		return await self.mtproto.read_channel(username, limit)
