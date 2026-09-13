"""Снимок «Обзора» сообщества: числа, ряды, справка (ADR-0027).

Вкладка «Обзор» получает от движка один готовый снимок и не знает, откуда
взят каждый ряд: из встроенной статистики Telegram (точной, считается
сервером) или из локальных снимков опроса (оценка за время работы
приложения). Источник назван в снимке — интерфейс честно подписывает его.

Расчёты по локальным снимкам — чистые функции над списком точек
«момент → участники, онлайн»: они тестируются без БД и без сети.
Приходы и уходы из снимков — **оценка снизу**: положительные приросты
числа участников между соседними снимками суммируются в «пришли»,
отрицательные — в «ушли»; внутри одного интервала приход и уход
гасят друг друга, и точный счёт даёт только Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum

from pxcontrol.engine.telegram.types import CommunityAnalytics, DayPoint

#: Горизонты рядов вкладки (дни): участники, приходы/уходы, часы, «за период».
GROWTH_DAYS = 30
FLOW_DAYS = 14
HOURS_DAYS = 7
PERIOD_DAYS = 7


class SeriesSource(StrEnum):
	"""Откуда взяты ряды снимка — подпись для человека."""

	TELEGRAM = "telegram"  # встроенная статистика Telegram (точная)
	SNAPSHOTS = "snapshots"  # локальные снимки опроса (оценка)
	NONE = "none"  # данных нет


@dataclass(frozen=True)
class HistorySample:
	"""Точка локальной истории: момент опроса, участники, онлайн."""

	at: datetime
	participants: int | None
	online: int | None


@dataclass(frozen=True)
class CommunityOverviewDto:
	"""Снимок «Обзора» для интерфейса.

	Всё, что интерфейс не может взять из :class:`CommunityDto`
	(тот описывает само сообщество: вид, публикатор, @имя).
	None — данных нет; ряд пустой — графика нет.

	Attributes:
		community_id: сообщество.
		participants: участники сейчас.
		participants_delta: изменение за ``PERIOD_DAYS`` дней.
		online: онлайн сейчас (только группы через userbot).
		joined: пришедшие за период; left: ушедшие за период.
		views_per_post: медиана просмотров последних постов (каналы).
		deleted_found / deleted_removed / deleted_checked_at: итог
			последнего прохода обслуживания по удалённым аккаунтам.
		growth: участники по дням за ``GROWTH_DAYS``.
		flow_joined / flow_left: приходы и уходы по дням за ``FLOW_DAYS``.
		hours: профиль по часам суток (24 значения) и его смысл:
			``hours_online`` True — средний онлайн из снимков, False —
			активность из статистики Telegram.
		source: откуда ряды (Telegram / снимки / нет).
		can_view_stats: доступна ли аккаунту статистика Telegram.
		linked_chat_id / linked_title: связанное сообщество (формат
			Bot API; название — если оно подключено в приложении).
		tg_created_at / last_post_at: создано, последний пост.
		fetched_at: когда кэш обновлялся последний раз.
	"""

	community_id: int
	participants: int | None = None
	participants_delta: int | None = None
	online: int | None = None
	joined: int | None = None
	left: int | None = None
	views_per_post: int | None = None
	deleted_found: int | None = None
	deleted_removed: int | None = None
	deleted_checked_at: datetime | None = None
	growth: tuple[DayPoint, ...] = ()
	flow_joined: tuple[DayPoint, ...] = ()
	flow_left: tuple[DayPoint, ...] = ()
	hours: tuple[int, ...] | None = None
	hours_online: bool = False
	source: SeriesSource = SeriesSource.NONE
	can_view_stats: bool = False
	linked_chat_id: str | None = None
	linked_title: str | None = None
	tg_created_at: datetime | None = None
	last_post_at: datetime | None = None
	fetched_at: datetime | None = None


# --- расчёты по локальным снимкам (чистые функции) -------------------------------


def _local_day(moment: datetime, tz: tzinfo) -> date:
	return moment.astimezone(tz).date()


def daily_last(
	samples: list[HistorySample], days: int, today: date, tz: tzinfo
) -> tuple[DayPoint, ...]:
	"""Участники по дням: последний снимок каждого дня за ``days`` дней.

	Дни без снимков пропускаются (график рисует то, что есть);
	снимки без числа участников не считаются.
	"""
	start = today - timedelta(days=days - 1)
	last: dict[date, int] = {}
	for sample in sorted(samples, key=lambda s: s.at):
		if sample.participants is None:
			continue
		day = _local_day(sample.at, tz)
		if start <= day <= today:
			last[day] = sample.participants
	return tuple(DayPoint(day, value) for day, value in sorted(last.items()))


def flow_from_samples(
	samples: list[HistorySample], days: int, today: date, tz: tzinfo
) -> tuple[tuple[DayPoint, ...], tuple[DayPoint, ...]]:
	"""Оценка приходов и уходов по дням из разностей соседних снимков.

	Прирост между соседними снимками идёт в «пришли» дня, к которому
	относится поздний снимок, убыль — в «ушли». Оценка снизу: приход
	и уход внутри одного интервала взаимно гасятся.
	"""
	start = today - timedelta(days=days - 1)
	joined: dict[date, int] = {}
	left: dict[date, int] = {}
	previous: int | None = None
	for sample in sorted(samples, key=lambda s: s.at):
		if sample.participants is None:
			continue
		day = _local_day(sample.at, tz)
		if previous is not None and start <= day <= today:
			delta = sample.participants - previous
			if delta > 0:
				joined[day] = joined.get(day, 0) + delta
			elif delta < 0:
				left[day] = left.get(day, 0) - delta
		previous = sample.participants
	days_seen = sorted(set(joined) | set(left))
	return (
		tuple(DayPoint(day, joined.get(day, 0)) for day in days_seen),
		tuple(DayPoint(day, left.get(day, 0)) for day in days_seen),
	)


def hourly_online(
	samples: list[HistorySample], days: int, today: date, tz: tzinfo
) -> tuple[int, ...] | None:
	"""Средний онлайн по часам суток за ``days`` дней; None — онлайна нет.

	Час берётся в местном времени: профиль суток нужен человеку, а не UTC.
	Час без снимков — 0: полотно графика ждёт ровно 24 значения.
	"""
	start = today - timedelta(days=days - 1)
	sums = [0] * 24
	counts = [0] * 24
	for sample in samples:
		if sample.online is None:
			continue
		local = sample.at.astimezone(tz)
		if start <= local.date() <= today:
			sums[local.hour] += sample.online
			counts[local.hour] += 1
	if not any(counts):
		return None
	return tuple(round(s / c) if c else 0 for s, c in zip(sums, counts, strict=True))


def delta_over(samples: list[HistorySample], days: int, today: date, tz: tzinfo) -> int | None:
	"""Изменение числа участников за ``days`` дней: последний минус первый в окне."""
	window = [
		s
		for s in sorted(samples, key=lambda s: s.at)
		if s.participants is not None
		and today - timedelta(days=days - 1) <= _local_day(s.at, tz) <= today
	]
	if len(window) < 2:
		return None
	first, last = window[0].participants, window[-1].participants
	if first is None or last is None:
		return None
	return last - first


def median(values: tuple[int, ...] | list[int]) -> int | None:
	"""Медиана целых (для чётного числа — среднее двух средних); None — пусто."""
	if not values:
		return None
	ordered = sorted(values)
	middle = len(ordered) // 2
	if len(ordered) % 2:
		return ordered[middle]
	# половина округляется вверх (а не к чётному, как round): показатель
	# для человека, «2,5 просмотра» здесь честнее назвать тройкой
	return (ordered[middle - 1] + ordered[middle] + 1) // 2


def _sum_last(points: tuple[DayPoint, ...], days: int, today: date) -> int | None:
	"""Сумма ряда за последние ``days`` дней; None — ряд пуст."""
	if not points:
		return None
	start = today - timedelta(days=days - 1)
	return sum(point.value for point in points if start <= point.day <= today)


def _tail(points: tuple[DayPoint, ...], days: int, today: date) -> tuple[DayPoint, ...]:
	"""Последние ``days`` дней ряда (по дате точки)."""
	start = today - timedelta(days=days - 1)
	return tuple(point for point in points if start <= point.day <= today)


def build_overview(
	community_id: int,
	*,
	participants: int | None,
	online: int | None,
	samples: list[HistorySample],
	analytics: CommunityAnalytics | None,
	today: date,
	tz: tzinfo,
	can_view_stats: bool = False,
	linked_chat_id: str | None = None,
	linked_title: str | None = None,
	tg_created_at: datetime | None = None,
	last_post_at: datetime | None = None,
	fetched_at: datetime | None = None,
	deleted: tuple[int | None, int | None, datetime | None] = (None, None, None),
) -> CommunityOverviewDto:
	"""Собирает снимок «Обзора»: Telegram — приоритетный источник рядов.

	Есть статистика Telegram — ряды из неё (точные), из снимков берётся
	только онлайн по часам, которого в статистике нет. Нет — всё
	из снимков как оценка; нет и снимков — ряды пустые, источник «нет».
	"""
	found, removed, checked_at = deleted
	base = {
		"community_id": community_id,
		"participants": participants,
		"online": online,
		"deleted_found": found,
		"deleted_removed": removed,
		"deleted_checked_at": checked_at,
		"can_view_stats": can_view_stats,
		"linked_chat_id": linked_chat_id,
		"linked_title": linked_title,
		"tg_created_at": tg_created_at,
		"last_post_at": last_post_at,
		"fetched_at": fetched_at,
	}
	online_hours = hourly_online(samples, HOURS_DAYS, today, tz)
	if analytics is not None:
		growth = _tail(analytics.growth, GROWTH_DAYS, today)
		delta = None
		if analytics.members is not None:
			delta = analytics.members[0] - analytics.members[1]
		elif len(growth) >= 2:
			delta = growth[-1].value - growth[0].value
		hours = analytics.hours if analytics.hours is not None else online_hours
		return CommunityOverviewDto(
			participants_delta=delta,
			joined=_sum_last(analytics.joined, PERIOD_DAYS, today),
			left=_sum_last(analytics.left, PERIOD_DAYS, today),
			views_per_post=median(analytics.recent_post_views),
			growth=growth,
			flow_joined=_tail(analytics.joined, FLOW_DAYS, today),
			flow_left=_tail(analytics.left, FLOW_DAYS, today),
			hours=hours,
			hours_online=analytics.hours is None and online_hours is not None,
			source=SeriesSource.TELEGRAM,
			**base,  # type: ignore[arg-type]  # ключи совпадают с полями
		)
	growth = daily_last(samples, GROWTH_DAYS, today, tz)
	joined_points, left_points = flow_from_samples(samples, FLOW_DAYS, today, tz)
	has_data = bool(growth) or online_hours is not None
	return CommunityOverviewDto(
		participants_delta=delta_over(samples, PERIOD_DAYS, today, tz),
		joined=_sum_last(joined_points, PERIOD_DAYS, today),
		left=_sum_last(left_points, PERIOD_DAYS, today),
		growth=growth,
		flow_joined=joined_points,
		flow_left=left_points,
		hours=online_hours,
		hours_online=online_hours is not None,
		source=SeriesSource.SNAPSHOTS if has_data else SeriesSource.NONE,
		**base,  # type: ignore[arg-type]  # ключи совпадают с полями
	)
