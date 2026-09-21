"""Расписание задачи сообщества: значение и расчёт следующего запуска (ADR-0038).

Три вида: **только по требованию** (расписания нет), **интервал** —
каждые N минут либо случайно в промежутке от min до max минут
(пауза между проходами задачи реакций: «от 35 до 96 минут»),
и **в моменты суток** — список «ЧЧ:ММ» по местному времени (ночная
уборка в 04:00).

Следующий момент считает чистая функция :func:`next_run` — и её ответ
**хранится** в строке задачи, а не считается заново на каждом тике:
случайный интервал вытягивается один раз после окончания запуска,
человек видит «следующий запуск в 14:37», а перезапуск приложения
ничего не сдвигает.

Модуль чистый: ни сети, ни базы, ни моделей.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any

from pxcontrol.engine.services.schedule_plan import parse_hhmm, try_hhmm
from pxcontrol.engine.tasks.model import TaskError, as_int, check_range


class ScheduleKind(StrEnum):
	"""Вид расписания — значение ключа ``kind`` в JSON задачи."""

	NONE = "none"  # только по требованию
	INTERVAL = "interval"  # каждые N минут или случайно в промежутке
	DAILY = "daily"  # в названные моменты суток


#: Пределы интервала, минуты: от минуты до недели. Меньше минуты —
#: чаще тика планировщика, смысла нет; больше недели — это уже
#: «в моменты суток» по календарю, которого у задач нет намеренно.
INTERVAL_MINUTES_RANGE = (1, 7 * 24 * 60)

#: Умолчания формы интервала: пауза задачи реакций из требований
#: владельца (ADR-0039) — она же разумный старт для любого вида.
DEFAULT_INTERVAL_MINUTES = (35, 96)


@dataclass(frozen=True)
class Schedule:
	"""Расписание задачи.

	Attributes:
		kind: вид.
		min_minutes: нижняя граница интервала, минуты (только ``INTERVAL``).
		max_minutes: верхняя граница интервала; равна нижней —
			интервал постоянный.
		times: моменты суток «ЧЧ:ММ» по местному времени (только ``DAILY``).
	"""

	kind: ScheduleKind = ScheduleKind.NONE
	min_minutes: int = DEFAULT_INTERVAL_MINUTES[0]
	max_minutes: int = DEFAULT_INTERVAL_MINUTES[1]
	times: tuple[str, ...] = ()

	def validate(self) -> None:
		"""Проверяет границы и формат.

		Raises:
			TaskError: Интервал вне пределов или перевёрнут; список
				моментов пуст или содержит не «ЧЧ:ММ».
		"""
		if self.kind is ScheduleKind.INTERVAL:
			check_range("Интервал, минуты", self.min_minutes, INTERVAL_MINUTES_RANGE)
			check_range("Интервал, минуты", self.max_minutes, INTERVAL_MINUTES_RANGE)
			if self.max_minutes < self.min_minutes:
				raise TaskError("Интервал: верхняя граница меньше нижней.")
		elif self.kind is ScheduleKind.DAILY:
			if not self.times:
				raise TaskError("Укажите хотя бы один момент суток в формате ЧЧ:ММ.")
			for text in self.times:
				try:
					parse_hhmm(text)
				except ValueError as exc:
					raise TaskError(str(exc)) from exc

	def to_payload(self) -> dict[str, Any]:
		"""Расписание в колонку JSON."""
		payload: dict[str, Any] = {"kind": str(self.kind)}
		if self.kind is ScheduleKind.INTERVAL:
			payload["min_minutes"] = self.min_minutes
			payload["max_minutes"] = self.max_minutes
		elif self.kind is ScheduleKind.DAILY:
			payload["times"] = list(self.times)
		return payload

	@classmethod
	def from_payload(cls, payload: Any) -> Schedule:
		"""Расписание из колонки JSON; незнакомое и битое — к умолчаниям.

		Битая запись не должна ронять чтение задачи: незнакомый вид
		читается как «только по требованию», негодные моменты
		отбрасываются — правило то же, что у параметров вида.
		"""
		if not isinstance(payload, dict):
			return cls()
		raw_kind = payload.get("kind")
		kind = (
			ScheduleKind(raw_kind)
			if isinstance(raw_kind, str) and raw_kind in ScheduleKind
			else ScheduleKind.NONE
		)
		raw_times = payload.get("times")
		times = (
			tuple(text for text in raw_times if isinstance(text, str) and try_hhmm(text))
			if isinstance(raw_times, list)
			else ()
		)
		return cls(
			kind=kind,
			min_minutes=as_int(payload, "min_minutes", DEFAULT_INTERVAL_MINUTES[0]),
			max_minutes=as_int(payload, "max_minutes", DEFAULT_INTERVAL_MINUTES[1]),
			times=times,
		)


def next_run(
	schedule: Schedule, after: datetime, tz: tzinfo, rng: random.Random | None = None
) -> datetime | None:
	"""Следующий момент запуска после ``after``; None — расписания нет.

	Интервал отсчитывается от ``after`` (конца прошлого запуска или
	момента включения): «пауза между проходами» — это пауза именно
	между ними, а не между началами. Случайная длина вытягивается
	здесь один раз — результат хранится вызывающим. Моменты суток
	считаются по ``tz``: ближайший из названных после ``after``, иначе
	первый по порядку завтра.

	Args:
		schedule: расписание (уже проверенное).
		after: от какого момента считать (со зоной).
		tz: местная зона для моментов суток.
		rng: источник случайности (тесты передают свой).
	"""
	if schedule.kind is ScheduleKind.INTERVAL:
		rng = rng if rng is not None else random.Random()
		minutes = rng.uniform(schedule.min_minutes, schedule.max_minutes)
		return after + timedelta(minutes=minutes)
	if schedule.kind is ScheduleKind.DAILY:
		return _next_daily(schedule.times, after, tz)
	return None


def _next_daily(times: tuple[str, ...], after: datetime, tz: tzinfo) -> datetime | None:
	"""Ближайший из моментов суток строго после ``after`` (по зоне ``tz``)."""
	local = after.astimezone(tz)
	candidates: list[datetime] = []
	for text in times:
		parsed = try_hhmm(text)
		if parsed is None:
			continue
		hours, minutes = parsed
		for day_shift in (0, 1):
			moment = (local + timedelta(days=day_shift)).replace(
				hour=hours, minute=minutes, second=0, microsecond=0
			)
			if moment > local:
				candidates.append(moment)
	# хранится в UTC, как всё время движка: SQLite зоны не помнит
	return min(candidates).astimezone(UTC) if candidates else None


def schedule_text(schedule: Schedule) -> str:
	"""Расписание по-русски для строки раздела и журнала."""
	if schedule.kind is ScheduleKind.INTERVAL:
		if schedule.min_minutes == schedule.max_minutes:
			return f"каждые {schedule.min_minutes} мин"
		return f"случайно каждые {schedule.min_minutes}–{schedule.max_minutes} мин"
	if schedule.kind is ScheduleKind.DAILY:
		return "ежедневно в " + ", ".join(schedule.times)
	return "только по требованию"
