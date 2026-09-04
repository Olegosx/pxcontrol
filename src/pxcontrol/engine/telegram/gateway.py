"""Единая точка доступа к Telegram поверх двух транспортов (ADR-0007).

Остальной код не знает, каким транспортом выполнена операция. Ориентир:
публикация любого контента и чтение — MTProto (userbot, ADR-0011);
Bot API — проверки, диагностика и запасная публикация для каналов без
userbot-админа (текст и медиа до 50 МБ, только «сейчас»).

Userbot-аккаунтов может быть несколько — по одному на канал-админа
(ADR-0019): шлюз держит пул клиентов MTProto «id аккаунта → транспорт»,
и каждая userbot-операция адресуется конкретному аккаунту. Лимиты
Telegram (флуд, Premium) — пер-аккаунтные, транспорты независимы.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pxcontrol.engine.telegram.bot_api import (
	check_channel,
	check_token,
	get_bot_events,
	send_media,
	send_text,
)
from pxcontrol.engine.telegram.mtproto import (
	MtprotoLoginManager,
	MtprotoTransport,
	UserbotNotConnectedError,
)
from pxcontrol.engine.telegram.types import (
	ChannelInfo,
	MediaKind,
	OutgoingPost,
	ScheduledMessage,
)

logger = logging.getLogger(__name__)


class TelegramGateway:
	"""Объединяет транспорты Bot API и MTProto за общим интерфейсом."""

	def __init__(self) -> None:
		# Реквизиты берутся из БД (ключ API — ADR-0018, сессии — tg_accounts):
		# движок активирует userbot-аккаунты при старте, боты — по токену
		# на операцию. Пул транспортов: id аккаунта → клиент MTProto.
		self._userbots: dict[int, MtprotoTransport] = {}
		self.login = MtprotoLoginManager()
		# точка подмены в тестах: фабрика транспорта с подставным клиентом
		self.transport_factory: Callable[[], MtprotoTransport] = MtprotoTransport

	async def stop(self) -> None:
		"""Останавливает подключения (включая незавершённые входы)."""
		await self.login.cancel_all()
		for transport in self._userbots.values():
			await transport.stop()
		self._userbots.clear()

	async def activate_userbot(
		self, account_id: int, api_id: int, api_hash: str, session: str
	) -> None:
		"""Настраивает и (пере)подключает userbot аккаунта (старт или вход).

		Прежний клиент этого аккаунта закрывается: новые реквизиты
		(повторный вход) должны применяться без перезапуска приложения.
		Транспорт регистрируется в пуле до подключения: неудача старта
		(нет сети) не выкидывает аккаунт — первая же операция чинит
		соединение сама (самопочинка транспорта).

		Raises:
			UserbotNotConnectedError: Соединение с Telegram не удалось.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
		"""
		old = self._userbots.pop(account_id, None)
		if old is not None:
			await old.stop()
		transport = self.transport_factory()
		transport.configure(api_id, api_hash, session)
		self._userbots[account_id] = transport
		await transport.start()

	async def deactivate_userbot(self, account_id: int) -> None:
		"""Отключает userbot аккаунта (например, после его удаления)."""
		transport = self._userbots.pop(account_id, None)
		if transport is not None:
			await transport.stop()

	def userbot_premium(self, account_id: int | None) -> bool:
		"""Есть ли у аккаунта подписка Premium (лимит файла 2000/4000 МиБ).

		None или неактивированный аккаунт — False: действует меньший,
		безопасный лимит.
		"""
		if account_id is None:
			return False
		transport = self._userbots.get(account_id)
		return transport.premium if transport is not None else False

	def any_userbot_premium(self) -> bool:
		"""Есть ли Premium хоть у одного подключённого аккаунта.

		Эвристика для подсказок без контекста канала (рекомендация
		битрейта на «Видео»: очередь обработки канала не знает).
		Строгая пер-канальная проверка лимита остаётся за публикацией.
		"""
		return any(t.premium for t in self._userbots.values())

	def _userbot(self, account_id: int) -> MtprotoTransport:
		"""Транспорт аккаунта из пула — или понятная ошибка.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован (нет сессии
				или ключа API) — нужен вход: Настройки → Аккаунты.
		"""
		transport = self._userbots.get(account_id)
		if transport is None:
			raise UserbotNotConnectedError(
				"Userbot этого канала не подключён — войдите в его аккаунт: Настройки → Аккаунты."
			)
		return transport

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

	async def check_channel_userbot(self, account_id: int, chat_ref: str) -> ChannelInfo:
		"""Проверяет канал и права аккаунта (админ + право публиковать)."""
		return await self._userbot(account_id).check_channel(chat_ref)

	async def publish(
		self,
		account_id: int,
		chat_id: str,
		post: OutgoingPost,
		on_progress: Callable[[float], None] | None = None,
	) -> None:
		"""Публикует пост из сессии привязанного к каналу аккаунта (ADR-0019).

		Текст или медиа с подписью; сразу (when=None) или отложенно —
		отложенные хранит и публикует сервер Telegram (ADR-0010).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotAccessError: Telegram подтвердил отсутствие прав/канала.
			UserbotScheduleFullError: Все слоты отложек канала заняты —
				очередь отправки возвращает пост в ожидание (ADR-0016).
			UserbotFloodError: Флуд-лимит «подождите N секунд» — очередь
				отправки ждёт названный срок и повторяет сама.
			UserbotUnavailableError: Прочие отказы Telegram (лимиты и т.п.).
		"""
		await self._userbot(account_id).publish(chat_id, post, on_progress)

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		"""Читает отложенные записи канала из Telegram (его аккаунтом)."""
		return await self._userbot(account_id).get_scheduled(chat_id)
