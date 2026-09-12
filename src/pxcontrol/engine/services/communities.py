"""Сервис сообществ: подключение, участники, публикаторы, список, удаление.

В сообществе состоит пул userbot-аккаунтов (``community_members``,
ADR-0022) с ролями из зондов прав; публикует аккаунт-умолчание
(``communities.default_tg_account_id``) — постинг идёт из его сессии.
Бот — самостоятельная сущность (работает по токену, без пользовательской
сессии) — ``communities.bot_id``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, CommunityMember, TgAccount
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.accounts import account_display
from pxcontrol.engine.services.posts import PublishCapabilities, publish_capabilities
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.bot_api import CommunityCheckError
from pxcontrol.engine.telegram.mtproto import UserbotAccessError
from pxcontrol.engine.telegram.types import CommunityInfo, CommunityKind, UserbotRole

logger = logging.getLogger(__name__)


class CommunityError(EngineError):
	"""Ошибка операций с каналами (с понятным человеку текстом)."""


#: Связи для снимка DTO: бот, аккаунт-умолчание, членства с аккаунтами.
_REF_LOADERS = (
	selectinload(Community.bot),
	selectinload(Community.default_account),
	selectinload(Community.members).selectinload(CommunityMember.tg_account),
)


@dataclass(frozen=True)
class MemberDto:
	"""Участник сообщества — userbot-аккаунт с ролью (ADR-0022)."""

	account_id: int
	label: str
	role: UserbotRole
	is_default: bool


@dataclass(frozen=True)
class _ProbeResult:
	"""Итог сетевого зонда прав публикатора.

	Attributes:
		ok: True/False — Telegram подтвердил наличие/отсутствие прав;
			None — проверить не удалось (нет связи, аккаунт отключён):
			это не знание о правах, менять привязку по нему нельзя.
		info: свежие данные сообщества при ``ok is True`` — из них
			обновляются изменчивые свойства: признак форума (ADR-0021),
			название и @имя.
	"""

	ok: bool | None
	info: CommunityInfo | None = None


class _CommunityChecker(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def bot_check_community(self, token: str, chat_ref: str) -> CommunityInfo: ...

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
	default_account_id: int | None = None
	default_account_label: str | None = None
	default_role: UserbotRole | None = None
	members_count: int = 0
	kind: CommunityKind = CommunityKind.CHANNEL
	forum: bool = False

	@property
	def userbot_assigned(self) -> bool:
		"""Назначен ли публикатор по умолчанию (ADR-0022)."""
		return self.default_account_id is not None

	@property
	def capabilities(self) -> PublishCapabilities:
		"""Чем это сообщество может публиковать (ADR-0011).

		Перевод «сообщество → способы публикации» живёт здесь, рядом
		с самими признаками: прежде интерфейс собирал его руками
		в трёх местах, и правило «бот назначен» пришлось бы менять
		в каждом.
		"""
		return publish_capabilities(self.bot_id is not None, self.userbot_assigned)


@dataclass(frozen=True)
class CommunityAccess:
	"""Итог перепроверки доступов канала.

	Attributes:
		community: канал с обновлённой привязкой userbot.
		userbot_ok: привязанный аккаунт — админ с правом публиковать
			(None — проверить не удалось: нет связи или аккаунт отключён;
			для канала без привязки None означает «админ не нашёлся
			и среди вошедших аккаунтов»).
		bot_ok: права бота на месте; None — либо бот не назначен, либо
			проверить не удалось (нет связи, Telegram не ответил).
			Различить помогает ``community.bot_id``: назначен, но
			``bot_ok`` None — значит не проверили, а не «потерял права».
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
		profile_sync: Callable[[int], Awaitable[None]] | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно:
		настройки каналов не кэшируются). ``profile_sync`` — крючок
		актуализации профиля аккаунта (движок передаёт
		``AccountsService.sync_profile``): зонд прав дёргает его при
		живом ответе — сервис аккаунтов сервису сообществ не нужен."""
		self._db = db
		self._gateway = gateway
		self._settings = settings if settings is not None else SettingsService(db)
		self._profile_sync = profile_sync

	async def list_communities(self) -> list[CommunityDto]:
		"""Возвращает все подключённые каналы (с именами публикаторов)."""
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(Community).options(*_REF_LOADERS).order_by(Community.id)
				)
			).scalars()
			return [
				self._dto(ch, enabled=enabled.get(ch.id, COMMUNITY_ENABLED.default)) for ch in rows
			]

	async def get_community(self, community_id: int) -> CommunityDto:
		"""Свежий снимок одного сообщества (страница сообщества в интерфейсе).

		Raises:
			CommunityError: Сообщество не найдено (например, уже удалено).
		"""
		return await self._fresh_dto(community_id)

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
		info = await self._gateway.bot_check_community(bot.token, chat_ref)
		# «не удалось проверить» при подключении равносильно «публикатора
		# нет»: участника добавит перепроверка, когда аккаунт появится
		found = await self._find_userbot_publisher(info.chat_id)
		community = await self._store_community(info, bot_id=bot.id, member=found)
		logger.info(
			"Подключено «%s» (бот %s, userbot-публикатор: %s).",
			info.title,
			bot.label,
			found[0] if found else "нет",
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
		logger.info(
			"Подключаю канал через userbot «%s»: ввод %r.",
			self._account_display(account),
			chat_ref,
		)
		info = await self._gateway.check_community_userbot(account_id, chat_ref)
		await self._sync_profile(account_id)
		role = info.role or UserbotRole.MEMBER  # userbot-зонд всегда отдаёт роль
		community = await self._store_community(info, bot_id=None, member=(account_id, role))
		logger.info(
			"Подключён канал «%s» (userbot «%s»).", info.title, self._account_display(account)
		)
		return await self._fresh_dto(community.id)

	async def _probe_userbot(self, account_id: int, chat_id: str) -> _ProbeResult:
		"""Проверяет права одного аккаунта (сбой не мешает операции)."""
		try:
			info = await self._gateway.check_community_userbot(account_id, chat_id)
		except UserbotAccessError:
			logger.info("Аккаунт id=%s не может публиковать в сообществе %s.", account_id, chat_id)
			return _ProbeResult(ok=False)
		except Exception as exc:  # noqa: BLE001 — вспомогательная проверка
			# тип и текст обязательны: без них обрыв сети и ошибка в коде
			# выглядят в журнале одинаково (единый приём движка)
			logger.warning(
				"Проверка аккаунта id=%s в сообществе %s не удалась (%s: %s).",
				account_id,
				chat_id,
				type(exc).__name__,
				exc,
			)
			return _ProbeResult(ok=None)
		await self._sync_profile(account_id)
		return _ProbeResult(ok=True, info=info)

	async def _sync_profile(self, account_id: int) -> None:
		"""Актуализирует профиль аккаунта после живого ответа Telegram.

		Крючок движка (``AccountsService.sync_profile``): зовётся везде,
		где проверка прав аккаунта подтвердилась — соединение живое,
		имя и @имя можно спросить. Свой сбой крючок гасит сам,
		операцию-носителя он не портит.
		"""
		if self._profile_sync is not None:
			await self._profile_sync(account_id)

	async def _find_userbot_publisher(self, chat_id: str) -> tuple[int, UserbotRole] | None:
		"""Ищет аккаунт, способный публиковать (первый подходящий), с ролью.

		Для попутного членства на бот-пути и перепроверки сообщества без
		участников. Порядок — по id аккаунта; сбои проверок пропускаются.
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
			probe = await self._probe_userbot(account_id, chat_id)
			if probe.ok is True and probe.info is not None:
				return account_id, probe.info.role or UserbotRole.MEMBER
		return None

	async def _store_community(
		self,
		info: CommunityInfo,
		*,
		bot_id: int | None,
		member: tuple[int, UserbotRole] | None,
	) -> Community:
		"""Сохраняет сообщество из проверенных данных, отклоняя дубликат.

		Вид и признак форума берутся из проверки транспорта (ADR-0021).
		``member`` — первый участник (id аккаунта, роль): он же становится
		публикатором по умолчанию (ADR-0022); None — без участников.

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
				default_tg_account_id=member[0] if member else None,
			)
			session.add(community)
			await session.flush()
			if member is not None:
				session.add(
					CommunityMember(
						community_id=community.id, tg_account_id=member[0], role=member[1]
					)
				)
			await session.commit()
			await session.refresh(community)
		return community

	async def recheck_community(self, community_id: int) -> CommunityAccess:
		"""Перепроверяет всех участников и бота сообщества (ADR-0022).

		Каждому участнику — свой зонд: подтверждённое право обновляет
		роль, подтверждённый отказ удаляет членство (участник-умолчание
		при этом теряет и умолчание — публикация останавливается честно,
		а не падала бы), сбой связи ничего не трогает. Сообщество совсем
		без участников — публикатор ищется среди вошедших аккаунтов
		(авто-восстановление, как раньше); при живых участниках без
		умолчания авто-выбора нет — выбор лица за пользователем.
		Потеря прав бота его не отвязывает — только сообщается.

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		# сессии короткие, сетевые зонды — между ними: открытая транзакция
		# чтения на время походов в Telegram держала бы SQLite занятым
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			tg_chat_id = community.tg_chat_id
			default_id = community.default_tg_account_id
			member_ids = [member.tg_account_id for member in community.members]
			bot_token = community.bot.token if community.bot is not None else None
		userbot_ok: bool | None = None
		fresh_info: CommunityInfo | None = None
		for account_id in member_ids:
			probe = await self._probe_userbot(account_id, tg_chat_id)
			if account_id == default_id:
				userbot_ok = probe.ok
				fresh_info = probe.info or fresh_info
			if probe.ok is True and probe.info is not None:
				await self._update_member_role(
					community_id, account_id, probe.info.role or UserbotRole.MEMBER
				)
			elif probe.ok is False:
				await self._drop_member(community_id, account_id)
		if not member_ids:
			found = await self._find_userbot_publisher(tg_chat_id)
			if found is not None:
				await self._adopt_member(community_id, found, make_default=True)
			userbot_ok = True if found is not None else None
		bot_ok: bool | None = None
		if bot_token is not None:
			bot_probe = await self._probe_bot(bot_token, tg_chat_id)
			bot_ok = bot_probe.ok
			fresh_info = fresh_info or bot_probe.info
		if fresh_info is not None:
			await self._refresh_mutable(community_id, fresh_info)
		dto = await self._fresh_dto(community_id)
		logger.info(
			"Доступы «%s»: умолчание=%s (аккаунт %s), участников %s, бот=%s.",
			dto.title,
			userbot_ok,
			dto.default_account_id or "—",
			dto.members_count,
			bot_ok,
		)
		return CommunityAccess(dto, userbot_ok, bot_ok)

	async def _update_member_role(
		self, community_id: int, account_id: int, role: UserbotRole
	) -> None:
		"""Обновляет роль членства по подтверждённому зонду (ADR-0022)."""
		async with self._db.session_factory() as session:
			member = await session.get(CommunityMember, (community_id, account_id))
			if member is not None and member.role != role:
				member.role = role
				await session.commit()
				logger.info(
					"Роль аккаунта id=%s в сообществе id=%s: %s.",
					account_id,
					community_id,
					role,
				)

	async def _drop_member(self, community_id: int, account_id: int) -> None:
		"""Удаляет членство по подтверждённому отказу Telegram.

		Участник-умолчание теряет и умолчание (инвариант ADR-0022:
		умолчание — действующий участник); авто-замены нет.
		"""
		async with self._db.session_factory() as session:
			member = await session.get(CommunityMember, (community_id, account_id))
			if member is None:
				return
			await session.delete(member)
			community = await self._community_in_session(session, community_id)
			if community.default_tg_account_id == account_id:
				community.default_tg_account_id = None
			await session.commit()
		logger.info(
			"Аккаунт id=%s исключён из сообщества id=%s (подтверждённый отказ прав).",
			account_id,
			community_id,
		)

	async def _adopt_member(
		self, community_id: int, member: tuple[int, UserbotRole], *, make_default: bool
	) -> None:
		"""Добавляет найденного публикатора (авто-восстановление)."""
		account_id, role = member
		async with self._db.session_factory() as session:
			if await session.get(CommunityMember, (community_id, account_id)) is None:
				session.add(
					CommunityMember(community_id=community_id, tg_account_id=account_id, role=role)
				)
			community = await self._community_in_session(session, community_id)
			if make_default and community.default_tg_account_id is None:
				community.default_tg_account_id = account_id
			await session.commit()
		logger.info(
			"Аккаунт id=%s принят участником сообщества id=%s (%s).",
			account_id,
			community_id,
			role,
		)

	async def list_members(self, community_id: int) -> list[MemberDto]:
		"""Участники сообщества с ролями; умолчание помечено (ADR-0022).

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			return [
				MemberDto(
					account_id=member.tg_account_id,
					label=self._account_display(member.tg_account),
					role=UserbotRole(member.role),
					is_default=member.tg_account_id == community.default_tg_account_id,
				)
				for member in sorted(community.members, key=lambda m: m.tg_account_id)
			]

	async def add_member(self, community_id: int, account_id: int) -> list[MemberDto]:
		"""Добавляет аккаунт участником (с проверкой его прав и ролью).

		Первый участник сообщества автоматически становится публикатором
		по умолчанию (иначе публикация так и осталась бы недоступной);
		дальше умолчание меняется только явно (:meth:`set_default`).

		Raises:
			CommunityError: Сообщество/аккаунт не найдены или уже участник.
			UserbotUnavailableError: Аккаунт не подключён или прав нет.
		"""
		account = await self._get_account(account_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			chat_id = community.tg_chat_id
			if await session.get(CommunityMember, (community_id, account_id)) is not None:
				raise CommunityError(
					f"«{self._account_display(account)}» уже участник этого сообщества."
				)
			had_members = bool(community.members)
		info = await self._gateway.check_community_userbot(account_id, chat_id)
		await self._sync_profile(account_id)
		await self._adopt_member(
			community_id,
			(account_id, info.role or UserbotRole.MEMBER),
			make_default=not had_members,
		)
		await self._refresh_mutable(community_id, info)
		logger.info(
			"Сообществу id=%s добавлен участник «%s».",
			community_id,
			self._account_display(account),
		)
		return await self.list_members(community_id)

	async def remove_member(self, community_id: int, account_id: int) -> list[MemberDto]:
		"""Удаляет участника; умолчание при этом сбрасывается (ADR-0022).

		Авто-выбора нового умолчания нет: смена «от чьего имени» — явное
		решение пользователя.

		Raises:
			CommunityError: Сообщество не найдено или аккаунт не участник.
		"""
		async with self._db.session_factory() as session:
			await self._community_in_session(session, community_id)
			if await session.get(CommunityMember, (community_id, account_id)) is None:
				raise CommunityError("Аккаунт не участник этого сообщества.")
		await self._drop_member(community_id, account_id)
		return await self.list_members(community_id)

	async def set_default(self, community_id: int, account_id: int) -> CommunityDto:
		"""Назначает публикатора по умолчанию из участников (ADR-0022).

		Raises:
			CommunityError: Сообщество не найдено или аккаунт не участник.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			if await session.get(CommunityMember, (community_id, account_id)) is None:
				raise CommunityError(
					"Публикатором может стать только участник — сначала добавьте аккаунт."
				)
			community.default_tg_account_id = account_id
			await session.commit()
		dto = await self._fresh_dto(community_id)
		logger.info("Публикатор «%s» по умолчанию: аккаунт id=%s.", dto.title, account_id)
		return dto

	async def assign_userbot(self, community_id: int, account_id: int) -> CommunityDto:
		"""Мост прежнего интерфейса: участник + умолчание одним действием.

		Страница этапа B заменит его диалогом «Участники…»; до тех пор
		кнопка «Привязать userbot…» работает через членства честно.

		Raises:
			CommunityError: Сообщество/аккаунт не найдены.
			UserbotUnavailableError: Аккаунт не подключён или прав нет.
		"""
		account = await self._get_account(account_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			chat_id = community.tg_chat_id
		info = await self._gateway.check_community_userbot(account_id, chat_id)
		await self._adopt_member(
			community_id, (account_id, info.role or UserbotRole.MEMBER), make_default=False
		)
		dto = await self.set_default(community_id, account_id)
		await self._refresh_mutable(community_id, info)
		logger.info("«%s»: публикатор userbot «%s».", dto.title, self._account_display(account))
		return await self._fresh_dto(community_id)

	async def unassign_userbot(self, community_id: int) -> CommunityDto:
		"""Мост прежнего интерфейса: убирает умолчание вместе с членством.

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			default_id = community.default_tg_account_id
		if default_id is not None:
			await self._drop_member(community_id, default_id)
		dto = await self._fresh_dto(community_id)
		logger.info("У «%s» снят публикатор userbot.", dto.title)
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
		info = await self._gateway.bot_check_community(bot.token, chat_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			community.bot_id = bot.id
			await session.commit()
		await self._refresh_mutable(community_id, info)
		dto = await self._fresh_dto(community_id)
		logger.info("Каналу «%s» назначен бот «%s».", dto.title, bot.label)
		return dto

	async def unassign_bot(self, community_id: int) -> CommunityDto:
		"""Отвязывает бота от канала (сам бот остаётся в приложении).

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			community.bot_id = None
			await session.commit()
		dto = await self._fresh_dto(community_id)
		logger.info("От канала «%s» отвязан бот.", dto.title)
		return dto

	async def _probe_bot(self, token: str, chat_id: str) -> _ProbeResult:
		"""Проверяет права бота, не роняя перепроверку.

		Различает то же, что и зонд userbot: подтверждённый отказ
		Telegram — это знание о правах (``ok=False``), а обрыв связи
		или неверный токен — отсутствие знания (``ok=None``). Раньше
		обе причины давали «права потеряны», и человек видел приговор
		правам из-за пропавшей сети.
		"""
		try:
			info = await self._gateway.bot_check_community(token, chat_id)
		except CommunityCheckError as exc:
			logger.info("Бот не может публиковать в сообществе %s: %s", chat_id, exc)
			return _ProbeResult(ok=False)
		except Exception as exc:  # noqa: BLE001 — вспомогательная проверка
			logger.warning(
				"Проверка бота в сообществе %s не удалась (%s: %s).",
				chat_id,
				type(exc).__name__,
				exc,
			)
			return _ProbeResult(ok=None)
		return _ProbeResult(ok=True, info=info)

	async def _refresh_mutable(self, community_id: int, info: CommunityInfo) -> None:
		"""Обновляет изменчивые свойства по свежей проверке (ADR-0021).

		Изменчивы признак форума, название и @имя (username; None —
		имя сняли, сообщество стало приватным): владелец меняет их
		в Telegram в любой момент, запись не должна застывать на момент
		подключения. Вид не трогается: он определяется подключением;
		расхождение с Telegram — предупреждение в лог (запись остаётся
		прежней, владелец переподключит).
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
			changed = False
			if community.forum != info.forum:
				community.forum = info.forum
				changed = True
				logger.info("Сообщество «%s»: признак форума → %s.", info.title, info.forum)
			if community.title != info.title:
				logger.info("Сообщество «%s» переименовано → «%s».", community.title, info.title)
				community.title = info.title
				changed = True
			if community.username != info.username:
				logger.info(
					"Сообщество «%s»: @имя %s → %s.",
					info.title,
					community.username or "—",
					info.username or "— (стало приватным)",
				)
				community.username = info.username
				changed = True
			if changed:
				await session.commit()

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
				logger.info("Сообщество id=%s уже отсутствует — удалять нечего.", community_id)
				return
			title = community.title
			await session.delete(community)
			await session.commit()
		logger.info("Сообщество «%s» (id=%s) удалено из приложения.", title, community_id)

	async def _fresh_dto(self, community_id: int) -> CommunityDto:
		"""Снимок канала из БД с подгруженными публикаторами и настройками.

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		enabled = await self._settings.get_for(COMMUNITY_ENABLED, community_id)
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			return self._dto(community, enabled=enabled)

	@staticmethod
	async def _community_in_session(
		session: AsyncSession, community_id: int, *, with_refs: bool = False
	) -> Community:
		"""Сообщество по id в переданной сессии — или «не найдено» понятным текстом.

		``with_refs=True`` подгружает бота и userbot-аккаунт сразу: ``_dto``
		работает на отсоединённом объекте, и ленивое обращение упало бы
		``MissingGreenlet`` вместо внятной ошибки.
		"""
		if with_refs:
			community = (
				await session.execute(
					select(Community).options(*_REF_LOADERS).where(Community.id == community_id)
				)
			).scalar_one_or_none()
		else:
			community = await session.get(Community, community_id)
		if community is None:
			raise CommunityError("Сообщество не найдено — обновите список.")
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
	def _account_display(account: TgAccount) -> str:
		"""Отображаемое имя аккаунта (единая точка — :func:`account_display`)."""
		return account_display(
			account.label, account.username, account.first_name, account.last_name, account.phone
		)

	@staticmethod
	def _dto(community: Community, enabled: bool = True) -> CommunityDto:
		"""Снимок сообщества; связи должны быть подгружены (with_refs)."""
		default = community.default_account
		default_role = next(
			(
				UserbotRole(member.role)
				for member in community.members
				if member.tg_account_id == community.default_tg_account_id
			),
			None,
		)
		return CommunityDto(
			community.id,
			community.title,
			community.username,
			community.tg_chat_id,
			community.bot_id,
			community.bot.label if community.bot is not None else None,
			enabled,
			community.default_tg_account_id,
			CommunitiesService._account_display(default) if default is not None else None,
			default_role=default_role,
			members_count=len(community.members),
			kind=CommunityKind(community.kind),
			forum=community.forum,
		)
