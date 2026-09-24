"""Пресеты подписи в интерфейсе (ADR-0042): экран пресета, окно сборки, пакет.

Чистые функции экрана проверяются без Qt. Экран пресета, окно сборки
и тело вкладки «Настройки» — с Qt без экрана (платформа ``offscreen``):
движок им в этих проверках не нужен — обращения к нему начинаются
только с сохранения, словаря и списка пресетов.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

# платформа без экрана выбирается до первого обращения к Qt
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402 — после выбора платформы

from pxcontrol.engine.services.captions import (  # noqa: E402
	CaptionPresetDto,
	FieldDto,
	FieldEdit,
	FieldStyle,
	PresetFieldDto,
	PresetFieldSpec,
	SourceRule,
	ValueDto,
)
from pxcontrol.engine.telegram.types import CommunityKind  # noqa: E402
from pxcontrol.ui.pages.caption_presets import (  # noqa: E402
	MANUAL_PLACEHOLDER,
	PresetEditor,
	PresetForm,
	preset_summary,
	preview_line,
	sample_source,
	without_field,
)
from pxcontrol.ui.pages.captions import (  # noqa: E402
	PER_FILE_HINT,
	CaptionDialog,
	split_prefill,
)
from pxcontrol.ui.pages.common import UnsavedChanges  # noqa: E402
from pxcontrol.ui.pages.publish_batch import BatchCaption  # noqa: E402

_MOVIE = "11.12.2014 627563 Bruce Lee, Jan Clod Van Dam, Ded Morozz (Prosto Film)"
_TITLE_STYLE = FieldStyle(hashtag=False, show_name=False, bold=True)
_TITLE_RULE = SourceRule(extract=r"\(([^()]*)\)\s*$")
_STARRING_RULE = SourceRule(extract=r"^\S+\s+\d+\s+(.+?)\s*\(")

_TITLE = FieldDto(1, "Title", _TITLE_STYLE, [])
_STARRING = FieldDto(2, "Starring", FieldStyle(multiple=True), [ValueDto(5, "Bruce Lee")])
_GENRE = FieldDto(3, "Genre", FieldStyle(multiple=True), [ValueDto(6, "action")])


def _movie_preset() -> CaptionPresetDto:
	return CaptionPresetDto(
		7,
		"Фильм",
		None,
		[
			PresetFieldDto(_TITLE, True, _TITLE_RULE),
			PresetFieldDto(_STARRING, True, _STARRING_RULE),
			PresetFieldDto(_GENRE, True, None),
		],
		"{Title} ({quality}) @{channel}",
	)


def _community() -> Any:
	"""Снимок сообщества: экрану нужны только id и имя канала."""
	from types import SimpleNamespace

	return SimpleNamespace(id=1, username="mych", kind=CommunityKind.CHANNEL, title="Канал")


# --- чистые функции ------------------------------------------------------------------


def test_without_field_mirrors_engine_cascade() -> None:
	"""Удалённое поле уходит из состава, зависимые от него — без связи."""
	form = PresetForm(
		"Ф",
		(PresetFieldSpec(1), PresetFieldSpec(2)),
		"",
		((1, FieldEdit(FieldStyle())), (2, FieldEdit(FieldStyle(), 1))),
	)
	assert without_field(form, 1) == PresetForm(
		"Ф", (PresetFieldSpec(2),), "", ((2, FieldEdit(FieldStyle(), None)),)
	)


def test_preset_summary_names_parsed_fields_and_pattern() -> None:
	assert preset_summary(_movie_preset()) == (
		"Title, Starring, Genre · из имени файла: Title, Starring · имя файла задано"
	)
	empty = CaptionPresetDto(1, "Пусто", None, [])
	assert preset_summary(empty) == "полей нет"


def test_preview_line_and_sample_source() -> None:
	"""Поле с правилом показывает разобранное, ручное — заглушку без решётки."""
	source = sample_source(f"  {_MOVIE}.mp4 ")
	assert source == _MOVIE
	assert sample_source("   ") is None
	parsed = preview_line("Title", _TITLE_STYLE, _TITLE_RULE, source)
	assert parsed.values == ["Prosto Film"]
	manual = preview_line("Genre", FieldStyle(multiple=True), None, source)
	assert manual.values == [MANUAL_PLACEHOLDER] and manual.style.hashtag is False
	no_sample = preview_line("Title", _TITLE_STYLE, _TITLE_RULE, None)
	assert no_sample.values == [MANUAL_PLACEHOLDER]


def test_split_prefill_marks_dictionary_values() -> None:
	"""Разобранное из словаря отмечается пилюлей, новое — вписывается строкой."""
	picked, typed = split_prefill(_STARRING, ["bruce lee", "Jan Clod Van Dam"])
	assert picked == {"bruce lee"} and typed == ["Jan Clod Van Dam"]


def test_batch_caption_merges_common_and_parsed_values() -> None:
	"""Строка пакета: общие значения окна плюс разобранные из имени её файла."""
	batch = BatchCaption(_movie_preset(), (1, 2, 3), {3: ["action"]})
	values = batch.values_for(f"/x/{_MOVIE}.mp4")
	assert values == {
		1: ["Prosto Film"],
		2: ["Bruce Lee", "Jan Clod Van Dam", "Ded Morozz"],
		3: ["action"],
	}
	caption = batch.caption_for(f"/x/{_MOVIE}.mp4")
	assert caption.text == (
		"Prosto Film\nStarring: #BruceLee, #JanClodVanDam, #DedMorozz\nGenre: #Action"
	)
	# у файла без скобок тайтла нет — строка пропадает, а не пишет всё имя
	assert batch.caption_for("/x/Без скобок.mp4").text == "Genre: #Action"


# --- Qt без экрана ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	"""Приложение Qt без экрана — одно на модуль."""
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def host(qapp: QApplication) -> Iterator[QWidget]:
	widget = QWidget()
	yield widget
	widget.deleteLater()


def _editor(host: QWidget) -> PresetEditor:
	editor = PresetEditor(Any, _community(), host)  # type: ignore[arg-type]
	editor.open(_movie_preset(), [_TITLE, _STARRING, _GENRE], [_movie_preset()])
	return editor


def test_editor_opens_clean_and_tracks_edits(host: QWidget) -> None:
	"""Открытый пресет без правок; правка имени и порядок — правки; откат — чисто."""
	editor = _editor(host)
	assert not editor.dirty
	assert isinstance(editor, UnsavedChanges)
	editor._name.setText("Фильм 2")
	assert editor.dirty
	editor.discard()
	assert not editor.dirty and editor._name.text() == "Фильм"
	editor._move_card(editor._cards[2], -1)
	assert editor.dirty
	assert [card.field.name for card in editor._cards] == ["Title", "Genre", "Starring"]


def test_editor_form_carries_rules_and_styles(host: QWidget) -> None:
	"""Черновик экрана: состав с правилами и правки оформления полей."""
	editor = _editor(host)
	draft = editor._form().draft()
	assert draft.name == "Фильм"
	assert [spec.rule for spec in draft.fields] == [_TITLE_RULE, _STARRING_RULE, None]
	assert draft.field_edits[1] == FieldEdit(_TITLE_STYLE, None)
	assert draft.filename_pattern == "{Title} ({quality}) @{channel}"


def test_editor_preview_from_sample(host: QWidget) -> None:
	"""Образец даёт результат под полем, подпись и имя файла в предпросмотре."""
	editor = _editor(host)
	editor._sample.setText(f"{_MOVIE}.mp4")
	assert editor._cards[0]._result.text() == "На образце: Prosto Film"
	assert editor._preview.toPlainText() == (
		"Prosto Film\nStarring: #BruceLee, #JanClodVanDam, #DedMorozz\nGenre: …"
	)
	# неизвестный без файла {quality} остаётся видимым как есть
	assert editor._preview_name.text() == "Имя файла: Prosto Film ({quality}) @mych.mp4"
	assert not editor.dirty  # образец — не правка пресета


def test_editor_rejects_broken_rule_before_engine(host: QWidget) -> None:
	"""Битое правило не уходит в движок: причина — строкой на экране."""
	editor = _editor(host)
	editor._cards[0]._rule._extract.setText("((")
	assert editor.dirty
	failed: list[bool] = []
	editor.save(failed=lambda: failed.append(True))
	assert failed == [True]
	assert "Выражение извлечения" in editor._error.text()


def test_editor_remove_card_and_new_preset(host: QWidget) -> None:
	"""Поле убирается из пресета; новый пресет чист, пока ничего не ввели."""
	editor = _editor(host)
	editor._remove_card(editor._cards[1])
	assert [card.field.name for card in editor._cards] == ["Title", "Genre"]
	assert editor.dirty
	editor.open(None, [_TITLE, _GENRE])
	assert not editor.dirty and editor._cards == []
	editor._add_existing(_GENRE)
	assert editor.dirty


def test_editor_leave_without_edits_goes_at_once(host: QWidget) -> None:
	editor = _editor(host)
	went: list[bool] = []
	editor.leave(lambda: went.append(True))
	assert went == [True]


def test_caption_dialog_prefills_from_file(host: QWidget) -> None:
	"""Одиночный пост: поля с правилом заполнены из имени файла."""
	dialog = CaptionDialog([_movie_preset()], _MOVIE, host)
	assert dialog.values() == {
		1: ["Prosto Film"],
		2: ["Bruce Lee", "Jan Clod Van Dam", "Ded Morozz"],
	}
	assert dialog.caption().text == ("Prosto Film\nStarring: #BruceLee, #JanClodVanDam, #DedMorozz")
	dialog.deleteLater()


def test_caption_dialog_per_file_mode_leaves_parsed_fields(host: QWidget) -> None:
	"""Пакет: поля с правилом не правятся, окно даёт только общие значения."""
	dialog = CaptionDialog([_movie_preset()], None, host, per_file=True)
	rows = {row.field.name: row for row in dialog._rows}
	assert rows["Title"].per_file and rows["Starring"].per_file
	assert not rows["Genre"].per_file
	rows["Genre"]._line.setText("drama")
	assert dialog.values() == {3: ["drama"]}
	assert dialog.enabled_ids() == [1, 2, 3]
	assert PER_FILE_HINT
	dialog.deleteLater()


def test_settings_tab_is_unsaved_guard(host: QWidget) -> None:
	"""Тело «Настроек» узнаётся страницей по общему признаку, а не по классу."""
	from pxcontrol.ui.pages.community_page import _SettingsTab

	tab = _SettingsTab(Any, _community(), host)  # type: ignore[arg-type]
	assert isinstance(tab, UnsavedChanges)
	assert not tab.dirty
	went: list[bool] = []
	tab.leave(lambda: went.append(True))
	assert went == [True]
