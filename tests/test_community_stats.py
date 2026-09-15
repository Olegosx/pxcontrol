"""Тесты кэша статистики: два темпа опроса, история, статистика Telegram."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, CommunityStatsHistory, TgAccount
from pxcontrol.engine.services.community_overview import SeriesSource
from pxcontrol.engine.services.community_stats import (
	CommunityStatsError,
	CommunityStatsService,
	analytics_from_payload,
	analytics_to_payload,
	due,
	window_start,
)
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.mtproto import UserbotFloodError, UserbotNotConnectedError
from pxcontrol.engine.telegram.types import (
	BotRef,
	CommunityAnalytics,
	CommunityStatsInfo,
	DayPoint,
	HistoryMarks,
	NamedSeries,
	RecentPost,
	ScheduledMessage,
	Share,
	TopAdmin,
	TopInviter,
	TopPoster,
)

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


class _FakeStatsGateway:
	"""Подставной шлюз статистики: значения и счётчики вызовов."""

	def __init__(self) -> None:
		self.participants = 1000
		self.online: int | None = 5
		self.scheduled = 3
		self.has_avatar = True
		self.can_view_stats = False
		self.fail_userbot = False  # имитация «нет связи»
		self.flood_accounts: set[int] = set()
		self.stats_calls: list[int] = []
		self.bot_calls: list[str] = []
		self.analytics_calls = 0
		self.created_requests: list[bool] = []

	def _check(self, account_id: int) -> None:
		if account_id in self.flood_accounts:
			raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
		if self.fail_userbot:
			raise UserbotNotConnectedError("Нет связи с Telegram.")

	async def userbot_community_stats(self, account_id: int, chat_id: str) -> CommunityStatsInfo:
		self._check(account_id)
		self.stats_calls.append(account_id)
		return CommunityStatsInfo(
			participants=self.participants,
			online=self.online,
			can_view_stats=self.can_view_stats,
			linked_chat_id="-1009",
		)

	async def userbot_avatar(self, account_id: int, chat_id: str, target: str) -> str | None:
		self._check(account_id)
		if not self.has_avatar:
			return None
		Path(target).write_bytes(b"jpg")
		return target

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		self._check(account_id)
		return [
			ScheduledMessage(
				id=i + 1, text=f"пост {i}", scheduled_at=datetime(2026, 9, 9, tzinfo=UTC)
			)
			for i in range(self.scheduled)
		]

	async def userbot_history_marks(
		self, account_id: int, chat_id: str, *, with_created: bool
	) -> HistoryMarks:
		self._check(account_id)
		self.created_requests.append(with_created)
		return HistoryMarks(
			last_post_at=datetime(2026, 9, 13, 18, 30, tzinfo=UTC),
			created_at=datetime(2024, 3, 12, tzinfo=UTC) if with_created else None,
		)

	async def userbot_community_analytics(
		self, account_id: int, chat_id: str
	) -> CommunityAnalytics:
		self._check(account_id)
		self.analytics_calls += 1
		today = _NOW.date()
		return CommunityAnalytics(
			period_from=today - timedelta(days=6),
			period_to=today,
			members=(1000, 962),
			growth=(DayPoint(today - timedelta(days=1), 990), DayPoint(today, 1000)),
			joined=(DayPoint(today, 40),),
			left=(DayPoint(today, 2),),
			hours=tuple(range(24)),
			views_per_post=(150, 120),
			recent_post_views=(100, 200, 300),
		)

	async def bot_community_stats(self, bot: BotRef, chat_id: str) -> CommunityStatsInfo:
		self.bot_calls.append(bot.token)
		return CommunityStatsInfo(participants=77, online=None, linked_chat_id="-1009")


async def _add_community(
	db: Database,
	chat_id: str,
	account_id: int | None = None,
	bot_id: int | None = None,
	title: str | None = None,
) -> int:
	async with db.session_factory() as session:
		community = Community(
			title=title or f"Сообщество {chat_id}",
			tg_chat_id=chat_id,
			kind="channel",
			default_tg_account_id=account_id,
			bot_id=bot_id,
		)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def _add_account(db: Database) -> int:
	async with db.session_factory() as session:
		account = TgAccount(label="ub", phone="+7900", session="s")
		session.add(account)
		await session.commit()
		await session.refresh(account)
		return account.id


async def _add_bot(db: Database) -> int:
	async with db.session_factory() as session:
		bot = Bot(label="b", token="123456:AAAbbb")
		session.add(bot)
		await session.commit()
		await session.refresh(bot)
		return bot.id


async def _history_count(db: Database) -> int:
	async with db.session_factory() as session:
		return len((await session.execute(select(CommunityStatsHistory))).scalars().all())


def _service(db: Database, gateway: _FakeStatsGateway, tmp_path: Path) -> CommunityStatsService:
	# окна опроса — в UTC: местный пояс машины не должен решать исход теста
	return CommunityStatsService(db, gateway, avatars_dir=tmp_path / "avatars", tz=UTC)


# --- правило «пора» и сериализация ---------------------------------------------------


def test_window_start_is_aligned_to_clock() -> None:
	"""Окна — по часам суток: :00/:15/:30/:45 и 00/06/12/18."""
	at = datetime(2026, 9, 14, 12, 7, 30, tzinfo=UTC)
	assert window_start(at, 15 * 60, UTC) == datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
	assert window_start(at, 6 * 3600, UTC) == datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
	at = datetime(2026, 9, 14, 17, 59, tzinfo=UTC)
	assert window_start(at, 15 * 60, UTC) == datetime(2026, 9, 14, 17, 45, tzinfo=UTC)
	assert window_start(at, 6 * 3600, UTC) == datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def test_due_rule_by_window_not_by_elapsed_time() -> None:
	"""Пора, когда прошлый проход — до начала текущего окна, а не по сроку."""
	now = datetime(2026, 9, 14, 12, 7, tzinfo=UTC)
	assert due(None, 900, now)
	assert not due(datetime(2026, 9, 14, 12, 1, tzinfo=UTC), 900, now), "то же окно"
	assert due(datetime(2026, 9, 14, 11, 59, tzinfo=UTC), 900, now), "прошлое окно"
	# два прохода в одном окне не случатся, даже если между ними 14 минут
	assert not due(datetime(2026, 9, 14, 12, 0, 30, tzinfo=UTC), 900, now)
	assert due(now.replace(tzinfo=None) - timedelta(days=1), 900, now)  # наивное из SQLite
	assert due(now, 0, now), "нулевое окно — принудительно"


def test_analytics_payload_roundtrip() -> None:
	analytics = CommunityAnalytics(
		period_from=date(2026, 9, 8),
		period_to=date(2026, 9, 14),
		members=(10, 8),
		growth=(DayPoint(date(2026, 9, 14), 10),),
		joined=(),
		left=(DayPoint(date(2026, 9, 13), 1),),
		hours=tuple(range(24)),
		views_per_post=None,
		recent_post_views=(5, 6),
		shares_per_post=(3, 4),
		notifications=(38, 100),
		messages=(120, 90),
		interactions=(
			NamedSeries("Views", (DayPoint(date(2026, 9, 14), 500),)),
			NamedSeries("Shares", ()),
		),
		languages=(Share("Русский", 70), Share("English", 30)),
		recent_posts=(RecentPost(41, 500, None, 12),),
		top_posters=(TopPoster("Олег К.", 12, 80),),
		top_admins=(TopAdmin("Админ", 3, 1, 0),),
		top_inviters=(TopInviter("@inviter", 5),),
	)
	assert analytics_from_payload(analytics_to_payload(analytics)) == analytics
	# запись прежнего формата (без новых полей) читается: новое — пустое
	old = {
		k: v
		for k, v in analytics_to_payload(analytics).items()
		if k in ("period_from", "period_to")
	}
	legacy = analytics_from_payload(old)
	assert legacy is not None and legacy.languages == () and legacy.messages is None
	assert analytics_from_payload({"period_from": "битая"}) is None
	assert analytics_from_payload(None) is None


# --- полный проход userbot -------------------------------------------------------------


async def test_full_pass_fills_cache_history_and_reference(db: Database, tmp_path: Path) -> None:
	"""Проход userbot заполняет кэш, справку, снимок истории; дата создания — однократно."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	await _add_community(db, "-1009", account_id, title="Чат обсуждений")
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is True
	row = next(r for r in await service.snapshot() if r.community_id == community_id)
	assert row.participants == 1000 and row.online == 5 and row.scheduled_count == 3
	assert row.avatar_path is not None and Path(row.avatar_path).exists()
	assert row.fetched_at == _NOW
	overview = await service.overview(community_id)
	assert overview.linked_chat_id == "-1009" and overview.linked_title == "Чат обсуждений"
	assert overview.last_post_at == datetime(2026, 9, 13, 18, 30, tzinfo=UTC)
	assert overview.tg_created_at == datetime(2024, 3, 12, tzinfo=UTC)
	assert overview.can_view_stats is False
	assert await _history_count(db) == 2  # по снимку на сообщество
	# второй проход: дата создания уже известна — её не запрашивают
	assert await service.refresh_due(_NOW + timedelta(hours=7)) is True
	assert gateway.created_requests == [True, True, False, False]


