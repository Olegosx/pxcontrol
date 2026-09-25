"""Страница «Видео»: снимок параметров у карточки и общий редактор.

Правило автоподстановки битрейта — чистая функция, проверяется без Qt.
Редактор карточек (:class:`_EntryEditor`) проверяется с Qt без экрана
(платформа ``offscreen``): он переносит одну форму между карточками,
и ошибка в этом переносе означала бы либо потерю правок человека, либо
удаление формы вместе с карточкой.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

# платформа без экрана выбирается до первого обращения к Qt: ниже
# импортируются виджеты, а приложение создаёт фикстура
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402 — после выбора платформы

from pxcontrol.engine.services.video import PresetFields  # noqa: E402
from pxcontrol.ui.pages.video import _EntryEditor, _FileEntry  # noqa: E402
from pxcontrol.ui.pages.video_form import apply_bitrate_advice  # noqa: E402

# --- правило автоподстановки (без Qt) ------------------------------------------


def test_advice_fills_empty_field() -> None:
	assert apply_bitrate_advice(None, False, 4.2) == 4200


def test_advice_overrides_previous_suggestion() -> None:
	assert apply_bitrate_advice(4200, True, 3.5) == 3500


def test_advice_keeps_manual_value() -> None:
	assert apply_bitrate_advice(6000, False, 3.5) is None


def test_advice_rounds_to_kbps() -> None:
	assert apply_bitrate_advice(None, False, 2.3456) == 2346


# --- общий редактор карточек (Qt offscreen) ----------------------------------------


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	"""Приложение Qt без экрана — одно на модуль."""
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def page(qapp: QApplication) -> Iterator[QWidget]:
	"""Виджет-страница: родитель карточек и редактора."""
	del qapp
	host = QWidget()
	yield host
	host.deleteLater()


def _entry(page: QWidget, name: str, **overrides: Any) -> _FileEntry:
	fields = replace(PresetFields(name="ручные"), **overrides)
	return _FileEntry(page, f"/tmp/{name}.mp4", 1024, "", fields, lambda _entry: None)


def test_attach_moves_form_into_card_and_fills_snapshot(page: QWidget) -> None:
	editor = _EntryEditor(page)
	first = _entry(page, "a", target_resolution=720)
	editor.attach(first)
	assert editor.editing is first
	assert editor.form.parentWidget() is not page
	assert editor.form.fields("ручные").target_resolution == 720


def test_attach_to_other_card_commits_and_collapses_previous(page: QWidget) -> None:
	editor = _EntryEditor(page)
	first, second = _entry(page, "a"), _entry(page, "b")
	first.card.set_expanded(True)
	editor.attach(first)
	# человек поменял ступень в раскрытой карточке (fill — как ввод в поля)
	editor.form.fill(replace(first.fields, target_resolution=720))
	assert editor.fields_of(first).target_resolution == 720  # живое значение
	editor.attach(second)
	assert first.fields.target_resolution == 720  # правка легла в снимок
	assert not first.card.expanded()  # прежняя карточка свернулась
	assert editor.editing is second
	assert editor.fields_of(first) is first.fields  # у свёрнутой — снимок


def test_detach_returns_form_to_page(page: QWidget) -> None:
	editor = _EntryEditor(page)
	entry = _entry(page, "a")
	editor.attach(entry)
	editor.detach()
	assert editor.editing is None
	assert editor.form.parentWidget() is page
	assert not editor.form.isVisible()


def test_release_detaches_only_edited_card(page: QWidget) -> None:
	editor = _EntryEditor(page)
	first, second = _entry(page, "a"), _entry(page, "b")
	editor.attach(first)
	editor.release(second)
	assert editor.editing is first
	editor.release(first)
	assert editor.editing is None
	assert editor.form.parentWidget() is page  # форма пережила уход карточки


def test_suggest_bitrate_updates_snapshot_and_flag(page: QWidget) -> None:
	editor = _EntryEditor(page)
	entry = _entry(page, "a")
	assert editor.suggest_bitrate(entry, 4.2)
	assert entry.fields.video_bitrate_kbps == 4200
	assert entry.bitrate_suggested
	assert editor.suggest_bitrate(entry, 3.5)  # рекомендация обновляется
	assert entry.fields.video_bitrate_kbps == 3500


def test_suggest_bitrate_respects_manual_value(page: QWidget) -> None:
	editor = _EntryEditor(page)
	entry = _entry(page, "a", video_bitrate_kbps=6000)
	assert not editor.suggest_bitrate(entry, 3.5)
	assert entry.fields.video_bitrate_kbps == 6000


def test_suggested_flag_survives_attach_and_detach(page: QWidget) -> None:
	editor = _EntryEditor(page)
	entry = _entry(page, "a")
	editor.suggest_bitrate(entry, 4.2)
	editor.attach(entry)
	assert editor.form.bitrate_suggested()  # fill не стёр признак
	editor.detach()
	assert entry.bitrate_suggested
	assert entry.fields.video_bitrate_kbps == 4200


def test_suggest_bitrate_on_open_card_goes_to_form(page: QWidget) -> None:
	editor = _EntryEditor(page)
	entry = _entry(page, "a")
	editor.attach(entry)
	assert editor.suggest_bitrate(entry, 4.2)
	assert editor.fields_of(entry).video_bitrate_kbps == 4200
	editor.detach()
	assert entry.fields.video_bitrate_kbps == 4200
	assert entry.bitrate_suggested


# --- режим битрейта при смене разрешения (ADR-0044) -----------------------------


def test_form_round_trips_rescale_mode(page: QWidget) -> None:
	"""Режим из пресета доходит до формы и обратно; незнакомый — умолчание."""
	form = _EntryEditor(page).form
	form.fill(PresetFields(name="п", rescale_bitrate_mode="scale"))
	assert form.fields("п").rescale_bitrate_mode == "scale"
	form.fill(PresetFields(name="п", rescale_bitrate_mode="из-будущей-версии"))
	assert form.fields("п").rescale_bitrate_mode == "crf"


def test_rescale_mode_active_only_without_bitrate_and_with_step(page: QWidget) -> None:
	"""Список активен, лишь когда режим может подействовать."""
	form = _EntryEditor(page).form
	form.fill(PresetFields(name="п"))  # ступень 1080, битрейт «как в оригинале»
	assert form._rescale.isEnabled()
	form.fill(PresetFields(name="п", video_bitrate_kbps=4000))
	assert not form._rescale.isEnabled()  # явный битрейт главнее
	form.fill(PresetFields(name="п", target_resolution=None))
	assert not form._rescale.isEnabled()  # кадр не масштабируется
