"""Разделы дашбордов по отпечатку: сетка, разделы, отпечаток строки сообщества.

С Qt без экрана: карточка живёт, пока не сменился отпечаток; сменился —
заменяется новой; исчезла строка — карточка снимается; раздел появляется
и исчезает целиком на своём месте в стопке. Отпечаток строки дашборда
сообществ — чистая функция.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from pxcontrol.engine.services.communities import CommunityDto  # noqa: E402
from pxcontrol.engine.services.community_stats import CommunityStatsDto  # noqa: E402
from pxcontrol.ui.pages.common import FlowGrid, QueueCounts  # noqa: E402
from pxcontrol.ui.pages.communities import Row, row_signature  # noqa: E402
from pxcontrol.ui.pages.dashboard import GridSection, SectionStack  # noqa: E402


@dataclass(frozen=True)
class _Item:
	id: int
	text: str


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def page(qapp: QApplication) -> Iterator[QWidget]:
	del qapp
	host = QWidget()
	yield host
	host.deleteLater()


def _section(page: QWidget) -> GridSection[_Item]:
	return GridSection(page, "Раздел", None, min_width=100, spacing=4)


def _sync(section: GridSection[_Item], items: list[_Item], made: list[int]) -> None:
	def make(item: _Item) -> QWidget:
		made.append(item.id)
		return QLabel(item.text)

	section.sync(items, key=lambda item: item.id, signature=lambda item: (item.text,), make=make)


def test_unchanged_cards_survive_and_changed_are_replaced(page: QWidget) -> None:
	section = _section(page)
	made: list[int] = []
	_sync(section, [_Item(1, "a"), _Item(2, "b")], made)
	first = section.cards[1]
	_sync(section, [_Item(1, "a"), _Item(2, "b2")], made)
	assert section.cards[1] is first  # не менялась — та же карточка
	assert section.cards[2] is not first and section.cards[2].text() == "b2"  # type: ignore[attr-defined]
	assert made == [1, 2, 2]  # собрана заново только вторая


def test_vanished_card_is_dropped_and_grid_follows_order(page: QWidget) -> None:
	section = _section(page)
	made: list[int] = []
	_sync(section, [_Item(1, "a"), _Item(2, "b"), _Item(3, "c")], made)
	_sync(section, [_Item(3, "c"), _Item(1, "a")], made)
	assert set(section.cards) == {1, 3}
	grid = section.body
	assert isinstance(grid, FlowGrid)
	layout = grid.layout()
	assert layout is not None and layout.count() == 2
	assert layout.itemAt(0).widget() is section.cards[3]  # порядок — как в снимке
	assert made == [1, 2, 3]  # ничего не пересоздано


def test_flow_grid_set_cards_keeps_widgets(page: QWidget) -> None:
	cards = [QLabel(str(index), page) for index in range(3)]
	grid = FlowGrid(cards, page, min_width=100, spacing=4)
	grid.set_cards([cards[2], cards[0]])
	layout = grid.layout()
	assert layout is not None and layout.count() == 2
	assert layout.itemAt(0).widget() is cards[2]
	assert layout.itemAt(1).widget() is cards[0]


def test_section_stack_keeps_order_and_drops(page: QWidget) -> None:
	box = QVBoxLayout(page)
	stack: SectionStack[str] = SectionStack(box, ["a", "b"])
	second = stack.ensure("b", lambda: _section(page))
	first = stack.ensure("a", lambda: _section(page))
	assert stack.ensure("a", lambda: _section(page)) is first  # повторно — тот же
	# «a» стоит раньше «b», хотя появился позже: заголовок, тело, заголовок, тело
	assert box.itemAt(0).widget() is first.header
	assert box.itemAt(1).widget() is first.body
	assert box.itemAt(2).widget() is second.header
	stack.drop("a")
	assert "a" not in stack and box.count() == 2
	stack.drop_all()
	assert box.count() == 0


# --- отпечаток строки дашборда сообществ (без Qt) --------------------------------------


def _community(**overrides: object) -> CommunityDto:
	base = CommunityDto(
		id=1,
		title="Канал",
		username="kino",
		tg_chat_id="-100",
		bot_id=None,
		bot_label=None,
		enabled=True,
	)
	return replace(base, **overrides)  # type: ignore[arg-type]


def _stats(**overrides: object) -> CommunityStatsDto:
	base = CommunityStatsDto(
		community_id=1,
		participants=10,
		online=None,
		scheduled_count=2,
		avatar_path=None,
		fetched_at=None,
	)
	return replace(base, **overrides)  # type: ignore[arg-type]


def test_row_signature_tracks_shown_fields_only() -> None:
	row = Row(_community(), QueueCounts(), _stats())
	assert row_signature(row) == row_signature(Row(_community(), QueueCounts(), _stats()))
	assert row_signature(row) != row_signature(replace(row, counts=QueueCounts(planned=1)))
	assert row_signature(row) != row_signature(replace(row, community=_community(title="Другой")))
	assert row_signature(row) != row_signature(replace(row, stats=_stats(participants=11)))
	assert row_signature(row) != row_signature(replace(row, stats=_stats(avatar_path="/a.png")))
	# онлайн и момент опроса карточка не показывает — отпечаток их не видит
	assert row_signature(row) == row_signature(replace(row, stats=_stats(online=5)))
	assert row_signature(Row(_community(), QueueCounts(), None)) != row_signature(row)
