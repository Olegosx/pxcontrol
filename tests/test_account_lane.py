"""Дорожка аккаунта Telegram (ADR-0024): очередь, зазор, приоритет, заморозка."""

from __future__ import annotations

import asyncio

import pytest

from pxcontrol.engine.telegram.lane import AccountLane, LaneOwner, OwnerKind, TelegramPriority
from pxcontrol.engine.telegram.types import TelegramFloodError


class _Clock:
	"""Управляемое время: «сон» просто двигает стрелки вперёд.

	Реальные паузы сделали бы тесты зазора медленными и шаткими —
	здесь же проверяется сама арифметика ожидания.
	"""

	def __init__(self) -> None:
		self.now = 0.0
		self.slept: list[float] = []

	def __call__(self) -> float:
		return self.now

	async def sleep(self, seconds: float) -> None:
		self.slept.append(seconds)
		self.now += seconds


def _lane(interval: float = 0.0, clock: _Clock | None = None) -> AccountLane:
	"""Дорожка на управляемых часах (без них зазор считать нечем)."""
	ticker = clock or _Clock()
	return AccountLane(LaneOwner(OwnerKind.USER, 1), interval, clock=ticker, sleep=ticker.sleep)


async def test_operations_do_not_overlap() -> None:
	"""Дорожка пускает операции аккаунта по одной."""
	lane = _lane()
	inside = 0
	peak = 0

	async def operation() -> None:
		nonlocal inside, peak
		async with lane.slot(TelegramPriority.BACKGROUND):
			inside += 1
			peak = max(peak, inside)
			await asyncio.sleep(0)  # уступаем цикл: без замка сюда влезли бы соседи
			inside -= 1

	await asyncio.gather(*(operation() for _ in range(5)))
	assert peak == 1


async def test_priority_decides_who_goes_next() -> None:
	"""Пока дорожка занята, ожидающие выстраиваются по важности."""
	lane = _lane()
	order: list[str] = []
	release = asyncio.Event()

	async def hold() -> None:
		async with lane.slot(TelegramPriority.PUBLISH):
			await release.wait()

	async def waiter(name: str, priority: TelegramPriority) -> None:
		async with lane.slot(priority):
			order.append(name)

	holder = asyncio.create_task(hold())
	await asyncio.sleep(0)  # держатель занял дорожку
	tasks = [
		asyncio.create_task(waiter("фон", TelegramPriority.BACKGROUND)),
		asyncio.create_task(waiter("человек", TelegramPriority.INTERACTIVE)),
		asyncio.create_task(waiter("пост", TelegramPriority.PUBLISH)),
	]
	for _ in range(5):
		await asyncio.sleep(0)  # все трое успели встать в очередь
	release.set()
	await asyncio.gather(holder, *tasks)
	assert order == ["пост", "человек", "фон"]


async def test_equal_priority_keeps_arrival_order() -> None:
	"""Внутри одного приоритета порядок — по времени постановки."""
	lane = _lane()
	order: list[int] = []
	release = asyncio.Event()

	async def hold() -> None:
		async with lane.slot(TelegramPriority.PUBLISH):
			await release.wait()

	async def waiter(number: int) -> None:
		async with lane.slot(TelegramPriority.BACKGROUND):
			order.append(number)

	holder = asyncio.create_task(hold())
	await asyncio.sleep(0)
	tasks = []
	for number in range(3):
		tasks.append(asyncio.create_task(waiter(number)))
		await asyncio.sleep(0)
	release.set()
	await asyncio.gather(holder, *tasks)
	assert order == [0, 1, 2]


async def test_interval_between_requests() -> None:
	"""Между запросами подряд выдерживается зазор, первый не ждёт."""
	clock = _Clock()
	lane = _lane(0.3, clock)
	async with lane.slot(TelegramPriority.BACKGROUND):
		pass
	assert clock.slept == []  # первому ждать нечего
	async with lane.slot(TelegramPriority.BACKGROUND):
		pass
	assert clock.slept == [pytest.approx(0.3)]


async def test_interval_counts_from_end_of_operation() -> None:
	"""Зазор отсчитывается от конца обращения, а не от его начала.

	Длинная загрузка — это поток запросов, и её завершение тоже
	обращение к серверу: залп сразу после неё нежелателен, поэтому
	зазор действует и здесь (на фоне минут загрузки он ничего не стоит).
	"""
	clock = _Clock()
	lane = _lane(0.3, clock)
	async with lane.slot(TelegramPriority.PUBLISH):
		clock.now += 5.0  # загрузка большого файла
	async with lane.slot(TelegramPriority.PUBLISH):
		pass
	assert clock.slept == [pytest.approx(0.3)]


