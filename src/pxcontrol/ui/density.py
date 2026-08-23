"""Плотность интерфейса: отступы вёрстки, высота полей, масштаб шрифта.

Значения читаются из настроек один раз при запуске (``app._run_qt``)
до создания первого виджета — вёрстка строится один раз, поэтому смена
настроек действует после перезапуска приложения. При штатных значениях
(обычные отступы, высота 33, шрифт 14) модуль ничего не переопределяет:
поведение приложения совпадает с библиотечным один в один.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Штатные значения QFluentWidgets (проверено на версии 1.11.3).
STOCK_CONTROL_HEIGHT = 33
STOCK_FONT_SIZE = 14

#: Пределы разумного: битое значение из БД зажимается, а не ломает вёрстку.
#: Нижняя граница высоты — кнопка очистки поля (25 пикс) должна помещаться.
CONTROL_HEIGHT_RANGE = (24, 40)
FONT_SIZE_RANGE = (11, 18)


@dataclass(frozen=True)
class Spacing:
	"""Набор отступов вёрстки (все значения — в пикселях)."""

	#: Поля содержимого страницы от краёв окна (слева, сверху, справа, снизу).
	page_margins: tuple[int, int, int, int]
	#: Интервал между блоками страницы.
	block_spacing: int
	#: Интервал разделов разреженных страниц (аккаунты).
	wide_spacing: int
	#: Интервал карточек в списках (каналы, очереди, готовые видео).
	list_spacing: int
	#: Интервал строк внутри панелей и форм.
	row_spacing: int
	#: Поля карточки-строки списка (row_card).
	card_margins: tuple[int, int, int, int]
	#: Поля тела карточки формы параметров (под шапкой).
	card_body_margins: tuple[int, int, int, int]
	#: Интервал строк в теле карточки формы.
	card_body_spacing: int


NORMAL = Spacing(
	page_margins=(28, 24, 28, 24),
	block_spacing=16,
	wide_spacing=24,
	list_spacing=8,
	row_spacing=12,
	card_margins=(16, 10, 10, 10),
	card_body_margins=(16, 0, 16, 12),
	card_body_spacing=10,
)
COMPACT = Spacing(
	page_margins=(20, 16, 20, 16),
	block_spacing=10,
	wide_spacing=16,
	list_spacing=4,
	row_spacing=8,
	card_margins=(12, 6, 8, 6),
	card_body_margins=(12, 0, 12, 8),
	card_body_spacing=8,
)

_spacing: Spacing = NORMAL
_control_height: int = STOCK_CONTROL_HEIGHT
_font_size: int = STOCK_FONT_SIZE


def init(compact: bool, control_height: int, font_size: int) -> None:
	"""Запоминает плотность на этот запуск (до создания виджетов).

	Числа зажимаются в допустимые пределы: реестр настроек проверяет
	только тип значения, а битое число не должно ломать вёрстку.
	"""
	global _spacing, _control_height, _font_size
	_spacing = COMPACT if compact else NORMAL
	_control_height = _clamp(control_height, CONTROL_HEIGHT_RANGE)
	_font_size = _clamp(font_size, FONT_SIZE_RANGE)


def spacing() -> Spacing:
	"""Текущий набор отступов (обычный, пока ``init`` не сказал иное)."""
	return _spacing


def apply_widget_metrics() -> None:
	"""Переопределяет зашитые размеры QFluentWidgets по настройкам.

	Вызывается один раз после :func:`init` и до создания первого
	виджета. Библиотека жёстко задаёт высоту 33 в конструкторах
	``LineEdit`` и ``SpinBoxBase``, а размеры шрифтов раздаёт функцией
	``common.font.getFont`` (проверено на 1.11.3, версия закреплена
	в зависимостях). При штатных значениях настроек ничего
	не переопределяется — этот вызов пуст.
	"""
	if _control_height != STOCK_CONTROL_HEIGHT:
		_patch_control_height(_control_height)
	if _font_size != STOCK_FONT_SIZE:
		_patch_font_scale(_font_size / STOCK_FONT_SIZE)


def _clamp(value: int, bounds: tuple[int, int]) -> int:
	"""Зажимает значение в границы (включительно)."""
	low, high = bounds
	return max(low, min(high, value))


def _patch_control_height(height: int) -> None:
	"""Высота полей: после конструктора ставим свою фикс-высоту.

	Конструкторы обоих классов заканчиваются ``setFixedHeight(33)`` —
	обёртка просто ставит высоту заново; наследники (``EditableComboBox``,
	``SpinBox``/``DoubleSpinBox`` и родня) получают её автоматически.
	"""
	from qfluentwidgets import LineEdit
	from qfluentwidgets.components.widgets.spin_box import SpinBoxBase

	for cls in (LineEdit, SpinBoxBase):
		original: Any = cls.__init__

		def patched(self: Any, *args: Any, _original: Any = original, **kwargs: Any) -> None:
			_original(self, *args, **kwargs)
			self.setFixedHeight(height)

		cls.__init__ = patched
	logger.info("Высота полей ввода переопределена: %d пикс.", height)


def _patch_font_scale(scale: float) -> None:
	"""Шрифты: ``getFont`` библиотеки выдаёт масштабированный размер.

	Все виджеты библиотеки берут шрифт через ``common.font.getFont`` —
	напрямую или через ``setFont`` (та зовёт ``getFont`` из своего же
	модуля, так что патч её накрывает). Модули, привязавшие имя через
	``from … import getFont``, держат собственную ссылку — они
	обходятся по ``sys.modules`` с заменой только исходной функции.
	"""
	from PySide6.QtGui import QFont
	from qfluentwidgets.common import font as qfw_font

	original = qfw_font.getFont

	def scaled(fontSize: int = STOCK_FONT_SIZE, weight: Any = QFont.Weight.Normal) -> QFont:  # noqa: N803 — API библиотеки
		font: QFont = original(max(1, round(fontSize * scale)), weight)
		return font

	qfw_font.getFont = scaled
	for name, module in list(sys.modules.items()):
		if name.startswith("qfluentwidgets") and getattr(module, "getFont", None) is original:
			setattr(module, "getFont", scaled)  # noqa: B010 — имя атрибута динамическое по смыслу
	logger.info("Шрифты масштабированы: базовый размер %d пикс.", _font_size)
