"""Единая точка доступа к Telegram поверх двух транспортов (ADR-0007).

Остальной код не знает, каким транспортом выполнена операция. Ориентир:
публикация любого контента и чтение — MTProto (постоянно подключённый
userbot, ADR-0011); Bot API — проверки, диагностика и запасная публикация
для каналов без userbot-админа (текст и медиа до 50 МБ, только «сейчас»).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pxcontrol.engine.telegram.bot_api import (
	ChannelInfo,
	check_channel,
	check_token,
	get_bot_events,
	send_media,
	send_text,
)
from pxcontrol.engine.telegram.mtproto import MtprotoLoginManager, MtprotoTransport
from pxcontrol.engine.telegram.types import (
	MediaKind,
	OutgoingPost,
	ScheduledMessage,
	UserbotChannelInfo,
)

logger = logging.getLogger(__name__)


class TelegramGateway:
	"""Объединяет транспорты Bot API и MTProto за общим интерфейсом."""

	def __init__(self) -> None:
		# Реквизиты берутся из БД (таблицы bots / tg_accounts, ADR-0009):
		# движок активирует userbot при старте, боты — по токену на операцию.
		self.mtproto = MtprotoTransport()
		self.login = MtprotoLoginManager()

	async def start(self) -> None:
		"""Подключает userbot (если настроен). Bot API соединений не держит."""
		await self.mtproto.start()

	async def stop(self) -> None:
		"""Останавливает подключения."""
		await self.mtproto.stop()

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

	async def send_media(
		self, token: str, chat_id: str, kind: MediaKind, path: str, caption: str
	) -> int:
		"""Отправляет медиа ботом (запасной транспорт, лимит 50 МБ)."""
		return await send_media(token, chat_id, kind, path, caption)

	# --- MTProto (userbot) -------------------------------------------------------

	async def check_channel_userbot(self, chat_ref: str) -> UserbotChannelInfo:
		"""Проверяет канал и права userbot (админ + право публиковать)."""
		return await self.mtproto.check_channel(chat_ref)

	async def publish(
		self,
		chat_id: str,
		post: OutgoingPost,
		on_progress: Callable[[float], None] | None = None,
	) -> None:
		"""Публикует пост любого типа через userbot (ADR-0011).

		Текст или медиа с подписью; сразу (when=None) или отложенно —
		отложенные хранит и публикует сервер Telegram (ADR-0010).
		"""
		await self.mtproto.publish(chat_id, post, on_progress)

	async def get_scheduled(self, chat_id: str) -> list[ScheduledMessage]:
		"""Читает отложенные записи канала из Telegram."""
		return await self.mtproto.get_scheduled(chat_id)