async def test_flood_from_operation_freezes_lane() -> None:
	"""Флуд-лимит из операции замораживает дорожку для остальных."""
	clock = _Clock()
	lane = _lane(0.0, clock)
	with pytest.raises(TelegramFloodError):
		async with lane.slot(TelegramPriority.PUBLISH):
			raise TelegramFloodError("Подождите 30 с.", retry_after_s=30)
	assert lane.frozen_for() == pytest.approx(30.0)
	# следующий желающий узнаёт срок, не тревожа Telegram
	entered = False
	with pytest.raises(TelegramFloodError) as info:
		async with lane.slot(TelegramPriority.BACKGROUND):
			entered = True
	assert entered is False
	assert info.value.retry_after_s == 30


async def test_lane_thaws_when_deadline_passes() -> None:
	"""По истечении срока дорожка снова пускает операции."""
	clock = _Clock()
	lane = _lane(0.0, clock)
	lane.freeze(30)
	clock.now += 31
	assert lane.frozen_for() == 0.0
	async with lane.slot(TelegramPriority.BACKGROUND):
		pass  # прошли без отказа


async def test_freeze_never_shortens_running_ban() -> None:
	"""Более короткий срок не сокращает действующую заморозку."""
	clock = _Clock()
	lane = _lane(0.0, clock)
	lane.freeze(60)
	lane.freeze(5)
	assert lane.frozen_for() == pytest.approx(60.0)


async def test_frozen_lane_rejects_before_taking_turn() -> None:
	"""Заморозка, наступившая при ожидании, отменяет и ожидающего.

	Сосед поймал флуд, пока мы стояли в очереди: идти в Telegram
	бессмысленно — настойчивость только удлиняет срок (ADR-0017).
	"""
	clock = _Clock()
	lane = _lane(0.0, clock)
	entered = False
	release = asyncio.Event()

	async def unlucky() -> None:
		async with lane.slot(TelegramPriority.PUBLISH):
			await release.wait()
			raise TelegramFloodError("Подождите 20 с.", retry_after_s=20)

	async def follower() -> None:
		nonlocal entered
		async with lane.slot(TelegramPriority.BACKGROUND):
			entered = True

	first = asyncio.create_task(unlucky())
	await asyncio.sleep(0)
	second = asyncio.create_task(follower())
	await asyncio.sleep(0)
	release.set()
	with pytest.raises(TelegramFloodError):
		await first
	with pytest.raises(TelegramFloodError):
		await second
	assert entered is False


async def test_cancelled_waiter_does_not_stall_lane() -> None:
	"""Отменённый ожидающий не запирает дорожку за собой."""
	lane = _lane()
	release = asyncio.Event()
	passed = False

	async def hold() -> None:
		async with lane.slot(TelegramPriority.PUBLISH):
			await release.wait()

	async def waiter() -> None:
		async with lane.slot(TelegramPriority.BACKGROUND):
			pass

	async def latecomer() -> None:
		nonlocal passed
		async with lane.slot(TelegramPriority.BACKGROUND):
			passed = True

	holder = asyncio.create_task(hold())
	await asyncio.sleep(0)
	doomed = asyncio.create_task(waiter())
	await asyncio.sleep(0)
	last = asyncio.create_task(latecomer())
	await asyncio.sleep(0)
	doomed.cancel()
	release.set()
	await asyncio.gather(holder, last)
	assert passed is True
	assert doomed.cancelled()


async def test_failed_operation_releases_lane() -> None:
	"""Любая ошибка операции отпускает дорожку — очередь не встаёт."""
	lane = _lane()
	with pytest.raises(RuntimeError):
		async with lane.slot(TelegramPriority.PUBLISH):
			raise RuntimeError("сеть отвалилась")
	async with lane.slot(TelegramPriority.BACKGROUND):
		pass  # дорожка свободна


# --- учёт активности (ADR-0030) --------------------------------------------------


