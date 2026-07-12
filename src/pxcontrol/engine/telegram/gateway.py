"""Единая точка доступа к Telegram поверх двух транспортов (ADR-0007).

Остальной код не знает, каким транспортом выполнена операция. Ориентир:
публикация «сейчас» — Bot API, отложенные посты и чтение — MTProto.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pxcontrol.config import Settings
from pxcontrol.engine.telegram.bot_api import (
	BotApiTransport,
	ChannelInfo,
	check_channel,
	check_token,
	get_bot_events,
	send_text,
)
from pxcontrol.engine.telegram.mtproto import MtprotoLoginManager, MtprotoTransport

logger = logging.getLogger(__name__)


class TelegramGateway:
	"""Объединяет транспорты Bot API и MTProto за общим интерфейсом."""

	def __init__(self, settings: Settings) -> None:
		# Реквизиты берутся из БД (таблицы bots / tg_accounts, ADR-0009):
		# движок активирует userbot при старте, боты — по токену на операцию.
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

	async def activate_userbot(self, api_id: int, api_hash: str, session: str) -> None:
		"""Настраивает и подключает userbot (при старте или после входа)."""
		self.mtproto.configure(api_id, api_hash, session)
		await self.mtproto.start()

	# --- Bot API ---------------------------------------------------------------

	async def check_bot_token(self, token: str) -> str:
		"""Проверяет токен бота через getMe и возвращает его @имя."""
		return await check_token(token)

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo:
		"""Проверяет канал и права бота в нём (getChat + getChatMember)."""
		return await check_channel(token, chat_ref)

	async def bot_events(self, token: str) -> list[str]:
		"""Диагностика: события бота за 24 ч (getUpdates, без удаления)."""
		return await get_bot_events(token)

	async def send_text(self, token: str, chat_id: str, text: str) -> int:
		"""Публикует текстовый пост «сейчас» через бота."""
		return await send_text(token, chat_id, text)

	# --- MTProto (userbot) -------------------------------------------------------

	async def schedule_post(self, chat_id: str, text: str, when: datetime) -> None:
		"""Создаёт отложенную запись прямо в канале (ADR-0010)."""
		await self.mtproto.schedule_post(chat_id, text, when)

	async def get_scheduled(self, chat_id: str) -> list[Any]:
		"""Читает отложенные записи канала из Telegram."""
		return await self.mtproto.get_scheduled(chat_id)

	async def read_channel(self, username: str, limit: int = 20) -> list[Any]:
		"""Читает канал-источник (через MTProto)."""
		return await self.mtproto.read_channel(username, limit)
