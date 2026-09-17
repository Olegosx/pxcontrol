"""Тесты перевода «документ Qt ↔ сущности разметки» (ADR-0033, подача C2).

Здесь проверяется самое опасное место оформления: арифметика смещений.
Ошибка в ней не падает и не видна в интерфейсе — она портит уже
опубликованный пост, поэтому перевод гоняется в обе стороны на случаях,
где смещения легко разъезжаются: эмодзи (суррогатная пара), несколько
абзацев, вложенные стили, ссылка со своим адресом.

Qt-приложение для этого не нужно: ``QTextDocument`` и форматы живут
без окна, а значит проверка идёт обычным прогоном тестов.
"""

from __future__ import annotations

from PySide6.QtGui import QTextDocument

from pxcontrol.engine.telegram.rich_text import RichText, TextEntity, TextStyle, trimmed
from pxcontrol.ui.pages.rich_edit import StyleRun, apply_rich, merge_runs, rich_of_document


def _run(start: int, length: int, *styles: tuple[TextStyle, str]) -> StyleRun:
	return StyleRun(start, length, styles)


def test_merge_runs_joins_neighbours_of_same_style() -> None:
	"""Соседние отрезки одного стиля — одна сущность, а не две.

	Документ Qt дробится на отрезки при любой смене формата: жирный
	внутри спойлера разрезал бы спойлер надвое, и правка перестала бы
	сходиться с тем, что вернёт сервер.
	"""
	runs = [
		_run(0, 3, (TextStyle.SPOILER, "")),
		_run(3, 4, (TextStyle.SPOILER, ""), (TextStyle.BOLD, "")),
		_run(7, 3, (TextStyle.SPOILER, "")),
	]
	assert merge_runs(runs) == (
		TextEntity(TextStyle.SPOILER, 0, 10),
		TextEntity(TextStyle.BOLD, 3, 4),
	)


def test_merge_runs_breaks_on_gap_and_on_value_change() -> None:
	"""Разрыв текста и смена значения разделяют сущности.

	Две ссылки подряд с разными адресами — это две ссылки: склеить их
	значило бы увести читателя не туда.
	"""
	gap = merge_runs([_run(0, 2, (TextStyle.BOLD, "")), _run(5, 2, (TextStyle.BOLD, ""))])
	assert gap == (TextEntity(TextStyle.BOLD, 0, 2), TextEntity(TextStyle.BOLD, 5, 2))
	links = merge_runs(
		[
			_run(0, 3, (TextStyle.LINK, "https://a.example")),
			_run(3, 3, (TextStyle.LINK, "https://b.example")),
		]
	)
	assert links == (
		TextEntity(TextStyle.LINK, 0, 3, "https://a.example"),
		TextEntity(TextStyle.LINK, 3, 3, "https://b.example"),
	)


def _round_trip(rich: RichText) -> RichText:
	document = QTextDocument()
	apply_rich(document, rich)
	return rich_of_document(document)


def test_round_trip_keeps_offsets_on_emoji() -> None:
	"""Эмодзи — две кодовые единицы: разметка после него не должна съехать."""
	rich = RichText(
		"🙂 Тайна и ссылка",
		(
			TextEntity(TextStyle.SPOILER, 3, 5),
			TextEntity(TextStyle.LINK, 11, 6, "https://telegram.org"),
			TextEntity(TextStyle.BOLD, 3, 14),
		),
	)
	back = _round_trip(rich)
	assert back.text == rich.text
	assert set(back.entities) == set(rich.entities)


def test_round_trip_across_paragraphs_and_kinds() -> None:
	"""Абзацы, цитата, моноширинный и блок кода с языком переживают перевод."""
	rich = RichText(
		"первый абзац\nвторой абзац",
		(
			TextEntity(TextStyle.EXPANDABLE_QUOTE, 0, 12),
			TextEntity(TextStyle.CODE, 13, 6),
		),
	)
	assert set(_round_trip(rich).entities) == set(rich.entities)
	pre = RichText("код блока", (TextEntity(TextStyle.PRE, 0, 9, "python"),))
	assert set(_round_trip(pre).entities) == set(pre.entities)


def test_round_trip_of_plain_text() -> None:
	"""Текст без оформления возвращается без единой сущности."""
	assert _round_trip(RichText("без оформления")) == RichText("без оформления")


def test_trimmed_shifts_entities() -> None:
	"""Обрезка пробелов двигает разметку вместе с текстом (ADR-0033).

	Простой ``strip()`` сдвинул бы текст, а смещения оставил на месте —
	оформление наехало бы на чужие буквы.
	"""
	rich = RichText("  жирный хвост  ", (TextEntity(TextStyle.BOLD, 2, 6),))
	assert trimmed(rich) == RichText("жирный хвост", (TextEntity(TextStyle.BOLD, 0, 6),))
	# кусок целиком в обрезанном крае снимается
	edge = RichText("текст  ", (TextEntity(TextStyle.ITALIC, 5, 2),))
	assert trimmed(edge) == RichText("текст")
	assert trimmed(RichText("   ", (TextEntity(TextStyle.BOLD, 0, 3),))) == RichText("")
	same = RichText("ровно", (TextEntity(TextStyle.BOLD, 0, 5),))
	assert trimmed(same) is same
