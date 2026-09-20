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

Окна снимка (час, сутки, неделя) считает **база одним запросом
с группировкой по владельцу** (с 2026-09-20): снимок читается раз
в пять секунд, пока видна страница исполнителей, и поднимать ради него
тысячи строк недели объектами было расточительно. Правило окна при этом
одно: чистая :func:`window_stats` осталась эталоном, и тест сверяет
с ней агрегаты базы на случайных данных. Длительности считаются
функцией ``julianday`` и многоаргументными ``min``/``max`` — это SQLite
(стек проекта, ADR-0009); при смене СУБД запрос переписывается вместе
с остальными местами, зависящими от диалекта.

Живое состояние (чем занята дорожка, сколько ждут, заморозка) в БД
не пишется — оно меняется каждую секунду и читается из шлюза.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Protocol

from sqlalchemy import BindParameter, DateTime, and_, bindparam, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AccountOperation, Bot, TgAccount
from pxcontrol.engine.db.types import as_utc
from pxcontrol.engine.periodic import PeriodicTask
from pxcontrol.engine.telegram.lane import LaneLiveState, OperationRecord, Outcome, TelegramPriority
from pxcontrol.engine.telegram.types import DayPoint, ExecutorRef, OwnerKind, Share

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

#: Окна снимка по порядку: имя поля ``OwnerActivityDto`` → длина, секунды.
#: Самое широкое — последнее: по нему режется выборка строк.
_WINDOWS: tuple[tuple[str, int], ...] = (
	("last_hour", WINDOW_HOUR_S),
	("last_day", WINDOW_DAY_S),
	("last_week", WINDOW_WEEK_S),
)

#: Секунд в сутках: разница ``julianday`` измеряется в сутках.
_DAY_S = 86400.0

#: До скольких знаков округлять занятость из базы: функции даты SQLite
#: считают с точностью до миллисекунды (до 1 мс погрешности на операцию),
#: показ идёт в долях окна — хвосты дробных микросекунд не нужны.
_BUSY_DIGITS = 3

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

	def live_states(self) -> dict[ExecutorRef, LaneLiveState]: ...


@dataclass(frozen=True)
class WindowStats:
	"""Активность владельца за окно показа.

	Attributes:
		operations: сколько операций завершилось в окне (по концу).
		busy_s: секунды занятости — сумма пересечений операций с окном,
			включая идущую сейчас.
		window_s: длина окна — для доли занятости.
		errors: операций с ошибкой.
		floods: флуд-лимитов.
	"""

	operations: int = 0
	busy_s: float = 0.0
	window_s: int = 0
	errors: int = 0
	floods: int = 0

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

	owner: ExecutorRef
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


