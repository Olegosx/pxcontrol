"""Дорожка аккаунта Telegram (ADR-0024): очередь, зазор, приоритет, заморозка."""

from __future__ import annotations

import asyncio

import pytest

from pxcontrol.engine.telegram.lane import AccountLane, TelegramPriority
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
	return AccountLane(1, interval, clock=ticker, sleep=ticker.sleep)


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
