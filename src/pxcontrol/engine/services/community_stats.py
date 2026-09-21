"""Кэш статистики сообществ и периодический опрос (ADR-0027).

Интерфейс читает только кэш: дашборд — снимок для карточек, страница
сообщества — сводку «Обзора». Наполняет кэш **периодическая задача
движка**, а не показ страницы, — иначе числа обновлялись бы только
пока человек смотрит на дашборд.

Два темпа опроса, по источникам:

- **бот, раз в 15 минут** — участники и связанное сообщество через
  Bot API. Бот-путь не проходит через дорожку аккаунта (лимиты Bot API —
  на бота), поэтому загрузки userbot ему не мешают: это дешёвый частый
  источник для локальной истории;
- **userbot, раз в 6 часов** — всё, что умеет только он: онлайн,
  отложенные, аватар, признак доступности встроенной статистики,
  связанное сообщество, последний пост, дата создания (однократно)
  и — где доступна — встроенная статистика Telegram. Фоновый приоритет
  дорожки (ADR-0024): публикация идёт вперёд, долгая загрузка
  задерживает опрос, а не наоборот.

Флуд-лимит действует на аккаунт целиком, и помнит об этом дорожка:
остальные его сообщества получат мгновенный отказ, не тревожа Telegram.
Сбои сети кэш не затирают — остаются прежние значения.

Каждый успешный проход оставляет снимок в истории (участники, онлайн);
история старше срока хранения убирается тем же проходом. Аватары
хранятся файлами в каталоге кэша (в БД — только путь); файл
перекачивается, когда его нет или он старше суток.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import (
	Community,
	CommunityAnalyticsRow,
	CommunityExecutor,
	CommunityStats,
	CommunityStatsHistory,
)
from pxcontrol.engine.db.types import as_utc_optional
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.periodic import PeriodicTask
from pxcontrol.engine.services.abilities import ExecutorAction
from pxcontrol.engine.services.community_overview import (
	GROWTH_DAYS,
	CommunityOverviewDto,
	HistorySample,
	build_overview,
)
from pxcontrol.engine.services.community_rights import bot_ref, ranked_executors
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.lane import LaneLiveState
from pxcontrol.engine.telegram.types import (
	ANALYTICS_DAILY,
	ANALYTICS_PAIRS,
	ANALYTICS_SHARES,
	BotRef,
	CommunityAnalytics,
	CommunityStatsInfo,
	DayPoint,
	ExecutorRef,
	HistoryMarks,
	NamedSeries,
	OwnerKind,
	RecentPost,
	ScheduledMessage,
	Share,
	TelegramFloodError,
	TopAdmin,
	TopInviter,
	TopPoster,
)
from pxcontrol.paths import cache_dir

logger = logging.getLogger(__name__)

#: Окно опроса ботом (участники, связанный чат), секунды. Окна выровнены
#: по часам суток, а не по старту приложения: :00, :15, :30, :45.
BOT_POLL_S = 15 * 60

#: Окно полного опроса userbot-ом (онлайн, отложенные, статистика),
#: секунды: 00, 06, 12, 18 часов местного времени.
FULL_POLL_S = 6 * 60 * 60

#: Шаг периодической задачи: как часто проверять, кому пора. Минута —
#: с запасом мельче любого темпа; сама проверка без сети дешёвая.
POLL_TICK_S = 60

#: Срок хранения локальной истории снимков, дни. Графикам нужны 30,
#: запас втрое; при опросе раз в 15 минут — до ~9 тысяч строк на сообщество.
HISTORY_KEEP_DAYS = 90

#: Свежесть файла аватара: старше — перекачивается (по mtime файла).
AVATAR_TTL_S = 24 * 60 * 60

#: Как часто убирать историю старше срока хранения: раз в сутки.
#: Хранение измеряется месяцами, и чаще смысла нет — а пишущая
#: транзакция каждым тиком идёт круглосуточно.
PRUNE_EVERY = timedelta(days=1)

#: Сколько ждать периодическую задачу при остановке движка (ADR-0020):
#: между обращениями к Telegram она выходит сразу, внутри обращения —
#: дожидается его конца.
_SHUTDOWN_TIMEOUT_S = 30.0


class CommunityStatsError(EngineError):
	"""Ошибка чтения статистики (с понятным человеку текстом)."""


class _StatsGateway(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def userbot_community_stats(
		self, account_id: int, chat_id: str
	) -> CommunityStatsInfo: ...

	async def userbot_avatar(self, account_id: int, chat_id: str, target: str) -> str | None: ...

	async def userbot_get_scheduled(
		self, account_id: int, chat_id: str
	) -> list[ScheduledMessage]: ...

	async def userbot_history_marks(
		self, account_id: int, chat_id: str, *, with_created: bool
	) -> HistoryMarks: ...

	def live_states(self) -> dict[ExecutorRef, LaneLiveState]: ...

	async def userbot_community_analytics(
		self, account_id: int, chat_id: str
	) -> CommunityAnalytics: ...

	async def bot_community_stats(self, bot: BotRef, chat_id: str) -> CommunityStatsInfo: ...


@dataclass(frozen=True)
class CommunityStatsDto:
	"""Снимок статистики сообщества для карточек дашборда (из кэша).

	None в поле — данные ещё не получены или источник их не отдаёт
	(онлайн есть только у групп; у бот-сообществ нет аватара и отложек).
	"""

	community_id: int
	participants: int | None
	online: int | None
	scheduled_count: int | None
	avatar_path: str | None
	fetched_at: datetime | None


def _as_int(value: object) -> int | None:
	"""Целое из собранного значения (None — не число)."""
	return value if isinstance(value, int) else None


def window_start(now: datetime, every_s: int, tz: tzinfo) -> datetime:
	"""Начало текущего окна опроса — выровнено по часам суток.

	Сутки делятся на окна длиной ``every_s`` от местной полуночи:
	при 15 минутах это :00, :15, :30, :45 каждого часа, при 6 часах —
	00, 06, 12, 18. Так проходы у всех запусков приложения попадают
	в одни и те же моменты, а не сдвигаются от старта программы.
	"""
	local = now.astimezone(tz)
	midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
	elapsed = (local - midnight).total_seconds()
	return midnight + timedelta(seconds=elapsed - elapsed % every_s)


def due(last: datetime | None, every_s: int, now: datetime, tz: tzinfo = UTC) -> bool:
	"""Пора ли опрашивать: прошлого прохода не было или он до начала окна.

	``every_s`` не больше нуля — всегда пора (принудительный проход).
	"""
	if every_s <= 0:
		return True
	last = as_utc_optional(last)
	return last is None or last < window_start(now, every_s, tz)


# --- сериализация встроенной статистики -------------------------------------------


def analytics_to_payload(analytics: CommunityAnalytics) -> dict[str, Any]:
	"""Разобранные ряды → JSON-совместимый словарь (даты — ISO)."""

	def points(series: tuple[DayPoint, ...]) -> list[list[Any]]:
		return [[point.day.isoformat(), point.value] for point in series]

	payload: dict[str, Any] = {
		"period_from": analytics.period_from.isoformat(),
		"period_to": analytics.period_to.isoformat(),
		"growth": points(analytics.growth),
		"joined": points(analytics.joined),
		"left": points(analytics.left),
		"hours": list(analytics.hours) if analytics.hours is not None else None,
		"recent_post_views": list(analytics.recent_post_views),
		"recent_posts": [
			[post.msg_id, post.views, post.forwards, post.reactions]
			for post in analytics.recent_posts
		],
		"top_posters": [[p.name, p.messages, p.avg_chars] for p in analytics.top_posters],
		"top_admins": [[a.name, a.deleted, a.kicked, a.banned] for a in analytics.top_admins],
		"top_inviters": [[i.name, i.invitations] for i in analytics.top_inviters],
	}
	for field in ANALYTICS_PAIRS:
		pair = getattr(analytics, field)
		payload[field] = list(pair) if pair is not None else None
	for field in ANALYTICS_DAILY:
		payload[field] = [[s.name, points(s.points)] for s in getattr(analytics, field)]
	for field in ANALYTICS_SHARES:
		payload[field] = [[s.name, s.value] for s in getattr(analytics, field)]
	return payload


def analytics_from_payload(
	payload: Any, community_id: int | None = None
) -> CommunityAnalytics | None:
	"""Словарь из БД → ряды; None — запись битая (в журнал, не наружу).

	``community_id`` попадает в запись журнала: без него по «не разобрана»
	не понять, у какого сообщества «Обзор» молча перешёл на локальные
	снимки вместо рядов Telegram.
	"""

	def points(raw: Any) -> tuple[DayPoint, ...]:
		return tuple(DayPoint(date.fromisoformat(day), int(value)) for day, value in raw)

	def pair(raw: Any) -> tuple[int, int] | None:
		return (int(raw[0]), int(raw[1])) if raw else None

	def opt(raw: Any) -> int | None:
		return None if raw is None else int(raw)

	try:
		extras: dict[str, Any] = {field: pair(payload.get(field)) for field in ANALYTICS_PAIRS}
		extras.update(
			{
				field: tuple(
					NamedSeries(str(name), points(raw)) for name, raw in payload.get(field, [])
				)
				for field in ANALYTICS_DAILY
			}
		)
		extras.update(
			{
				field: tuple(Share(str(name), int(value)) for name, value in payload.get(field, []))
				for field in ANALYTICS_SHARES
			}
		)
		return CommunityAnalytics(
			period_from=date.fromisoformat(payload["period_from"]),
			period_to=date.fromisoformat(payload["period_to"]),
			growth=points(payload.get("growth", [])),
			joined=points(payload.get("joined", [])),
			left=points(payload.get("left", [])),
			hours=tuple(int(v) for v in payload["hours"]) if payload.get("hours") else None,
			recent_post_views=tuple(int(v) for v in payload.get("recent_post_views", [])),
			recent_posts=tuple(
				RecentPost(int(msg_id), opt(views), opt(forwards), opt(reactions))
				for msg_id, views, forwards, reactions in payload.get("recent_posts", [])
			),
			top_posters=tuple(
				TopPoster(str(name), int(messages), int(avg_chars))
				for name, messages, avg_chars in payload.get("top_posters", [])
			),
			top_admins=tuple(
				TopAdmin(str(name), int(deleted), int(kicked), int(banned))
				for name, deleted, kicked, banned in payload.get("top_admins", [])
			),
			top_inviters=tuple(
				TopInviter(str(name), int(invitations))
				for name, invitations in payload.get("top_inviters", [])
			),
			**extras,  # ключи — поля границы
		)
	except (KeyError, TypeError, ValueError, AttributeError):
		# чья запись и что именно сломалось: без этого по журналу не понять,
		# у какого сообщества «Обзор» молча перешёл на локальные снимки
		logger.warning(
			"Запись статистики Telegram%s не разобрана — считаю, что её нет.",
			f" сообщества id={community_id}" if community_id is not None else "",
			exc_info=True,
		)
		return None


class CommunityStatsService:
	"""Кэш статистики, периодический опрос и сводка «Обзора»."""

	def __init__(
		self,
		db: Database,
		gateway: _StatsGateway,
		settings: SettingsService | None = None,
		avatars_dir: Path | None = None,
		tz: tzinfo | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка (None — свой,
		для тестов); ``avatars_dir`` — каталог файлов аватаров
		(None — подпапка каталога кэша приложения); ``tz`` — часовой
		пояс окон опроса и окон «за N дней» (None — местный; тесты
		передают UTC ради предсказуемости)."""
		self._db = db
		self._gateway = gateway
		self._settings = settings if settings is not None else SettingsService(db)
		self._avatars_dir = avatars_dir if avatars_dir is not None else cache_dir() / "avatars"
		self._tz: tzinfo = tz if tz is not None else (datetime.now(UTC).astimezone().tzinfo or UTC)
		self._pruned_at: datetime | None = None
		self._poller = PeriodicTask(
			self.refresh_due,
			name="Опрос статистики",
			interval_s=POLL_TICK_S,
			shutdown_timeout_s=_SHUTDOWN_TIMEOUT_S,
		)

	# --- чтение кэша -------------------------------------------------------------

	async def snapshot(self) -> list[CommunityStatsDto]:
		"""Текущий кэш статистики всех сообществ (без похода в сеть)."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(select(CommunityStats))).scalars().all()
			return [
				CommunityStatsDto(
					community_id=row.community_id,
					participants=row.participants,
					online=row.online,
					scheduled_count=row.scheduled_count,
					avatar_path=row.avatar_path,
					fetched_at=as_utc_optional(row.fetched_at),
				)
				for row in rows
			]

	async def overview(
		self, community_id: int, now: datetime | None = None
	) -> CommunityOverviewDto:
		"""Сводка «Обзора» сообщества из кэша, истории и статистики Telegram.

		Без похода в сеть. Ряды — из статистики Telegram, если она
		есть, иначе из локальных снимков (оценка); связанное сообщество
		называется по имени, если оно подключено в приложении. ``now`` —
		точка отсчёта окон «за N дней» (по умолчанию текущий момент;
		тесты передают свою).

		Raises:
			CommunityStatsError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			if await session.get(Community, community_id) is None:
				raise CommunityStatsError("Сообщество не найдено — обновите список.")
			row = await session.get(CommunityStats, community_id)
			analytics_row = await session.get(CommunityAnalyticsRow, community_id)
			# горизонт снимков — ровно тот, что показывают графики
			# (GROWTH_DAYS). Хранится втрое больше (HISTORY_KEEP_DAYS),
			# и поднимать всё хранилище ради месяца незачем: это тысячи
			# строк на сообщество, которые потом трижды сортируются
			since = (now or datetime.now(UTC)) - timedelta(days=GROWTH_DAYS)
			history = (
				(
					await session.execute(
						select(CommunityStatsHistory)
						.where(
							CommunityStatsHistory.community_id == community_id,
							CommunityStatsHistory.at >= since,
						)
						.order_by(CommunityStatsHistory.at)
					)
				)
				.scalars()
				.all()
			)
			linked_title: str | None = None
			if row is not None and row.linked_chat_id:
				linked = (
					await session.execute(
						select(Community.title).where(Community.tg_chat_id == row.linked_chat_id)
					)
				).scalar_one_or_none()
				linked_title = linked
		samples = [
			HistorySample(
				as_utc_optional(item.at) or datetime.now(UTC), item.participants, item.online
			)
			for item in history
		]
		# статистика Telegram годится, только пока сервер подтверждает
		# доступ: право могли отобрать, а прежний ответ остался в кэше
		analytics = (
			analytics_from_payload(analytics_row.payload, community_id)
			if analytics_row is not None and row is not None and row.can_view_stats
			else None
		)
		now = (now if now is not None else datetime.now(UTC)).astimezone(self._tz)
		return build_overview(
			community_id,
			participants=row.participants if row is not None else None,
			online=row.online if row is not None else None,
			samples=samples,
			analytics=analytics,
			today=now.date(),
			tz=self._tz,
			can_view_stats=bool(row.can_view_stats) if row is not None else False,
			linked_chat_id=row.linked_chat_id if row is not None else None,
			linked_title=linked_title,
			tg_created_at=as_utc_optional(row.tg_created_at) if row is not None else None,
			last_post_at=as_utc_optional(row.last_post_at) if row is not None else None,
			fetched_at=as_utc_optional(row.fetched_at) if row is not None else None,
			deleted=(
				(row.deleted_found, row.deleted_removed, as_utc_optional(row.deleted_checked_at))
				if row is not None
				else (None, None, None)
			),
		)

	# --- периодический опрос (ADR-0027) ------------------------------------------

	def start_polling(self) -> None:
		"""Запускает периодическую задачу опроса (при старте движка)."""
		self._poller.start()

	async def shutdown(self) -> None:
		"""Гасит периодическую задачу кооперативно (ADR-0020).

		Взводится событие остановки: между обращениями задача выходит
		сразу, начатое обращение к Telegram дожидается конца; не успевшая
		за страховочный срок — отменяется как последнее средство.
		"""
		await self._poller.shutdown()

	async def refresh_due(
		self,
		now: datetime | None = None,
		*,
		bot_every_s: int = BOT_POLL_S,
		full_every_s: int = FULL_POLL_S,
	) -> bool:
		"""Опрашивает сообщества, чей срок по источнику вышел.

		Только активные сообщества. Бот — где назначен и началось новое
		окно его темпа (:00, :15, :30, :45); userbot — где есть публикатор
		и началось новое окно его темпа (00, 06, 12, 18; у сообщества
		без бота только он и обновляет участников, и это осознанно:
		частая беготня userbot-ом задевает публикацию). Окна выровнены
		по часам суток, а не по старту приложения (:func:`window_start`).
		Каждый успешный проход оставляет снимок в истории; в конце
		убирается история старше срока хранения.

		Returns:
			True — хоть одно сообщество обновилось (интерфейсу пора
			перечитать кэш), False — никому не пора или всё недоступно.
		"""
		now = now if now is not None else datetime.now(UTC)
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		async with self._db.session_factory() as session:
			communities = (
				(
					await session.execute(
						select(Community)
						.options(
							selectinload(Community.executors).selectinload(
								CommunityExecutor.tg_account
							),
							selectinload(Community.executors).selectinload(CommunityExecutor.bot),
						)
						.order_by(Community.id)
					)
				)
				.scalars()
				.all()
			)
			rows = {
				row.community_id: row
				for row in (await session.execute(select(CommunityStats))).scalars()
			}
			# кого спросить — решает диспетчер по пулу (ADR-0036): любой
			# состоящий исполнитель своего вида, свободный раньше занятого
			# загрузкой. Приостановленные (ADR-0029) и вышедшие
			# не опрашиваются: бот — просто пропуск, userbot получил бы
			# отказ шлюза на каждом тике и засорял бы журнал
			live = self._gateway.live_states()
			bot_refs: dict[int, BotRef] = {}
			account_ids: dict[int, int] = {}
			for c in communities:
				bots = ranked_executors(c, ExecutorAction.READ_HISTORY, live, kind=OwnerKind.BOT)
				if bots:
					bot_refs[c.id] = bot_ref(bots[0])
				users = ranked_executors(c, ExecutorAction.READ_HISTORY, live, kind=OwnerKind.USER)
				if users:
					account_ids[c.id] = int(users[0].tg_account_id or 0)
		changed = False
		for community in communities:
			if not enabled.get(community.id, COMMUNITY_ENABLED.default):
				continue
			row = rows.get(community.id)
			if self._poller.stopping:
				break
			bot = bot_refs.get(community.id)
			if bot is not None and due(
				row.bot_fetched_at if row else None, bot_every_s, now, self._tz
			):
				update = await self._bot_pass(community, bot)
				if update is not None:
					await self._store(community.id, update, now, stamp="bot_fetched_at")
					changed = True
				else:
					await self._mark_pass(community.id, now, "bot_fetched_at")
			account_id = account_ids.get(community.id)
			if account_id is not None and due(
				row.full_fetched_at if row else None, full_every_s, now, self._tz
			):
				update = await self._full_pass(community, account_id, row, now)
				if update is not None:
					await self._store(community.id, update, now, stamp="full_fetched_at")
					changed = True
				else:
					await self._mark_pass(community.id, now, "full_fetched_at")
		await self._prune_if_due(now)
		return changed

	async def record_members_report(
		self, community_id: int, found: int, removed: int, at: datetime | None = None
	) -> None:
		"""Запоминает итог прохода обслуживания по удалённым аккаунтам.

		Крючок из очереди обслуживания (ADR-0026): «Обзор» показывает
		число мёртвых душ и дату прохода, сам проход остаётся во вкладке
		«Обслуживание».
		"""
		await self._store(
			community_id,
			{"deleted_found": found, "deleted_removed": removed, "deleted_checked_at": at},
			at or datetime.now(UTC),
			stamp=None,
		)

	async def drop(self, community_id: int) -> None:
		"""Убирает файл аватара удаляемого сообщества (строки — каскад БД)."""
		target = self._avatar_target(community_id)
		removed = await asyncio.to_thread(self._remove_file, target)
		if removed:
			logger.info("Аватар сообщества id=%s удалён из кэша.", community_id)

	# --- сбор данных -------------------------------------------------------------

	async def _bot_pass(self, community: Community, bot: BotRef) -> dict[str, object] | None:
		"""Частый дешёвый проход ботом: участники и связанный чат."""
		try:
			info = await self._gateway.bot_community_stats(bot, community.tg_chat_id)
		except Exception as exc:  # noqa: BLE001 — фоновая сводка, кэш не затираем
			logger.info(
				"Участники «%s» через бота не обновлены (%s: %s).",
				community.title,
				type(exc).__name__,
				exc,
			)
			return None
		update: dict[str, object] = {}
		if info.participants is not None:
			# Telegram числа не дал — прежнее в кэше вернее, чем пустота
			# (тот же приём, что у связанного сообщества ниже)
			update["participants"] = info.participants
		if info.linked_chat_id is not None:
			update["linked_chat_id"] = info.linked_chat_id
		return update or None

	async def _full_pass(
		self,
		community: Community,
		account_id: int,
		row: CommunityStats | None,
		now: datetime,
	) -> dict[str, object] | None:
		"""Редкий полный проход userbot-ом; None — не удалось ничего.

		Все обращения прохода идут под одним ``except``: **первый сбой
		прекращает проход по сообществу**, но собранное до него
		засчитывается — частичное обновление лучше пустого. Раньше
		здесь было написано «каждый источник независим», и это было
		неправдой: отказ на первом же обращении отменял остальные.
		Флуд-лимит прекращает проход по той же ветке: дорожка аккаунта
		уже заморожена (ADR-0024), и остальные его сообщества получат
		отказ, не дойдя до сети.
		"""
		update: dict[str, object] = {}
		chat_id = community.tg_chat_id
		try:
			stats = await self._gateway.userbot_community_stats(account_id, chat_id)
			update["participants"] = stats.participants
			update["online"] = stats.online
			update["can_view_stats"] = stats.can_view_stats
			if stats.linked_chat_id is not None:
				update["linked_chat_id"] = stats.linked_chat_id
			scheduled = await self._gateway.userbot_get_scheduled(account_id, chat_id)
			update["scheduled_count"] = len(scheduled)
			avatar_changed, avatar_path = await self._refresh_avatar(account_id, community)
			if avatar_changed:
				update["avatar_path"] = avatar_path
			marks = await self._gateway.userbot_history_marks(
				account_id, chat_id, with_created=row is None or row.tg_created_at is None
			)
			update["last_post_at"] = marks.last_post_at
			if marks.created_at is not None:
				update["tg_created_at"] = marks.created_at
			if stats.can_view_stats:
				analytics = await self._gateway.userbot_community_analytics(account_id, chat_id)
				await self._store_analytics(community.id, analytics, now)
		except TelegramFloodError as exc:
			logger.info(
				"Статистика «%s» пропущена: аккаунт id=%s под флуд-лимитом (%s).",
				community.title,
				account_id,
				exc,
			)
		except Exception as exc:  # noqa: BLE001 — фоновая сводка, кэш не затираем
			logger.info(
				"Статистика «%s» не обновлена (%s: %s).",
				community.title,
				type(exc).__name__,
				exc,
			)
		return update or None

	async def _refresh_avatar(
		self, account_id: int, community: Community
	) -> tuple[bool, str | None]:
		"""Файл аватара: скачивает отсутствующий или устаревший (сутки).

		Returns:
			Пара «менять ли путь в кэше, путь»: (False, None) — файл
			свеж, качать не нужно; (True, путь) — скачан; (True, None) —
			аватара у сообщества нет, старый файл удалён.
		"""
		target = self._avatar_target(community.id)
		fresh = await asyncio.to_thread(self._file_is_fresh, target)
		if fresh:
			return False, None
		await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
		path = await self._gateway.userbot_avatar(account_id, community.tg_chat_id, str(target))
		if path is None:
			await asyncio.to_thread(self._remove_file, target)
			return True, None
		return True, path

	def _avatar_target(self, community_id: int) -> Path:
		"""Путь файла аватара сообщества в каталоге кэша."""
		return self._avatars_dir / f"{community_id}.jpg"

	@staticmethod
	def _file_is_fresh(target: Path) -> bool:
		"""Файл существует и моложе суток (по mtime)."""
		try:
			age = datetime.now(UTC).timestamp() - target.stat().st_mtime
		except OSError:
			return False
		return age < AVATAR_TTL_S

	@staticmethod
	def _remove_file(target: Path) -> bool:
		"""Удаляет файл, если он есть; ошибки — не критичны (след в логе)."""
		try:
			target.unlink()
			return True
		except FileNotFoundError:
			return False
		except OSError:
			logger.warning("Не удалось удалить файл кэша %s.", target, exc_info=True)
			return False

	# --- хранение --------------------------------------------------------------------

	async def _store(
		self,
		community_id: int,
		update: dict[str, object],
		now: datetime,
		*,
		stamp: str | None,
	) -> None:
		"""Пишет собранные значения в кэш и снимок в историю.

		``stamp`` — какой момент прохода отметить (``bot_fetched_at`` /
		``full_fetched_at``; None — не проход, а запись отчёта). Снимок
		истории оставляется, когда в обновлении есть число участников.
		"""
		async with self._db.session_factory() as session:
			row = await session.get(CommunityStats, community_id)
			if row is None:
				row = CommunityStats(community_id=community_id)
				session.add(row)
			for field, value in update.items():
				setattr(row, field, value)
			if stamp is not None:
				setattr(row, stamp, now)
				row.fetched_at = now
				if "participants" in update:
					session.add(
						CommunityStatsHistory(
							community_id=community_id,
							at=now,
							participants=row.participants,
							online=_as_int(update.get("online")),
						)
					)
			await session.commit()

	async def _mark_pass(self, community_id: int, now: datetime, stamp: str) -> None:
		"""Отмечает, что проход был, хотя данных он не принёс.

		Момент прохода и момент обновления данных — разные факты. Без
		этой отметки сообщество, которое не читается (бота исключили,
		сессия отозвана), опрашивалось бы **каждый тик** — раз в минуту
		вместо раза в 15 минут и 6 часов, — потому что «пора» считается
		по моменту прохода. Видимое человеку «Обновлено» (``fetched_at``)
		при этом не двигается: данных не прибавилось, и говорить обратное
		нельзя.
		"""
		async with self._db.session_factory() as session:
			row = await session.get(CommunityStats, community_id)
			if row is None:
				row = CommunityStats(community_id=community_id)
				session.add(row)
			setattr(row, stamp, now)
			await session.commit()

	async def _store_analytics(
		self, community_id: int, analytics: CommunityAnalytics, now: datetime
	) -> None:
		"""Перезаписывает последний ответ статистики Telegram."""
		async with self._db.session_factory() as session:
			row = await session.get(CommunityAnalyticsRow, community_id)
			if row is None:
				row = CommunityAnalyticsRow(community_id=community_id, fetched_at=now, payload={})
				session.add(row)
			row.fetched_at = now
			row.payload = analytics_to_payload(analytics)
			await session.commit()

	async def _prune_if_due(self, now: datetime) -> None:
		"""Убирает старьё не чаще раза в сутки.

		Уборка — пишущая транзакция и просмотр всей растущей таблицы,
		а хранение измеряется месяцами: делать её каждым тиком (раз
		в минуту, круглосуточно) незачем. Тот же приём, что у учёта
		активности.
		"""
		if self._pruned_at is not None and now - self._pruned_at < PRUNE_EVERY:
			return
		self._pruned_at = now
		await self._prune_history(now)

	async def _prune_history(self, now: datetime, keep_days: int = HISTORY_KEEP_DAYS) -> None:
		"""Убирает историю старше срока хранения (одним запросом)."""
		threshold = now - timedelta(days=keep_days)
		async with self._db.session_factory() as session:
			result = await session.execute(
				delete(CommunityStatsHistory).where(CommunityStatsHistory.at < threshold)
			)
			await session.commit()
		# у результата DELETE число строк есть, но общий тип Result его не обещает
		removed = int(getattr(result, "rowcount", 0) or 0)
		if removed:
			logger.info(
				"История статистики: удалено снимков старше %d дней — %d.", keep_days, removed
			)