def live_busy_s(live: LiveDto | None, window_start: datetime, now: datetime) -> float:
	"""Занятость идущей прямо сейчас операции внутри окна, секунды.

	Идущая операция в базе ещё не лежит: в занятость она входит от своего
	начала (не раньше начала окна) до ``now``, в число операций — нет.
	Правило одно на Python-эталон (:func:`window_stats`) и на снимок
	из агрегатов базы.
	"""
	if live is None or live.busy_since is None:
		return 0.0
	return max(0.0, (now - max(live.busy_since, window_start)).total_seconds())


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

	Снимок для интерфейса это правило считает базой (:meth:`ActivityService.snapshot`);
	функция осталась **эталоном**: по ней тест сверяет агрегаты SQL.
	"""
	operations = 0
	busy = 0.0
	errors = floods = 0
	for item in intervals:
		overlap = (min(item.finished_at, now) - max(item.started_at, window_start)).total_seconds()
		if overlap > 0:
			busy += overlap
		if window_start <= item.finished_at <= now:
			operations += 1
			if item.outcome == Outcome.ERROR:
				errors += 1
			elif item.outcome == Outcome.FLOOD:
				floods += 1
	busy += live_busy_s(live, window_start, now)
	return WindowStats(
		operations=operations,
		busy_s=busy,
		window_s=int((now - window_start).total_seconds()),
		errors=errors,
		floods=floods,
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


def _owner_of(tg_account_id: int | None, bot_id: int | None) -> ExecutorRef | None:
	"""Владелец строки (None — строка без владельца, чего быть не должно)."""
	if tg_account_id is not None:
		return ExecutorRef(OwnerKind.USER, tg_account_id)
	if bot_id is not None:
		return ExecutorRef(OwnerKind.BOT, bot_id)
	return None


def _window_columns(label: str, start: BindParameter[Any], now: BindParameter[Any]) -> list[Any]:
	"""Столбцы агрегатов одного окна: операции, ошибки, флуд-лимиты, занятость.

	Число операций и исходов — по концу внутри окна; занятость — сумма
	положительных пересечений ``[started_at, finished_at]`` с ``[start, now]``
	в секундах (``julianday`` даёт сутки — переводится через :data:`_DAY_S`).
	Строка, кончившаяся до начала окна, даёт отрицательное пересечение
	и обнуляется ``max(0, …)`` — как ``overlap > 0`` в эталоне.
	"""
	finished = AccountOperation.finished_at
	started = AccountOperation.started_at
	in_window = and_(finished >= start, finished <= now)
	overlap_days = func.min(func.julianday(finished), func.julianday(now)) - func.max(
		func.julianday(started), func.julianday(start)
	)
	busy_s = func.coalesce(func.sum(func.max(0.0, overlap_days * _DAY_S)), 0.0)
	outcome = AccountOperation.outcome
	errors = and_(in_window, outcome == str(Outcome.ERROR))
	floods = and_(in_window, outcome == str(Outcome.FLOOD))
	return [
		func.count(case((in_window, 1))).label(f"{label}_operations"),
		func.count(case((errors, 1))).label(f"{label}_errors"),
		func.count(case((floods, 1))).label(f"{label}_floods"),
		busy_s.label(f"{label}_busy_s"),
	]


def _with_live(
	stats: WindowStats | None, window_s: int, live: LiveDto, now: datetime
) -> WindowStats:
	"""Окно из агрегатов базы (или пустое) плюс идущая сейчас операция."""
	base = stats if stats is not None else WindowStats(window_s=window_s)
	running = live_busy_s(live, now - timedelta(seconds=window_s), now)
	return replace(base, busy_s=base.busy_s + running) if running else base


class ActivityService:
	"""Сброс операций в БД, уборка, снимки активности для интерфейса."""

	def __init__(self, db: Database, gateway: _ActivitySource, tz: tzinfo | None = None) -> None:
		"""``tz`` — местный пояс для дней и часов суток истории; None — пояс машины."""
		self._db = db
		self._gateway = gateway
		self._tz: tzinfo = tz if tz is not None else (datetime.now(UTC).astimezone().tzinfo or UTC)
		self._task = PeriodicTask(
			self._tick,
			name="Учёт активности",
			interval_s=FLUSH_INTERVAL_S,
			shutdown_timeout_s=_SHUTDOWN_TIMEOUT_S,
		)
		self._last_prune: datetime | None = None

	# --- жизненный цикл ------------------------------------------------------------

	def start(self) -> None:
		"""Запускает периодический сброс буфера (и уборку раз в час)."""
		self._task.start()

	async def shutdown(self) -> None:
		"""Гасит задачу кооперативно (ADR-0020) и досбрасывает буфер.

		Зовётся до остановки шлюза: последние операции сессии не должны
		пропасть вместе с его буфером.
		"""
		await self._task.shutdown()
		try:
			await self.flush()
		except Exception:  # noqa: BLE001 — остановка важнее последней пачки
			logger.exception("Последний сброс активности не удался.")

	async def _tick(self) -> None:
		"""Один проход: сброс буфера и — изредка — уборка старья."""
		await self.flush()
		await self._prune_if_due()

	# --- запись ----------------------------------------------------------------------

	async def flush(self) -> int:
		"""Переносит записи из буфера шлюза в БД пачкой.

		Записи владельца, которого уже удалили (аккаунт или бот убрали,
		пока операция шла), пропускаются: внешний ключ их не примет,
		а учитывать их не за кем. Сбой записи возвращает пачку в буфер
		шлюза — не потерять важнее, чем не задержать.

		Возврат ловит и отмену задачи: пачка уже изъята из буфера,
		и молчаливая отмена (остановка движка на полпути) унесла бы
		её с собой — в базу не попала, в буфере её больше нет.

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
		except BaseException:
			# пачка возвращается в буфер вперёд накапавших за время попытки;
			# BaseException — ради отмены задачи: она не ошибка, но пачку
			# теряет так же безвозвратно
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

	async def snapshot(self, now: datetime | None = None) -> dict[ExecutorRef, OwnerActivityDto]:
		"""Активность всех владельцев: живое состояние и окна час / сутки / неделя.

		Буфер сбрасывается перед чтением: снимок не должен отставать
		от только что завершённой операции на период сброса. Читаются
		операции, пересекающие самое широкое окно, — одним запросом
		на всех; окна считаются в памяти.

		Момент последней операции берётся **без окна** — отдельным
		запросом по всей таблице: исполнитель, работавший девять дней
		назад, должен видеть свою дату, а не «ещё не было».
		"""
		now = now or datetime.now(UTC)
		await self.flush()
		async with self._db.session_factory() as session:
			windows = await self._window_aggregates(session, now)
			last_seen = await self._last_operations(session)
		lives = {
			owner: LiveDto(state.busy_kind, state.busy_since, state.waiting, state.frozen_for_s)
			for owner, state in self._gateway.live_states().items()
		}
		result: dict[ExecutorRef, OwnerActivityDto] = {}
		for owner in set(windows) | set(lives) | set(last_seen):
			live = lives.get(owner, LiveDto(None, None, 0, 0.0))
			stats = windows.get(owner, {})
			result[owner] = OwnerActivityDto(
				owner=owner,
				live=live,
				last_hour=_with_live(stats.get("last_hour"), WINDOW_HOUR_S, live, now),
				last_day=_with_live(stats.get("last_day"), WINDOW_DAY_S, live, now),
				last_week=_with_live(stats.get("last_week"), WINDOW_WEEK_S, live, now),
				last_operation_at=last_seen.get(owner),
			)
		return result

	@staticmethod
	async def _window_aggregates(
		session: AsyncSession, now: datetime
	) -> dict[ExecutorRef, dict[str, WindowStats]]:
		"""Окна час / сутки / неделя по каждому владельцу — одним запросом базы.

		Выборка режется по самому широкому окну (неделя) — она ложится
		на индекс по ``finished_at``; для каждого окна база считает число
		операций, ошибок и флуд-лимитов по концу внутри окна и занятость —
		сумму пересечений интервалов с окном (правило :func:`window_stats`).
		Группировка идёт по строкам недели, а не по всей таблице, поэтому
		дорогого случая из :meth:`_last_operations` здесь нет.

		Владельцы без операций за неделю в ответе отсутствуют — снимок
		дополняет их нулевыми окнами.
		"""
		now_bind = bindparam("now", now, type_=DateTime(timezone=True))
		columns: list[Any] = [AccountOperation.tg_account_id, AccountOperation.bot_id]
		for label, seconds in _WINDOWS:
			start_bind = bindparam(
				f"{label}_start", now - timedelta(seconds=seconds), type_=DateTime(timezone=True)
			)
			columns.extend(_window_columns(label, start_bind, now_bind))
		week_start = now - timedelta(seconds=WINDOW_WEEK_S)
		statement = (
			select(*columns)
			.where(AccountOperation.finished_at >= week_start)
			.group_by(AccountOperation.tg_account_id, AccountOperation.bot_id)
		)
		result: dict[ExecutorRef, dict[str, WindowStats]] = {}
		for row in (await session.execute(statement)).mappings():
			owner = _owner_of(row["tg_account_id"], row["bot_id"])
			if owner is None:
				continue
			result[owner] = {
				label: WindowStats(
					operations=int(row[f"{label}_operations"]),
					busy_s=round(float(row[f"{label}_busy_s"]), _BUSY_DIGITS),
					window_s=seconds,
					errors=int(row[f"{label}_errors"]),
					floods=int(row[f"{label}_floods"]),
				)
				for label, seconds in _WINDOWS
			}
		return result

	@staticmethod
	async def _last_operations(session: AsyncSession) -> dict[ExecutorRef, datetime]:
		"""Момент последней операции каждого владельца (по всей истории).

		Отдельный запрос, а не максимум по прочитанным строкам окна:
		окно показа — про «сколько работал за неделю», а справка про
		последнюю операцию — про «когда работал вообще».

		Запросов **два, по одному на вид владельца**, и это не прихоть,
		а цена: у таблицы есть составные индексы «владелец + момент
		конца», и группировка по одной колонке ложится на них покрывающим
		поиском. Одна общая группировка по двум колонкам на них не
		ложится и строит временное дерево — на годовом объёме (730 тысяч
		строк) это 540 мс против 40 мс, а снимок читается раз в пять
		секунд, пока открыта страница исполнителя (измерено 18.09.2026).
		"""
		result: dict[ExecutorRef, datetime] = {}
		for column, kind in (
			(AccountOperation.tg_account_id, OwnerKind.USER),
			(AccountOperation.bot_id, OwnerKind.BOT),
		):
			rows = await session.execute(
				select(column, func.max(AccountOperation.finished_at))
				.where(column.is_not(None))
				.group_by(column)
			)
			for owner_id, last in rows:
				if owner_id is not None and last is not None:
					result[ExecutorRef(kind, owner_id)] = as_utc(last)
		return result

	async def history(self, owner: ExecutorRef, now: datetime | None = None) -> ActivityHistoryDto:
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
				row.kind, as_utc(row.started_at), as_utc(row.finished_at), row.outcome, row.wait_s
			)
			for row in rows
		]
		return history_stats(intervals, now, self._tz)
