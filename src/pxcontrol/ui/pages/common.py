"""Общие помощники страниц: привязка обработчиков, диалоги, плашки."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import TypeVar

from PySide6.QtCore import QDate, QTime, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBox,
	MessageBoxBase,
	ProgressBar,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TimePicker,
	TransparentToolButton,
)

_T = TypeVar("_T")

#: Длительность всплывающих плашек с ошибками/предупреждениями (мс).
TOAST_DURATION_MS = 6000


def noop(*_args: object) -> None:
	"""Пустой колбэк для операций, результат которых не нужен интерфейсу."""


def bind(action: Callable[[_T], None], item: _T) -> Callable[[], None]:
	"""Ранняя привязка элемента к обработчику (замена lambda в цикле).

	Обычная lambda в цикле захватывает переменную, а не значение, и все
	обработчики получили бы последний элемент списка.
	"""
	def handler() -> None:
		action(item)

	return handler


def clear_layout(layout: QLayout) -> None:
	"""Удаляет все виджеты из компоновки."""
	while layout.count():
		item = layout.takeAt(0)
		if item is None:
			break
		widget = item.widget()
		if widget is not None:
			widget.deleteLater()


def confirm_delete(parent: QWidget, text: str, accept_text: str = "Удалить") -> bool:
	"""Спрашивает подтверждение необратимого действия."""
	box = MessageBox("Подтверждение", text, parent.window())
	box.yesButton.setText(accept_text)
	box.cancelButton.setText("Отмена")
	return bool(box.exec())


def show_error(parent: QWidget, message: str) -> None:
	"""Показывает ошибку всплывающей плашкой."""
	InfoBar.error("Ошибка", message, parent=parent, duration=TOAST_DURATION_MS)


def error_reporter(parent: QWidget) -> Callable[[str], None]:
	"""Колбэк показа ошибок, привязанный к странице/диалогу.

	Один помощник вместо одинаковых методов ``_show_error`` на каждой
	странице; результат передаётся в ``run_in_engine`` как ``on_error``.
	"""
	return partial(show_error, parent)


def pick_file(parent: QWidget, caption: str, file_filter: str) -> str | None:
	"""Открывает диалог выбора файла; None — пользователь отменил."""
	path, _ = QFileDialog.getOpenFileName(parent, caption, "", file_filter)
	return path or None


def row_card(
	parent: QWidget,
	title: str,
	subtitle: str,
	trailing: QWidget | None = None,
	on_delete: Callable[[], None] | None = None,
) -> CardWidget:
	"""Карточка-строка списка: название, подпись, хвостовые элементы.

	Единый вид строк на всех страницах (аккаунты, каналы, расписание).
	"""
	card = CardWidget(parent)
	layout = QHBoxLayout(card)
	layout.setContentsMargins(16, 10, 10, 10)
	column = QVBoxLayout()
	column.setSpacing(2)
	# перенос строк: длинный текст (имя файла и т.п.) не должен
	# распирать карточку и уводить элементы за пределы окна
	title_label = StrongBodyLabel(title, card)
	title_label.setWordWrap(True)
	subtitle_label = CaptionLabel(subtitle, card)
	subtitle_label.setWordWrap(True)
	column.addWidget(title_label)
	column.addWidget(subtitle_label)
	layout.addLayout(column, stretch=1)
	if trailing is not None:
		layout.addWidget(trailing)
	if on_delete is not None:
		delete_button = TransparentToolButton(FluentIcon.DELETE, card)
		delete_button.clicked.connect(on_delete)
		layout.addWidget(delete_button)
	return card


class ProgressPanel(QWidget):
	"""Полоса прогресса с подписью и мостом из потока движка.

	``emit_progress`` передаётся колбэком в движок: сигнал Qt доставляет
	долю готовности (0.0..1.0) в поток интерфейса. Используется страницами
	«Видео» (кодирование) и «Публикация» (загрузка файла).
	"""

	_progressed = Signal(float)

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self._bar = ProgressBar(self)
		self._bar.setRange(0, 100)
		self._label = CaptionLabel("", self)
		layout.addWidget(self._bar)
		layout.addWidget(self._label)
		self._prefix = ""
		self._progressed.connect(self._on_progress)
		self.hide()

	@property
	def emit_progress(self) -> Callable[[float], None]:
		"""Колбэк для движка (безопасен к вызову из другого потока)."""
		return self._progressed.emit

	def begin(self, prefix: str, note: str = "") -> None:
		"""Показывает панель: префикс для процентов и стартовая подпись."""
		self._prefix = prefix
		self._bar.setValue(0)
		self._label.setText(note or f"{prefix}…")
		self.show()

	def finish(self) -> None:
		"""Прячет панель."""
		self.hide()

	def _on_progress(self, fraction: float) -> None:
		percent = int(fraction * 100)
		self._bar.setValue(percent)
		self._label.setText(f"{self._prefix}: {percent}%")


class WhenRow:
	"""Строка «Опубликовать сейчас» + скрываемые дата и время.

	Общий блок диалогов публикации (пост, видео): переключатель «сейчас»
	и выбор момента для отложенной записи.
	"""

	def __init__(self, dialog: QWidget, layout: QVBoxLayout) -> None:
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Опубликовать сейчас", dialog))
		self._now_switch = SwitchButton(dialog)
		self._now_switch.setChecked(True)
		self._now_switch.checkedChanged.connect(self._on_now_toggled)
		row.addWidget(self._now_switch)
		row.addStretch()
		self._date = CalendarPicker(dialog)
		self._date.setDate(QDate.currentDate())
		self._time = TimePicker(dialog)
		self._time.setTime(QTime.currentTime().addSecs(3600))
		self._date.hide()
		self._time.hide()
		row.addWidget(self._date)
		row.addWidget(self._time)
		layout.addLayout(row)

	def _on_now_toggled(self, now: bool) -> None:
		self._date.setVisible(not now)
		self._time.setVisible(not now)

	def set_schedule_allowed(self, allowed: bool, hint: str = "") -> None:
		"""Разрешает/запрещает отложенную публикацию (иначе — только «сейчас»)."""
		if not allowed:
			self._now_switch.setChecked(True)
		self._now_switch.setEnabled(allowed)
		self._now_switch.setToolTip("" if allowed else hint)

	def when(self) -> datetime | None:
		"""None — «сейчас», иначе выбранный момент (в UTC)."""
		if self._now_switch.isChecked():
			return None
		date, time = self._date.getDate(), self._time.getTime()
		local = datetime(
			date.year(), date.month(), date.day(), time.hour(), time.minute()
		)
		return local.astimezone(UTC)


class FormDialog(MessageBoxBase):
	"""Диалог с набором текстовых полей."""

	def __init__(
		self,
		title: str,
		fields: list[tuple[str, str]],
		parent: QWidget,
		accept_text: str = "Добавить",
		password_fields: tuple[str, ...] = (),
	) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel(title, self))
		self._edits: dict[str, LineEdit] = {}
		for key, placeholder in fields:
			edit = LineEdit(self)
			edit.setPlaceholderText(placeholder)
			edit.setClearButtonEnabled(True)
			if key in password_fields:
				edit.setEchoMode(LineEdit.EchoMode.Password)
			self.viewLayout.addWidget(edit)
			self._edits[key] = edit
		self.yesButton.setText(accept_text)
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def value(self, key: str) -> str:
		"""Возвращает введённый текст поля без крайних пробелов."""
		return str(self._edits[key].text()).strip()
