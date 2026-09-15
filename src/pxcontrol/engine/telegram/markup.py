"""Клавиатура под постом: типы кнопок, пределы Telegram, проверка (ADR-0031).

Кнопки — отдельное поле сообщения, а не часть текста, и поставить их
может только бот. Этот модуль — о самой клавиатуре: из чего она состоит,
какие у неё пределы и как она переживает хранение в БД. К транспортам
он не привязан: перевод в формат Bot API — дело шлюза.

Пределы, кроме копируемого текста, в документации Telegram **не
объявлены** и вычислены опытом 15.09.2026
(`docs/03-modules/telegram-buttons.md`): сервер не отказывает, а молча
отбрасывает лишние кнопки и обрезает подписи. Поэтому проверка — наша
обязанность, и до отправки: иначе человек увидит в канале не ту
клавиатуру, которую собрал, и ошибки нигде не будет.

Виды кнопок ограничены сознательно (ADR-0031, п. 13): ссылка
и «скопировать текст». Кнопка с обратным вызовом требует ответа бота
в ту же секунду, а приложение запускается по надобности — читатель видел
бы вечное ожидание.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.types import telegram_text_length

logger = logging.getLogger(__name__)

#: Кнопок в одном ряду. Опыт: девятую сервер молча отбрасывает.
MAX_BUTTONS_IN_ROW = 8

#: Рядов в клавиатуре. Опыт: сто первый ряд сервер молча отбрасывает.
MAX_ROWS = 100

#: Длина подписи кнопки. Опыт: длиннее сервер молча обрезает.
BUTTON_TEXT_LIMIT = 128

#: Длина копируемого текста (документация Bot API: 1–256). Единственный
#: предел клавиатуры, который сервер проверяет сам и отказывает явно
#: (ошибка ``BUTTON_COPY_TEXT_INVALID``).
COPY_TEXT_LIMIT = 256

#: Схемы адресов, которые Telegram принимает у кнопки-ссылки. Прочие
#: он отвергает ошибкой ``BUTTON_URL_INVALID``, поэтому отсекаем сами —
#: с понятным текстом вместо «Telegram отклонил операцию».
ALLOWED_URL_SCHEMES = ("https://", "http://", "tg://")


class MarkupError(EngineError):
	"""Клавиатура не годится для отправки (текст — для человека)."""


class ButtonKind(StrEnum):
	"""Вид кнопки под постом."""

	LINK = "link"  # открывает ссылку
	COPY = "copy"  # копирует заданный текст в буфер обмена


@dataclass(frozen=True)
class PostButton:
	"""Кнопка под постом.

	Attributes:
		kind: вид кнопки.
		text: подпись на кнопке (её видит читатель).
		value: адрес для кнопки-ссылки или копируемый текст для кнопки
			«скопировать» — смысл задаёт ``kind``.
	"""

	kind: ButtonKind
	text: str
	value: str


@dataclass(frozen=True)
class PostMarkup:
	"""Клавиатура поста: ряды кнопок сверху вниз.

	Пустая клавиатура (``rows`` без кнопок) означает «кнопок нет»
	и в ложном контексте ведёт себя как ложь — так вызывающим коду
	не нужно различать None и пустоту.
	"""

	rows: tuple[tuple[PostButton, ...], ...] = ()

	def __bool__(self) -> bool:
		"""Есть ли в клавиатуре хоть одна кнопка."""
		return any(row for row in self.rows)

	@property
	def buttons(self) -> tuple[PostButton, ...]:
		"""Все кнопки одним списком (для показа и подсчёта)."""
		return tuple(button for row in self.rows for button in row)


def validate_markup(markup: PostMarkup) -> None:
	"""Проверяет клавиатуру по пределам Telegram.

	Проверка обязательна до отправки: лишние кнопки и длинные подписи
	сервер отбрасывает и обрезает молча (см. модуль). Пустая клавиатура
	проверку проходит — это «кнопок нет», а не ошибка.

	Raises:
		MarkupError: Клавиатура не пройдёт целиком — с указанием, что
			именно поправить.
	"""
	if not markup:
		return
	rows = [row for row in markup.rows if row]
	if len(rows) > MAX_ROWS:
		raise MarkupError(
			f"Слишком много рядов кнопок: {len(rows)}, предел Telegram — {MAX_ROWS}. "
			"Лишние он отбросит молча."
		)
	for number, row in enumerate(rows, start=1):
		if len(row) > MAX_BUTTONS_IN_ROW:
			raise MarkupError(
				f"В ряду {number} кнопок {len(row)}, предел Telegram — "
				f"{MAX_BUTTONS_IN_ROW}. Лишние он отбросит молча."
			)
		for button in row:
			_validate_button(button, number)


def _validate_button(button: PostButton, row_number: int) -> None:
	"""Проверяет одну кнопку (подпись и значение по её виду).

	Raises:
		MarkupError: Подпись пуста или длинна, адрес или копируемый текст
			не годятся.
	"""
	where = f"ряд {row_number}, кнопка «{button.text[:20]}»"
	if not button.text.strip():
		raise MarkupError(f"У кнопки нет подписи ({where}) — читатель увидит пустую кнопку.")
	length = telegram_text_length(button.text)
	if length > BUTTON_TEXT_LIMIT:
		raise MarkupError(
			f"Подпись кнопки длиннее {BUTTON_TEXT_LIMIT} символов ({where}, сейчас "
			f"{length}) — Telegram обрежет её молча."
		)
	value = button.value.strip()
	if not value:
		raise MarkupError(
			f"У кнопки не задано значение ({where}): нужен адрес ссылки или текст для копирования."
		)
	if button.kind is ButtonKind.LINK:
		if not value.lower().startswith(ALLOWED_URL_SCHEMES):
			raise MarkupError(
				f"Адрес кнопки должен начинаться с https://, http:// или tg:// ({where})."
			)
	elif telegram_text_length(value) > COPY_TEXT_LIMIT:
		raise MarkupError(
			f"Текст для копирования длиннее {COPY_TEXT_LIMIT} символов ({where}) — "
			"Telegram такую кнопку не примет."
		)


def markup_to_json(markup: PostMarkup | None) -> list[list[dict[str, str]]] | None:
	"""Переводит клавиатуру в JSON для колонки БД (None — кнопок нет).

	Пустая клавиатура тоже даёт None: в базе не должно быть записи,
	означающей «кнопок нет» двумя способами.
	"""
	if markup is None or not markup:
		return None
	return [
		[{"kind": str(button.kind), "text": button.text, "value": button.value} for button in row]
		for row in markup.rows
		if row
	]


def markup_from_json(raw: Any) -> PostMarkup | None:
	"""Собирает клавиатуру из значения колонки БД.

	Повреждённая запись (чужой формат, неизвестный вид кнопки) — не повод
	ронять восстановление очереди: такой пост поедет без кнопок, а разбор
	останется в журнале. Молчать нельзя, но и падать здесь нечестно:
	пост уже принят, а кнопки — его украшение.

	Returns:
		Клавиатура или None, если её нет (или запись не разобралась).
	"""
	if raw is None:
		return None
	try:
		rows = tuple(
			tuple(
				PostButton(ButtonKind(item["kind"]), str(item["text"]), str(item["value"]))
				for item in row
			)
			for row in raw
		)
	except (TypeError, ValueError, KeyError):
		logger.warning("Клавиатура в базе не разобралась — пост пойдёт без кнопок.", exc_info=True)
		return None
	markup = PostMarkup(rows)
	return markup if markup else None
