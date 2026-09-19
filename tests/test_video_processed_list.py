"""«Готовые видео» на странице «Видео»: правила показа и точечный список.

Правила показа (заголовок, подпись, отпечаток, слова итога) — чистые
функции без Qt. Общий список карточек с крючком заголовка проверяется
с Qt без экрана: карточка изменившегося файла обновляется на месте,
а не создаётся заново, карточка исчезнувшего — снимается.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from pxcontrol.engine.services.video import VideoFile  # noqa: E402
from pxcontrol.ui.pages.card_list import CardList  # noqa: E402
from pxcontrol.ui.pages.list_view import paginate, summary_text  # noqa: E402
from pxcontrol.ui.pages.video import (  # noqa: E402
	PROCESSED_WORDS,
	processed_signature,
	processed_subtitle,
	processed_title,
)

_WHEN = datetime(2026, 9, 19, 12, 30)


def _file(name: str, size: int = 1024) -> VideoFile:
	return VideoFile(name, f"/media/processed/{name}", size, _WHEN)


# --- правила показа (без Qt) -----------------------------------------------------


def test_title_is_file_name_without_subdir() -> None:
	assert processed_title(_file("пакет/clip.mp4")) == "clip.mp4"
	assert processed_title(_file("clip.mp4")) == "clip.mp4"


def test_subtitle_names_subdir_only_for_nested_file() -> None:
	nested = processed_subtitle(_file("пакет/clip.mp4"))
	assert nested.startswith("подпапка «пакет» · ")
	assert "подпапка" not in processed_subtitle(_file("clip.mp4"))


def test_signature_changes_with_size_and_time_only() -> None:
	item = _file("clip.mp4")
	assert processed_signature(item) == processed_signature(replace(item, path="/other"))
	assert processed_signature(item) != processed_signature(replace(item, size_bytes=2048))
	assert processed_signature(item) != processed_signature(
		replace(item, modified_at=datetime(2026, 9, 20))
	)


def test_summary_words_for_empty_and_pages() -> None:
	assert summary_text(paginate([], 1), 0, PROCESSED_WORDS) == PROCESSED_WORDS.empty
	items = [_file(f"{index}.mp4") for index in range(120)]
	assert summary_text(paginate(items, 1), 120, PROCESSED_WORDS) == (
		"Показаны 1–50 из 120 готовых видео."
	)
	assert summary_text(paginate(items[:7], 1), 7, PROCESSED_WORDS) == (
		"Показано 7 из 7 готовых видео."
	)


# --- список карточек с крючком заголовка (Qt offscreen) ---------------------------


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def cards(qapp: QApplication) -> Iterator[tuple[CardList, QVBoxLayout]]:
	del qapp
	host = QWidget()
	box = QVBoxLayout(host)
	yield (
		CardList(
			host,
			box,
			title=processed_title,
			subtitle=processed_subtitle,
			signature=processed_signature,
			key=lambda item: item.path,
			compact=True,
		),
		box,
	)
	host.deleteLater()


def test_card_title_comes_from_hook(cards: tuple[CardList, QVBoxLayout]) -> None:
	card_list, box = cards
	item = _file("пакет/clip.mp4")
	card_list.sync([item])
	assert box.count() == 1
	assert card_list._cards[item.path].widget._title.text() == "clip.mp4"  # noqa: SLF001


def test_changed_file_updates_card_in_place(cards: tuple[CardList, QVBoxLayout]) -> None:
	card_list, box = cards
	first, second = _file("a.mp4"), _file("b.mp4")
	card_list.sync([first, second])
	widget_before = card_list._cards[first.path].widget  # noqa: SLF001
	card_list.sync([replace(first, size_bytes=4096), second])
	assert box.count() == 2  # карточек не прибавилось
	assert card_list._cards[first.path].widget is widget_before  # noqa: SLF001 — та же карточка


def test_vanished_file_drops_its_card(cards: tuple[CardList, QVBoxLayout]) -> None:
	card_list, box = cards
	first, second = _file("a.mp4"), _file("b.mp4")
	card_list.sync([first, second])
	card_list.sync([second])
	assert box.count() == 1
	assert first.path not in card_list._cards  # noqa: SLF001
