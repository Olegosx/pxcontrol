"""Тесты расчётов «Обзора» по локальным снимкам и сборки снимка (без БД)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pxcontrol.engine.services.community_overview import (
	HistorySample,
	SeriesSource,
	build_overview,
	daily_last,
	delta_over,
	flow_from_samples,
	hourly_online,
	median,
)
from pxcontrol.engine.telegram.types import CommunityAnalytics, DayPoint

_TODAY = date(2026, 9, 14)


def _at(day: int, hour: int = 12) -> datetime:
	return datetime(2026, 9, day, hour, tzinfo=UTC)


def _samples() -> list[HistorySample]:
	return [
		HistorySample(_at(12, 9), 100, 10),
		HistorySample(_at(12, 21), 103, 30),  # +3 за 12-е
		HistorySample(_at(13, 9), 101, 12),  # −2 за 13-е
		HistorySample(_at(13, 21), 106, 40),  # +5 за 13-е
		HistorySample(_at(14, 9), 106, None),  # без онлайна
	]


def test_daily_last_takes_last_sample_of_each_day() -> None:
	points = daily_last(_samples(), 30, _TODAY, UTC)
	assert points == (
		DayPoint(date(2026, 9, 12), 103),
		DayPoint(date(2026, 9, 13), 106),
		DayPoint(date(2026, 9, 14), 106),
	)
	assert daily_last(_samples(), 1, _TODAY, UTC) == (DayPoint(_TODAY, 106),)
	assert daily_last([], 30, _TODAY, UTC) == ()


def test_flow_from_samples_sums_positive_and_negative_deltas() -> None:
	joined, left = flow_from_samples(_samples(), 14, _TODAY, UTC)
	assert joined == (DayPoint(date(2026, 9, 12), 3), DayPoint(date(2026, 9, 13), 5))
	assert left == (DayPoint(date(2026, 9, 12), 0), DayPoint(date(2026, 9, 13), 2))


def test_hourly_online_averages_by_local_hour() -> None:
	profile = hourly_online(_samples(), 7, _TODAY, UTC)
	assert profile is not None and len(profile) == 24
	assert profile[9] == 11  # (10 + 12) / 2
	assert profile[21] == 35  # (30 + 40) / 2
	assert profile[0] == 0
	assert hourly_online([HistorySample(_at(14), 5, None)], 7, _TODAY, UTC) is None


def test_delta_over_and_median() -> None:
	assert delta_over(_samples(), 7, _TODAY, UTC) == 6
	assert delta_over(_samples()[:1], 7, _TODAY, UTC) is None
	assert median([]) is None
	assert median([5, 1, 3]) == 3
	assert median([4, 1, 3, 2]) == 3  # (2 + 3) / 2 = 2,5 → половина вверх
	assert median([10]) == 10


def test_build_overview_prefers_telegram_analytics() -> None:
	analytics = CommunityAnalytics(
		period_from=_TODAY - timedelta(days=6),
		period_to=_TODAY,
		members=(2104, 2066),
		growth=tuple(
			DayPoint(_TODAY - timedelta(days=offset), 2000 + offset) for offset in range(40, -1, -1)
		),
		joined=(DayPoint(_TODAY - timedelta(days=1), 40), DayPoint(_TODAY, 21)),
		left=(DayPoint(_TODAY - timedelta(days=1), 20), DayPoint(_TODAY, 3)),
		hours=None,
		views_per_post=(1500, 1200),
		recent_post_views=(100, 300, 200),
	)
	dto = build_overview(
		1,
		participants=2104,
		online=96,
		samples=_samples(),
		analytics=analytics,
		today=_TODAY,
		tz=UTC,
		can_view_stats=True,
	)
	assert dto.source is SeriesSource.TELEGRAM
	assert dto.participants_delta == 38
	assert dto.joined == 61 and dto.left == 23
	assert dto.views_per_post == 200
	assert len(dto.growth) == 30  # хвост ряда за 30 дней
	assert dto.hours is not None and dto.hours_online  # часов у Telegram нет — онлайн из снимков


def test_build_overview_falls_back_to_snapshots_and_none() -> None:
	dto = build_overview(
		1, participants=106, online=None, samples=_samples(), analytics=None, today=_TODAY, tz=UTC
	)
	assert dto.source is SeriesSource.SNAPSHOTS
	assert dto.participants_delta == 6
	assert dto.joined == 8 and dto.left == 2
	assert dto.views_per_post is None
	assert dto.hours_online
	empty = build_overview(
		1, participants=None, online=None, samples=[], analytics=None, today=_TODAY, tz=UTC
	)
	assert empty.source is SeriesSource.NONE
	assert empty.growth == () and empty.hours is None and empty.joined is None
