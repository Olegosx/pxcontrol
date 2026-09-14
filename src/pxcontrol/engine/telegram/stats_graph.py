"""Разбор графиков встроенной статистики Telegram (чистые функции, без сети).

Статистику канала или супергруппы (``stats.getBroadcastStats`` /
``stats.getMegagroupStats``) Telegram отдаёт графиками в формате своих
клиентов: JSON с колонками — первая помечена типом ``x`` и несёт моменты
времени (миллисекунды от эпохи), остальные — ряды значений, у каждого
своё имя (``names``). Формат задокументирован только по примерам
клиентов (core.telegram.org/api/stats), поэтому разбор терпим к деталям:
незнакомые колонки не ломают ряд, пустой график — пустой результат.

Всё здесь — без объектов Telethon: транспорт достаёт из ответа строку
JSON, а дальше работает этот модуль, и его можно проверить на записанных
ответах без сети.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphSeries:
	"""Один ряд графика: имя и точки «момент → значение».

	Attributes:
		name: имя ряда из графика («Joined», «Followers»…) — как отдал
			Telegram; по нему ряды опознаются, регистр не важен.
		points: пары (момент в миллисекундах от эпохи, значение)
			в порядке следования.
	"""

	name: str
	points: tuple[tuple[int, float], ...]


def parse_graph(json_text: str) -> list[GraphSeries]:
	"""Разбирает JSON графика Telegram в ряды.

	Колонка типа ``x`` даёт моменты, каждая остальная — ряд. Ряд короче
	оси или длиннее — обрезается по общей длине: график с рассинхроном
	колонок считается частично пригодным, а не битым. График без оси
	(«пирог»: языки, дни недели — по одному значению на ряд) получает
	порядковые моменты 0, 1, 2… — для долей ось не нужна.

	Returns:
		Ряды в порядке колонок; пустой список — график пуст или
		не разобран (причина — в журнале, уровень предупреждения).
	"""
	try:
		data = json.loads(json_text)
	except (TypeError, ValueError):
		logger.warning("График статистики Telegram: JSON не разобран.")
		return []
	columns = data.get("columns") if isinstance(data, dict) else None
	if not isinstance(columns, list):
		logger.warning("График статистики Telegram: нет колонок.")
		return []
	types = data.get("types") if isinstance(data.get("types"), dict) else {}
	names = data.get("names") if isinstance(data.get("names"), dict) else {}
	axis: list[int] | None = None
	values: list[tuple[str, list[float]]] = []
	for column in columns:
		if not isinstance(column, list) or not column or not isinstance(column[0], str):
			continue
		key, raw = column[0], column[1:]
		if types.get(key) == "x" or (key == "x" and axis is None):
			axis = [int(v) for v in raw if isinstance(v, int | float)]
			continue
		values.append(
			(str(names.get(key, key)), [float(v) for v in raw if isinstance(v, int | float)])
		)
	if axis is None:
		if not values:
			logger.warning("График статистики Telegram: нет ни оси, ни рядов.")
			return []
		axis = list(range(max(len(series) for _name, series in values)))
	result = []
	for name, series in values:
		length = min(len(axis), len(series))
		if length == 0:
			continue  # колонка без значений — не ряд
		result.append(GraphSeries(name, tuple(zip(axis[:length], series[:length], strict=True))))
	return result


def pick_series(
	series: list[GraphSeries], *hints: str, position: int | None = None
) -> GraphSeries | None:
	"""Находит ряд по подсказкам имени, иначе — по позиции.

	Имена рядов Telegram не документирует (в клиентах видны «Joined»
	и «Left»), поэтому сначала ищется вхождение подсказки в имя без
	учёта регистра, а при промахе берётся ряд по номеру — так график
	с переименованными рядами не теряется молча.
	"""
	for hint in hints:
		needle = hint.casefold()
		for item in series:
			if needle in item.name.casefold():
				return item
	if position is not None and 0 <= position < len(series):
		return series[position]
	return None


def day_of(moment_ms: int) -> date:
	"""Дата (UTC) момента графика в миллисекундах от эпохи."""
	return datetime.fromtimestamp(moment_ms / 1000, tz=UTC).date()


def daily(series: GraphSeries | None) -> list[tuple[date, int]]:
	"""Ряд по дням: (дата UTC, целое значение); пустой ряд — пустой список."""
	if series is None:
		return []
	return [(day_of(moment), int(round(value))) for moment, value in series.points]


def named_daily(series: list[GraphSeries]) -> list[tuple[str, list[tuple[date, int]]]]:
	"""Все ряды графика по дням с именами (порядок — как в графике)."""
	return [(item.name, daily(item)) for item in series]


def shares(series: list[GraphSeries]) -> list[tuple[str, int]]:
	"""Доли: имя ряда и сумма его значений за период (нулевые ряды выброшены).

	Одинаково подходит графику «по дням» (источники просмотров по дням
	складываются в долю источника) и «пирогу» с одним значением на ряд.
	"""
	result = []
	for item in series:
		total = int(round(sum(value for _moment, value in item.points)))
		if total > 0:
			result.append((item.name, total))
	return result


def hourly(series: GraphSeries | None) -> list[int] | None:
	"""Профиль по часам суток: 24 значения; None — ряд не про часы.

	Ось графика «по часам» Telegram задаёт смещениями внутри суток;
	значения раскладываются по номеру часа в порядке следования.
	Ряд не из 24 точек — не профиль суток, и лучше промолчать.
	"""
	if series is None or len(series.points) != 24:
		return None
	return [int(round(value)) for _moment, value in series.points]