async def test_lane_records_operations_with_outcome_and_live_state() -> None:
	"""Каждое выполненное тело — запись: вид, интервал, исход; живое состояние честное."""
	from datetime import UTC, datetime, timedelta

	from pxcontrol.engine.telegram.lane import OperationLog, Outcome

	clock = _Clock()
	first = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
	moments = [first + timedelta(seconds=i) for i in range(10)]
	log = OperationLog()
	lane = AccountLane(
		LaneOwner(OwnerKind.BOT, 7),
		0.0,
		clock=clock,
		sleep=clock.sleep,
		wall_clock=lambda: moments.pop(0),
		log=log,
	)
	assert lane.live_state().busy_kind is None
	async with lane.slot(TelegramPriority.PUBLISH):
		live = lane.live_state()
		assert live.busy_kind is TelegramPriority.PUBLISH and live.busy_since == first
	with pytest.raises(RuntimeError):
		async with lane.slot(TelegramPriority.BACKGROUND):
			raise RuntimeError("сбой")
	with pytest.raises(TelegramFloodError):
		async with lane.slot(TelegramPriority.MAINTENANCE):
			raise TelegramFloodError("подождите", retry_after_s=40)
	records = log.drain()
	assert [(r.kind, r.outcome, r.wait_s) for r in records] == [
		(TelegramPriority.PUBLISH, Outcome.OK, 0),
		(TelegramPriority.BACKGROUND, Outcome.ERROR, 0),
		(TelegramPriority.MAINTENANCE, Outcome.FLOOD, 40),
	]
	assert all(r.owner == LaneOwner(OwnerKind.BOT, 7) for r in records)
	assert records[0].finished_at - records[0].started_at == timedelta(seconds=1)
	# заморозка: отказ до тела записи не даёт — Telegram не тревожили
	with pytest.raises(TelegramFloodError):
		async with lane.slot(TelegramPriority.PUBLISH):
			pass
	assert len(log) == 0
	assert lane.live_state().frozen_for_s == pytest.approx(40.0)
	assert lane.live_state().busy_kind is None


async def test_lane_records_cancelled_operation() -> None:
	"""Обрыв загрузки человеком — исход «отменено», не ошибка."""
	from pxcontrol.engine.telegram.lane import OperationLog, Outcome

	log = OperationLog()
	lane = AccountLane(LaneOwner(OwnerKind.USER, 1), 0.0, log=log)
	started = asyncio.Event()

	async def upload() -> None:
		async with lane.slot(TelegramPriority.PUBLISH):
			started.set()
			await asyncio.sleep(3600)

	task = asyncio.create_task(upload())
	await started.wait()
	assert lane.live_state().busy_kind is TelegramPriority.PUBLISH
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert [r.outcome for r in log.drain()] == [Outcome.CANCELLED]
	assert lane.live_state().busy_kind is None


async def test_live_state_counts_waiting() -> None:
	"""Живое состояние знает, сколько операций ждут очереди."""
	lane = _lane()
	release = asyncio.Event()

	async def hold() -> None:
		async with lane.slot(TelegramPriority.PUBLISH):
			await release.wait()

	async def wait_turn() -> None:
		async with lane.slot(TelegramPriority.BACKGROUND):
			pass

	holder = asyncio.create_task(hold())
	await asyncio.sleep(0)
	waiters = [asyncio.create_task(wait_turn()) for _ in range(2)]
	for _ in range(4):
		await asyncio.sleep(0)
	assert lane.live_state().waiting == 2
	release.set()
	await asyncio.gather(holder, *waiters)
	assert lane.live_state().waiting == 0


async def test_lane_keeps_writing_after_log_drained() -> None:
	"""Дорожка пишет в журнал и после выемки — иначе учёт замолкает навсегда.

	Замок на дефект, из-за которого приложение показывало исполнителям
	ноль операций при круглосуточной работе: журнал был обычным списком,
	выемка подменяла его новым, а дорожки оставались у прежнего — того,
	которого больше никто не читал. Первая выемка делала каждую
	созданную до неё дорожку немой до перезапуска приложения.
	"""
	from pxcontrol.engine.telegram.lane import OperationLog

	log = OperationLog()
	lane = AccountLane(LaneOwner(OwnerKind.USER, 1), 0.0, log=log)
	for _ in range(3):
		async with lane.slot(TelegramPriority.PUBLISH):
			pass
		assert len(log.drain()) == 1, "операция после выемки обязана попасть в журнал"


async def test_operation_log_drains_in_place_and_caps_capacity() -> None:
	"""Выемка опустошает журнал на месте; переполнение вытесняет старьё."""
	from datetime import UTC, datetime

	from pxcontrol.engine.telegram.lane import OperationLog, OperationRecord, Outcome

	def _record(number: int) -> OperationRecord:
		moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
		return OperationRecord(
			LaneOwner(OwnerKind.BOT, number), TelegramPriority.PUBLISH, moment, moment, Outcome.OK
		)

	log = OperationLog(capacity=3)
	for number in range(5):
		log.record(_record(number))
	# вытеснены самые старые, а не самые свежие: свежие ещё нужны
	assert [r.owner.id for r in log.drain()] == [2, 3, 4]
	assert len(log) == 0, "выемка опустошает журнал"
	# возврат неудавшейся пачки: записи встают вперёд свежих
	log.record(_record(9))
	log.restore([_record(7), _record(8)])
	assert [r.owner.id for r in log.drain()] == [7, 8, 9]