async def test_full_pass_respects_six_hour_cadence(db: Database, tmp_path: Path) -> None:
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is True
	assert await service.refresh_due(_NOW + timedelta(hours=5)) is False, "рано — сеть не трогается"
	assert len(gateway.stats_calls) == 1
	assert await service.refresh_due(_NOW + timedelta(hours=6)) is True
	assert len(gateway.stats_calls) == 2
	assert await service.refresh_due(_NOW + timedelta(hours=6), full_every_s=0) is True


async def test_analytics_fetched_only_when_available(db: Database, tmp_path: Path) -> None:
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	await service.refresh_due(_NOW)
	assert gateway.analytics_calls == 0
	assert (await service.overview(community_id, _NOW)).source is SeriesSource.SNAPSHOTS
	gateway.can_view_stats = True
	await service.refresh_due(_NOW + timedelta(hours=6))
	assert gateway.analytics_calls == 1
	overview = await service.overview(community_id, _NOW)
	assert overview.source is SeriesSource.TELEGRAM
	assert overview.can_view_stats is True
	assert overview.joined == 40 and overview.left == 2
	assert overview.views_per_post == 200
	assert overview.hours == tuple(range(24)) and not overview.hours_online


async def test_flood_skips_rest_of_account(db: Database, tmp_path: Path) -> None:
	"""Флуд-лимит аккаунта пропускает его сообщества (ADR-0017/0024)."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	await _add_community(db, "-1002", account_id)
	gateway.flood_accounts.add(account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is False
	assert gateway.stats_calls == [], "после флуда аккаунт не опрашивается"


async def test_failure_keeps_previous_values(db: Database, tmp_path: Path) -> None:
	"""Сбой сети не затирает собранное ранее."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is True
	gateway.fail_userbot = True
	assert await service.refresh_due(_NOW, full_every_s=0) is False
	row = (await service.snapshot())[0]
	assert row.participants == 1000 and row.scheduled_count == 3


