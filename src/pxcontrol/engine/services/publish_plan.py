"""Раскладка времени постов пакета по расписанию (ADR-0015).

Чистые функции без БД и сети: стратегия + количество постов на входе,
список моментов публикации на выходе. Раскладка — только начальное
заполнение черновика пакета: время каждой строки пользователь потом
правит руками, поэтому существующие отложенные записи канала здесь
сознательно не опрашиваются (сеть, медленно, и всё равно не истина
до момента отправки).

Моменты считаются и возвращаются в местном наивном времени (как ввод
пользователя в форме); в UTC для ``PostDraft.when`` их переводит
вызывающая сторона — тем же правилом, что и одиночная форма.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from pxcontrol.engine.errors import EngineError

#: Первый слот раскладки — не раньше «сейчас + запас»: движок отклоняет
#: время публикации ближе минуты (MIN_SCHEDULE_AHEAD), а пользователь
#: ещё будет править черновик — берём запас с обычной длительностью правки.
PLAN_MARGIN = timedelta(minutes=2)


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
		channel_times: стандартные времена канала «ЧЧ:ММ» для CHANNEL_TIMES.
	"""

	kind: PlanKind
	at: tuple[int, int] | None = None
	every_days: int = 1
	every_hours: int = 3
	start: datetime | None = None
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


def plan_times(plan: SchedulePlan, count: int, now: datetime) -> list[datetime | None]:
	"""Считает моменты публикации для ``count`` постов по стратегии.

	Args:
		plan: стратегия и её параметры.
		count: сколько постов раскладывается.
		now: текущий момент (местное наивное время) — параметром ради
			тестируемости и повторяемости.

	Returns:
		Список длиной ``count``: момент публикации или None («сейчас»).

	Raises:
		PlanError: Параметры стратегии не годятся (нет времени, нулевой
			шаг, стартовый момент в прошлом, пустые времена канала).
	"""
	if count <= 0:
		return []
	if plan.kind is PlanKind.NOW:
		return [None] * count
	if plan.kind is PlanKind.DAILY:
		return list(_daily(plan, count, now))
	if plan.kind is PlanKind.EVERY_HOURS:
		return list(_every_hours(plan, count, now))
	return list(_channel_times(plan, count, now))


def _daily(plan: SchedulePlan, count: int, now: datetime) -> list[datetime]:
	"""Раз в ``every_days`` дней в ``at``; старт — сегодня или завтра."""
	if plan.at is None:
		raise PlanError("Укажите время публикации (ЧЧ:ММ).")
	if plan.every_days < 1:
		raise PlanError("Шаг в днях должен быть хотя бы 1.")
	first = datetime.combine(now.date(), time(*plan.at))
	if first <= now + PLAN_MARGIN:
		# сегодняшнее время уже прошло — начинаем с завтрашнего дня,
		# а не через полный шаг: первый пост не должен ждать N дней
		first += timedelta(days=1)
	step = timedelta(days=plan.every_days)
	return [first + step * index for index in range(count)]


def _every_hours(plan: SchedulePlan, count: int, now: datetime) -> list[datetime]:
	"""Каждые ``every_hours`` часов от стартового момента ``start``."""
	if plan.start is None:
		raise PlanError("Укажите стартовый момент раскладки.")
	if plan.every_hours < 1:
		raise PlanError("Шаг в часах должен быть хотя бы 1.")
	if plan.start <= now + PLAN_MARGIN:
		raise PlanError("Стартовый момент уже прошёл — укажите время в будущем.")
	step = timedelta(hours=plan.every_hours)
	return [plan.start + step * index for index in range(count)]


def _channel_times(plan: SchedulePlan, count: int, now: datetime) -> list[datetime]:
	"""По стандартным временам канала вперёд по дням.

	Битые элементы списка пропускаются (как в форме одиночной
	публикации); слоты идут по дням начиная с сегодняшнего, прошедшие
	отбрасываются.
	"""
	slots = [parsed for item in plan.channel_times if (parsed := _try_hhmm(str(item))) is not None]
	if not slots:
		raise PlanError(
			"У канала нет стандартных времён публикации — задайте их "
			"на странице «Каналы» или выберите другую стратегию."
		)
	slots = sorted(set(slots))
	moments: list[datetime] = []
	day = now.date()
	while len(moments) < count:
		for hours, minutes in slots:
			moment = datetime.combine(day, time(hours, minutes))
			if moment > now + PLAN_MARGIN:
				moments.append(moment)
				if len(moments) == count:
					break
		day += timedelta(days=1)
	return moments
