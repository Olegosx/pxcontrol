"""Тесты учёта активности (ADR-0030): окна, сброс, уборка, снимок."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AccountOperation, Bot, TgAccount
from pxcontrol.engine.services.activity import (
	WINDOW_DAY_S,
	WINDOW_HOUR_S,
	WINDOW_WEEK_S,
	ActivityService,
	LiveDto,
	OwnerActivityDto,
	WindowStats,
	_Interval,
	window_stats,
)
from pxcontrol.engine.telegram.lane import LaneLiveState, OperationRecord, Outcome, WorkKind
from pxcontrol.engine.telegram.types import ExecutorRef, OwnerKind, Share

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
	return _NOW + timedelta(seconds=seconds)


class _FakeGateway:
	"""Подставной шлюз: буфер записей и живые состояния."""

	def __init__(self) -> None:
		self.buffer: list[OperationRecord] = []
		self.live: dict[ExecutorRef, LaneLiveState] = {}

	def drain_operations(self) -> list[OperationRecord]:
		records, self.buffer = self.buffer, []
		return records

	def restore_operations(self, records: Sequence[OperationRecord]) -> None:
		self.buffer[:0] = list(records)

	def live_states(self) -> dict[ExecutorRef, LaneLiveState]:
		return dict(self.live)


def _record(
	owner: ExecutorRef,
	start_s: float,
	end_s: float,
	kind: WorkKind = WorkKind.BACKGROUND,
	outcome: Outcome = Outcome.OK,
	wait_s: int = 0,
) -> OperationRecord:
	return OperationRecord(owner, kind, _at(start_s), _at(end_s), outcome, wait_s)


# --- окно: чистая арифметика интервалов ---------------------------------------------


def test_window_clips_intervals_and_counts_by_end() -> None:
	"""Занятость — пересечение с окном; операция считается по моменту конца."""
	window_start = _at(-3600)
	intervals = [
		# началась за два часа до окна, кончилась внутри: в занятость — 10 минут
		_Interval("publish", _at(-7200), _at(-3000), "ok", 0),
		# целиком в окне: 60 секунд
		_Interval("background", _at(-1000), _at(-940), "ok", 0),
		# кончилась после «сейчас» (часы теста) — не считается операцией,
		# но в занятость входит до «сейчас»
		_Interval("interactive", _at(-30), _at(30), "ok", 0),
		# целиком до окна — ни в чём не участвует
		_Interval("background", _at(-9000), _at(-8000), "ok", 0),
		_Interval("maintenance", _at(-500), _at(-490), "flood", 25),
		_Interval("maintenance", _at(-400), _at(-399), "error", 0),
	]
	stats = window_stats(intervals, window_start, _NOW)
	assert stats.operations == 4
	assert stats.busy_s == 600 + 60 + 30 + 10 + 1
	assert stats.window_s == 3600
	assert (stats.errors, stats.floods) == (1, 1)
	assert 0 < stats.busy_share < 1


def test_window_adds_running_operation_from_live_state() -> None:
	"""Идущая операция входит в занятость от своего начала, но не в число операций."""
	live = LiveDto(WorkKind.PUBLISH, _at(-120), 0, 0.0)
	stats = window_stats([], _at(-60), _NOW, live)
	assert stats.operations == 0
	assert stats.busy_s == 60, "обрезано началом окна"
	assert stats.busy_share == 1.0
	assert window_stats([], _at(-60), _NOW, None).busy_s == 0


# --- сервис: сброс, снимок, уборка -------------------------------------------------------


async def _owners(db: Database) -> tuple[ExecutorRef, ExecutorRef]:
	async with db.session_factory() as session:
		account = TgAccount(label="ub", phone="+7900", session="s")
		bot = Bot(label="b", token="123456:AAAbbb")
		session.add_all([account, bot])
		await session.commit()
		await session.refresh(account)
		await session.refresh(bot)
	return ExecutorRef(OwnerKind.USER, account.id), ExecutorRef(OwnerKind.BOT, bot.id)


async def test_flush_writes_rows_and_skips_unknown_owner(db: Database) -> None:
	"""Пачка уходит в таблицу с правильными ссылками; чужой владелец пропускается."""
	user, bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	gateway.buffer = [
		_record(user, -100, -90, WorkKind.PUBLISH),
		_record(bot, -80, -79, WorkKind.BACKGROUND, Outcome.FLOOD, 30),
		_record(ExecutorRef(OwnerKind.USER, 999), -70, -60),  # удалён, пока шла операция
	]
	assert await service.flush() == 2
	assert gateway.buffer == []
	async with db.session_factory() as session:
		rows = (await session.execute(select(AccountOperation))).scalars().all()
	assert {(r.tg_account_id, r.bot_id, r.kind, r.outcome, r.wait_s) for r in rows} == {
		(user.id, None, "publish", "ok", 0),
		(None, bot.id, "background", "flood", 30),
	}
	assert await service.flush() == 0, "пустой буфер — без записи"


async def test_snapshot_windows_and_live(db: Database) -> None:
	"""Снимок: три окна по строкам БД плюс живое состояние из шлюза."""
	user, bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	gateway.buffer = [
		_record(user, -600, -590, WorkKind.PUBLISH),  # в часе
		_record(user, -5 * 3600, -5 * 3600 + 10),  # в сутках, не в часе
		_record(user, -3 * 86400, -3 * 86400 + 10),  # в неделе
		_record(user, -10 * 86400, -10 * 86400 + 10),  # за неделей — не читается
		_record(bot, -30, -29, WorkKind.BACKGROUND, Outcome.ERROR),
	]
	gateway.live = {
		user: LaneLiveState(WorkKind.MAINTENANCE, _at(-5), 2, 0.0),
		ExecutorRef(OwnerKind.BOT, 42): LaneLiveState(None, None, 0, 12.0),  # только дорожка
	}
	snapshot = await service.snapshot(_NOW)
	assert set(snapshot) == {user, bot, ExecutorRef(OwnerKind.BOT, 42)}
	me = snapshot[user]
	assert (me.last_hour.operations, me.last_day.operations, me.last_week.operations) == (1, 2, 3)
	# занятость считает база: функции даты SQLite работают в миллисекундах
	assert me.last_hour.busy_s == pytest.approx(10 + 5, abs=0.002), "плюс идущая операция"
	assert me.live.busy_kind is WorkKind.MAINTENANCE and me.live.waiting == 2
	assert me.last_operation_at == _at(-590)
	assert snapshot[bot].last_hour.errors == 1
	frozen_only = snapshot[ExecutorRef(OwnerKind.BOT, 42)]
	assert frozen_only.live.frozen_for_s == 12.0 and frozen_only.last_operation_at is None


async def test_prune_removes_older_than_keep_days(db: Database) -> None:
	user, _bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	gateway.buffer = [
		_record(user, -400 * 86400, -400 * 86400 + 1),
		_record(user, -10, -9),
	]
	await service.flush()
	assert await service.prune(_NOW) == 1
	assert await service.prune(_NOW) == 0
	async with db.session_factory() as session:
		assert len((await session.execute(select(AccountOperation))).scalars().all()) == 1


async def test_flush_failure_returns_records_to_buffer(db: Database) -> None:
	"""Сбой записи не теряет пачку: она возвращается в буфер вперёд свежих."""
	user, _bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	first = _record(user, -10, -9)
	gateway.buffer = [first]

	class _BrokenFactory:
		"""Сессия, у которой падает запись."""

		def __call__(self) -> _BrokenFactory:
			return self

		async def __aenter__(self) -> _BrokenFactory:
			return self

		async def __aexit__(self, *args: object) -> None:
			return None

		async def execute(self, *args: object) -> None:
			raise RuntimeError("disk I/O error")

	service._db = type("_Db", (), {"session_factory": _BrokenFactory()})()  # type: ignore[assignment]
	with pytest.raises(RuntimeError, match="disk"):
		await service.flush()
	assert gateway.buffer == [first]


async def test_deleting_owner_cascades_operations(db: Database) -> None:
	"""Удаление пользователя уносит его операции (CASCADE)."""
	user, _bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	gateway.buffer = [_record(user, -10, -9)]
	await service.flush()
	async with db.session_factory() as session:
		account = await session.get(TgAccount, user.id)
		assert account is not None
		await session.delete(account)
		await session.commit()
	async with db.session_factory() as session:
		assert (await session.execute(select(AccountOperation))).scalars().all() == []


# --- история для графиков ----------------------------------------------------------------


def test_history_stats_hours_days_and_kinds() -> None:
	"""Часы — по местному концу; занятость делится между днями через полночь."""
	from datetime import timezone

	from pxcontrol.engine.services.activity import HISTORY_DAYS, history_stats

	tz = timezone(timedelta(hours=3))  # местный пояс отличается от UTC
	now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)  # 15:00 местного
	intervals = [
		# кончилась в 15:00 местного сегодня
		_Interval("publish", _at(-60), now, "ok", 0),
		# загрузка через местную полночь: 23:30 → 00:30 (местного) — по 30 минут двум дням
		_Interval(
			"publish",
			datetime(2026, 9, 14, 20, 30, tzinfo=UTC),
			datetime(2026, 9, 14, 21, 30, tzinfo=UTC),
			"flood",
			10,
		),
		# старше недели — в часы не попадает, в дни (30) — да
		_Interval(
			"background", now - timedelta(days=10), now - timedelta(days=10, seconds=-5), "ok", 0
		),
		# старше месяца — никуда
		_Interval(
			"background", now - timedelta(days=40), now - timedelta(days=40, seconds=-5), "ok", 0
		),
	]
	history = history_stats(intervals, now, tz)
	assert len(history.hours) == 24 and sum(history.hours) == 2
	assert history.hours[15] == 1 and history.hours[0] == 1, "00:30 местного — час 0"
	assert len(history.busy_days) == HISTORY_DAYS
	by_day = {point.day: point.value for point in history.busy_days}
	assert by_day[date(2026, 9, 14)] == 1800 and by_day[date(2026, 9, 15)] == 1800 + 60
	assert by_day[date(2026, 9, 5)] == 5
	floods = {point.day: point.value for point in history.flood_days}
	assert floods[date(2026, 9, 15)] == 1 and floods[date(2026, 9, 14)] == 0
	assert history.operations == 3
	assert {s.name: s.value for s in history.kinds} == {"publish": 2, "background": 1}


async def test_history_reads_only_owner_rows(db: Database) -> None:
	user, bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway, tz=UTC)
	gateway.buffer = [_record(user, -100, -90, WorkKind.PUBLISH), _record(bot, -50, -40)]
	history = await service.history(user, _NOW)
	assert history.operations == 1 and sum(history.hours) == 1
	assert history.busy_days[-1].value == 10
	assert (await service.history(bot, _NOW)).kinds == (Share("background", 1),)


async def test_snapshot_keeps_last_operation_beyond_window(db: Database) -> None:
	"""«Последняя операция» — по всей истории, а не по окну недели.

	Окно показа отвечает на «сколько работал за неделю», а справка —
	на «когда работал вообще»: исполнитель, замолчавший девять дней
	назад, должен видеть свою дату, а не «ещё не было».
	"""
	user, _bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	old = -9 * 24 * 3600
	gateway.buffer = [_record(user, old, old + 5, WorkKind.PUBLISH)]
	await service.flush()
	snapshot = await service.snapshot(_NOW)
	assert snapshot[user].last_week.operations == 0, "в окно недели операция не попала"
	assert snapshot[user].last_operation_at == _at(old + 5)


async def test_flush_returns_batch_to_buffer_when_cancelled(db: Database) -> None:
	"""Отмена задачи не должна уносить пачку: она уже изъята из буфера."""
	user, _bot = await _owners(db)

	class _CancellingDb:
		"""База, чья сессия обрывается отменой (остановка движка на полпути)."""

		def session_factory(self) -> object:
			raise asyncio.CancelledError

	gateway = _FakeGateway()
	service = ActivityService(_CancellingDb(), gateway)  # type: ignore[arg-type]
	records = [_record(user, -100, -90, WorkKind.PUBLISH)]
	gateway.buffer = list(records)
	with pytest.raises(asyncio.CancelledError):
		await service.flush()
	assert gateway.buffer == records, "пачка вернулась в буфер, а не пропала"


async def test_service_counts_operations_of_live_gateway(db: Database) -> None:
	"""Круг через настоящий шлюз: операции доходят до базы и после сброса.

	Подставной шлюз тестов повторяет ожидаемое поведение своими руками
	и потому не замечает, если настоящий его не выполняет. Именно так
	дефект «учёт замолкает после первой выемки» и жил: сервис был
	проверен, шлюз — нет.
	"""
	from pxcontrol.engine.telegram.gateway import TelegramGateway
	from pxcontrol.engine.telegram.lane import TelegramPriority as Priority

	user, _bot = await _owners(db)
	gateway = TelegramGateway()
	service = ActivityService(db, gateway)
	for _ in range(3):
		async with gateway._lane(user).slot(Priority.PUBLISH_DUE):  # noqa: SLF001 — дорожка изнутри
			pass
		assert await service.flush() == 1, "каждая операция доходит до базы"
	snapshot = await service.snapshot()
	assert snapshot[user].last_day.operations == 3
	await gateway.stop()


# --- агрегаты базы против Python-эталона ------------------------------------------------


def _reference(records: list[OperationRecord], owner: ExecutorRef) -> dict[str, WindowStats]:
	"""Окна владельца по эталонной ``window_stats`` из тех же записей."""
	intervals = [
		_Interval(r.kind.name.lower(), r.started_at, r.finished_at, str(r.outcome), r.wait_s)
		for r in records
		if r.owner == owner
	]
	return {
		"last_hour": window_stats(intervals, _at(-WINDOW_HOUR_S), _NOW),
		"last_day": window_stats(intervals, _at(-WINDOW_DAY_S), _NOW),
		"last_week": window_stats(intervals, _at(-WINDOW_WEEK_S), _NOW),
	}


def _assert_windows_match(actual: OwnerActivityDto, expected: dict[str, WindowStats]) -> None:
	for label, want in expected.items():
		got: WindowStats = getattr(actual, label)
		assert (got.operations, got.errors, got.floods, got.window_s) == (
			want.operations,
			want.errors,
			want.floods,
			want.window_s,
		), label
		# функции даты SQLite считают в миллисекундах: до 1 мс на операцию,
		# у суммы за окно погрешность растёт с их числом (доля окна её не видит)
		tolerance = 0.002 + 0.001 * want.operations
		assert got.busy_s == pytest.approx(want.busy_s, abs=tolerance), label


async def test_snapshot_matches_python_reference_on_random_operations(db: Database) -> None:
	"""Агрегаты базы совпадают с эталоном ``window_stats`` на случайных операциях.

	Операции с разными началами и длительностями (в том числе длиннее
	часа и через границы окон), всеми исходами, у двух владельцев;
	часть — за пределами недели. Правило окна одно, и разойтись ему
	нельзя незаметно.
	"""
	user, bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	rng = random.Random(20260920)
	kinds = list(WorkKind)
	outcomes = list(Outcome)
	records: list[OperationRecord] = []
	for _ in range(240):
		owner = user if rng.random() < 0.6 else bot
		end = -rng.uniform(0, 9 * 86400)  # до девяти дней назад — часть за неделей
		length = rng.choice([rng.uniform(0.1, 30), rng.uniform(60, 5400)])  # секунды и часы
		records.append(_record(owner, end - length, end, rng.choice(kinds), rng.choice(outcomes)))
	gateway.buffer = list(records)
	snapshot = await service.snapshot(_NOW)
	for owner in (user, bot):
		_assert_windows_match(snapshot[owner], _reference(records, owner))


async def test_snapshot_window_edges(db: Database) -> None:
	"""Границы окна: частичное пересечение, конец ровно в now, будущее начало."""
	user, _bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	gateway.buffer = [
		# началась до окна, кончилась внутри: в занятость — 20 с, в счёт — да (по концу)
		_record(user, -WINDOW_HOUR_S - 100, -WINDOW_HOUR_S + 20),
		_record(user, -5, 0),  # кончилась ровно в now — считается
		_record(user, 30, 60),  # часы сбились: началась после now — ни счёта, ни занятости
	]
	snapshot = await service.snapshot(_NOW)
	hour = snapshot[user].last_hour
	assert hour.operations == 2
	assert hour.busy_s == pytest.approx(20 + 5, abs=0.002)
	assert snapshot[user].last_day.busy_s == pytest.approx(120 + 5, abs=0.002)
