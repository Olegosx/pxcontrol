"""Общие помощники страниц: привязка обработчиков, диалоги, плашки."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Generic, TypeVar

from PySide6.QtCore import QDate, QTime, Signal
from PySide6.QtWidgets import (
	QDialog,
	QFileDialog,
	QHBoxLayout,
	QLayout,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	ComboBox,
	EditableComboBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBox,
	MessageBoxBase,
	ProgressBar,
	ScrollArea,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TransparentToolButton,
)

_T = TypeVar("_T")

#: Длительность всплывающих плашек с ошибками/предупреждениями (мс).
TOAST_DURATION_MS = 6000

#: Отступы содержимого страницы от краёв окна (слева, сверху, справа, снизу).
PAGE_MARGINS = (28, 24, 28, 24)


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


def page_layout(page: ScrollArea, spacing: int = 16) -> QVBoxLayout:
	"""Каркас прокручиваемой страницы: контейнер с едиными отступами.

	Одна сборка вместо одинаковых семи строк на каждой странице:
	контейнер, отступы :data:`PAGE_MARGINS`, интервал между блоками,
	растяжение по ширине и прозрачный фон (после ``setWidget`` —
	иначе фон контейнера не перекрашивается).

	Returns:
		Компоновка контейнера — страница добавляет в неё содержимое.
	"""
	container = QWidget(page)
	layout = QVBoxLayout(container)
	layout.setContentsMargins(*PAGE_MARGINS)
	layout.setSpacing(spacing)
	page.setWidget(container)
	page.setWidgetResizable(True)
	page.enableTransparentBackground()
	return layout


def clear_layout(layout: QLayout) -> None:
	"""Опустошает компоновку: виджеты, вложенные компоновки, распорки.

	Виджеты удаляются; вложенные компоновки чистятся рекурсивно;
	распорки просто изымаются (владение переходит Python-обёртке,
	сборщик мусора её освобождает).
	"""
	while layout.count():
		item = layout.takeAt(0)
		if item is None:
			break
		widget = item.widget()
		if widget is not None:
			widget.deleteLater()
			continue
		child = item.layout()
		if child is not None:
			clear_layout(child)


def exec_dialog(dialog: QDialog) -> bool:
	"""Показывает модальный диалог и удаляет его после закрытия.

	Диалоги QFluentWidgets не удаляют себя после ``exec()`` (нет
	``WA_DeleteOnClose``) и накапливались бы детьми окна до выхода —
	вместе с содержимым (например, плитками кадров с картинками).
	Все страницы показывают диалоги через эту обёртку.

	Returns:
		True — диалог принят (кнопка подтверждения).
	"""
	accepted = bool(dialog.exec())
	dialog.deleteLater()
	return accepted


def confirm_delete(parent: QWidget, text: str, accept_text: str = "Удалить") -> bool:
	"""Спрашивает подтверждение необратимого действия."""
	box = MessageBox("Подтверждение", text, parent.window())
	box.yesButton.setText(accept_text)
	box.cancelButton.setText("Отмена")
	return exec_dialog(box)


def show_error(parent: QWidget, message: str) -> None:
	"""Показывает ошибку всплывающей плашкой."""
	InfoBar.error("Ошибка", message, parent=parent, duration=TOAST_DURATION_MS)


def error_reporter(parent: QWidget) -> Callable[[str], None]:
	"""Колбэк показа ошибок, привязанный к странице/диалогу.

	Один помощник вместо одинаковых методов ``_show_error`` на каждой
	странице; результат передаётся в ``run_in_engine`` как ``on_error``.
	"""
	return partial(show_error, parent)


def pick_file(
	parent: QWidget, caption: str, file_filter: str, start_dir: str = ""
) -> str | None:
	"""Открывает диалог выбора файла; None — пользователь отменил.

	``start_dir`` — стартовая папка диалога (пусто — на усмотрение Qt).
	"""
	path, _ = QFileDialog.getOpenFileName(parent, caption, start_dir, file_filter)
	return path or None


def pick_dir(parent: QWidget, caption: str, start_dir: str = "") -> str | None:
	"""Открывает диалог выбора папки; None — пользователь отменил."""
	path = QFileDialog.getExistingDirectory(parent, caption, start_dir)
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


class DtoComboBox(ComboBox, Generic[_T]):
	"""Комбобокс списка DTO, хранящий элементы рядом с виджетом.

	Заменяет ручную арифметику «индекс минус служебный пункт» и парные
	списки DTO на страницах — из этой ручной синхронизации вырастали
	ошибки, когда выбор восстанавливался по позиции в изменившемся
	списке и молча указывал на другой элемент.
	"""

	def __init__(self, parent: QWidget, placeholder: str | None = None) -> None:
		"""``placeholder`` — служебный первый пункт («(не выбран)»);
		None — список начинается сразу с элементов."""
		super().__init__(parent)
		self._placeholder = placeholder
		self._dtos: list[_T] = []

	def set_items(
		self,
		items: list[_T],
		label: Callable[[_T], str],
		key: Callable[[_T], object] | None = None,
	) -> None:
		"""Пересобирает список без промежуточных сигналов.

		Сигналы блокируются на время пересборки (первый ``addItem``
		Qt-комбобокса излучает ``currentIndexChanged``) — обработчик
		выбора страница вызывает сама один раз после пересборки.

		Args:
			items: новые элементы списка.
			label: текст пункта для элемента.
			key: идентичность элемента (обычно ``lambda x: x.id``) — по ней
				восстанавливается прежний выбор; None или элемент исчез —
				выбор встаёт на первый пункт.
		"""
		previous = self.selected()
		self.blockSignals(True)
		try:
			self.clear()
			self._dtos = list(items)
			if self._placeholder is not None:
				self.addItem(self._placeholder)
			for item in self._dtos:
				self.addItem(label(item))
			index = 0 if self.count() else -1
			if key is not None and previous is not None:
				wanted = key(previous)
				for position, item in enumerate(self._dtos):
					if key(item) == wanted:
						index = position + self._offset()
						break
			self.setCurrentIndex(index)
		finally:
			self.blockSignals(False)

	def selected(self) -> _T | None:
		"""Выбранный элемент; None — служебный пункт или пустой список."""
		index = int(self.currentIndex()) - self._offset()
		# локальная переменная с типом: базовый класс не типизирован,
		# и чтение атрибута через self даёт Any
		dtos: list[_T] = self._dtos
		if 0 <= index < len(dtos):
			return dtos[index]
		return None

	def select(self, predicate: Callable[[_T], bool]) -> bool:
		"""Выбирает первый подходящий элемент; False — такого нет."""
		for position, item in enumerate(self._dtos):
			if predicate(item):
				self.setCurrentIndex(position + self._offset())
				return True
		return False

	def _offset(self) -> int:
		"""Сдвиг индексов элементов из-за служебного пункта."""
		return 1 if self._placeholder is not None else 0


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


def parse_hhmm(text: str) -> tuple[int, int]:
	"""Разбирает время «ЧЧ:ММ» (часы 0–23, минуты 0–59).

	Returns:
		Пара (часы, минуты).

	Raises:
		ValueError: Формат не «ЧЧ:ММ» или значения вне диапазона.
	"""
	error = ValueError("Время — в формате ЧЧ:ММ, например 18:30.")
	parts = text.strip().split(":")
	if len(parts) != 2 or not all(part.isdigit() for part in parts):
		raise error
	hours, minutes = int(parts[0]), int(parts[1])
	if hours > 23 or minutes > 59:
		raise error
	return hours, minutes


class WhenRow:
	"""Строка «Опубликовать сейчас» + дата и время отложенной записи.

	По умолчанию «сейчас» выключен: посты обычно отложенные. Время —
	редактируемый список (`EditableComboBox`): пункты — стандартные
	времена канала (:func:`set_times`), текст правится вручную («ЧЧ:ММ»).
	"""

	def __init__(self, dialog: QWidget, layout: QVBoxLayout) -> None:
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Опубликовать сейчас", dialog))
		self._now_switch = SwitchButton(dialog)
		self._now_switch.setChecked(False)
		self._now_switch.checkedChanged.connect(self._on_now_toggled)
		row.addWidget(self._now_switch)
		row.addStretch()
		self._date = CalendarPicker(dialog)
		self._date.setDate(QDate.currentDate())
		self._time = EditableComboBox(dialog)
		self._time.setPlaceholderText("ЧЧ:ММ")
		self._time.setText(QTime.currentTime().addSecs(3600).toString("HH:mm"))
		self._time.setMaximumWidth(120)
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

	def set_times(self, times: list[str]) -> None:
		"""Наполняет список стандартными временами канала (первое — выбрано).

		Битые элементы пропускаются; пустой список — текущее время + 1 ч.
		Если выбранное время сегодня уже прошло — дата переставляется
		на завтра (пользователь видит это в календаре).
		"""
		valid: list[str] = []
		for item in times:
			try:
				parse_hhmm(str(item))
			except ValueError:
				continue
			valid.append(str(item).strip())
		self._time.clear()
		self._time.addItems(valid)
		if valid:
			self._time.setCurrentIndex(0)
			self._time.setText(valid[0])
		else:
			self._time.setCurrentIndex(-1)
			self._time.setText(
				QTime.currentTime().addSecs(3600).toString("HH:mm")
			)
		self._adjust_date()

	def _adjust_date(self) -> None:
		"""Сегодняшнее прошедшее время переносит дату на завтра."""
		try:
			hours, minutes = parse_hhmm(str(self._time.text()))
		except ValueError:
			return
		today = QDate.currentDate()
		if self._date.getDate() > today:
			return  # дата уже выбрана вперёд — не трогаем
		passed = QTime(hours, minutes) <= QTime.currentTime()
		self._date.setDate(today.addDays(1) if passed else today)

	def when(self) -> datetime | None:
		"""None — «сейчас», иначе выбранный момент (в UTC).

		Raises:
			ValueError: Время не в формате «ЧЧ:ММ».
		"""
		if self._now_switch.isChecked():
			return None
		hours, minutes = parse_hhmm(str(self._time.text()))
		date = self._date.getDate()
		local = datetime(date.year(), date.month(), date.day(), hours, minutes)
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
