"""Сервис сообществ: подключение, исполнители, публикаторы, список, удаление.

В сообществе работает пул **исполнителей** обоих видов
(``community_executors``, ADR-0035): userbot-аккаунты и боты, у каждого —
своё участие и полный снимок прав. Членство здесь факт, а не пропуск:
строка живёт, пока её не убрал человек, а потеря прав меняет снимок.

Назначения хранятся отдельно от прав, по одному на вид:
``communities.default_tg_account_id`` — публикатор-пользователь (постинг
идёт из его сессии, ADR-0011), ``communities.default_bot_id`` —
публикатор-бот (запасной путь: работает по токену, без пользовательской
сессии). Права приходят от Telegram, назначение принимает человек —
смешивать их нельзя.

Здесь же живут правила «что это сообщество может» (``community_capabilities``)
и «почему не может» (``publisher_paused``, ``publisher_incapable``):
их читают и подготовка публикации, и дозор кнопок, и интерфейс.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, CommunityExecutor, TgAccount
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.abilities import (
	BOT_ADMIN_RIGHTS,
	ExecutorAction,
	can,
)
from pxcontrol.engine.services.accounts import account_display
from pxcontrol.engine.services.publish_route import (
	PublishCapabilities,
	publish_capabilities,
)
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.bot_api import BotError
from pxcontrol.engine.telegram.lane import LaneOwner, OwnerKind
from pxcontrol.engine.telegram.mtproto import UserbotAccessError, UserbotUnavailableError
from pxcontrol.engine.telegram.rights import AdminRights, ExecutorRights, ParticipantStatus
from pxcontrol.engine.telegram.types import BotRef, CommunityInfo, CommunityKind

logger = logging.getLogger(__name__)


class CommunityError(EngineError):
	"""Ошибка операций с каналами (с понятным человеку текстом)."""


class JoinOutcome(StrEnum):
	"""Чем кончился ввод исполнителя в сообщество (ADR-0035).

	Исходов несколько не ради дробности: человеку важно разное — «он
	уже был там» ничего не изменило в Telegram, «вступил» и «приглашён»
	изменили, а «заявка отправлена» требует чьего-то одобрения, и без
	него исполнитель не заработает.
	"""

	ALREADY_IN = "already_in"  # состоял и раньше — в Telegram ничего не делали
	JOINED = "joined"  # вступил сам: по @имени или по ссылке
	REQUESTED = "requested"  # заявка на вступление отправлена и ждёт одобрения
	INVITED = "invited"  # приглашён нашим исполнителем
	PROMOTED = "promoted"  # принят в канал назначением администратором
	# ввести нечем: сообщество приватное, готовой ссылки Telegram не отдал,
	# пригласить некому. Это **исход**, а не ошибка: в Telegram ничего
	# не изменилось, и операция продолжится, когда человек даст ссылку.
	# Исключением такой случай быть не может — мост отдаёт интерфейсу
	# текст ошибки, а не её тип, и распознавание свелось бы к разбору строки
	NEEDS_LINK = "needs_link"


@dataclass(frozen=True)
class JoinResult:
	"""Итог ввода исполнителя: что произошло и каким стал пул."""

	outcome: JoinOutcome
	executors: list[ExecutorDto]


#: Связи для снимка DTO: назначенные публикаторы и все исполнители
#: со своими учётками — снимок строится на отсоединённом объекте,
#: и ленивое обращение упало бы ``MissingGreenlet``.
_REF_LOADERS = (
	selectinload(Community.default_bot),
	selectinload(Community.default_account),
	selectinload(Community.executors).selectinload(CommunityExecutor.tg_account),
	selectinload(Community.executors).selectinload(CommunityExecutor.bot),
)


def executor_owner(row: CommunityExecutor) -> LaneOwner:
	"""Владелец строки — ключ, общий со шлюзом и учётом активности (ADR-0035).

	Raises:
		CommunityError: Строка без владельца — такого не допускает схема,
			но читать данные вслепую нельзя.
	"""
	if row.tg_account_id is not None:
		return LaneOwner(OwnerKind.USER, row.tg_account_id)
	if row.bot_id is not None:
		return LaneOwner(OwnerKind.BOT, row.bot_id)
	raise CommunityError("Строка исполнителя без владельца — данные повреждены.")


def executor_rights(row: CommunityExecutor) -> ExecutorRights:
	"""Снимок прав исполнителя из его строки."""
	return ExecutorRights.from_payload(ParticipantStatus(row.status), row.rights)


def executor_paused(row: CommunityExecutor) -> bool:
	"""Приостановлен ли исполнитель человеком (ADR-0029).

	Связь ``tg_account``/``bot`` должна быть подгружена.
	"""
	owner = row.tg_account if row.tg_account_id is not None else row.bot
	return bool(owner is not None and owner.paused)


def executor_label(row: CommunityExecutor) -> str:
	"""Человеческое имя исполнителя: пометка пользователя или название бота."""
	if row.tg_account is not None:
		return account_display(
			row.tg_account.label,
			row.tg_account.username,
			row.tg_account.first_name,
			row.tg_account.last_name,
			row.tg_account.phone,
		)
	return row.bot.label if row.bot is not None else "исполнитель"


def _executor_row(community_id: int, owner: LaneOwner, rights: ExecutorRights) -> CommunityExecutor:
	"""Новая строка исполнителя: владелец — ровно одна из двух ссылок."""
	return CommunityExecutor(
		community_id=community_id,
		tg_account_id=owner.id if owner.kind is OwnerKind.USER else None,
		bot_id=owner.id if owner.kind is OwnerKind.BOT else None,
		status=rights.status,
		rights=rights.to_payload(),
		checked_at=datetime.now(UTC),
	)


def publisher_row(community: Community, kind: OwnerKind) -> CommunityExecutor | None:
	"""Строка назначенного публикатора этого вида (None — не назначен).

	Назначение — ссылка сообщества, исполнитель — строка пула; здесь они
	сводятся. Связь ``executors`` должна быть подгружена.
	"""
	target = community.default_tg_account_id if kind is OwnerKind.USER else community.default_bot_id
	if target is None:
		return None
	for row in community.executors:
		owner_id = row.tg_account_id if kind is OwnerKind.USER else row.bot_id
		if owner_id == target:
			return row
	return None


def publisher_ready(community: Community, kind: OwnerKind) -> bool:
	"""Может ли назначенный публикатор этого вида публиковать сейчас.

	Три условия: назначен, не приостановлен человеком (ADR-0029)
	и по последнему снимку прав способен публиковать в сообществе
	такого вида (ADR-0035).
	"""
	row = publisher_row(community, kind)
	if row is None or executor_paused(row):
		return False
	return can(executor_rights(row), ExecutorAction.PUBLISH, CommunityKind(community.kind))


def community_capabilities(community: Community) -> PublishCapabilities:
	"""Чем это сообщество может публиковать (ADR-0011, ADR-0035).

	Одна точка на весь движок и интерфейс: подготовка публикации, дозор
	кнопок, дашборд и формы спрашивают её, а не собирают правило заново.
	Связи ``executors`` и учётки исполнителей должны быть подгружены.
	"""
	bot_ready = publisher_ready(community, OwnerKind.BOT)
	markup_edit = False
	if bot_ready:
		row = publisher_row(community, OwnerKind.BOT)
		markup_edit = row is not None and can(
			executor_rights(row), ExecutorAction.EDIT_OTHERS, CommunityKind(community.kind)
		)
	return publish_capabilities(
		bot_ready, publisher_ready(community, OwnerKind.USER), markup_edit=markup_edit
	)


def publisher_paused(community: Community) -> bool:
	"""Есть ли у сообщества **приостановленный** публикатор (ADR-0029).

	Зовут это только из ветки «публиковать некем», чтобы отличить
	«нет публикатора» от «публикатор на паузе»: в первом случае человеку
	нужно назначить нового, во втором — возобновить прежнего.
	"""
	rows = (publisher_row(community, OwnerKind.USER), publisher_row(community, OwnerKind.BOT))
	return any(row is not None and executor_paused(row) for row in rows)


def publisher_incapable(community: Community) -> bool:
	"""Назначен, не на паузе — и по правам публиковать не может (ADR-0035).

	Третья причина ожидания рядом с «выключено» и «приостановлен»:
	права в Telegram меняет владелец сообщества, и приложение узнаёт
	об этом перепроверкой доступов. Пост в таком случае ждёт, а не падает.
	"""
	kind = CommunityKind(community.kind)
	for owner_kind in (OwnerKind.USER, OwnerKind.BOT):
		row = publisher_row(community, owner_kind)
		if row is None or executor_paused(row):
			continue
		if not can(executor_rights(row), ExecutorAction.PUBLISH, kind):
			return True
	return False


@dataclass(frozen=True)
class AccountMembershipDto:
	"""Сообщество глазами аккаунта: снимок, участие в нём и признак умолчания (ADR-0029)."""

	community: CommunityDto
	status: ParticipantStatus
	is_default: bool


@dataclass(frozen=True)
class ExecutorDto:
	"""Исполнитель сообщества для показа: кто он и что ему здесь можно (ADR-0035).

	Attributes:
		owner: вид и id — тот же ключ, что у дорожки шлюза и учёта
			активности.
		label: человеческое имя (пометка пользователя, название бота).
		status: участие в сообществе.
		rights: полный снимок прав.
		is_default: назначен публикатором своего вида.
		paused: приостановлен человеком (ADR-0029) — приложение его
			не использует, но назначение сохраняется.
		can_publish: по правам способен публиковать в этом сообществе
			(вид сообщества уже учтён).
		checked_at: когда снимали снимок; None — снимка не было, права
			перенесены из прежней модели.
	"""

	owner: LaneOwner
	label: str
	status: ParticipantStatus
	rights: ExecutorRights
	is_default: bool
	paused: bool
	can_publish: bool
	checked_at: datetime | None = None


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

	async def bot_check_community(self, bot: BotRef, chat_ref: str) -> CommunityInfo: ...

	async def userbot_check_community(self, account_id: int, chat_ref: str) -> CommunityInfo: ...

	async def userbot_join_public(self, account_id: int, username: str) -> None: ...

	async def userbot_join_by_invite(self, account_id: int, link: str) -> bool: ...

	async def userbot_invite_link(self, account_id: int, chat_id: str) -> str | None: ...

	async def userbot_invite_participant(
		self, account_id: int, chat_id: str, target: str
	) -> None: ...

	async def userbot_promote(
		self, account_id: int, chat_id: str, target: str, rights: AdminRights
	) -> None: ...


@dataclass(frozen=True)
class CommunityDto:
	"""Сообщество для показа в интерфейсе (вид и форум — ADR-0021)."""

	id: int
	title: str
	username: str | None
	tg_chat_id: str
	default_bot_id: int | None
	default_bot_label: str | None
	enabled: bool
	default_account_id: int | None = None
	default_account_label: str | None = None
	default_status: ParticipantStatus | None = None
	executors_count: int = 0
	kind: CommunityKind = CommunityKind.CHANNEL
	forum: bool = False
	# публикаторы приостановлены человеком (ADR-0029): назначение
	# сохранено, но приложение их не использует
	default_account_paused: bool = False
	default_bot_paused: bool = False
	# готовность публикаторов — уже с учётом прав и паузы (ADR-0035):
	# движок считает их одной точкой (``community_capabilities``),
	# интерфейс получает готовый ответ и правило не пересобирает
	userbot_ready: bool = False
	bot_ready: bool = False
	markup_edit: bool = False
	# публикатор назначен и не на паузе, но по правам публиковать
	# не может — отдельная причина, не «нет публикатора» (ADR-0035)
	publisher_incapable: bool = False

	@property
	def userbot_assigned(self) -> bool:
		"""Назначен ли публикатор по умолчанию (ADR-0022).

		Именно назначен — приостановленный тоже считается: признак
		отвечает на вопрос «кому принадлежит лицо поста», а не «уйдёт
		ли пост сейчас» (за это отвечает :attr:`capabilities`).
		"""
		return self.default_account_id is not None

	@property
	def capabilities(self) -> PublishCapabilities:
		"""Чем это сообщество может публиковать (ADR-0011, ADR-0035).

		Готовность публикаторов уже посчитана движком одной точкой
		(``community_capabilities``): в ней и пауза, и права, и вид
		сообщества. Интерфейс правило не пересобирает — прежде оно
		жило в трёх местах сразу.
		"""
		return publish_capabilities(
			self.bot_ready, self.userbot_ready, markup_edit=self.markup_edit
		)

	@property
	def publisher_paused(self) -> bool:
		"""Публиковать некому только из-за паузы (ADR-0029).

		Истинно, когда действующего публикатора нет, но назначенный есть
		и приостановлен: дашборд показывает «публикатор приостановлен»
		вместо «нет публикатора» — назначать нового не нужно, нужно
		возобновить прежнего.
		"""
		caps = self.capabilities
		if caps.userbot or caps.bot:
			return False
		return self.default_account_paused or self.default_bot_paused


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
		"""Подключает сообщество через бота (с попутным поиском публикатора).

		Права публиковать здесь **не требуются** (ADR-0035, п. 7):
		подключение отвечает на вопрос «видно ли сообщество», а «кто
		и что в нём может» — это снимок прав, и он записывается. Бот
		становится исполнителем и публикатором-ботом; попутно опрашиваются
		вошедшие аккаунты, и первый способный публиковать становится
		публикатором-пользователем (ADR-0019).

		Raises:
			CommunityError: Бот не найден или сообщество уже подключено.
			BotError: Telegram не показал сообщество боту.
			ConnectionError: Нет связи с Telegram.
		"""
		bot = await self._get_bot(bot_id)
		logger.info(
			"Подключаю сообщество: ввод %r, бот «%s» (@%s, id=%s).",
			chat_ref,
			bot.label,
			bot.username,
			bot.id,
		)
		info = await self._gateway.bot_check_community(BotRef(bot.id, bot.token), chat_ref)
		executors = [(LaneOwner(OwnerKind.BOT, bot.id), info.rights)]
		# «не удалось проверить» при подключении равносильно «публикатора
		# нет»: исполнителя добавит перепроверка, когда аккаунт появится
		found = await self._find_userbot_publisher(info.chat_id, info.kind)
		if found is not None:
			executors.append((LaneOwner(OwnerKind.USER, found[0]), found[1]))
		community = await self._store_community(info, executors)
		logger.info(
			"Подключено «%s» (бот %s, userbot-публикатор: %s).",
			info.title,
			bot.label,
			found[0] if found else "нет",
		)
		return await self._fresh_dto(community.id)

	async def add_community_via_userbot(self, account_id: int, chat_ref: str) -> CommunityDto:
		"""Подключает сообщество через выбранный аккаунт — бот не нужен.

		Аккаунт выбирается явно (ADR-0019): проверяется и записывается
		именно его снимок прав, и он же становится публикатором-пользователем.
		Права публиковать не требуются (ADR-0035, п. 7) — сообщество можно
		подключить и ради чтения или реакций.

		Raises:
			CommunityError: Сообщество уже подключено или аккаунт не найден.
			UserbotUnavailableError: Аккаунт не подключён или сообщество
				ему не видно.
		"""
		account = await self._get_account(account_id)
		logger.info(
			"Подключаю сообщество через userbot «%s»: ввод %r.",
			self._account_display(account),
			chat_ref,
		)
		info = await self._gateway.userbot_check_community(account_id, chat_ref)
		await self._sync_profile(account_id)
		community = await self._store_community(
			info, [(LaneOwner(OwnerKind.USER, account_id), info.rights)]
		)
		logger.info(
			"Подключено «%s» (userbot «%s», участие: %s).",
			info.title,
			self._account_display(account),
			info.rights.status,
		)
		return await self._fresh_dto(community.id)

	async def _probe_userbot(self, account_id: int, chat_id: str) -> _ProbeResult:
		"""Проверяет права одного аккаунта (сбой не мешает операции)."""
		try:
			info = await self._gateway.userbot_check_community(account_id, chat_id)
		except UserbotAccessError:
			logger.info("Сообщество %s не видно аккаунту id=%s.", chat_id, account_id)
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

	async def _find_userbot_publisher(
		self, chat_id: str, kind: CommunityKind
	) -> tuple[int, ExecutorRights] | None:
		"""Ищет аккаунт, **способный публиковать** здесь (первый подходящий).

		Для попутного поиска на бот-пути и для сообщества, оставшегося
		без исполнителей. Порядок — по id аккаунта; сбои проверок
		пропускаются; приостановленные (ADR-0029) не опрашиваются.
		Способность считается по снимку прав (ADR-0035): видеть
		сообщество мало — публикатором становится тот, кто может писать.
		"""
		async with self._db.session_factory() as session:
			account_ids = (
				(
					await session.execute(
						select(TgAccount.id)
						.where(TgAccount.session.is_not(None), TgAccount.paused.is_(False))
						.order_by(TgAccount.id)
					)
				)
				.scalars()
				.all()
			)
		for account_id in account_ids:
			probe = await self._probe_userbot(account_id, chat_id)
			if (
				probe.ok is True
				and probe.info is not None
				and can(probe.info.rights, ExecutorAction.PUBLISH, kind)
			):
				return account_id, probe.info.rights
		return None

	async def _store_community(
		self, info: CommunityInfo, executors: Sequence[tuple[LaneOwner, ExecutorRights]]
	) -> Community:
		"""Сохраняет сообщество из проверенных данных, отклоняя дубликат.

		Вид и признак форума берутся из проверки транспорта (ADR-0021).
		``executors`` — те, кто уже проверен: каждый становится строкой
		пула, а первый своего вида — публикатором по умолчанию (ADR-0035).

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
			)
			session.add(community)
			await session.flush()
			for owner, rights in executors:
				session.add(_executor_row(community.id, owner, rights))
				if owner.kind is OwnerKind.USER and community.default_tg_account_id is None:
					community.default_tg_account_id = owner.id
				elif owner.kind is OwnerKind.BOT and community.default_bot_id is None:
					community.default_bot_id = owner.id
			await session.commit()
			await session.refresh(community)
		return community

	async def recheck_community(self, community_id: int) -> CommunityAccess:
		"""Перепроверяет всех исполнителей сообщества (ADR-0035).

		Каждому исполнителю — свой зонд своим транспортом: подтверждённый
		ответ обновляет снимок прав, сбой связи ничего не трогает.
		**Строка не удаляется никогда**: потеря прав и выход из сообщества
		меняют участие, а не членство — иначе вместе с ним пропадало бы
		и назначение публикатором. Приостановленные (ADR-0029)
		не зондируются: обращений к ним нет.

		Сообщество без исполнителей-людей — публикатор ищется среди
		вошедших аккаунтов (авто-восстановление); при живом пуле
		без умолчания авто-выбора нет: выбор лица за человеком.

		Returns:
			Итог: свежий снимок и приговор правам публикаторов —
			True (может публиковать), False (не может) или None
			(проверить не удалось).

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		kind, chat_id, defaults, targets = await self._recheck_targets(community_id)
		verdicts: dict[OwnerKind, bool | None] = {OwnerKind.USER: None, OwnerKind.BOT: None}
		fresh: dict[OwnerKind, CommunityInfo] = {}
		for owner, ref in targets:
			probe = await self._probe_executor(owner, ref, chat_id)
			if probe.ok is True and probe.info is not None:
				await self._store_executor_rights(community_id, owner, probe.info.rights)
				fresh.setdefault(owner.kind, probe.info)
			if defaults.get(owner.kind) == owner.id:
				verdicts[owner.kind] = self._verdict(probe, kind)
		if not any(owner.kind is OwnerKind.USER for owner, _ref in targets):
			verdicts[OwnerKind.USER] = await self._restore_publisher(community_id, chat_id, kind)
		# свежие данные предпочитаем от бота: изменчивые свойства у обоих
		# зондов одинаковы, но бот-путь приносит их вместе с правом правки
		info = fresh.get(OwnerKind.BOT) or fresh.get(OwnerKind.USER)
		if info is not None:
			await self._refresh_mutable(community_id, info)
		dto = await self._fresh_dto(community_id)
		logger.info(
			"Доступы «%s»: публикатор-пользователь=%s (аккаунт %s), бот=%s, исполнителей %s.",
			dto.title,
			verdicts[OwnerKind.USER],
			dto.default_account_id or "—",
			verdicts[OwnerKind.BOT],
			dto.executors_count,
		)
		return CommunityAccess(dto, verdicts[OwnerKind.USER], verdicts[OwnerKind.BOT])

	async def _recheck_targets(
		self, community_id: int
	) -> tuple[
		CommunityKind, str, dict[OwnerKind, int | None], list[tuple[LaneOwner, BotRef | None]]
	]:
		"""Кого зондировать: вид, чат, назначения и адреса исполнителей.

		Сессия закрывается до похода в Telegram: открытая транзакция
		чтения на время сетевых зондов держала бы SQLite занятым.

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			targets: list[tuple[LaneOwner, BotRef | None]] = []
			for row in community.executors:
				owner = executor_owner(row)
				if executor_paused(row):
					logger.info("Доступы: исполнитель %s приостановлен — зонд пропущен.", owner)
					continue
				ref = BotRef(row.bot.id, row.bot.token) if row.bot is not None else None
				targets.append((owner, ref))
			return (
				CommunityKind(community.kind),
				community.tg_chat_id,
				{
					OwnerKind.USER: community.default_tg_account_id,
					OwnerKind.BOT: community.default_bot_id,
				},
				targets,
			)

	async def _probe_executor(
		self, owner: LaneOwner, ref: BotRef | None, chat_id: str
	) -> _ProbeResult:
		"""Зондирует одного исполнителя его собственным транспортом."""
		if owner.kind is OwnerKind.USER:
			return await self._probe_userbot(owner.id, chat_id)
		return await self._probe_bot(ref, chat_id) if ref is not None else _ProbeResult(ok=None)

	@staticmethod
	def _verdict(probe: _ProbeResult, kind: CommunityKind) -> bool | None:
		"""Приговор правам публикатора по итогу зонда.

		Различие обязательно: подтверждённый ответ Telegram — знание
		(«может» или «не может»), а сбой связи — его отсутствие. Прежде
		обе причины давали одинаковый приговор, и человек видел «права
		потеряны» из-за пропавшей сети.
		"""
		if probe.ok is None:
			return None
		if probe.ok is False or probe.info is None:
			return False
		return can(probe.info.rights, ExecutorAction.PUBLISH, kind)

	async def _restore_publisher(
		self, community_id: int, chat_id: str, kind: CommunityKind
	) -> bool | None:
		"""Ищет публикатора-пользователя, когда людей в пуле не осталось.

		Авто-восстановление касается только людей: бот в пуле человека
		не заменяет — у него нет ни сессии, ни отложенных записей.
		"""
		found = await self._find_userbot_publisher(chat_id, kind)
		if found is None:
			return None
		await self._adopt_executor(
			community_id, LaneOwner(OwnerKind.USER, found[0]), found[1], make_default=True
		)
		return True

	async def _store_executor_rights(
		self, community_id: int, owner: LaneOwner, rights: ExecutorRights
	) -> None:
		"""Записывает снимок прав исполнителя по подтверждённому зонду (ADR-0035).

		Снимок пишется целиком при каждом подтверждённом ответе: он
		изменчив, и «обновлять только при разнице» пришлось бы сравнивать
		три десятка флагов ради экономии одной записи в локальную базу.
		В журнал попадает смена участия — это и есть человекочитаемое
		событие; перемена отдельных прав видна на странице сообщества.
		"""
		async with self._db.session_factory() as session:
			row = await self._executor_in_session(session, community_id, owner)
			if row is None:
				return
			was = row.status
			row.status = rights.status
			row.rights = rights.to_payload()
			row.checked_at = datetime.now(UTC)
			await session.commit()
		if was != rights.status:
			logger.info(
				"Участие %s в сообществе id=%s: %s → %s.",
				owner,
				community_id,
				was,
				rights.status,
			)

	@staticmethod
	async def _executor_in_session(
		session: AsyncSession, community_id: int, owner: LaneOwner
	) -> CommunityExecutor | None:
		"""Строка исполнителя в переданной сессии (None — такого нет)."""
		column = (
			CommunityExecutor.tg_account_id
			if owner.kind is OwnerKind.USER
			else CommunityExecutor.bot_id
		)
		return (
			await session.execute(
				select(CommunityExecutor).where(
					CommunityExecutor.community_id == community_id, column == owner.id
				)
			)
		).scalar_one_or_none()

	async def _adopt_executor(
		self,
		community_id: int,
		owner: LaneOwner,
		rights: ExecutorRights,
		*,
		make_default: bool,
	) -> None:
		"""Заводит исполнителя (подключение, добавление, авто-восстановление)."""
		async with self._db.session_factory() as session:
			if await self._executor_in_session(session, community_id, owner) is None:
				session.add(_executor_row(community_id, owner, rights))
			community = await self._community_in_session(session, community_id)
			if make_default:
				if owner.kind is OwnerKind.USER and community.default_tg_account_id is None:
					community.default_tg_account_id = owner.id
				elif owner.kind is OwnerKind.BOT and community.default_bot_id is None:
					community.default_bot_id = owner.id
			await session.commit()
		logger.info(
			"Исполнитель %s принят в сообщество id=%s (%s).", owner, community_id, rights.status
		)

	async def list_executors(self, community_id: int) -> list[ExecutorDto]:
		"""Исполнители сообщества обоих видов; публикаторы помечены (ADR-0035).

		Порядок — пользователи, затем боты, внутри вида по id: список
		один, и вид исполнителя в нём виден, а не угадывается.

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			kind = CommunityKind(community.kind)
			rows = sorted(
				community.executors,
				key=lambda row: (row.tg_account_id is None, row.tg_account_id or row.bot_id or 0),
			)
			result: list[ExecutorDto] = []
			for row in rows:
				owner = executor_owner(row)
				rights = executor_rights(row)
				is_default = (
					owner.id == community.default_tg_account_id
					if owner.kind is OwnerKind.USER
					else owner.id == community.default_bot_id
				)
				result.append(
					ExecutorDto(
						owner=owner,
						label=executor_label(row),
						status=rights.status,
						rights=rights,
						is_default=is_default,
						paused=executor_paused(row),
						can_publish=can(rights, ExecutorAction.PUBLISH, kind),
						checked_at=row.checked_at,
					)
				)
			return result

	async def communities_of_account(self, account_id: int) -> list[AccountMembershipDto]:
		"""Сообщества, где аккаунт состоит, с участием и признаком умолчания.

		Обратная сторона пула (ADR-0035) для страницы исполнителя:
		порядок — по id сообщества.
		"""
		return await self._communities_of(LaneOwner(OwnerKind.USER, account_id))

	async def communities_of_bot(self, bot_id: int) -> list[AccountMembershipDto]:
		"""Сообщества, где бот состоит, с участием и признаком умолчания."""
		return await self._communities_of(LaneOwner(OwnerKind.BOT, bot_id))

	async def _communities_of(self, owner: LaneOwner) -> list[AccountMembershipDto]:
		"""Сообщества исполнителя — общая половина обеих обратных сторон."""
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		column = (
			CommunityExecutor.tg_account_id
			if owner.kind is OwnerKind.USER
			else CommunityExecutor.bot_id
		)
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(CommunityExecutor)
						.where(column == owner.id)
						.options(selectinload(CommunityExecutor.community).options(*_REF_LOADERS))
						.order_by(CommunityExecutor.community_id)
					)
				)
				.scalars()
				.all()
			)
			return [
				AccountMembershipDto(
					community=self._dto(
						row.community,
						enabled=enabled.get(row.community_id, COMMUNITY_ENABLED.default),
					),
					status=ParticipantStatus(row.status),
					is_default=(
						row.community.default_tg_account_id == owner.id
						if owner.kind is OwnerKind.USER
						else row.community.default_bot_id == owner.id
					),
				)
				for row in rows
			]

	async def executors_for(self, community_id: int, action: ExecutorAction) -> list[ExecutorDto]:
		"""Исполнители сообщества, способные на это действие (ADR-0035).

		Точка подбора исполнителя: её спрашивают там, где работу можно
		поручить не только публикатору, — обслуживание (ADR-0026) и,
		в будущем, распределение нагрузки между исполнителями. Выбор идёт
		по **хранимому снимку прав**, без обращения к Telegram: зонд перед
		каждым проходом стоил бы обращения там, где ответ уже известен,
		а устаревший снимок поправит отказ сервера — он и так остаётся
		последним словом.

		Приостановленные (ADR-0029) в список не попадают. Публикатор
		по умолчанию идёт первым: когда он способен, работу делает он —
		так сохраняется прежнее поведение, а прочие исполнители
		подхватывают лишь то, чего он не может.

		Raises:
			CommunityError: Сообщество не найдено.
		"""
		community = await self.get_community(community_id)
		executors = await self.list_executors(community_id)
		capable = [
			executor
			for executor in executors
			if not executor.paused and can(executor.rights, action, community.kind)
		]
		return sorted(capable, key=lambda executor: not executor.is_default)

	async def add_executor(
		self, community_id: int, owner: LaneOwner, invite: str | None = None
	) -> JoinResult:
		"""Вводит исполнителя в сообщество и заводит ему строку пула (ADR-0035).

		Лестницей, от простого к сложному — и усложнение только
		по названной причине. Уже состоит: в Telegram ничего не делаем,
		только читаем права. Не состоит: в публичное сообщество аккаунт
		**вступает сам** по @имени; в приватное — по ссылке-приглашению,
		которую приложение **читает** у Telegram (создавать ссылки оно
		не умеет намеренно) или получает от человека; если ссылки нет —
		исполнителя приглашает наш же администратор. Бот сам вступить
		не может: в группу его приглашают, в канал принимают назначением
		администратором — участником бот там не бывает.

		Вступление и приглашение делаются **только по этому вызову**,
		то есть по явному действию человека: в фоне приложение в чужие
		сообщества не вступает (ADR-0035, п. 11).

		Первый исполнитель своего вида становится публикатором
		по умолчанию — иначе публикация осталась бы недоступной
		до второго действия.

		Args:
			community_id: сообщество.
			owner: кого вводим.
			invite: ссылка-приглашение от человека (когда своей нет).

		Returns:
			Итог ввода и пул после него. Исход ``NEEDS_LINK`` значит, что
			в Telegram ничего не делали и нужна ссылка-приглашение
			от человека — повторите вызов с ней.

		Raises:
			CommunityError: Сообщество или исполнитель не найдены, либо
				он уже в пуле, либо ввести его нечем.
			UserbotUnavailableError: Отказ Telegram на userbot-пути.
			BotError: Отказ Telegram на бот-пути.
		"""
		community = await self._executor_context(community_id, owner)
		if owner.kind is OwnerKind.USER:
			account = await self._get_account(owner.id)
			label = self._account_display(account)
			outcome, rights, info = await self._bring_user(community, owner.id, account, invite)
		else:
			bot = await self._get_bot(owner.id)
			label = bot.label
			outcome, rights, info = await self._bring_bot(community, bot)
		if outcome is JoinOutcome.NEEDS_LINK:
			# в Telegram ничего не менялось — и строки пула не заводим
			return JoinResult(outcome, await self.list_executors(community_id))
		had_kind = any(
			(row.tg_account_id is not None) == (owner.kind is OwnerKind.USER)
			for row in community.executors
		)
		await self._adopt_executor(community_id, owner, rights, make_default=not had_kind)
		if info is not None:
			await self._refresh_mutable(community_id, info)
		logger.info(
			"Сообществу id=%s добавлен исполнитель «%s» (%s).", community_id, label, outcome
		)
		return JoinResult(outcome, await self.list_executors(community_id))

	async def _executor_context(self, community_id: int, owner: LaneOwner) -> Community:
		"""Сообщество со связями — и отказ, если исполнитель уже в пуле.

		Raises:
			CommunityError: Сообщество не найдено или исполнитель уже в пуле.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id, with_refs=True)
			if await self._executor_in_session(session, community_id, owner) is not None:
				raise CommunityError("Этот исполнитель уже в пуле сообщества.")
			session.expunge(community)
			return community

	async def _bring_user(
		self, community: Community, account_id: int, account: TgAccount, invite: str | None
	) -> tuple[JoinOutcome, ExecutorRights, CommunityInfo | None]:
		"""Вводит пользователя: лестница «состоит → @имя → ссылка → приглашение»."""
		info = await self._seen_by(account_id, community.tg_chat_id)
		if info is not None and info.rights.status.in_community:
			return JoinOutcome.ALREADY_IN, info.rights, info
		outcome = await self._let_user_in(community, account_id, account, invite)
		if outcome is JoinOutcome.NEEDS_LINK:
			return outcome, ExecutorRights(ParticipantStatus.LEFT), None
		if outcome is JoinOutcome.REQUESTED:
			# заявку ещё не одобрили: прав нет и спрашивать их не у кого
			return outcome, ExecutorRights(ParticipantStatus.REQUESTED), None
		fresh = await self._gateway.userbot_check_community(account_id, community.tg_chat_id)
		await self._sync_profile(account_id)
		return outcome, fresh.rights, fresh

	async def _let_user_in(
		self, community: Community, account_id: int, account: TgAccount, invite: str | None
	) -> JoinOutcome:
		"""Заводит пользователя в сообщество — ступенями (ADR-0035, п. 9).

		Порядок ступеней — от независимой к самой отказоопасной: вступить
		по @имени можно без чьей-либо помощи; ссылка нужна приватному;
		приглашение упирается в чужие настройки приватности. Последним
		идёт не приглашение, а вопрос человеку: это единственная ступень,
		которая останавливает операцию, и уводить в неё, пока остаются
		автоматические пути, значило бы звать человека зря.
		"""
		if community.username and invite is None:
			await self._gateway.userbot_join_public(account_id, f"@{community.username}")
			return JoinOutcome.JOINED
		link = invite or await self._known_invite_link(community)
		if link is not None:
			joined = await self._gateway.userbot_join_by_invite(account_id, link)
			return JoinOutcome.JOINED if joined else JoinOutcome.REQUESTED
		if account.username and await self._invite_by_pool(community, f"@{account.username}"):
			return JoinOutcome.INVITED
		return JoinOutcome.NEEDS_LINK

	async def _bring_bot(
		self, community: Community, bot: Bot
	) -> tuple[JoinOutcome, ExecutorRights, CommunityInfo | None]:
		"""Вводит бота: сам он вступить не может — его вводит наш администратор.

		Raises:
			CommunityError: У бота нет @имени или в пуле некому его ввести.
		"""
		ref = BotRef(bot.id, bot.token)
		info = await self._seen_by_bot(ref, community.tg_chat_id)
		if info is not None and info.rights.status.in_community:
			return JoinOutcome.ALREADY_IN, info.rights, info
		if not bot.username:
			raise CommunityError(
				f"У бота «{bot.label}» не известно @имя — без него Telegram не найдёт, "
				"кого добавлять. Проверьте бота в разделе «Пользователи и боты»."
			)
		target = f"@{bot.username}"
		if CommunityKind(community.kind) is CommunityKind.CHANNEL:
			# в канале бот бывает только администратором — значит ввод
			# это назначение, и нужен наш исполнитель с правом назначать
			account_id = self._executor_for(community, ExecutorAction.PROMOTE)
			if account_id is None:
				raise CommunityError(
					f"Некому принять бота в канал «{community.title}»: нужен исполнитель "
					"с правом назначать администраторов. Добавьте бота администратором "
					"вручную в Telegram."
				)
			await self._gateway.userbot_promote(
				account_id, community.tg_chat_id, target, BOT_ADMIN_RIGHTS
			)
			outcome = JoinOutcome.PROMOTED
		else:
			account_id = self._executor_for(community, ExecutorAction.INVITE)
			if account_id is None:
				raise CommunityError(
					f"Некому пригласить бота в «{community.title}»: нужен исполнитель "
					"с правом приглашать. Добавьте бота в группу вручную в Telegram."
				)
			await self._gateway.userbot_invite_participant(account_id, community.tg_chat_id, target)
			outcome = JoinOutcome.INVITED
		fresh = await self._gateway.bot_check_community(ref, community.tg_chat_id)
		return outcome, fresh.rights, fresh

	async def _seen_by(self, account_id: int, chat_id: str) -> CommunityInfo | None:
		"""Что видит аккаунт в сообществе (None — не видит вовсе).

		От :meth:`_probe_userbot` отличается тем, что не глушит сбои:
		ввод исполнителя — действие человека, и «нет связи» он должен
		увидеть ошибкой, а не молчаливым переходом к вступлению.

		Удавшийся зонд — подтверждённая связь с аккаунтом, поэтому здесь
		же актуализируется его профиль: ответ «не состоит» для этого
		годится не хуже ответа «состоит».
		"""
		try:
			info = await self._gateway.userbot_check_community(account_id, chat_id)
		except UserbotAccessError:
			return None
		await self._sync_profile(account_id)
		return info

	async def _seen_by_bot(self, bot: BotRef, chat_id: str) -> CommunityInfo | None:
		"""Что видит бот в сообществе (None — Telegram его туда не пускает)."""
		try:
			return await self._gateway.bot_check_community(bot, chat_id)
		except BotError:
			return None

	async def _known_invite_link(self, community: Community) -> str | None:
		"""Основная ссылка-приглашение сообщества, если её кто-то из пула видит.

		Ссылка у приватного сообщества уже есть, и Telegram отдаёт её
		администратору с правом приглашать. Приложение её **читает**,
		а не создаёт: создание — изменение состояния сообщества, за
		которым тянется вопрос «кто её завёл и кто по ней пришёл».
		"""
		account_id = self._executor_for(community, ExecutorAction.INVITE)
		if account_id is None:
			return None
		try:
			return await self._gateway.userbot_invite_link(account_id, community.tg_chat_id)
		except UserbotUnavailableError as exc:
			logger.info("Ссылку-приглашение «%s» прочитать не удалось: %s", community.title, exc)
			return None

	async def _invite_by_pool(self, community: Community, target: str) -> bool:
		"""Приглашает исполнителя силами пула; False — не вышло.

		Самая отказоопасная ступень: она упирается не в наши права,
		а в чужие настройки приватности, поэтому отказ здесь не ошибка
		операции, а повод попросить у человека ссылку.
		"""
		account_id = self._executor_for(community, ExecutorAction.INVITE)
		if account_id is None:
			return False
		try:
			await self._gateway.userbot_invite_participant(account_id, community.tg_chat_id, target)
		except UserbotUnavailableError as exc:
			logger.info("Пригласить %s в «%s» не вышло: %s", target, community.title, exc)
			return False
		return True

	@staticmethod
	def _executor_for(community: Community, action: ExecutorAction) -> int | None:
		"""Аккаунт из пула, способный на названное действие (None — такого нет).

		Ввод исполнителя делают руками своих же администраторов, и выбрать
		их можно только по правам. Приостановленные (ADR-0029)
		не рассматриваются — приложение их не использует ни для чего.
		"""
		kind = CommunityKind(community.kind)
		for row in community.executors:
			if row.tg_account_id is None or executor_paused(row):
				continue
			if can(executor_rights(row), action, kind):
				return row.tg_account_id
		return None

	async def remove_executor(self, community_id: int, owner: LaneOwner) -> list[ExecutorDto]:
		"""Убирает исполнителя из пула сообщества (из приложения, не из Telegram).

		Публикатор при этом теряет назначение: инвариант «публикатор —
		исполнитель пула» (ADR-0022, ADR-0035); авто-замены нет — смена
		лица поста не должна происходить молча.

		Raises:
			CommunityError: Сообщество или исполнитель не найдены.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			row = await self._executor_in_session(session, community_id, owner)
			if row is None:
				raise CommunityError("Этого исполнителя нет в пуле сообщества.")
			await session.delete(row)
			if owner.kind is OwnerKind.USER and community.default_tg_account_id == owner.id:
				community.default_tg_account_id = None
			if owner.kind is OwnerKind.BOT and community.default_bot_id == owner.id:
				community.default_bot_id = None
			await session.commit()
		logger.info("Исполнитель %s убран из сообщества id=%s.", owner, community_id)
		return await self.list_executors(community_id)

	async def set_default_publisher(self, community_id: int, owner: LaneOwner) -> CommunityDto:
		"""Назначает публикатора по умолчанию из пула — своего вида (ADR-0035).

		У сообщества два назначения, по одному на вид: пользователь
		публикует из своей сессии, бот — запасным путём и рисует кнопки.
		Назначить можно и того, кто сейчас публиковать не может: права —
		знание Telegram, назначение — решение человека, и смешивать их
		приложение не вправе. Что мешает публиковать, скажет состояние
		сообщества.

		Raises:
			CommunityError: Сообщество не найдено или исполнитель не в пуле.
		"""
		async with self._db.session_factory() as session:
			community = await self._community_in_session(session, community_id)
			if await self._executor_in_session(session, community_id, owner) is None:
				raise CommunityError(
					"Публикатором может стать только исполнитель пула — сначала добавьте его."
				)
			if owner.kind is OwnerKind.USER:
				community.default_tg_account_id = owner.id
			else:
				community.default_bot_id = owner.id
			await session.commit()
		dto = await self._fresh_dto(community_id)
		logger.info("Публикатор «%s» по умолчанию: %s.", dto.title, owner)
		return dto

	async def _probe_bot(self, bot: BotRef, chat_id: str) -> _ProbeResult:
		"""Проверяет права бота, не роняя перепроверку.

		Различает то же, что и зонд userbot: подтверждённый отказ
		Telegram — это знание о правах (``ok=False``), а обрыв связи
		или неверный токен — отсутствие знания (``ok=None``). Раньше
		обе причины давали «права потеряны», и человек видел приговор
		правам из-за пропавшей сети.
		"""
		try:
			info = await self._gateway.bot_check_community(bot, chat_id)
		except BotError as exc:
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

		Право бота править чужие сообщения (ADR-0031) сюда больше
		не относится: оно принадлежит паре «сообщество + бот» и живёт
		в снимке прав этого бота (ADR-0035). Прежде оно хранилось
		колонкой сообщества, и userbot-зонд мог бы затереть подтверждённое
		ботом право — потому и существовал признак «свежие данные принёс бот».
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
		bot = community.default_bot
		user_row = publisher_row(community, OwnerKind.USER)
		caps = community_capabilities(community)
		return CommunityDto(
			community.id,
			community.title,
			community.username,
			community.tg_chat_id,
			community.default_bot_id,
			bot.label if bot is not None else None,
			enabled,
			community.default_tg_account_id,
			CommunitiesService._account_display(default) if default is not None else None,
			default_status=ParticipantStatus(user_row.status) if user_row is not None else None,
			executors_count=len(community.executors),
			kind=CommunityKind(community.kind),
			forum=community.forum,
			default_account_paused=default is not None and default.paused,
			default_bot_paused=bot is not None and bot.paused,
			userbot_ready=caps.userbot,
			bot_ready=caps.bot,
			markup_edit=caps.markup_edit,
			publisher_incapable=publisher_incapable(community),
		)
