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
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from pxcontrol.engine.telegram.bot_api import (
	check_community,
	check_token,
	edit_markup,
	get_bot_events,
	get_community_stats,
	send_media,
	send_text,
)
from pxcontrol.engine.telegram.lane import (
	BOT_MIN_INTERVAL_S,
	DEFAULT_MIN_INTERVAL_S,
	AccountLane,
	LaneLiveState,
	LaneOwner,
	OperationRecord,
	OwnerKind,
	TelegramPriority,
)
from pxcontrol.engine.telegram.markup import PostMarkup
from pxcontrol.engine.telegram.mtproto import (
	MtprotoLoginManager,
	MtprotoTransport,
	UserbotFloodError,
	UserbotNotConnectedError,
	UserbotPausedError,
)
from pxcontrol.engine.telegram.rich_text import TextEntity
from pxcontrol.engine.telegram.types import (
	BotRef,
	CommunityAnalytics,
	CommunityInfo,
	CommunityStatsInfo,
	DeletedAccount,
	ForumTopicInfo,
	HistoryMarks,
	LinkPreview,
	MediaKind,
	OutgoingPost,
	ParticipantsPage,
	PublishedMessage,
	PublishedPage,
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
		# и повторный вход не должен стирать знание о нём. Ключ — владелец:
		# у ботов свои дорожки (ADR-0030), без зазора, с той же заморозкой
		self._lanes: dict[LaneOwner, AccountLane] = {}
		# записи о выполненных операциях (ADR-0030): дорожки складывают
		# их сюда, сервис активности забирает пачкой (drain_operations)
		self._operations: list[OperationRecord] = []
		# приостановленные человеком аккаунты (ADR-0029): транспорта у них
		# нет, а любая операция получает отказ с причиной «приостановлен»,
		# а не «войдите» — иначе человек шёл бы входить в аккаунт, который
		# сам же и остановил
		self._paused: set[int] = set()
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
		self._paused.clear()

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
		"""Отключает userbot аккаунта (например, после его удаления).

		Пометка паузы снимается: аккаунт покидает приложение целиком,
		а SQLite может отдать его id следующей записи — та не должна
		унаследовать чужую паузу.
		"""
		self._paused.discard(account_id)
		transport = self._userbots.pop(account_id, None)
		if transport is not None:
			await transport.stop()
		# дорожка аккаунта намеренно остаётся: действующая заморозка
		# принадлежит аккаунту Telegram, а не нашему соединению с ним

	async def pause_userbot(self, account_id: int) -> None:
		"""Приостанавливает аккаунт (ADR-0029): транспорт закрыт, операции — отказ.

		Идемпотентно: при старте так регистрируются аккаунты, которые
		человек приостановил в прошлой сессии, — их транспорт вовсе
		не поднимается. Возобновляет :meth:`resume_userbot` вместе
		с новой активацией (реквизиты знает сервис аккаунтов).
		"""
		await self.deactivate_userbot(account_id)
		self._paused.add(account_id)

	def resume_userbot(self, account_id: int) -> None:
		"""Снимает пометку паузы — дальше аккаунт активируется штатно.

		Подключение здесь не поднимается: без сохранённой сессии
		возобновлённый аккаунт просто ждёт входа, как новый.
		"""
		self._paused.discard(account_id)

	def userbot_paused(self, account_id: int) -> bool:
		"""Приостановлен ли аккаунт в этом шлюзе."""
		return account_id in self._paused

	def userbot_connected(self, account_id: int) -> bool:
		"""Есть ли у аккаунта живое соединение прямо сейчас (снимок для показа).

		False — не активирован, приостановлен или связь потеряна;
		различать причины — дело сервиса аккаунтов, у него есть БД.
		"""
		transport = self._userbots.get(account_id)
		return transport is not None and transport.connected

	def _lane(self, owner: LaneOwner) -> AccountLane:
		"""Дорожка владельца (заводится при первом обращении).

		Пользователю — зазор ADR-0024, боту — без зазора (ADR-0030);
		записи операций обеих уходят в общий буфер активности.
		"""
		lane = self._lanes.get(owner)
		if lane is None:
			interval = BOT_MIN_INTERVAL_S if owner.kind is OwnerKind.BOT else DEFAULT_MIN_INTERVAL_S
			lane = AccountLane(owner, interval, record=self._operations.append)
			self._lanes[owner] = lane
		return lane

	def drain_operations(self) -> list[OperationRecord]:
		"""Забирает накопленные записи операций (буфер очищается).

		Единственный читатель — сервис активности (ADR-0030): пишет их
		в БД пачкой; шлюз о хранилище не знает.
		"""
		records, self._operations = self._operations, []
		return records

	def restore_operations(self, records: Sequence[OperationRecord]) -> None:
		"""Возвращает записи в буфер (сброс в БД не удался) — вперёд свежих."""
		self._operations[:0] = list(records)

	def live_states(self) -> dict[LaneOwner, LaneLiveState]:
		"""Живое состояние всех дорожек — снимок для показа (ADR-0030)."""
		return {owner: lane.live_state() for owner, lane in self._lanes.items()}

	@asynccontextmanager
	async def _bot_slot(self, bot: BotRef, priority: TelegramPriority) -> AsyncIterator[str]:
		"""Дорожка бота под одну операцию; отдаёт токен для запроса.

		Заморозка после «подождите N секунд» у бота такая же, как
		у пользователя (ADR-0030): следующая операция получает отказ
		сразу, не тревожа Telegram.

		Raises:
			TelegramFloodError: Дорожка бота заморожена — остаток срока
				в ``retry_after_s``.
		"""
		async with self._lane(LaneOwner(OwnerKind.BOT, bot.id)).slot(priority):
			yield bot.token

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
			UserbotPausedError: Аккаунт приостановлен человеком (ADR-0029).
			UserbotNotConnectedError: Аккаунт не активирован.
			UserbotFloodError: Аккаунт под флуд-лимитом (ADR-0024) —
				в ``retry_after_s`` остаток названного сервером срока.
		"""
		transport = self._userbot(account_id)
		try:
			async with self._lane(LaneOwner(OwnerKind.USER, account_id)).slot(priority):
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

		Пауза проверяется раньше пула: у приостановленного аккаунта
		транспорта нет по замыслу, и отказ должен называть причину.

		Raises:
			UserbotPausedError: Аккаунт приостановлен человеком (ADR-0029).
			UserbotNotConnectedError: Аккаунт не активирован (нет сессии
				или ключа API) — нужен вход: «Пользователи и боты».
		"""
		if account_id in self._paused:
			raise UserbotPausedError(
				"Аккаунт приостановлен — возобновите его в разделе «Пользователи и боты»."
			)
		transport = self._userbots.get(account_id)
		if transport is None:
			raise UserbotNotConnectedError(
				"Userbot этого сообщества не подключён — войдите в его аккаунт: "
				"«Пользователи и боты»."
			)
		return transport

	# --- Bot API ---------------------------------------------------------------

	# Исходы бот-методов — таксономия бот-пути, единая для всех пяти
	# (см. Raises одноимённых функций bot_api): InvalidBotTokenError /
	# TelegramFloodError / CommunityCheckError / ConnectionError.
	# Плюс отказ дорожки бота (ADR-0030) — тот же TelegramFloodError.

	# --- запасной путь: Bot API ------------------------------------------------
	#
	# Правило имён: методы бот-пути начинаются с ``bot_``, методы
	# основного пути (userbot, ADR-0011) — нет. Прежде часть бот-методов
	# звалась без пометки (``send_text``, ``check_community``), и по
	# вызову в сервисе нельзя было понять, основной это путь или
	# запасной — при том что у них разные лимиты и разные возможности.
	# Адрес операции — BotRef (id + токен): по id ведутся дорожка
	# и учёт активности бота (ADR-0030).

	async def bot_check_token(self, token: str) -> str:
		"""Проверяет токен бота через getMe и возвращает его @имя.

		Единственная бот-операция без дорожки: бота в приложении ещё нет,
		учитывать её не за кем.

		Raises: см. :func:`bot_api.check_token`.
		"""
		return await check_token(token)

	async def bot_check_community(self, bot: BotRef, chat_ref: str) -> CommunityInfo:
		"""Проверяет канал и права бота в нём (getChat + getChatMember).

		Raises: см. :func:`bot_api.check_community` (+ ``ChatRefError``).
		"""
		async with self._bot_slot(bot, TelegramPriority.INTERACTIVE) as token:
			return await check_community(token, chat_ref)

	async def bot_events(self, bot: BotRef) -> list[str]:
		"""Диагностика: события бота за 24 ч (getUpdates, без удаления).

		Raises: см. :func:`bot_api.get_bot_events`.
		"""
		async with self._bot_slot(bot, TelegramPriority.INTERACTIVE) as token:
			return await get_bot_events(token)

	async def bot_send_text(
		self,
		bot: BotRef,
		chat_id: str,
		text: str,
		topic_id: int | None = None,
		markup: PostMarkup | None = None,
		entities: tuple[TextEntity, ...] = (),
		preview: LinkPreview | None = None,
	) -> int:
		"""Публикует текстовый пост «сейчас» через бота (с кнопками, если есть).

		``entities`` — разметка текста (ADR-0033): пусто — прежний разбор
		разделителей строки.

		Raises: см. :func:`bot_api.send_text`.
		"""
		async with self._bot_slot(bot, TelegramPriority.PUBLISH) as token:
			return await send_text(token, chat_id, text, topic_id, markup, entities, preview)

	async def bot_send_media(
		self,
		bot: BotRef,
		chat_id: str,
		kind: MediaKind,
		path: str,
		caption: str,
		topic_id: int | None = None,
		markup: PostMarkup | None = None,
		entities: tuple[TextEntity, ...] = (),
	) -> int:
		"""Отправляет медиа ботом (лимит 50 МБ; с кнопками, если есть).

		``entities`` — разметка подписи (ADR-0033).

		Raises: см. :func:`bot_api.send_media`.
		"""
		async with self._bot_slot(bot, TelegramPriority.PUBLISH) as token:
			return await send_media(token, chat_id, kind, path, caption, topic_id, markup, entities)

	async def bot_edit_markup(
		self, bot: BotRef, chat_id: str, message_id: int, markup: PostMarkup | None
	) -> None:
		"""Ставит, меняет или снимает клавиатуру у поста (ADR-0031).

		Приоритет публикации: правка идёт следом за отправкой поста
		и не должна ждать фоновых чтений — иначе окно без кнопок
		растянулось бы на минуты.

		Raises: см. :func:`bot_api.edit_markup`.
		"""
		async with self._bot_slot(bot, TelegramPriority.PUBLISH) as token:
			await edit_markup(token, chat_id, message_id, markup)

	async def bot_community_stats(self, bot: BotRef, chat_id: str) -> CommunityStatsInfo:
		"""Участники и связанный чат через бота — дешёвый частый опрос.

		Дорожка у бота своя (ADR-0030): загрузки userbot на него
		не влияют — этим бот-путь и ценен для частого опроса.

		Raises: см. :func:`bot_api.get_community_stats`.
		"""
		async with self._bot_slot(bot, TelegramPriority.BACKGROUND) as token:
			info: CommunityStatsInfo = await get_community_stats(token, chat_id)
			return info

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
	) -> int:
		"""Публикует пост из сессии привязанного к каналу аккаунта (ADR-0019).

		Текст или медиа с подписью; сразу (when=None) или отложенно —
		отложенные хранит и публикует сервер Telegram (ADR-0010).

		Returns:
			Номер отправленного поста (у отложенного — номер записи
			в очереди отложенных сервера). Нужен кнопкам: бот
			дорисовывает их правкой по этому номеру (ADR-0031).

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
			return await transport.publish(chat_id, post, on_progress)

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

	async def userbot_community_analytics(
		self, account_id: int, chat_id: str
	) -> CommunityAnalytics:
		"""Встроенная статистика Telegram аккаунтом (редкий фоновый опрос).

		Несколько запросов подряд (сами данные и графики, отданные
		по токену) — все на одной занятой дорожке с фоновым приоритетом.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Статистика недоступна (не админ, мало участников).
			UserbotFloodError: Флуд-лимит — вызывающий пропускает аккаунт.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.community_analytics(chat_id)

	async def userbot_history_marks(
		self, account_id: int, chat_id: str, *, with_created: bool
	) -> HistoryMarks:
		"""Момент последнего сообщения и (по запросу) создания сообщества.

		Raises: как у :meth:`userbot_community_stats`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.history_marks(chat_id, with_created=with_created)

	async def userbot_avatar(self, account_id: int, chat_id: str, target: str) -> str | None:
		"""Скачивает аватар сообщества аккаунтом (None — аватара нет).

		Raises: как у :meth:`userbot_community_stats`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.download_avatar(chat_id, target)

	async def userbot_find_published(
		self, account_id: int, chat_id: str, text: str, after: datetime, limit: int
	) -> int | None:
		"""Ищет вышедший пост по тексту (для кнопок отложенного, ADR-0031).

		Фоновый приоритет: дозор кнопок не должен обгонять публикацию
		и не должен заставлять человека ждать на экране — кнопки появятся
		секундой позже, и это не беда.

		Raises: см. :meth:`MtprotoTransport.find_published`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.BACKGROUND) as transport:
			return await transport.find_published(chat_id, text, after, limit)

	async def userbot_history_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> PublishedPage:
		"""Читает страницу ленты сообщества (экран «Опубликовано», ADR-0032).

		Приоритет интерактивный: человек ждёт ответа на экране. Ниже
		публикации — лента подождёт, пока уходит пост.

		Raises: см. :meth:`MtprotoTransport.history_page`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			return await transport.history_page(chat_id, offset_id, limit)

	async def userbot_get_post(
		self, account_id: int, chat_id: str, message_id: int
	) -> PublishedMessage | None:
		"""Читает один вышедший пост (форма правки на «Опубликовано»).

		Raises: см. :meth:`MtprotoTransport.get_post`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			return await transport.get_post(chat_id, message_id)

	async def userbot_edit_post(
		self,
		account_id: int,
		chat_id: str,
		message_id: int,
		text: str,
		entities: tuple[TextEntity, ...] = (),
	) -> None:
		"""Меняет текст вышедшего поста публикатором (ADR-0032, подача A4).

		Raises: см. :meth:`MtprotoTransport.edit_post`.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			await transport.edit_post(chat_id, message_id, text, entities)

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

	async def delete_messages(
		self,
		account_id: int,
		chat_id: str,
		message_ids: list[int],
		priority: TelegramPriority = TelegramPriority.MAINTENANCE,
	) -> int:
		"""Удаляет сообщения сообщества; возвращает число удалённых.

		Пачка, которую Telegram отказался удалять целиком (служебные
		записи бывают защищёнными), считается пропущенной — 0 удалённых,
		без ошибки (ADR-0026).

		``priority`` — место в очереди дорожки: обслуживание идёт своим
		темпом (умолчание), а удаление поста человеком с экрана
		«Опубликовано» ждать наравне с уборкой не должно.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Нет права удалять (подтверждённый отказ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, priority) as transport:
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

	async def kick_participant(
		self, account_id: int, chat_id: str, account: DeletedAccount
	) -> int | None:
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
			return await transport.kick_participant(chat_id, account)

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

	async def get_scheduled_message(
		self, account_id: int, chat_id: str, message_id: int
	) -> ScheduledMessage | None:
		"""Читает одну отложенную запись целиком (None — её уже нет).

		Приоритет интерактивный: человек открыл форму правки и ждёт.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			return await transport.get_scheduled_message(chat_id, message_id)

	async def edit_scheduled(
		self,
		account_id: int,
		chat_id: str,
		message_id: int,
		text: str,
		when: datetime,
		entities: tuple[TextEntity, ...] = (),
	) -> None:
		"""Меняет текст и/или время отложенной записи аккаунтом, который её видит.

		В группе отложку видит только её создатель (ADR-0022), поэтому
		аккаунт — тот, чьим чтением запись попала в список.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Записи в очереди отложенных уже нет.
			UserbotAccessError: Нет права править (подтверждённый отказ).
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Время отклонено и прочие отказы.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			await transport.edit_scheduled(chat_id, message_id, text, when, entities)

	async def send_scheduled_now(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> None:
		"""Публикует отложенные записи немедленно.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Записи в очереди отложенных уже нет.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			await transport.send_scheduled_now(chat_id, message_ids)

	async def delete_scheduled(self, account_id: int, chat_id: str, message_ids: list[int]) -> None:
		"""Удаляет отложенные записи, не публикуя.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Записи в очереди отложенных уже нет.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		async with self._userbot_slot(account_id, TelegramPriority.INTERACTIVE) as transport:
			await transport.delete_scheduled(chat_id, message_ids)
