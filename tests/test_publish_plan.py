"""Тесты раскладки времени пакета отправки (ADR-0015, чистые функции)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from pxcontrol.engine.services.publish_plan import (
	PlanError,
	PlanKind,
	SchedulePlan,
	plan_times,
)

#: Опорный момент «сейчас»: 15.08.2026, 10:00 местного времени.
NOW = datetime(2026, 8, 15, 10, 0)


def test_now_strategy_and_empty_count() -> None:
	"""«Сейчас» — None на каждый пост; нулевое количество — пустой список."""
	plan = SchedulePlan(PlanKind.NOW)
	assert plan_times(plan, 3, NOW) == [None, None, None]
	assert plan_times(plan, 0, NOW) == []


def test_daily_starts_today_if_time_ahead() -> None:
	"""Время ещё не прошло — первый пост сегодня, дальше по дню на пост."""
	plan = SchedulePlan(PlanKind.DAILY, at=(18, 30))
	assert plan_times(plan, 3, NOW) == [
		datetime(2026, 8, 15, 18, 30),
		datetime(2026, 8, 16, 18, 30),
		datetime(2026, 8, 17, 18, 30),
	]


def test_daily_starts_tomorrow_if_time_passed_or_too_close() -> None:
	"""Прошедшее (или впритык к «сейчас») время — старт завтра, не через шаг."""
	passed = SchedulePlan(PlanKind.DAILY, at=(9, 0), every_days=5)
	assert plan_times(passed, 1, NOW)[0] == datetime(2026, 8, 16, 9, 0)
	# 10:01 — внутри двухминутного запаса от 10:00
	too_close = SchedulePlan(PlanKind.DAILY, at=(10, 1))
	assert plan_times(too_close, 1, NOW)[0] == datetime(2026, 8, 16, 10, 1)


def test_daily_step_in_days() -> None:
	"""«Раз в N дней» шагает на N суток между постами."""
	plan = SchedulePlan(PlanKind.DAILY, at=(12, 0), every_days=3)
	assert plan_times(plan, 3, NOW) == [
		datetime(2026, 8, 15, 12, 0),
		datetime(2026, 8, 18, 12, 0),
		datetime(2026, 8, 21, 12, 0),
	]


def test_daily_validation() -> None:
	"""Понятные ошибки: нет времени, нулевой шаг."""
	with pytest.raises(PlanError, match="время"):
		plan_times(SchedulePlan(PlanKind.DAILY), 1, NOW)
	with pytest.raises(PlanError, match="Шаг в днях"):
		plan_times(SchedulePlan(PlanKind.DAILY, at=(12, 0), every_days=0), 1, NOW)


def test_every_hours() -> None:
	"""Каждые N часов от стартового момента."""
	plan = SchedulePlan(PlanKind.EVERY_HOURS, start=datetime(2026, 8, 15, 11, 0), every_hours=4)
	assert plan_times(plan, 3, NOW) == [
		datetime(2026, 8, 15, 11, 0),
		datetime(2026, 8, 15, 15, 0),
		datetime(2026, 8, 15, 19, 0),
	]


def test_every_hours_validation() -> None:
	"""Понятные ошибки: нет старта, старт в прошлом, нулевой шаг."""
	with pytest.raises(PlanError, match="стартовый момент"):
		plan_times(SchedulePlan(PlanKind.EVERY_HOURS), 1, NOW)
	past = SchedulePlan(PlanKind.EVERY_HOURS, start=datetime(2026, 8, 15, 9, 0))
	with pytest.raises(PlanError, match="уже прошёл"):
		plan_times(past, 1, NOW)
	zero = SchedulePlan(PlanKind.EVERY_HOURS, start=datetime(2026, 8, 15, 11, 0), every_hours=0)
	with pytest.raises(PlanError, match="Шаг в часах"):
		plan_times(zero, 1, NOW)


def test_channel_times_skips_passed_and_rolls_days() -> None:
	"""Слоты — по временам канала вперёд по дням, прошедшие — мимо."""
	plan = SchedulePlan(PlanKind.CHANNEL_TIMES, channel_times=("18:00", "09:00", "12:00"))
	# 09:00 сегодня прошло; порядок слотов — по времени, не по списку
	assert plan_times(plan, 4, NOW) == [
		datetime(2026, 8, 15, 12, 0),
		datetime(2026, 8, 15, 18, 0),
		datetime(2026, 8, 16, 9, 0),
		datetime(2026, 8, 16, 12, 0),
	]


def test_channel_times_ignores_broken_and_duplicate_items() -> None:
	"""Битые элементы настройки пропускаются, дубликаты не удваивают слот."""
	plan = SchedulePlan(
		PlanKind.CHANNEL_TIMES,
		channel_times=("12:00", "мусор", "25:99", "12:00"),
	)
	assert plan_times(plan, 2, NOW) == [
		datetime(2026, 8, 15, 12, 0),
		datetime(2026, 8, 16, 12, 0),
	]


def test_channel_times_requires_valid_times() -> None:
	"""Пустой (или целиком битый) список времён — понятная ошибка."""
	with pytest.raises(PlanError, match="стандартных времён"):
		plan_times(SchedulePlan(PlanKind.CHANNEL_TIMES), 1, NOW)
	broken = SchedulePlan(PlanKind.CHANNEL_TIMES, channel_times=("мусор",))
	with pytest.raises(PlanError, match="стандартных времён"):
		plan_times(broken, 1, NOW)


def test_start_date_for_daily_and_channel_times() -> None:
	"""Будущая дата начала соблюдается; прошедшая равнозначна сегодняшней."""
	future = SchedulePlan(PlanKind.DAILY, at=(9, 0), start_date=date(2026, 8, 20))
	assert plan_times(future, 2, NOW) == [
		datetime(2026, 8, 20, 9, 0),
		datetime(2026, 8, 21, 9, 0),
	]
	past = SchedulePlan(PlanKind.DAILY, at=(18, 0), start_date=date(2026, 8, 1))
	assert plan_times(past, 1, NOW)[0] == datetime(2026, 8, 15, 18, 0)
	times = SchedulePlan(
		PlanKind.CHANNEL_TIMES, channel_times=("12:00",), start_date=date(2026, 8, 18)
	)
	assert plan_times(times, 2, NOW) == [
		datetime(2026, 8, 18, 12, 0),
		datetime(2026, 8, 19, 12, 0),
	]


def test_busy_slots_are_skipped() -> None:
	"""Занятый слот (совпадение по минуте) пропускается — пост идёт дальше."""
	daily = SchedulePlan(PlanKind.DAILY, at=(12, 0))
	busy = [datetime(2026, 8, 16, 12, 0, 30)]  # секунды не важны
	assert plan_times(daily, 2, NOW, busy=busy) == [
		datetime(2026, 8, 15, 12, 0),
		datetime(2026, 8, 17, 12, 0),
	]
	times = SchedulePlan(PlanKind.CHANNEL_TIMES, channel_times=("12:00", "18:00"))
	assert plan_times(times, 2, NOW, busy=[datetime(2026, 8, 15, 18, 0)]) == [
		datetime(2026, 8, 15, 12, 0),
		datetime(2026, 8, 16, 12, 0),
	]
	hourly = SchedulePlan(PlanKind.EVERY_HOURS, start=datetime(2026, 8, 15, 11, 0), every_hours=1)
	assert plan_times(hourly, 3, NOW, busy=[datetime(2026, 8, 15, 12, 0)]) == [
		datetime(2026, 8, 15, 11, 0),
		datetime(2026, 8, 15, 13, 0),
		datetime(2026, 8, 15, 14, 0),
	]
