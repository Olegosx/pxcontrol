"""Тесты клавиатуры под постом: пределы и хранение (ADR-0031).

Пределы Telegram не заявлены в документации и вычислены опытом: сервер
не отказывает, а молча отбрасывает лишние кнопки и обрезает подписи.
Значит эти тесты — замок на нашей проверке: пока они зелёные, приложение
не отправит клавиатуру, которая в канале окажется другой.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.telegram.markup import (
	BUTTON_TEXT_LIMIT,
	COPY_TEXT_LIMIT,
	MAX_BUTTONS_IN_ROW,
	MAX_ROWS,
	ButtonKind,
	MarkupError,
	PostButton,
	PostMarkup,
	markup_from_json,
	markup_to_json,
	validate_markup,
)


def link(text: str = "Смотреть", url: str = "https://telegram.org") -> PostButton:
	"""Кнопка-ссылка для проб."""
	return PostButton(ButtonKind.LINK, text, url)


def copy(text: str = "Скопировать", value: str = "PROMO") -> PostButton:
	"""Кнопка «скопировать текст» для проб."""
	return PostButton(ButtonKind.COPY, text, value)


def test_empty_markup_is_falsy_and_valid() -> None:
	"""Пустая клавиатура — это «кнопок нет», а не ошибка."""
	markup = PostMarkup()
	assert not markup
	assert markup.buttons == ()
	validate_markup(markup)  # не бросает
	# ряд без кнопок — тоже пустота: различать две пустоты вызывающим незачем
	assert not PostMarkup(((),))


def test_buttons_flattens_rows() -> None:
	"""Все кнопки одним списком — в порядке рядов."""
	markup = PostMarkup(((link("1"), link("2")), (copy("3"),)))
	assert [button.text for button in markup.buttons] == ["1", "2", "3"]
	assert bool(markup)


def test_full_row_passes_and_extra_button_fails() -> None:
	"""Восемь кнопок в ряду проходят, девятая — нет (сервер её отбросит)."""
	validate_markup(PostMarkup((tuple(link(f"к{i}") for i in range(MAX_BUTTONS_IN_ROW)),)))
	with pytest.raises(MarkupError, match=f"предел Telegram — {MAX_BUTTONS_IN_ROW}"):
		validate_markup(PostMarkup((tuple(link(f"к{i}") for i in range(MAX_BUTTONS_IN_ROW + 1)),)))


def test_row_limit() -> None:
	"""Сто рядов проходят, сто первый — нет."""
	rows = tuple((link(f"р{i}"),) for i in range(MAX_ROWS))
	validate_markup(PostMarkup(rows))
	with pytest.raises(MarkupError, match=f"предел Telegram — {MAX_ROWS}"):
		validate_markup(PostMarkup((*rows, (link("лишний"),))))


def test_button_text_limit_counted_in_utf16() -> None:
	"""Подпись длиннее предела не проходит; счёт — как у Telegram (UTF-16)."""
	validate_markup(PostMarkup(((link("Д" * BUTTON_TEXT_LIMIT),),)))
	with pytest.raises(MarkupError, match="обрежет её молча"):
		validate_markup(PostMarkup(((link("Д" * (BUTTON_TEXT_LIMIT + 1)),),)))
	# эмодзи — за два кодовых пункта, как считает сам Telegram
	with pytest.raises(MarkupError, match="обрежет её молча"):
		validate_markup(PostMarkup(((link("🙂" * (BUTTON_TEXT_LIMIT // 2 + 1)),),)))


def test_empty_text_and_value_rejected() -> None:
	"""Кнопка без подписи или без значения бессмысленна."""
	with pytest.raises(MarkupError, match="нет подписи"):
		validate_markup(PostMarkup(((link("   "),),)))
	with pytest.raises(MarkupError, match="не задано значение"):
		validate_markup(PostMarkup(((link("Есть подпись", "  "),),)))


@pytest.mark.parametrize("url", ["https://telegram.org", "http://example.com", "tg://resolve?x=1"])
def test_allowed_url_schemes(url: str) -> None:
	"""Три схемы, которые Telegram принимает у кнопки-ссылки."""
	validate_markup(PostMarkup(((link("Открыть", url),),)))


@pytest.mark.parametrize("url", ["telegram.org", "ftp://files", "javascript:alert(1)"])
def test_bad_url_rejected(url: str) -> None:
	"""Прочие адреса отсекаем сами — иначе Telegram ответит непонятным отказом."""
	with pytest.raises(MarkupError, match="должен начинаться с https://"):
		validate_markup(PostMarkup(((link("Открыть", url),),)))


def test_copy_text_limit() -> None:
	"""Копируемый текст длиннее 256 символов Telegram не примет."""
	validate_markup(PostMarkup(((copy("Код", "Ц" * COPY_TEXT_LIMIT),),)))
	with pytest.raises(MarkupError, match="Текст для копирования"):
		validate_markup(PostMarkup(((copy("Код", "Ц" * (COPY_TEXT_LIMIT + 1)),),)))


def test_json_round_trip() -> None:
	"""Клавиатура переживает хранение в БД без потерь."""
	markup = PostMarkup(((link("Смотреть"), copy("Код", "PROMO-2026")), (link("Ещё"),)))
	restored = markup_from_json(markup_to_json(markup))
	assert restored == markup


def test_json_of_nothing_is_none() -> None:
	"""Отсутствие кнопок хранится одним способом — NULL."""
	assert markup_to_json(None) is None
	assert markup_to_json(PostMarkup()) is None
	assert markup_to_json(PostMarkup(((),))) is None
	assert markup_from_json(None) is None
	assert markup_from_json([]) is None


@pytest.mark.parametrize(
	"raw",
	[
		"вовсе не клавиатура",
		[[{"kind": "callback", "text": "Нажми", "value": "x"}]],  # неизвестный вид кнопки
		[[{"text": "без вида", "value": "x"}]],
		[[None]],
	],
)
def test_broken_row_gives_no_markup(raw: object) -> None:
	"""Повреждённая запись не роняет чтение: пост пойдёт без кнопок.

	Падать здесь нечестно — пост уже принят в очередь, а кнопки его
	украшение; причина остаётся в журнале.
	"""
	assert markup_from_json(raw) is None
