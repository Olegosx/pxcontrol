"""Тесты разбора графиков встроенной статистики Telegram (без сети)."""

from __future__ import annotations

import json
from datetime import date

from pxcontrol.engine.telegram.stats_graph import (
	GraphSeries,
	daily,
	hourly,
	parse_graph,
	pick_series,
)

#: 1 сентября 2026, полночь UTC — в миллисекундах от эпохи.
_DAY_MS = 24 * 60 * 60 * 1000
_SEP_1 = 1788220800000


def _graph(columns: list[list[object]], names: dict[str, str] | None = None) -> str:
	return json.dumps(
		{
			"columns": columns,
			"types": {"x": "x", **{c[0]: "line" for c in columns if c[0] != "x"}},
			"names": names or {},
		}
	)


def test_parse_graph_splits_axis_and_series() -> None:
	text = _graph(
		[["x", _SEP_1, _SEP_1 + _DAY_MS], ["y0", 10, 12.0], ["y1", 3, 1]],
		{"y0": "Joined", "y1": "Left"},
	)
	series = parse_graph(text)
	assert [s.name for s in series] == ["Joined", "Left"]
	assert series[0].points == ((_SEP_1, 10.0), (_SEP_1 + _DAY_MS, 12.0))
	assert series[1].points == ((_SEP_1, 3.0), (_SEP_1 + _DAY_MS, 1.0))


def test_parse_graph_trims_to_common_length_and_ignores_junk() -> None:
	text = _graph([["x", _SEP_1, _SEP_1 + _DAY_MS, _SEP_1 + 2 * _DAY_MS], ["y0", 1, 2], ["oops"]])
	series = parse_graph(text)
	assert len(series) == 1
	assert len(series[0].points) == 2


def test_parse_graph_without_axis_or_bad_json_is_empty() -> None:
	assert parse_graph(json.dumps({"columns": [["y0", 1, 2]]})) == []
	assert parse_graph("not json") == []
	assert parse_graph(json.dumps({"nope": 1})) == []


def test_pick_series_by_name_then_position() -> None:
	series = [GraphSeries("Members", ((1, 1.0),)), GraphSeries("Gone", ((1, 2.0),))]
	assert pick_series(series, "join", position=0) is series[0]  # по позиции: имени нет
	assert pick_series(series, "gone") is series[1]
	assert pick_series(series, "GONE") is series[1]  # регистр не важен
	assert pick_series(series, "left", position=5) is None
	assert pick_series([], position=0) is None


def test_daily_and_hourly_projections() -> None:
	growth = GraphSeries("Followers", ((_SEP_1, 100.4), (_SEP_1 + _DAY_MS, 101.6)))
	assert daily(growth) == [(date(2026, 9, 1), 100), (date(2026, 9, 2), 102)]
	assert daily(None) == []
	hours = GraphSeries("Views", tuple((h * 3_600_000, float(h)) for h in range(24)))
	assert hourly(hours) == list(range(24))
	assert hourly(GraphSeries("Views", ((0, 1.0),))) is None
	assert hourly(None) is None
