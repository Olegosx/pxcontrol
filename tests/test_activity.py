"""Тесты учёта активности (ADR-0030): окна, сброс, уборка, снимок."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AccountOperation, Bot, TgAccount
from pxcontrol.engine.services.activity import (
	ActivityService,
	LiveDto,
	_Interval,
	window_stats,
)
from pxcontrol.engine.telegram.lane import (
	LaneLiveState,
	LaneOwner,
	OperationRecord,
	Outcome,
	OwnerKind,
	TelegramPriority,
)

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
	return _NOW + timedelta(seconds=seconds)


class _FakeGateway:
	"""Подставной шлюз: буфер записей и живые состояния."""

	def __init__(self) -> None:
		self.buffer: list[OperationRecord] = []
		self.live: dict[LaneOwner, LaneLiveState] = {}

	def drain_operations(self) -> list[OperationRecord]:
		records, self.buffer = self.buffer, []
		return records

	def restore_operations(self, records: Sequence[OperationRecord]) -> None:
		self.buffer[:0] = list(records)

	def live_states(self) -> dict[LaneOwner, LaneLiveState]:
		return dict(self.live)


def _record(
	owner: LaneOwner,
	start_s: float,
	end_s: float,
	kind: TelegramPriority = TelegramPriority.BACKGROUND,
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
	assert stats.by_kind == {"publish": 1, "background": 1, "maintenance": 2}
	assert stats.busy_s == 600 + 60 + 30 + 10 + 1
	assert stats.window_s == 3600
	assert (stats.errors, stats.floods, stats.flood_wait_s) == (1, 1, 25)
	assert 0 < stats.busy_share < 1


def test_window_adds_running_operation_from_live_state() -> None:
	"""Идущая операция входит в занятость от своего начала, но не в число операций."""
	live = LiveDto(TelegramPriority.PUBLISH, _at(-120), 0, 0.0)
	stats = window_stats([], _at(-60), _NOW, live)
	assert stats.operations == 0
	assert stats.busy_s == 60, "обрезано началом окна"
	assert stats.busy_share == 1.0
	assert window_stats([], _at(-60), _NOW, None).busy_s == 0


# --- сервис: сброс, снимок, уборка -------------------------------------------------------


async def _owners(db: Database) -> tuple[LaneOwner, LaneOwner]:
	async with db.session_factory() as session:
		account = TgAccount(label="ub", phone="+7900", session="s")
		bot = Bot(label="b", token="123456:AAAbbb")
		session.add_all([account, bot])
		await session.commit()
		await session.refresh(account)
		await session.refresh(bot)
	return LaneOwner(OwnerKind.USER, account.id), LaneOwner(OwnerKind.BOT, bot.id)


async def test_flush_writes_rows_and_skips_unknown_owner(db: Database) -> None:
	"""Пачка уходит в таблицу с правильными ссылками; чужой владелец пропускается."""
	user, bot = await _owners(db)
	gateway = _FakeGateway()
	service = ActivityService(db, gateway)
	gateway.buffer = [
		_record(user, -100, -90, TelegramPriority.PUBLISH),
		_record(bot, -80, -79, TelegramPriority.BACKGROUND, Outcome.FLOOD, 30),
		_record(LaneOwner(OwnerKind.USER, 999), -70, -60),  # удалён, пока шла операция
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
		_record(user, -600, -590, TelegramPriority.PUBLISH),  # в часе
		_record(user, -5 * 3600, -5 * 3600 + 10),  # в сутках, не в часе
		_record(user, -3 * 86400, -3 * 86400 + 10),  # в неделе
		_record(user, -10 * 86400, -10 * 86400 + 10),  # за неделей — не читается
		_record(bot, -30, -29, TelegramPriority.BACKGROUND, Outcome.ERROR),
	]
	gateway.live = {
		user: LaneLiveState(TelegramPriority.MAINTENANCE, _at(-5), 2, 0.0),
		LaneOwner(OwnerKind.BOT, 42): LaneLiveState(None, None, 0, 12.0),  # только дорожка
	}
	snapshot = await service.snapshot(_NOW)
	assert set(snapshot) == {user, bot, LaneOwner(OwnerKind.BOT, 42)}
	me = snapshot[user]
	assert (me.last_hour.operations, me.last_day.operations, me.last_week.operations) == (1, 2, 3)
	assert me.last_hour.busy_s == 10 + 5, "плюс идущая операция"
	assert me.live.busy_kind is TelegramPriority.MAINTENANCE and me.live.waiting == 2
	assert me.last_operation_at == _at(-590)
	assert snapshot[bot].last_hour.errors == 1
	frozen_only = snapshot[LaneOwner(OwnerKind.BOT, 42)]
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
