"""Сервис каналов: подключение (бот или userbot), привязки, список, удаление.

Userbot-админ канала — конкретный аккаунт (``communities.tg_account_id``,
ADR-0019): постинг идёт из его сессии. Бот — самостоятельная сущность
(работает по токену, без пользовательской сессии) — ``communities.bot_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, TgAccount
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.mtproto import UserbotAccessError
from pxcontrol.engine.telegram.types import CommunityInfo, CommunityKind

logger = logging.getLogger(__name__)


class CommunityError(EngineError):
	"""Ошибка операций с каналами (с понятным человеку текстом)."""


@dataclass(frozen=True)
class _ProbeResult:
	"""Итог сетевого зонда прав публикатора.

	Attributes:
		ok: True/False — Telegram подтвердил наличие/отсутствие прав;
			None — проверить не удалось (нет связи, аккаунт отключён):
			это не знание о правах, менять привязку по нему нельзя.
		info: свежие данные сообщества при ``ok is True`` — из них
			обновляются изменчивые свойства (признак форума, ADR-0021).
	"""

	ok: bool | None
	info: CommunityInfo | None = None


class _CommunityChecker(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def check_community(self, token: str, chat_ref: str) -> CommunityInfo: ...

	async def check_community_userbot(self, account_id: int, chat_ref: str) -> CommunityInfo: ...


@dataclass(frozen=True)
class CommunityDto:
	"""Сообщество для показа в интерфейсе (вид и форум — ADR-0021)."""

	id: int
	title: str
	username: str | None
	tg_chat_id: str
	bot_id: int | None
	bot_label: str | None
	enabled: bool
	tg_account_id: int | None = None
	tg_account_label: str | None = None
	kind: CommunityKind = CommunityKind.CHANNEL
	forum: bool = False

	@property
	def userbot_admin(self) -> bool:
		"""Есть ли у канала userbot-админ (выводится из привязки)."""
		return self.tg_account_id is not None


@dataclass(frozen=True)
class CommunityAccess:
	"""Итог перепроверки доступов канала.

	Attributes:
		community: канал с обновлённой привязкой userbot.
		userbot_ok: привязанный аккаунт — админ с правом публиковать
			(None — проверить не удалось: нет связи или аккаунт отключён;
			для канала без привязки None означает «админ не нашёлся
			и среди вошедших аккаунтов»).
		bot_ok: права бота на месте (None — бот не назначен).
	"""

	community: CommunityDto
	userbot_ok: bool | None
	bot_ok: bool | None


class CommunitiesService:
	"""Подключение каналов, привязки публикаторов и хранение настроек."""

	def __init__(
		self,
		db: Database,
		gateway: _CommunityChecker,
		settings: SettingsService | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно:
		настройки каналов не кэшируются)."""
		self._db = db
		self._gateway = gateway
		self._settings = settings if settings is not None else SettingsService(db)

	async def list_communities(self) -> list[CommunityDto]:
		"""Возвращает все подключённые каналы (с именами публикаторов)."""
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(Community)
					.options(selectinload(Community.bot), selectinload(Community.tg_account))
					.order_by(Community.id)
				)
			).scalars()
			return [
				self._dto(ch, enabled=enabled.get(ch.id, COMMUNITY_ENABLED.default)) for ch in rows
			]

	async def add_community(self, bot_id: int, chat_ref: str) -> CommunityDto:
		"""Подключает канал через бота (с попутным поиском userbot-админа).

		Порядок: бот существует → канал доступен и бот в нём админ
		с правом публикации → дубликата нет → сохранить. Попутно
		опрашиваются вошедшие userbot-аккаунты: первый, кто оказался
		админом, привязывается к каналу (ADR-0019).

		Raises:
			CommunityError: Бот не найден или канал уже подключён.
			CommunityCheckError: Канал не прошёл проверку Telegram.
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
		info = await self._gateway.check_community(bot.token, chat_ref)
		# «не удалось проверить» при подключении равносильно «админа нет»:
		# привязку добавит перепроверка доступов, когда аккаунт появится
		account_id = await self._find_userbot_admin(info.chat_id)
		community = await self._store_community(info, bot_id=bot.id, tg_account_id=account_id)
		logger.info(
			"Подключён канал «%s» (бот %s, userbot-админ: %s).",
			info.title,
			bot.label,
			account_id or "нет",
		)
		return await self._fresh_dto(community.id)

	async def add_community_via_userbot(self, account_id: int, chat_ref: str) -> CommunityDto:
		"""Подключает канал через выбранный userbot-аккаунт — бот не нужен.

		Аккаунт выбирается явно (ADR-0019): проверяются права именно его,
		и именно он привязывается к каналу как публикатор.

		Raises:
			CommunityError: Канал уже подключён или аккаунт не найден.
			UserbotUnavailableError: Аккаунт не подключён, не админ или без
				права публиковать.
		"""
		account = await self._get_account(account_id)
		logger.info("Подключаю канал через userbot «%s»: ввод %r.", account.label, chat_ref)
		info = await self._gateway.check_community_userbot(account_id, chat_ref)
		community = await self._store_community(info, bot_id=None, tg_account_id=account_id)
		logger.info("Подключён канал «%s» (userbot «%s»).", info.title, account.label)
		return await self._fresh_dto(community.id)

	async def _probe_userbot(self, account_id: int, chat_id: str) -> _ProbeResult:
		"""Проверяет права одного аккаунта (сбой не мешает операции)."""
		try:
			info = await self._gateway.check_community_userbot(account_id, chat_id)
		except UserbotAccessError:
			logger.info("Аккаунт id=%s не может публиковать в сообществе %s.", account_id, chat_id)
			return _ProbeResult(ok=False)
		except Exception:  # noqa: BLE001 — вспомогательная проверка
			logger.info(
				"Проверка аккаунта id=%s в сообществе %s не удалась (сеть или подключение).",
				account_id,
				chat_id,
			)
			return _ProbeResult(ok=None)
		return _ProbeResult(ok=True, info=info)

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
			if (await self._probe_userbot(account_id, chat_id)).ok is True:
				return account_id
		return None

	async def _store_community(
		self,
		info: CommunityInfo,
		*,
		bot_id: int | None,
		tg_account_id: int | None,
	) -> Community:
		"""Сохраняет сообщество из проверенных данных, отклоняя дубликат.

		Вид и признак форума берутся из проверки транспорта (ADR-0021):
		вид дальше не меняется, форум обновляют перепроверки.

		Raises:
			CommunityError: Сообщество уже подключено.
		"""
		async with self._db.session_factory() as session:
			existing = await session.execute(
				select(Community.id).where(Community.tg_chat_id == info.chat_id)
			)
			if existing.scalar_one_or_none() is not None:
				raise CommunityError(f"«{info.title}» уже подключено.")
			community = Community(
				title=info.title,
				tg_chat_id=info.chat_id,
				username=info.username,
				kind=info.kind,
				forum=info.forum,
				bot_id=bot_id,
				tg_account_id=tg_account_id,
			)
			session.add(community)
			await session.commit()
			await session.refresh(community)
		return community

	async def recheck_community(self, community_id: int) -> CommunityAccess:
		"""Перепроверяет оба способа администрирования канала.

		Привязка userbot обновляется в обе стороны, но только
		по подтверждённому ответу Telegram: подтверждённый отказ
		привязанного аккаунта снимает привязку (иначе публикация падала
		бы), сбой связи — не повод её трогать (канал молча терял бы
		отложенные посты и большие файлы). У канала без привязки админ
		ищется среди вошедших аккаунтов. Потеря прав бота его
		не отвязывает — только сообщается: бота могут вернуть.

		Raises:
			CommunityError: Канал не найден.
		"""
		# сессии короткие, сетевые зонды — между ними (образец — assign_bot):
		# открытая транзакция чтения на время походов в Telegram держала бы
		# SQLite занятым для параллельных задач движка
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			tg_chat_id = community.tg_chat_id
			bound_account_id = community.tg_account_id
			bot_token = community.bot.token if community.bot is not None else None
		userbot_ok: bool | None
		fresh_info: CommunityInfo | None = None
		new_account_id = bound_account_id
		if bound_account_id is not None:
			probe = await self._probe_userbot(bound_account_id, tg_chat_id)
			userbot_ok = probe.ok
			fresh_info = probe.info
			if userbot_ok is False:
				new_account_id = None  # подтверждённый отказ — привязка снимается
		else:
			found = await self._find_userbot_admin(tg_chat_id)
			new_account_id = found
			userbot_ok = True if found is not None else None
		bot_ok: bool | None = None
		if bot_token is not None:
			bot_probe = await self._probe_bot(bot_token, tg_chat_id)
			bot_ok = bot_probe.ok
			fresh_info = fresh_info or bot_probe.info
		if new_account_id != bound_account_id:
			async with self._db.session_factory() as session:
				community = await self._community_in_session(session, community_id)
				community.tg_account_id = new_account_id
				await session.commit()
		if fresh_info is not None:
			await self._refresh_forum(community_id, fresh_info)
		dto = await self._fresh_dto(community_id)
		logger.info(
			"Доступы канала «%s»: userbot=%s (аккаунт %s), бот=%s.",
			dto.title,
			userbot_ok,
			new_account_id or "—",
			bot_ok,
		)
		return CommunityAccess(dto, userbot_ok, bot_ok)

	async def assign_userbot(self, community_id: int, account_id: int) -> CommunityDto:
		"""Привязывает к каналу userbot-аккаунт (с проверкой его прав).

		Raises:
			CommunityError: Канал или аккаунт не найдены.
			UserbotUnavailableError: Аккаунт не подключён, не админ или без
				права публиковать.
		"""
		account = await self._get_account(account_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			chat_id = community.tg_chat_id
		info = await self._gateway.check_community_userbot(account_id, chat_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			community.tg_account_id = account_id
			await session.commit()
		await self._refresh_forum(community_id, info)
		dto = await self._fresh_dto(community_id)
		logger.info("Каналу «%s» привязан userbot «%s».", dto.title, account.label)
		return dto

	async def unassign_userbot(self, community_id: int) -> CommunityDto:
		"""Отвязывает userbot от канала (сам аккаунт остаётся в приложении).

		Raises:
			CommunityError: Канал не найден.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			community.tg_account_id = None
			await session.commit()
		dto = await self._fresh_dto(community_id)
		logger.info("От канала «%s» отвязан userbot.", dto.title)
		return dto

	async def assign_bot(self, community_id: int, bot_id: int) -> CommunityDto:
		"""Назначает каналу бота (с проверкой его прав в канале).

		Raises:
			CommunityError: Канал или бот не найдены.
			CommunityCheckError: Бот не админ канала / без права публиковать.
		"""
		bot = await self._get_bot(bot_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			chat_id = community.tg_chat_id
		info = await self._gateway.check_community(bot.token, chat_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			community.bot_id = bot.id
			await session.commit()
		await self._refresh_forum(community_id, info)
		dto = await self._fresh_dto(community_id)
		logger.info("Каналу «%s» назначен бот «%s».", dto.title, bot.label)
		return dto

	async def unassign_bot(self, community_id: int) -> CommunityDto:
		"""Отвязывает бота от канала (сам бот остаётся в приложении).

		Raises:
			CommunityError: Канал не найден.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			community.bot_id = None
			await session.commit()
		dto = await self._fresh_dto(community_id)
		logger.info("От канала «%s» отвязан бот.", dto.title)
		return dto

	async def _probe_bot(self, token: str, chat_id: str) -> _ProbeResult:
		"""Проверяет права бота, не роняя перепроверку."""
		try:
			info = await self._gateway.check_community(token, chat_id)
		except Exception:  # noqa: BLE001 — итог отражается в ответе
			return _ProbeResult(ok=False)
		return _ProbeResult(ok=True, info=info)

	async def _refresh_forum(self, community_id: int, info: CommunityInfo) -> None:
		"""Обновляет изменчивые свойства по свежей проверке (ADR-0021).

		Сейчас изменчив только признак форума. Вид не трогается: он
		определяется подключением; расхождение с Telegram — предупреждение
		в лог (запись остаётся прежней, владелец переподключит).
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			if community.kind != info.kind:
				logger.warning(
					"Вид сообщества «%s» по Telegram (%s) расходится с записью (%s) — не меняю.",
					community.title,
					info.kind,
					community.kind,
				)
			if community.forum != info.forum:
				community.forum = info.forum
				await session.commit()
				logger.info("Сообщество «%s»: признак форума → %s.", community.title, info.forum)

	async def delete_community(self, community_id: int) -> None:
		"""Удаляет канал со всем хозяйством (из приложения, не из Telegram).

		Настройки и подписи канала убирают каскады внешних ключей:
		политики объявлены в схеме, проверку ключей включает ``Database``
		на каждом соединении.
		"""
		async with self._db.session_factory() as session:
			community = await session.get(Community, community_id)
			if community is None:
				# идемпотентность сознательная (повторный клик), но след нужен
				logger.info("Канал id=%s уже отсутствует — удалять нечего.", community_id)
				return
			title = community.title
			await session.delete(community)
			await session.commit()
		logger.info("Канал «%s» (id=%s) удалён из приложения.", title, community_id)

	async def _fresh_dto(self, community_id: int) -> CommunityDto:
		"""Снимок канала из БД с подгруженными публикаторами и настройками.

		Raises:
			CommunityError: Канал не найден.
		"""
		enabled = await self._settings.get_for(COMMUNITY_ENABLED, community_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			return self._dto(community, enabled=enabled)

	@staticmethod
	async def _community_in_session(
		session: AsyncSession, community_id: int, *, with_refs: bool = False
	) -> Community:
		"""Канал по id в переданной сессии — или «не найден» понятным текстом.

		``with_refs=True`` подгружает бота и userbot-аккаунт сразу: ``_dto``
		работает на отсоединённом объекте, и ленивое обращение упало бы
		``MissingGreenlet`` вместо внятной ошибки.
		"""
		if with_refs:
			community = (
				await session.execute(
					select(Community)
					.options(selectinload(Community.bot), selectinload(Community.tg_account))
					.where(Community.id == community_id)
				)
			).scalar_one_or_none()
		else:
			community = await session.get(Community, community_id)
		if community is None:
			raise CommunityError("Канал не найден — обновите список.")
		return community

	async def _get_bot(self, bot_id: int) -> Bot:
		"""Возвращает бота или объясняет, что он не найден."""
		async with self._db.session_factory() as session:
			bot = await session.get(Bot, bot_id)
		if bot is None:
			raise CommunityError("Бот не найден — добавьте его в Настройках.")
		return bot

	async def _get_account(self, account_id: int) -> TgAccount:
		"""Возвращает userbot-аккаунт или объясняет, что он не найден."""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
		if account is None:
			raise CommunityError("Аккаунт не найден — добавьте его в Настройках.")
		return account

	@staticmethod
	def _dto(community: Community, enabled: bool = True) -> CommunityDto:
		"""Снимок канала; связи должны быть подгружены (with_refs)."""
		return CommunityDto(
			community.id,
			community.title,
			community.username,
			community.tg_chat_id,
			community.bot_id,
			community.bot.label if community.bot is not None else None,
			enabled,
			community.tg_account_id,
			community.tg_account.label if community.tg_account is not None else None,
			kind=CommunityKind(community.kind),
			forum=community.forum,
		)
