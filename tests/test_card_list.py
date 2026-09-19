"""Список карточек: части шапки пересобираются по своим отпечаткам.

Проверяется с Qt без экрана: у карточки после обновления те же объекты
кнопок и начала шапки, пока не сменился их отпечаток; порядок компоновки
следует за порядком снимка; прогресс двигается только при новом значении.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget  # noqa: E402

from pxcontrol.ui.pages.card_list import CardList  # noqa: E402


@dataclass(frozen=True)
class _Item:
	id: int
	title: str
	status: str
	note: str = ""
	slot: str = "10:00"


class _Harness:
	"""Список с крючками-счётчиками: сколько раз собирались части."""

	def __init__(self, host: QWidget) -> None:
		self.box = QVBoxLayout(host)
		self.leading_builds = 0
		self.actions_builds = 0
		self.progress_calls: list[tuple[float, str] | None] = []
		self.progress: dict[int, tuple[float, str] | None] = {}
		self.list = CardList(
			host,
			self.box,
			subtitle=lambda item: f"{item.status} · {item.note}",
			signature=lambda item: (item.title, item.status, item.note, item.slot),
			leading=self._leading,
			leading_signature=lambda item: (item.slot,),
			actions=self._actions,
			actions_signature=lambda item: (item.status,),
			progress=lambda item: self.progress.get(item.id),
			compact=True,
		)

	def _leading(self, item: _Item, parent: QWidget) -> list[QWidget]:
		self.leading_builds += 1
		return [QLabel(item.slot, parent)]

	def _actions(self, item: _Item, parent: QWidget) -> list[QWidget]:
		self.actions_builds += 1
		return [QPushButton(item.status, parent)]

	def card(self, item_id: int) -> Any:
		return self.list._cards[item_id]  # noqa: SLF001 — проверка внутренностей списка

	def order(self) -> list[int]:
		widgets = [self.box.itemAt(i).widget() for i in range(self.box.count())]
		return [next(k for k, c in self.list._cards.items() if c.widget is w) for w in widgets]  # noqa: SLF001


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def harness(qapp: QApplication) -> Iterator[_Harness]:
	del qapp
	host = QWidget()
	yield _Harness(host)
	host.deleteLater()


def test_note_change_keeps_leading_and_actions(harness: _Harness) -> None:
	harness.list.sync([_Item(1, "a", "pending")])
	card = harness.card(1)
	leading_before = card._leading_box.itemAt(0).widget()  # noqa: SLF001
	actions_before = card._actions_box.itemAt(0).widget()  # noqa: SLF001
	harness.list.sync([_Item(1, "a", "pending", note="пауза")])
	assert card._leading_box.itemAt(0).widget() is leading_before  # noqa: SLF001
	assert card._actions_box.itemAt(0).widget() is actions_before  # noqa: SLF001
	assert (harness.leading_builds, harness.actions_builds) == (1, 1)


def test_status_change_rebuilds_actions_only(harness: _Harness) -> None:
	harness.list.sync([_Item(1, "a", "pending")])
	card = harness.card(1)
	leading_before = card._leading_box.itemAt(0).widget()  # noqa: SLF001
	harness.list.sync([_Item(1, "a", "running")])
	assert card._leading_box.itemAt(0).widget() is leading_before  # noqa: SLF001
	assert card._actions_box.itemAt(0).widget().text() == "running"  # noqa: SLF001
	assert (harness.leading_builds, harness.actions_builds) == (1, 2)


def test_slot_change_rebuilds_leading_only(harness: _Harness) -> None:
	harness.list.sync([_Item(1, "a", "pending")])
	harness.list.sync([_Item(1, "a", "pending", slot="18:30")])
	card = harness.card(1)
	assert card._leading_box.itemAt(0).widget().text() == "18:30"  # noqa: SLF001
	assert (harness.leading_builds, harness.actions_builds) == (2, 1)


def test_reorder_follows_snapshot_without_rebuilding(harness: _Harness) -> None:
	items = [_Item(1, "a", "pending"), _Item(2, "b", "pending"), _Item(3, "c", "pending")]
	harness.list.sync(items)
	assert harness.order() == [1, 2, 3]
	widgets = {k: harness.card(k).widget for k in (1, 2, 3)}
	harness.list.sync([items[2], items[0], items[1]])
	assert harness.order() == [3, 1, 2]
	assert all(harness.card(k).widget is widgets[k] for k in (1, 2, 3))
	harness.list.sync([items[1]])
	assert harness.order() == [2]


def test_progress_is_applied_only_on_change(harness: _Harness) -> None:
	harness.list.sync([_Item(1, "a", "running")])
	card = harness.card(1)
	applied: list[tuple[float | None, str]] = []
	card.widget.set_progress = lambda fraction=None, text="": applied.append((fraction, text))  # type: ignore[method-assign]
	harness.progress[1] = (0.5, "50 %")
	harness.list.sync([_Item(1, "a", "running")])
	harness.list.sync([_Item(1, "a", "running")])  # тот же прогресс — ничего не делает
	assert applied == [(0.5, "50 %")]
	harness.progress[1] = None
	harness.list.sync([_Item(1, "a", "running")])
	assert applied[-1] == (None, "")
