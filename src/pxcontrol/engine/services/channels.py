"""Сервис каналов: подключение (бот или userbot), привязки, список, удаление.

Userbot-админ канала — конкретный аккаунт (``channels.tg_account_id``,
ADR-0019): постинг идёт из его сессии. Бот — самостоятельная сущность
(работает по токену, без пользовательской сессии) — ``channels.bot_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Channel, TgAccount
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.settings import CHANNEL_ENABLED, SettingsService
from pxcontrol.engine.telegram.mtproto import UserbotAccessError
from pxcontrol.engine.telegram.types import ChannelInfo

logger = logging.getLogger(__name__)


class ChannelError(EngineError):
	"""Ошибка операций с каналами (с понятным человеку текстом)."""


class _ChannelChecker(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def check_channel(self, token: str, chat_ref: str) -> ChannelInfo: ...

	async def check_channel_userbot(self, account_id: int, chat_ref: str) -> ChannelInfo: ...


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
	tg_account_id: int | None = None
	tg_account_label: str | None = None

	@property
	def userbot_admin(self) -> bool:
		"""Есть ли у канала userbot-админ (выводится из привязки)."""
		return self.tg_account_id is not None


@dataclass(frozen=True)
class ChannelAccess:
	"""Итог перепроверки доступов канала.

	Attributes:
		channel: канал с обновлённой привязкой userbot.
		userbot_ok: привязанный аккаунт — админ с правом публиковать
			(None — проверить не удалось: нет связи или аккаунт отключён;
			для канала без привязки None означает «админ не нашёлся
			и среди вошедших аккаунтов»).
		bot_ok: права бота на месте (None — бот не назначен).
	"""

	channel: ChannelDto
	userbot_ok: bool | None
	bot_ok: bool | None


class ChannelsService:
	"""Подключение каналов, привязки публикаторов и хранение настроек."""

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
		"""Возвращает все подключённые каналы (с именами публикаторов)."""
		enabled = await self._settings.get_for_all(CHANNEL_ENABLED)
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(Channel)
					.options(selectinload(Channel.bot), selectinload(Channel.tg_account))
					.order_by(Channel.id)
				)
			).scalars()
			return [
				self._dto(ch, enabled=enabled.get(ch.id, CHANNEL_ENABLED.default)) for ch in rows
			]

	async def add_channel(self, bot_id: int, chat_ref: str) -> ChannelDto:
		"""Подключает канал через бота (с попутным поиском userbot-админа).

		Порядок: бот существует → канал доступен и бот в нём админ
		с правом публикации → дубликата нет → сохранить. Попутно
		опрашиваются вошедшие userbot-аккаунты: первый, кто оказался
		админом, привязывается к каналу (ADR-0019).

		Raises:
			ChannelError: Бот не найден или канал уже подключён.
			ChannelCheckError: Канал не прошёл проверку Telegram.
			ConnectionError: Нет связи с Telegram.
		"""
		bot = await self._get_bot(bot_id)
		logger.info(
			"Подключаю канал: ввод %r, бот «%s» (@%s, id=%s).",
			chat_ref,
			bot.label,
			bot.username,
			bot.id,
		)
		info = await self._gateway.check_channel(bot.token, chat_ref)
		# «не удалось проверить» при подключении равносильно «админа нет»:
		# привязку добавит перепроверка доступов, когда аккаунт появится
		account_id = await self._find_userbot_admin(info.chat_id)
		channel = await self._store_channel(
			title=info.title,
			tg_chat_id=info.chat_id,
			username=info.username,
			bot_id=bot.id,
			tg_account_id=account_id,
		)
		logger.info(
			"Подключён канал «%s» (бот %s, userbot-админ: %s).",
			info.title,
			bot.label,
			account_id or "нет",
		)
		return await self._fresh_dto(channel.id)

	async def add_channel_via_userbot(self, account_id: int, chat_ref: str) -> ChannelDto:
		"""Подключает канал через выбранный userbot-аккаунт — бот не нужен.

		Аккаунт выбирается явно (ADR-0019): проверяются права именно его,
		и именно он привязывается к каналу как публикатор.

		Raises:
			ChannelError: Канал уже подключён или аккаунт не найден.
			UserbotUnavailableError: Аккаунт не подключён, не админ или без
				права публиковать.
		"""
		account = await self._get_account(account_id)
		logger.info("Подключаю канал через userbot «%s»: ввод %r.", account.label, chat_ref)
		info = await self._gateway.check_channel_userbot(account_id, chat_ref)
		channel = await self._store_channel(
			title=info.title,
			tg_chat_id=info.chat_id,
			username=info.username,
			bot_id=None,
			tg_account_id=account_id,
		)
		logger.info("Подключён канал «%s» (userbot «%s»).", info.title, account.label)
		return await self._fresh_dto(channel.id)

	async def _probe_userbot(self, account_id: int, chat_id: str) -> bool | None:
		"""Проверяет права одного аккаунта (сбой не мешает операции).

		Returns:
			True/False — Telegram подтвердил наличие/отсутствие прав;
			None — проверить не удалось (нет связи, аккаунт отключён):
			это не знание о правах, менять привязку по нему нельзя.
		"""
		try:
			await self._gateway.check_channel_userbot(account_id, chat_id)
		except UserbotAccessError:
			logger.info("Аккаунт id=%s не админ канала %s.", account_id, chat_id)
			return False
		except Exception:  # noqa: BLE001 — вспомогательная проверка
			logger.info(
				"Проверка аккаунта id=%s в канале %s не удалась (сеть или подключение).",
				account_id,
				chat_id,
			)
			return None
		return True

	async def _find_userbot_admin(self, chat_id: str) -> int | None:
		"""Ищет админа канала среди вошедших аккаунтов (первый подходящий).

		Для попутной привязки на бот-пути и перепроверки доступов канала
		без привязки. Порядок — по id аккаунта; сбои проверок пропускаются.
		"""
		async with self._db.session_factory() as session:
			account_ids = (
				(
					await session.execute(
						select(TgAccount.id)
						.where(TgAccount.session.is_not(None))
						.order_by(TgAccount.id)
					)
				)
				.scalars()
				.all()
			)
		for account_id in account_ids:
			if await self._probe_userbot(account_id, chat_id) is True:
				return account_id
		return None

	async def _store_channel(
		self,
		*,
		title: str,
		tg_chat_id: str,
		username: str | None,
		bot_id: int | None,
		tg_account_id: int | None,
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
				title=title,
				tg_chat_id=tg_chat_id,
				username=username,
				bot_id=bot_id,
				tg_account_id=tg_account_id,
			)
			session.add(channel)
			await session.commit()
			await session.refresh(channel)
		return channel

	async def recheck_channel(self, channel_id: int) -> ChannelAccess:
		"""Перепроверяет оба способа администрирования канала.

		Привязка userbot обновляется в обе стороны, но только
		по подтверждённому ответу Telegram: подтверждённый отказ
		привязанного аккаунта снимает привязку (иначе публикация падала
		бы), сбой связи — не повод её трогать (канал молча терял бы
		отложенные посты и большие файлы). У канала без привязки админ
		ищется среди вошедших аккаунтов. Потеря прав бота его
		не отвязывает — только сообщается: бота могут вернуть.

		Raises:
			ChannelError: Канал не найден.
		"""
		# сессии короткие, сетевые зонды — между ними (образец — assign_bot):
		# открытая транзакция чтения на время походов в Telegram держала бы
		# SQLite занятым для параллельных задач движка
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id, with_refs=True)
			tg_chat_id = channel.tg_chat_id
			bound_account_id = channel.tg_account_id
			bot_token = channel.bot.token if channel.bot is not None else None
		userbot_ok: bool | None
		new_account_id = bound_account_id
		if bound_account_id is not None:
			userbot_ok = await self._probe_userbot(bound_account_id, tg_chat_id)
			if userbot_ok is False:
				new_account_id = None  # подтверждённый отказ — привязка снимается
		else:
			found = await self._find_userbot_admin(tg_chat_id)
			new_account_id = found
			userbot_ok = True if found is not None else None
		bot_ok: bool | None = None
		if bot_token is not None:
			bot_ok = await self._probe_bot(bot_token, tg_chat_id)
		if new_account_id != bound_account_id:
			async with self._db.session_factory() as session:
				channel = await self._channel_in_session(session, channel_id)
				channel.tg_account_id = new_account_id
				await session.commit()
		dto = await self._fresh_dto(channel_id)
		logger.info(
			"Доступы канала «%s»: userbot=%s (аккаунт %s), бот=%s.",
			dto.title,
			userbot_ok,
			new_account_id or "—",
			bot_ok,
		)
		return ChannelAccess(dto, userbot_ok, bot_ok)

	async def assign_userbot(self, channel_id: int, account_id: int) -> ChannelDto:
		"""Привязывает к каналу userbot-аккаунт (с проверкой его прав).

		Raises:
			ChannelError: Канал или аккаунт не найдены.
			UserbotUnavailableError: Аккаунт не подключён, не админ или без
				права публиковать.
		"""
		account = await self._get_account(account_id)
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id)
			chat_id = channel.tg_chat_id
		await self._gateway.check_channel_userbot(account_id, chat_id)
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id)
			channel.tg_account_id = account_id
			await session.commit()
		dto = await self._fresh_dto(channel_id)
		logger.info("Каналу «%s» привязан userbot «%s».", dto.title, account.label)
		return dto

	async def unassign_userbot(self, channel_id: int) -> ChannelDto:
		"""Отвязывает userbot от канала (сам аккаунт остаётся в приложении).

		Raises:
			ChannelError: Канал не найден.
		"""
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id)
			channel.tg_account_id = None
			await session.commit()
		dto = await self._fresh_dto(channel_id)
		logger.info("От канала «%s» отвязан userbot.", dto.title)
		return dto

	async def assign_bot(self, channel_id: int, bot_id: int) -> ChannelDto:
		"""Назначает каналу бота (с проверкой его прав в канале).

		Raises:
			ChannelError: Канал или бот не найдены.
			ChannelCheckError: Бот не админ канала / без права публиковать.
		"""
		bot = await self._get_bot(bot_id)
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id)
			chat_id = channel.tg_chat_id
		await self._gateway.check_channel(bot.token, chat_id)
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id)
			channel.bot_id = bot.id
			await session.commit()
		dto = await self._fresh_dto(channel_id)
		logger.info("Каналу «%s» назначен бот «%s».", dto.title, bot.label)
		return dto

	async def unassign_bot(self, channel_id: int) -> ChannelDto:
		"""Отвязывает бота от канала (сам бот остаётся в приложении).

		Raises:
			ChannelError: Канал не найден.
		"""
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id)
			channel.bot_id = None
			await session.commit()
		dto = await self._fresh_dto(channel_id)
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

	async def _fresh_dto(self, channel_id: int) -> ChannelDto:
		"""Снимок канала из БД с подгруженными публикаторами и настройками.

		Raises:
			ChannelError: Канал не найден.
		"""
		enabled = await self._settings.get_for(CHANNEL_ENABLED, channel_id)
		async with self._db.session_factory() as session:
			channel = await self._channel_in_session(session, channel_id, with_refs=True)
			return self._dto(channel, enabled=enabled)

	@staticmethod
	async def _channel_in_session(
		session: AsyncSession, channel_id: int, *, with_refs: bool = False
	) -> Channel:
		"""Канал по id в переданной сессии — или «не найден» понятным текстом.

		``with_refs=True`` подгружает бота и userbot-аккаунт сразу: ``_dto``
		работает на отсоединённом объекте, и ленивое обращение упало бы
		``MissingGreenlet`` вместо внятной ошибки.
		"""
		if with_refs:
			channel = (
				await session.execute(
					select(Channel)
					.options(selectinload(Channel.bot), selectinload(Channel.tg_account))
					.where(Channel.id == channel_id)
				)
			).scalar_one_or_none()
		else:
			channel = await session.get(Channel, channel_id)
		if channel is None:
			raise ChannelError("Канал не найден — обновите список.")
		return channel

	async def _get_bot(self, bot_id: int) -> Bot:
		"""Возвращает бота или объясняет, что он не найден."""
		async with self._db.session_factory() as session:
			bot = await session.get(Bot, bot_id)
		if bot is None:
			raise ChannelError("Бот не найден — добавьте его в Настройках.")
		return bot

	async def _get_account(self, account_id: int) -> TgAccount:
		"""Возвращает userbot-аккаунт или объясняет, что он не найден."""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
		if account is None:
			raise ChannelError("Аккаунт не найден — добавьте его в Настройках.")
		return account

	@staticmethod
	def _dto(channel: Channel, enabled: bool = True) -> ChannelDto:
		"""Снимок канала; связи должны быть подгружены (with_refs)."""
		return ChannelDto(
			channel.id,
			channel.title,
			channel.username,
			channel.tg_chat_id,
			channel.bot_id,
			channel.bot.label if channel.bot is not None else None,
			enabled,
			channel.tg_account_id,
			channel.tg_account.label if channel.tg_account is not None else None,
		)
