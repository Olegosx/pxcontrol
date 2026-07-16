"""Тесты разбора времени публикации «ЧЧ:ММ» (чистая функция, без Qt)."""

from __future__ import annotations

import pytest

from pxcontrol.ui.pages.common import parse_hhmm


def test_parse_hhmm_valid() -> None:
	"""Валидные формы: «Ч:ММ», «ЧЧ:ММ», крайние значения, пробелы по краям."""
	assert parse_hhmm("9:05") == (9, 5)
	assert parse_hhmm("18:30") == (18, 30)
	assert parse_hhmm("00:00") == (0, 0)
	assert parse_hhmm("23:59") == (23, 59)
	assert parse_hhmm("  10:00  ") == (10, 0)


@pytest.mark.parametrize(
	"text",
	["", "10", "10:", ":30", "25:00", "10:60", "10-30", "aa:bb", "10:00:00", "-1:05"],
)
def test_parse_hhmm_invalid(text: str) -> None:
	"""Мусор и значения вне диапазона — понятная ошибка формата."""
	with pytest.raises(ValueError, match="ЧЧ:ММ"):
		parse_hhmm(text)
