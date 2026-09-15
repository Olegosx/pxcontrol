"""Учёт активности пользователей и ботов в Telegram (ADR-0030).

Единица учёта — одно обращение к Telegram через дорожку шлюза
(:mod:`pxcontrol.engine.telegram.lane`): дорожка записывает начало,
конец и исход каждой операции, а этот сервис забирает записи из буфера
шлюза и складывает в таблицу ``account_operations`` — пачкой, раз
в несколько секунд и при остановке движка. Запись на операцию, а не
почасовые корзины: операция может длиться дольше часа (загрузка файла),
и дробить её по границам часов при записи было бы костылём. Занятость
окна считается при чтении — суммой пересечений интервалов операций
с окном; идущая прямо сейчас операция в базе ещё не лежит и добавляется
из живого состояния дорожки.

Объём честный: при десятке сообществ — около двух тысяч строк в сутки,
70 МБ за год хранения (``KEEP_DAYS``); строки старше убирает сам сервис.

Живое состояние (чем занята дорожка, сколько ждут, заморозка) в БД
не пишется — оно меняется каждую секунду и читается из шлюза.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Protocol

from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AccountOperation, Bot, TgAccount
from pxcontrol.engine.telegram.lane import (
	LaneLiveState,
	LaneOwner,
	OperationRecord,
	Outcome,
	OwnerKind,
	TelegramPriority,
)
from pxcontrol.engine.telegram.types import DayPoint, Share

logger = logging.getLogger(__name__)

#: Как часто буфер шлюза сбрасывается в БД, секунды. Пустой буфер
#: записи не даёт; снимок для интерфейса сбрасывает буфер сам.
FLUSH_INTERVAL_S = 10

#: Срок хранения операций, дни (решение владельца: год).
KEEP_DAYS = 365

#: Как часто убирать строки старше срока хранения, секунды (раз в час).
PRUNE_EVERY_S = 3600

#: Окна показа: последний час, сутки, неделя.
WINDOW_HOUR_S = 3600
WINDOW_DAY_S = 24 * 3600
WINDOW_WEEK_S = 7 * 24 * 3600

#: Окна истории страницы аккаунта (ADR-0030): профиль по часам суток —
#: за неделю, занятость и флуд-лимиты по дням — за месяц.
HOURS_DAYS = 7
HISTORY_DAYS = 30

#: Сколько ждать периодическую задачу при остановке движка (ADR-0020).
_SHUTDOWN_TIMEOUT_S = 10.0


class _ActivitySource(Protocol):
	"""Часть шлюза, нужная сервису (для подмены в тестах)."""

	def drain_operations(self) -> list[OperationRecord]: ...

	def restore_operations(self, records: Sequence[OperationRecord]) -> None: ...

	def live_states(self) -> dict[LaneOwner, LaneLiveState]: ...


@dataclass(frozen=True)
class WindowStats:
	"""Активность владельца за окно показа.

	Attributes:
		operations: сколько операций завершилось в окне (по концу).
		by_kind: из них по видам (имя ``TelegramPriority`` → число).
		busy_s: секунды занятости — сумма пересечений операций с окном,
			включая идущую сейчас.
		window_s: длина окна — для доли занятости.
		errors: операций с ошибкой.
		floods: флуд-лимитов.
		flood_wait_s: суммарный срок, названный Telegram при них.
	"""

	operations: int = 0
	by_kind: dict[str, int] = field(default_factory=dict)
	busy_s: float = 0.0
	window_s: int = 0
	errors: int = 0
	floods: int = 0
	flood_wait_s: int = 0

	@property
	def busy_share(self) -> float:
		"""Доля занятости окна, 0..1 (окно нулевой длины — 0)."""
		if self.window_s <= 0:
			return 0.0
		return min(1.0, self.busy_s / self.window_s)


@dataclass(frozen=True)
class LiveDto:
	"""Живое состояние дорожки владельца для карточки."""

	busy_kind: TelegramPriority | None
	busy_since: datetime | None
	waiting: int
	frozen_for_s: float


@dataclass(frozen=True)
class OwnerActivityDto:
	"""Снимок активности одного владельца: живое состояние и три окна."""

	owner: LaneOwner
	live: LiveDto
	last_hour: WindowStats
	last_day: WindowStats
	last_week: WindowStats
	last_operation_at: datetime | None


@dataclass(frozen=True)
class ActivityHistoryDto:
	"""История активности владельца для графиков страницы аккаунта.

	Attributes:
		hours: операций по часам суток (24 значения, местное время)
			за последние ``HOURS_DAYS`` дней.
		busy_days: занятость по дням за ``HISTORY_DAYS`` дней — секунды,
			пересечение операций с каждым местным днём.
		flood_days: флуд-лимитов по дням за тот же срок.
		kinds: операции по видам за тот же срок — доли для строк с полосой.
		operations: сколько операций за тот же срок.
	"""

	hours: tuple[int, ...]
	busy_days: tuple[DayPoint, ...]
	flood_days: tuple[DayPoint, ...]
	kinds: tuple[Share, ...]
	operations: int


@dataclass(frozen=True)
class _Interval:
	"""Операция в чистом виде для расчётов: без владельца."""

	kind: str
	started_at: datetime
	finished_at: datetime
	outcome: str
	wait_s: int


def _as_utc(moment: datetime) -> datetime:
	"""Момент из БД → aware-UTC (SQLite возвращает наивные значения)."""
	return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def window_stats(
	intervals: Iterable[_Interval],
	window_start: datetime,
	now: datetime,
	live: LiveDto | None = None,
) -> WindowStats:
	"""Считает окно по операциям, пересекающим его (чистая функция).

	Операция засчитывается в число операций, если завершилась внутри
	окна; в занятость — пересечением с окном, сколько бы она ни длилась
	и когда бы ни началась. Идущая сейчас операция (``live``) в занятость
	входит от своего начала до ``now``, в число операций — нет.
	"""
	operations = 0
	by_kind: dict[str, int] = {}
	busy = 0.0
	errors = floods = wait = 0
	for item in intervals:
		overlap = (min(item.finished_at, now) - max(item.started_at, window_start)).total_seconds()
		if overlap > 0:
			busy += overlap
		if window_start <= item.finished_at <= now:
			operations += 1
			by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
			if item.outcome == Outcome.ERROR:
				errors += 1
			elif item.outcome == Outcome.FLOOD:
				floods += 1
				wait += item.wait_s
	if live is not None and live.busy_since is not None:
		running = (now - max(live.busy_since, window_start)).total_seconds()
		if running > 0:
			busy += running
	return WindowStats(
		operations=operations,
		by_kind=by_kind,
		busy_s=busy,
		window_s=int((now - window_start).total_seconds()),
		errors=errors,
		floods=floods,
		flood_wait_s=wait,
	)


def _local_day_start(day: date, tz: tzinfo) -> datetime:
	"""Начало местного дня в UTC."""
	return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)


def history_stats(intervals: Iterable[_Interval], now: datetime, tz: tzinfo) -> ActivityHistoryDto:
	"""Считает историю для графиков по операциям владельца (чистая функция).

	Часы суток — по местному моменту конца операции за ``HOURS_DAYS``
	дней; занятость по дням — пересечение каждой операции с каждым
	местным днём, поэтому загрузка через полночь честно делится между
	днями; флуд-лимиты и виды — по концу операции за ``HISTORY_DAYS``.
	"""
	items = list(intervals)
	today = now.astimezone(tz).date()
	hours = [0] * 24
	hours_from = now - timedelta(days=HOURS_DAYS)
	for item in items:
		if hours_from <= item.finished_at <= now:
			hours[item.finished_at.astimezone(tz).hour] += 1
	days = [today - timedelta(days=offset) for offset in range(HISTORY_DAYS - 1, -1, -1)]
	busy_days: list[DayPoint] = []
	flood_days: list[DayPoint] = []
	for day in days:
		start = _local_day_start(day, tz)
		end = min(start + timedelta(days=1), now)
		busy = 0.0
		floods = 0
		for item in items:
			overlap = (min(item.finished_at, end) - max(item.started_at, start)).total_seconds()
			if overlap > 0:
				busy += overlap
			if item.outcome == Outcome.FLOOD and start <= item.finished_at < end:
				floods += 1
		busy_days.append(DayPoint(day, int(round(busy))))
		flood_days.append(DayPoint(day, floods))
	history_from = _local_day_start(days[0], tz)
	by_kind: dict[str, int] = {}
	operations = 0
	for item in items:
		if history_from <= item.finished_at <= now:
			operations += 1
			by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
	kinds = tuple(Share(name, count) for name, count in by_kind.items())
	return ActivityHistoryDto(tuple(hours), tuple(busy_days), tuple(flood_days), kinds, operations)


def _row(record: OperationRecord) -> AccountOperation:
	"""Строка таблицы из записи дорожки."""
	return AccountOperation(
		tg_account_id=record.owner.id if record.owner.kind is OwnerKind.USER else None,
		bot_id=record.owner.id if record.owner.kind is OwnerKind.BOT else None,
		kind=record.kind.name.lower(),
		started_at=record.started_at,
		finished_at=record.finished_at,
		outcome=str(record.outcome),
		wait_s=record.wait_s,
	)


def _owner_of(row: AccountOperation) -> LaneOwner | None:
	"""Владелец строки (None — строка без владельца, чего быть не должно)."""
	if row.tg_account_id is not None:
		return LaneOwner(OwnerKind.USER, row.tg_account_id)
	if row.bot_id is not None:
		return LaneOwner(OwnerKind.BOT, row.bot_id)
	return None


class ActivityService:
	"""Сброс операций в БД, уборка, снимки активности для интерфейса."""

	def __init__(self, db: Database, gateway: _ActivitySource, tz: tzinfo | None = None) -> None:
		"""``tz`` — местный пояс для дней и часов суток истории; None — пояс машины."""
		self._db = db
		self._gateway = gateway
		self._tz: tzinfo = tz if tz is not None else (datetime.now(UTC).astimezone().tzinfo or UTC)
		self._task: asyncio.Task[None] | None = None
		self._stop = asyncio.Event()
		self._last_prune: datetime | None = None

	# --- жизненный цикл ------------------------------------------------------------

	def start(self) -> None:
		"""Запускает периодический сброс буфера (и уборку раз в час)."""
		if self._task is None or self._task.done():
			self._task = asyncio.create_task(self._run())

	async def shutdown(self) -> None:
		"""Гасит задачу кооперативно (ADR-0020) и досбрасывает буфер.

		Зовётся до остановки шлюза: последние операции сессии не должны
		пропасть вместе с его буфером.
		"""
		self._stop.set()
		if self._task is not None:
			with contextlib.suppress(TimeoutError, asyncio.CancelledError):
				await asyncio.wait_for(self._task, timeout=_SHUTDOWN_TIMEOUT_S)
			self._task = None
		try:
			await self.flush()
		except Exception:  # noqa: BLE001 — остановка важнее последней пачки
			logger.exception("Последний сброс активности не удался.")

	async def _run(self) -> None:
		"""Цикл: сброс буфера, изредка уборка, пауза — до остановки."""
		while not self._stop.is_set():
			try:
				await self.flush()
				await self._prune_if_due()
			except Exception:  # noqa: BLE001 — учёт не должен умирать
				logger.exception("Сброс активности не удался.")
			with contextlib.suppress(TimeoutError):
				await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_INTERVAL_S)

	# --- запись ----------------------------------------------------------------------

	async def flush(self) -> int:
		"""Переносит записи из буфера шлюза в БД пачкой.

		Записи владельца, которого уже удалили (аккаунт или бот убрали,
		пока операция шла), пропускаются: внешний ключ их не примет,
		а учитывать их не за кем. Сбой записи возвращает пачку в буфер
		шлюза — не потерять важнее, чем не задержать.

		Returns:
			Сколько строк записано.
		"""
		records = self._gateway.drain_operations()
		if not records:
			return 0
		try:
			async with self._db.session_factory() as session:
				accounts = set((await session.execute(select(TgAccount.id))).scalars())
				bots = set((await session.execute(select(Bot.id))).scalars())
				rows = [
					_row(record)
					for record in records
					if record.owner.id
					in (accounts if record.owner.kind is OwnerKind.USER else bots)
				]
				session.add_all(rows)
				await session.commit()
		except Exception:
			# пачка возвращается в буфер вперёд накапавших за время попытки
			self._gateway.restore_operations(records)
			raise
		skipped = len(records) - len(rows)
		if skipped:
			logger.info("Активность: %d записей без владельца пропущено.", skipped)
		return len(rows)

	async def _prune_if_due(self, now: datetime | None = None) -> None:
		"""Раз в час убирает строки старше срока хранения."""
		now = now or datetime.now(UTC)
		if (
			self._last_prune is not None
			and (now - self._last_prune).total_seconds() < PRUNE_EVERY_S
		):
			return
		self._last_prune = now
		await self.prune(now)

	async def prune(self, now: datetime | None = None, keep_days: int = KEEP_DAYS) -> int:
		"""Удаляет операции старше срока хранения; возвращает число удалённых."""
		now = now or datetime.now(UTC)
		threshold = now - timedelta(days=keep_days)
		async with self._db.session_factory() as session:
			result = await session.execute(
				delete(AccountOperation).where(AccountOperation.finished_at < threshold)
			)
			await session.commit()
		# у результата DELETE число строк есть, но общий тип Result его не обещает
		removed = int(getattr(result, "rowcount", 0) or 0)
		if removed:
			logger.info("Активность: удалено %d записей старше %d дней.", removed, keep_days)
		return removed

	# --- чтение ----------------------------------------------------------------------

	async def snapshot(self, now: datetime | None = None) -> dict[LaneOwner, OwnerActivityDto]:
		"""Активность всех владельцев: живое состояние и окна час / сутки / неделя.

		Буфер сбрасывается перед чтением: снимок не должен отставать
		от только что завершённой операции на период сброса. Читаются
		операции, пересекающие самое широкое окно, — одним запросом
		на всех; окна считаются в памяти.
		"""
		now = now or datetime.now(UTC)
		await self.flush()
		week_start = now - timedelta(seconds=WINDOW_WEEK_S)
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(AccountOperation).where(AccountOperation.finished_at >= week_start)
					)
				)
				.scalars()
				.all()
			)
		by_owner: dict[LaneOwner, list[_Interval]] = {}
		for row in rows:
			owner = _owner_of(row)
			if owner is None:
				continue
			by_owner.setdefault(owner, []).append(
				_Interval(
					row.kind,
					_as_utc(row.started_at),
					_as_utc(row.finished_at),
					row.outcome,
					row.wait_s,
				)
			)
		lives = {
			owner: LiveDto(state.busy_kind, state.busy_since, state.waiting, state.frozen_for_s)
			for owner, state in self._gateway.live_states().items()
		}
		result: dict[LaneOwner, OwnerActivityDto] = {}
		for owner in set(by_owner) | set(lives):
			intervals = by_owner.get(owner, [])
			live = lives.get(owner, LiveDto(None, None, 0, 0.0))
			result[owner] = OwnerActivityDto(
				owner=owner,
				live=live,
				last_hour=window_stats(
					intervals, now - timedelta(seconds=WINDOW_HOUR_S), now, live
				),
				last_day=window_stats(intervals, now - timedelta(seconds=WINDOW_DAY_S), now, live),
				last_week=window_stats(intervals, week_start, now, live),
				last_operation_at=max((i.finished_at for i in intervals), default=None),
			)
		return result

	async def history(self, owner: LaneOwner, now: datetime | None = None) -> ActivityHistoryDto:
		"""История одного владельца для графиков страницы аккаунта.

		Читаются операции, пересекающие месячное окно (по концу — после
		начала первого дня), буфер сбрасывается перед чтением.
		"""
		now = now or datetime.now(UTC)
		await self.flush()
		first_day = now.astimezone(self._tz).date() - timedelta(days=HISTORY_DAYS - 1)
		since = _local_day_start(first_day, self._tz)
		column = (
			AccountOperation.tg_account_id
			if owner.kind is OwnerKind.USER
			else AccountOperation.bot_id
		)
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(AccountOperation).where(
							column == owner.id, AccountOperation.finished_at >= since
						)
					)
				)
				.scalars()
				.all()
			)
		intervals = [
			_Interval(
				row.kind, _as_utc(row.started_at), _as_utc(row.finished_at), row.outcome, row.wait_s
			)
			for row in rows
		]
		return history_stats(intervals, now, self._tz)
