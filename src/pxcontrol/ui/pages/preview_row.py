"""Ряд настроек превью ссылки у текстового поста (ADR-0033, подача C3).

Превью Telegram собирает сам по первой ссылке поста. Управлять им можно
тремя способами, и все три здесь: не показывать вовсе, показать крупно,
поставить над текстом.

Ряд виден только у поста **без вложения**: место превью там занято
файлом, и предлагать настройку, которая ничего не изменит, нечестно.
Крупное превью и превью над текстом строятся по конкретному адресу
(так устроен запрос MTProto), поэтому без ссылки в тексте они
недоступны — подпись ряда говорит об этом прямо, а не гасит галочки
молча.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CheckBox

from pxcontrol.engine.telegram.rich_text import RichText, first_link
from pxcontrol.engine.telegram.types import LinkPreview


def preview_hint(rich: RichText, with_media: bool) -> str:
	"""Подсказка ряда: что сейчас можно, а что нет и почему.

	Чистая функция — правило показа проверяется тестом.
	"""
	if with_media:
		return "У поста с вложением превью не бывает — место занято файлом."
	link = first_link(rich)
	if not link:
		return "Добавьте ссылку в текст — крупное превью и превью над текстом строятся по ней."
	return f"Превью по ссылке: {link}"


class PreviewRow(QWidget):
	"""Три галочки превью: выключить, крупное, над текстом."""

	#: Настройки изменились (форма пересобирает подсказку).
	changed = Signal()

	def __init__(self, parent: QWidget, layout: QVBoxLayout) -> None:
		super().__init__(parent)
		layout.addWidget(self)
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(2)
		row = QHBoxLayout()
		self._off = CheckBox("Без превью", self)
		self._off.setToolTip("Не показывать превью ссылки под постом")
		self._large = CheckBox("Крупное превью", self)
		self._large.setToolTip("Показать превью большой картинкой")
		self._above = CheckBox("Превью над текстом", self)
		self._above.setToolTip("Поставить превью выше текста поста")
		for check in (self._off, self._large, self._above):
			check.stateChanged.connect(self._on_changed)
			row.addWidget(check)
		row.addStretch()
		box.addLayout(row)
		self._hint = CaptionLabel("", self)
		self._hint.setWordWrap(True)
		box.addWidget(self._hint)

	def preview(self) -> LinkPreview:
		"""Выбранные настройки превью."""
		return LinkPreview(
			disabled=self._off.isChecked(),
			large=self._large.isChecked(),
			above=self._above.isChecked(),
		)

	def set_preview(self, preview: LinkPreview) -> None:
		"""Показывает сохранённые настройки (правка поста в очереди)."""
		for check, value in (
			(self._off, preview.disabled),
			(self._large, preview.large),
			(self._above, preview.above),
		):
			check.blockSignals(True)
			check.setChecked(value)
			check.blockSignals(False)
		self._on_changed()

	def refresh(self, rich: RichText, with_media: bool) -> None:
		"""Приводит ряд к состоянию поста: виден ли он и что в подсказке."""
		self.setVisible(not with_media)
		self._hint.setText(preview_hint(rich, with_media))
		has_link = bool(first_link(rich))
		# выключенное превью делает остальные настройки бессмысленными
		for check in (self._large, self._above):
			check.setEnabled(has_link and not self._off.isChecked())

	def _on_changed(self) -> None:
		self.changed.emit()
