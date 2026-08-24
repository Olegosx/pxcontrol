"""Раскладка времени постов пакета по расписанию (ADR-0015).

Чистые функции без БД и сети: стратегия + количество постов на входе,
список моментов публикации на выходе. Раскладка — только начальное
заполнение черновика пакета: время каждой строки пользователь потом
правит руками. Существующие отложки канала передаются вызывающей
стороной готовым списком ``busy`` (их читает движок, а не этот модуль):
занятый слот — совпадение с точностью до минуты — пропускается.

Моменты считаются и возвращаются в местном наивном времени (как ввод
пользователя в форме); в UTC для ``PostDraft.when`` их переводит
вызывающая сторона — тем же правилом, что и одиночная форма.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from pxcontrol.engine.errors import EngineError

#: Первый слот раскладки — не раньше «сейчас + запас»: движок отклоняет
#: время публикации ближе минуты (MIN_SCHEDULE_AHEAD), а пользователь
#: ещё будет править черновик — берём запас с обычной длительностью правки.
PLAN_MARGIN = timedelta(minutes=2)

#: Предохранитель перебора слотов: если свободных не нашлось за столько
#: кандидатов подряд, раскладка честно сдаётся (всё занято или параметры
#: вырожденные), а не крутится вечно.
_MAX_CANDIDATES = 10_000


class PlanError(EngineError):
	"""Ошибка параметров раскладки (с понятным человеку текстом)."""


class PlanKind(StrEnum):
	"""Стратегия раскладки времени постов пакета."""

	NOW = "now"  # все посты — «сейчас», без отложки
	DAILY = "daily"  # раз в N дней в ЧЧ:ММ (N=1 — каждый день)
	EVERY_HOURS = "every-hours"  # каждые N часов от стартового момента
	CHANNEL_TIMES = "channel-times"  # по стандартным временам канала


@dataclass(frozen=True)
class SchedulePlan:
	"""Параметры раскладки; какие поля обязательны — зависит от стратегии.

	Attributes:
		kind: стратегия.
		at: время «ЧЧ:ММ» для DAILY (часы, минуты).
		every_days: шаг в днях для DAILY (1 — каждый день).
		every_hours: шаг в часах для EVERY_HOURS.
		start: стартовый момент для EVERY_HOURS (местное время).
		start_date: дата начала для DAILY и CHANNEL_TIMES; None или
			прошедшая дата равнозначны «с сегодняшнего дня».
		channel_times: стандартные времена канала «ЧЧ:ММ» для CHANNEL_TIMES.
	"""

	kind: PlanKind
	at: tuple[int, int] | None = None
	every_days: int = 1
	every_hours: int = 3
	start: datetime | None = None
	start_date: date | None = None
	channel_times: tuple[str, ...] = ()


def _try_hhmm(text: str) -> tuple[int, int] | None:
	"""Щадящий разбор «ЧЧ:ММ»: None вместо ошибки (битый элемент — мимо).

	Строгий разбор пользовательского ввода — забота интерфейса
	(``parse_hhmm`` в общих помощниках страниц); здесь разбираются
	значения настройки канала, где битый элемент просто пропускается —
	как в форме одиночной публикации.
	"""
	parts = text.strip().split(":")
	if len(parts) != 2 or not all(part.isdigit() for part in parts):
		return None
	hours, minutes = int(parts[0]), int(parts[1])
	if hours > 23 or minutes > 59:
		return None
	return hours, minutes


def _busy_keys(busy: Collection[datetime]) -> set[datetime]:
	"""Занятые моменты с точностью до минуты (секунды Telegram не важны)."""
	return {moment.replace(second=0, microsecond=0) for moment in busy}


def _take_free(candidates: Iterator[datetime], count: int, busy: set[datetime]) -> list[datetime]:
	"""Первые ``count`` свободных кандидатов (занятая минута — мимо).

	Raises:
		PlanError: Свободные слоты не нашлись за разумный перебор.
	"""
	moments: list[datetime] = []
	for index, candidate in enumerate(candidates):
		if index >= _MAX_CANDIDATES:
			break
		if candidate.replace(second=0, microsecond=0) in busy:
			continue
		moments.append(candidate)
		if len(moments) == count:
			return moments
	raise PlanError("Свободных слотов не нашлось — проверьте параметры раскладки.")


def _start_day(plan: SchedulePlan, now: datetime) -> date:
	"""Первый день раскладки: заданная дата, но не раньше сегодняшней."""
	if plan.start_date is None or plan.start_date <= now.date():
		return now.date()
	return plan.start_date


def plan_times(
	plan: SchedulePlan,
	count: int,
	now: datetime,
	busy: Collection[datetime] = (),
) -> list[datetime | None]:
	"""Считает моменты публикации для ``count`` постов по стратегии.

	Args:
		plan: стратегия и её параметры.
		count: сколько постов раскладывается.
		now: текущий момент (местное наивное время) — параметром ради
			тестируемости и повторяемости.
		busy: занятые моменты (существующие отложки канала, местное
			наивное время): совпавший по минуте слот пропускается,
			пост уходит на следующий свободный.

	Returns:
		Список длиной ``count``: момент публикации или None («сейчас»).

	Raises:
		PlanError: Параметры стратегии не годятся (нет времени, нулевой
			шаг, стартовый момент в прошлом, пустые времена канала)
			или свободных слотов не нашлось.
	"""
	if count <= 0:
		return []
	if plan.kind is PlanKind.NOW:
		return [None] * count
	keys = _busy_keys(busy)
	if plan.kind is PlanKind.DAILY:
		return list(_take_free(_daily(plan, now), count, keys))
	if plan.kind is PlanKind.EVERY_HOURS:
		return list(_take_free(_every_hours(plan, now), count, keys))
	return list(_take_free(_channel_times(plan, now), count, keys))


def _daily(plan: SchedulePlan, now: datetime) -> Iterator[datetime]:
	"""Кандидаты «раз в ``every_days`` дней в ``at``» от даты начала.

	Дата начала не задана (или прошла) — старт сегодня; сегодняшнее
	время уже прошло — с завтрашнего дня, а не через полный шаг:
	первый пост не должен ждать N дней.
	"""
	if plan.at is None:
		raise PlanError("Укажите время публикации (ЧЧ:ММ).")
	if plan.every_days < 1:
		raise PlanError("Шаг в днях должен быть хотя бы 1.")
	slot = datetime.combine(_start_day(plan, now), time(*plan.at))
	if slot <= now + PLAN_MARGIN:
		slot += timedelta(days=1)
	step = timedelta(days=plan.every_days)
	while True:
		yield slot
		slot += step


def _every_hours(plan: SchedulePlan, now: datetime) -> Iterator[datetime]:
	"""Кандидаты «каждые ``every_hours`` часов» от стартового момента."""
	if plan.start is None:
		raise PlanError("Укажите стартовый момент раскладки.")
	if plan.every_hours < 1:
		raise PlanError("Шаг в часах должен быть хотя бы 1.")
	if plan.start <= now + PLAN_MARGIN:
		raise PlanError("Стартовый момент уже прошёл — укажите время в будущем.")
	slot = plan.start
	step = timedelta(hours=plan.every_hours)
	while True:
		yield slot
		slot += step


def _channel_times(plan: SchedulePlan, now: datetime) -> Iterator[datetime]:
	"""Кандидаты по стандартным временам канала вперёд по дням.

	Битые элементы списка пропускаются (как в форме одиночной
	публикации); слоты идут по дням начиная с даты начала (не раньше
	сегодняшней), прошедшие отбрасываются.
	"""
	slots = [parsed for item in plan.channel_times if (parsed := _try_hhmm(str(item))) is not None]
	if not slots:
		raise PlanError(
			"У канала нет стандартных времён публикации — задайте их "
			"на странице «Каналы» или выберите другую стратегию."
		)
	slots = sorted(set(slots))
	day = _start_day(plan, now)
	while True:
		for hours, minutes in slots:
			moment = datetime.combine(day, time(hours, minutes))
			if moment > now + PLAN_MARGIN:
				yield moment
		day += timedelta(days=1)