# --- частый проход ботом ------------------------------------------------------------


async def test_bot_pass_every_fifteen_minutes(db: Database, tmp_path: Path) -> None:
	"""Сообщество с ботом: участники и связанный чат ботом раз в 15 минут."""
	gateway = _FakeStatsGateway()
	bot_id = await _add_bot(db)
	community_id = await _add_community(db, "-1001", account_id=None, bot_id=bot_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is True
	row = (await service.snapshot())[0]
	assert row.participants == 77
	assert row.scheduled_count is None and row.avatar_path is None
	assert gateway.bot_calls == ["123456:AAAbbb"]
	assert (await service.overview(community_id)).linked_chat_id == "-1009"
	assert await service.refresh_due(_NOW + timedelta(minutes=14)) is False
	assert await service.refresh_due(_NOW + timedelta(minutes=15)) is True
	assert len(gateway.bot_calls) == 2
	assert await _history_count(db) == 2


async def test_bot_and_userbot_have_independent_cadence(db: Database, tmp_path: Path) -> None:
	"""У сообщества с обоими публикаторами бот бегает часто, userbot — редко."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	bot_id = await _add_bot(db)
	await _add_community(db, "-1001", account_id, bot_id)
	service = _service(db, gateway, tmp_path)
	await service.refresh_due(_NOW)
	assert len(gateway.bot_calls) == 1 and len(gateway.stats_calls) == 1
	await service.refresh_due(_NOW + timedelta(minutes=16))
	assert len(gateway.bot_calls) == 2 and len(gateway.stats_calls) == 1
	# число участников — от бота (он ходил последним), онлайн — от userbot
	row = (await service.snapshot())[0]
	assert row.participants == 77 and row.online == 5


async def test_disabled_community_not_polled(db: Database, tmp_path: Path) -> None:
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	await SettingsService(db).set_for(COMMUNITY_ENABLED, community_id, False)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is False
	assert gateway.stats_calls == []


# --- история, отчёт обслуживания, обзор ----------------------------------------------


async def test_history_pruned_after_retention(db: Database, tmp_path: Path) -> None:
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	async with db.session_factory() as session:
		session.add(
			CommunityStatsHistory(
				community_id=community_id,
				at=_NOW - timedelta(days=91),
				participants=5,
				online=None,
			)
		)
		await session.commit()
	assert await _history_count(db) == 1
	await service.refresh_due(_NOW)
	assert await _history_count(db) == 1, "старый снимок убран, свежий добавлен"


async def test_members_report_recorded_and_shown(db: Database, tmp_path: Path) -> None:
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	await service.record_members_report(community_id, 47, 20, _NOW)
	overview = await service.overview(community_id)
	assert (overview.deleted_found, overview.deleted_removed) == (47, 20)
	assert overview.deleted_checked_at == _NOW
	assert overview.fetched_at is None, "отчёт — не проход опроса"


async def test_overview_unknown_community(db: Database, tmp_path: Path) -> None:
	service = _service(db, _FakeStatsGateway(), tmp_path)
	with pytest.raises(CommunityStatsError):
		await service.overview(404)


async def test_polling_task_runs_and_stops(db: Database, tmp_path: Path) -> None:
	"""Периодическая задача делает проход и гасится кооперативно."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	service.start_polling()
	# первый проход идёт сразу после старта задачи — дожидаемся его,
	# уступая цикл событий, затем останавливаем задачу
	for _ in range(200):
		if gateway.stats_calls:
			break
		await asyncio.sleep(0.01)
	await service.shutdown()
	assert len(gateway.stats_calls) == 1


async def test_avatar_absence_and_drop(db: Database, tmp_path: Path) -> None:
	"""Нет аватара — NULL и удаление старого файла; drop чистит кэш."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	await service.refresh_due(_NOW)
	avatar = (await service.snapshot())[0].avatar_path
	assert avatar is not None and Path(avatar).exists()
	# аватар свеж (моложе суток) — второй проход его не перекачивает
	gateway.has_avatar = False
	await service.refresh_due(_NOW, full_every_s=0)
	assert (await service.snapshot())[0].avatar_path == avatar
	# состарим файл руками — теперь отсутствие аватара честно фиксируется
	old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
	os.utime(avatar, (old, old))
	await service.refresh_due(_NOW, full_every_s=0)
	assert (await service.snapshot())[0].avatar_path is None
	assert not Path(avatar).exists()
	# drop убирает файл удаляемого сообщества
	gateway.has_avatar = True
	await service.refresh_due(_NOW, full_every_s=0)
	restored = (await service.snapshot())[0].avatar_path
	assert restored is not None and Path(restored).exists()
	await service.drop(community_id)
	assert not Path(restored).exists()


async def test_paused_publishers_not_polled(db: Database, tmp_path: Path) -> None:
	"""Приостановленные бот и userbot (ADR-0029) опросом пропускаются."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	bot_id = await _add_bot(db)
	await _add_community(db, "-1001", account_id, bot_id)
	async with db.session_factory() as session:
		account = await session.get(TgAccount, account_id)
		bot = await session.get(Bot, bot_id)
		assert account is not None and bot is not None
		account.paused = True
		bot.paused = True
		await session.commit()
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_due(_NOW) is False
	assert gateway.stats_calls == [] and gateway.bot_calls == []
