"""Единая точка доступа к Telegram поверх двух транспортов (ADR-0007).

Остальной код не знает, каким транспортом выполнена операция. Ориентир:
публикация любого контента и чтение — MTProto (userbot, ADR-0011);
Bot API — проверки, диагностика и запасная публикация для каналов без
userbot-админа (текст и медиа до 50 МБ, только «сейчас»).

Userbot-аккаунтов может быть несколько — по одному на канал-админа
(ADR-0019): шлюз держит пул клиентов MTProto «id аккаунта → транспорт»,
и каждая userbot-операция адресуется конкретному аккаунту. Лимиты
Telegram (флуд, Premium) — пер-аккаунтные, транспорты независимы.

Рядом с пулом транспортов — пул **дорожек** (ADR-0024,
:mod:`pxcontrol.engine.telegram.lane`): все userbot-операции аккаунта
идут по его дорожке, то есть по очереди, с зазором между запросами
и с общей заморозкой после флуд-лимита. Приоритет операции задаёт сам
шлюз — он знает, что публикация важнее фонового чтения (ADR-0017);
вызывающему указывать его не нужно. Жизненный цикл соединения
(:meth:`activate_userbot`, :meth:`deactivate_userbot`) дорожкой
не регулируется: она про поток запросов, а не про подключение.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from pxcontrol.engine.telegram.bot_api import (
	check_community,
	check_token,
	get_bot_events,
	get_member_count,
	send_media,
	send_text,
)
from pxcontrol.engine.telegram.lane import AccountLane, TelegramPriority
from pxcontrol.engine.telegram.mtproto import (
	MtprotoLoginManager,
	MtprotoTransport,
	UserbotFloodError,
	UserbotNotConnectedError,
)
from pxcontrol.engine.telegram.types import (
	CommunityInfo,
	CommunityStatsInfo,
	ForumTopicInfo,
	MediaKind,
	OutgoingPost,
	ParticipantsPage,
	ScheduledMessage,
	ServiceMessagesPage,
	TelegramFloodError,
	UserbotProfile,
)

logger = logging.getLogger(__name__)


class TelegramGateway:
	"""Объединяет транспорты Bot API и MTProto за общим интерфейсом."""

	def __init__(self) -> None:
		# Реквизиты берутся из БД (ключ API — ADR-0018, сессии — tg_accounts):
		# движок активирует userbot-аккаунты при старте, боты — по токену
		# на операцию. Пул транспортов: id аккаунта → клиент MTProto.
		self._userbots: dict[int, MtprotoTransport] = {}
		# дорожки (ADR-0024) живут отдельно от транспортов и переживают
		# их замену: флуд-лимит Telegram назначает аккаунту, а не сессии,
		# и повторный вход не должен стирать знание о нём
		self._lanes: dict[int, AccountLane] = {}
		self.login = MtprotoLoginManager()
		# точка подмены в тестах: фабрика транспорта с подставным клиентом
		self.transport_factory: Callable[[], MtprotoTransport] = MtprotoTransport

	async def stop(self) -> None:
		"""Останавливает подключения (включая незавершённые входы)."""
		await self.login.cancel_all()
		for transport in self._userbots.values():
			await transport.stop()
		self._userbots.clear()
		self._lanes.clear()

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
		# дорожка аккаунта намеренно остаётся: действующая заморозка
		# принадлежит аккаунту Telegram, а не нашему соединению с ним

	def _lane(self, account_id: int) -> AccountLane:
		"""Дорожка аккаунта (заводится при первом обращении)."""
		lane = self._lanes.get(account_id)
		if lane is None:
			lane = AccountLane(account_id)
			self._lanes[account_id] = lane
		return lane

	@asynccontextmanager
	async def _userbot_slot(
		self, account_id: int, priority: TelegramPriority
	) -> AsyncIterator[MtprotoTransport]:
		"""Транспорт аккаунта на занятой дорожке — обвязка всех операций.

		Проверка «аккаунт активирован» идёт до дорожки: занимать очередь
		ради заведомо невозможной операции незачем.

		Отказ дорожки переводится в таксономию userbot
		(:class:`UserbotFloodError`): дорожка — механизм транспортно
		нейтральный и знает только общий :class:`TelegramFloodError`,
		а потребители userbot-операций разбирают исходы по
		``UserbotUnavailableError`` и его подклассам. Без перевода
		отказ дорожки пролетал бы мимо их обработчиков.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован.
			UserbotFloodError: Аккаунт под флуд-лимитом (ADR-0024) —
				в ``retry_after_s`` остаток названного сервером срока.
		"""
		transport = self._userbot(account_id)
		try:
			async with self._lane(account_id).slot(priority):
				yield transport
		except UserbotFloodError:
			raise  # флуд от самого Telegram — уже в нужном классе
		except TelegramFloodError as exc:
			raise UserbotFloodError(str(exc), retry_after_s=exc.retry_after_s) from exc

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

	# Исходы бот-методов — таксономия бот-пути, единая для всех пяти
	# (см. Raises одноимённых функций bot_api): InvalidBotTokenError /
	# TelegramFloodError / CommunityCheckError / ConnectionError.

	# --- запасной путь: Bot API ------------------------------------------------
	#
	# Правило имён: методы бот-пути начинаются с ``bot_``, методы
	# основного пути (userbot, ADR-0011) — нет. Прежде часть бот-методов
	# звалась без пометки (``send_text``, ``check_community``), и по
	# вызову в сервисе нельзя было понять, основной это путь или
	# запасной — при том что у них разные лимиты и разные возможности.

	async def bot_check_token(self, token: str) -> str:
		"""Проверяет токен бота через getMe и возвращает его @имя.

		Raises: см. :func:`bot_api.check_token`.
		"""
		return await check_token(token)

	async def bot_check_community(self, token: str, chat_ref: str) -> CommunityInfo:
		"""Проверяет канал и права бота в нём (getChat + getChatMember).

		Raises: см. :func:`bot_api.check_community` (+ ``ChatRefError``).
		"""
		return await check_community(token, chat_ref)

	async def bot_events(self, token: str) -> list[str]:
		"""Диагностика: события бота за 24 ч (getUpdates, без удаления).

		Raises: см. :func:`bot_api.get_bot_events`.
		"""
		return await get_bot_events(token)

	async def bot_send_text(
		self, token: str, chat_id: str, text: str, topic_id: int | None = None
	) -> int:
		"""Публикует текстовый пост «сейчас» через бота.

		Raises: см. :func:`bot_api.send_text`.
		"""
		return await send_text(token, chat_id, text, topic_id)

	async def bot_send_media(
		self,
		token: str,
		chat_id: str,
		kind: MediaKind,
		path: str,
		caption: str,
		topic_id: int | None = None,
	) -> int:
		"""Отправляет медиа ботом (запасной транспорт, лимит 50 МБ).

		Raises: см. :func:`bot_api.send_media`.
		"""
		return await send_media(token, chat_id, kind, path, caption, topic_id)

	async def bot_member_count(self, token: str, chat_id: str) -> int:
		"""Число участников сообщества через бота (запасной путь).

		Raises: см. :func:`bot_api.get_member_count`.
		"""
		return await get_member_count(token, chat_id)

	# --- MTProto (userbot) -------------------------------------------------------

	async def userbot_me(self, account_id: int) -> UserbotProfile:
		"""Профиль владельца сессии аккаунта: @имя и имя (живой запрос).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotUnavailableError: Прочие отказы Telegram (включая флуд).
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.me()

	async def check_community_userbot(self, account_id: int, chat_ref: str) -> CommunityInfo:
		"""Проверяет канал и права аккаунта (админ + право публиковать).

		Raises:
			ChatRefError: Введённую ссылку/имя не удалось разобрать.
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotAccessError: Прав нет или канал не виден (подтверждено).
			UserbotUnavailableError: Прочие отказы Telegram (включая флуд).
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			return await transport.check_community(chat_ref)

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
		async with self._userbot_slot(account_id, TelegramPriority.PUBLISH) as transport:
			await transport.publish(chat_id, post, on_progress)

	async def get_forum_topics(self, account_id: int, chat_id: str) -> list[ForumTopicInfo]:
		"""Читает темы форума аккаунтом сообщества (только userbot, ADR-0021).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotUnavailableError: Прочие отказы Telegram (не форум и т.п.).
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			return await transport.get_forum_topics(chat_id)

	async def userbot_community_stats(self, account_id: int, chat_id: str) -> CommunityStatsInfo:
		"""Подписчики и онлайн сообщества аккаунтом (один запрос).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotFloodError: Флуд-лимит — вызывающий пропускает аккаунт.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.community_stats(chat_id)

	async def userbot_avatar(self, account_id: int, chat_id: str, target: str) -> str | None:
		"""Скачивает аватар сообщества аккаунтом (None — аватара нет).

		Raises: как у :meth:`userbot_community_stats`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.download_avatar(chat_id, target)

	async def service_messages_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage:
		"""Читает страницу истории сообщества, отбирая служебные записи.

		Одна страница — один запрос: между страницами дорожка пропускает
		вперёд публикацию, а очередь обслуживания проверяет отмену
		(ADR-0026).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.MAINTENANCE) as transport:
			return await transport.service_messages_page(chat_id, offset_id, limit)

	async def delete_messages(self, account_id: int, chat_id: str, message_ids: list[int]) -> int:
		"""Удаляет сообщения сообщества; возвращает число удалённых.

		Пачка, которую Telegram отказался удалять целиком (служебные
		записи бывают защищёнными), считается пропущенной — 0 удалённых,
		без ошибки (ADR-0026).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Нет права удалять (подтверждённый отказ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.MAINTENANCE) as transport:
			return await transport.delete_messages(chat_id, message_ids)

	async def participants_page(
		self, account_id: int, chat_id: str, offset: int, limit: int
	) -> ParticipantsPage:
		"""Читает страницу участников, отбирая удалённые аккаунты (ADR-0026).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Список участников недоступен (нужен админ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.MAINTENANCE) as transport:
			return await transport.participants_page(chat_id, offset, limit)

	async def kick_participant(self, account_id: int, chat_id: str, user_id: int) -> int | None:
		"""Исключает участника; отдаёт id служебной записи об этом.

		None — записи не было. В супергруппе она есть всегда, и чистка
		удалённых аккаунтов убирает её за собой (ADR-0026).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Нет права исключать (подтверждённый отказ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.MAINTENANCE) as transport:
			return await transport.kick_participant(chat_id, user_id)

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		"""Читает отложенные записи канала из Telegram (его аккаунтом).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotAccessError: Прав нет или канал не виден (подтверждено).
			UserbotFloodError: Флуд-лимит — потребители пропускают
				остальные каналы аккаунта до конца прохода (ADR-0017).
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.get_scheduled(chat_id)
