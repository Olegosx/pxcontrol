"""Общие помощники страниц: привязка обработчиков, диалоги, плашки."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from PySide6.QtWidgets import QHBoxLayout, QLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CardWidget,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBox,
	MessageBoxBase,
	StrongBodyLabel,
	SubtitleLabel,
	TransparentToolButton,
)

_T = TypeVar("_T")


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


def confirm_delete(parent: QWidget, text: str) -> bool:
	"""Спрашивает подтверждение удаления."""
	box = MessageBox("Подтверждение", text, parent.window())
	box.yesButton.setText("Удалить")
	box.cancelButton.setText("Отмена")
	return bool(box.exec())


def show_error(parent: QWidget, message: str) -> None:
	"""Показывает ошибку всплывающей плашкой."""
	InfoBar.error("Ошибка", message, parent=parent, duration=6000)


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
	column.addWidget(StrongBodyLabel(title, card))
	column.addWidget(CaptionLabel(subtitle, card))
	layout.addLayout(column)
	layout.addStretch()
	if trailing is not None:
		layout.addWidget(trailing)
	if on_delete is not None:
		delete_button = TransparentToolButton(FluentIcon.DELETE, card)
		delete_button.clicked.connect(on_delete)
		layout.addWidget(delete_button)
	return card


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
