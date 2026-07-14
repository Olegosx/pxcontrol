"""Диалоги подписей: сборка по шаблону и настройка полей/шаблонов канала.

Сборка (`CaptionDialog`) работает на уже загруженных данных и ничего
не тянет из движка; настройка (`FieldsDialog`) выполняет CRUD через
`run_in_engine` прямо из диалога.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QHBoxLayout,
	QListWidget,
	QListWidgetItem,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	ComboBox,
	EditableComboBox,
	LineEdit,
	MessageBoxBase,
	PushButton,
	SubtitleLabel,
	SwitchButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import (
	CaptionLine,
	FieldDto,
	TemplateDto,
	TemplateFieldDto,
	build_caption,
)
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import bind, clear_layout, show_error


class _FieldRow:
	"""Строка поля в диалоге сборки: включённость и ввод значений."""

	def __init__(
		self, dialog: QWidget, layout: QVBoxLayout, tf: TemplateFieldDto
	) -> None:
		self.field = tf.field
		row = QHBoxLayout()
		self.check = CheckBox(self.field.name, dialog)
		self.check.setChecked(tf.enabled)
		self.check.setMinimumWidth(140)
		row.addWidget(self.check)
		if self.field.multiple:
			self._build_multi(dialog, row)
		else:
			self._build_single(dialog, row)
		layout.addLayout(row)

	def _build_single(self, dialog: QWidget, row: QHBoxLayout) -> None:
		"""Одно значение: редактируемый список со словарём."""
		self._edit = EditableComboBox(dialog)
		self._edit.addItems(self.field.values)
		self._edit.setCurrentIndex(-1)
		row.addWidget(self._edit, stretch=1)

	def _build_multi(self, dialog: QWidget, row: QHBoxLayout) -> None:
		"""Несколько значений: строка через запятую + добавление из словаря."""
		self._line = LineEdit(dialog)
		self._line.setPlaceholderText("значения через запятую…")
		row.addWidget(self._line, stretch=1)
		picker = ComboBox(dialog)
		picker.addItems(["— из словаря —", *self.field.values])
		picker.activated.connect(partial(self._pick, picker))
		row.addWidget(picker)

	def _pick(self, picker: ComboBox, index: int) -> None:
		"""Дополняет строку выбранным из словаря значением (без дублей)."""
		if index <= 0:
			return
		value = str(picker.itemText(index))
		current = self.values()
		if value not in current:
			self._line.setText(", ".join([*current, value]))
		picker.setCurrentIndex(0)

	def values(self) -> list[str]:
		"""Введённые значения (пустые отбрасываются)."""
		if self.field.multiple:
			raw = str(self._line.text()).split(",")
		else:
			raw = [str(self._edit.currentText())]
		return [v.strip() for v in raw if v.strip()]


class CaptionDialog(MessageBoxBase):
	"""Сборка подписи: шаблон, название, поля со словарями."""

	def __init__(
		self, templates: list[TemplateDto], suggested_title: str, parent: QWidget
	) -> None:
		super().__init__(parent)
		self._templates = templates
		self._rows: list[_FieldRow] = []
		self.viewLayout.addWidget(SubtitleLabel("Собрать подпись", self))
		self._build_template_combo()
		self._title = LineEdit(self)
		self._title.setPlaceholderText("Название (первой строкой, жирным)…")
		self._title.setText(suggested_title)
		self.viewLayout.addWidget(self._title)
		self._fields_box = QVBoxLayout()
		self.viewLayout.addLayout(self._fields_box)
		# индекс и перерисовка — после сборки формы, сигнал подключаем последним
		index = self._last_used_index()
		self._combo.setCurrentIndex(index)
		self._show_template(index)
		self._combo.currentIndexChanged.connect(self._show_template)
		self.yesButton.setText("Вставить в подпись")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(560)

	def _build_template_combo(self) -> None:
		"""Выбор шаблона (скрыт, если шаблон один)."""
		self._combo = ComboBox(self)
		for template in self._templates:
			self._combo.addItem(template.name)
		if len(self._templates) < 2:
			self._combo.hide()
		self.viewLayout.addWidget(self._combo)

	def _last_used_index(self) -> int:
		"""Индекс последнего использованного шаблона (или первого)."""
		stamps = [
			(t.last_used_at, i) for i, t in enumerate(self._templates)
			if t.last_used_at is not None
		]
		return max(stamps)[1] if stamps else 0

	def _show_template(self, index: int) -> None:
		"""Перестраивает строки полей под выбранный шаблон."""
		clear_layout(self._fields_box)
		self._rows = [
			_FieldRow(self, self._fields_box, tf)
			for tf in self._templates[index].fields
		]

	def template_id(self) -> int:
		"""Идентификатор выбранного шаблона."""
		return self._templates[int(self._combo.currentIndex())].id

	def caption(self) -> str:
		"""Собранный текст подписи (только включённые поля)."""
		lines = [
			CaptionLine(row.field.name, row.field.hashtag, row.values())
			for row in self._rows if row.check.isChecked()
		]
		return build_caption(str(self._title.text()), lines)

	def used_values(self) -> dict[int, list[str]]:
		"""Значения по полям — для автопополнения словарей."""
		return {
			row.field.id: row.values()
			for row in self._rows
			if row.check.isChecked() and row.values()
		}


class FieldsDialog(MessageBoxBase):
	"""Настройка канала: пул полей со словарями и шаблоны."""

	def __init__(
		self, worker: EngineWorker, channel_id: int, channel_title: str,
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._channel_id = channel_id
		self._fields: list[FieldDto] = []
		self.viewLayout.addWidget(SubtitleLabel(
			f"Подписи канала «{channel_title}»", self
		))
		self._build_fields_block()
		self._build_templates_block()
		self.yesButton.setText("Готово")
		self.cancelButton.hide()
		self.widget.setMinimumWidth(560)
		self._reload()

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку всплывающей плашкой."""
		show_error(self, message)

	# --- поля -----------------------------------------------------------------

	def _build_fields_block(self) -> None:
		self.viewLayout.addWidget(BodyLabel("Поля (словарь общий для шаблонов):", self))
		self._fields_box = QVBoxLayout()
		self.viewLayout.addLayout(self._fields_box)
		row = QHBoxLayout()
		self._field_name = LineEdit(self)
		self._field_name.setPlaceholderText("Новое поле (например, Genre)…")
		row.addWidget(self._field_name, stretch=1)
		row.addWidget(CaptionLabel("решётки", self))
		self._field_hashtag = SwitchButton(self)
		self._field_hashtag.setChecked(True)
		row.addWidget(self._field_hashtag)
		row.addWidget(CaptionLabel("несколько", self))
		self._field_multiple = SwitchButton(self)
		row.addWidget(self._field_multiple)
		add = PushButton("Добавить", self)
		add.clicked.connect(self._on_add_field)
		row.addWidget(add)
		self.viewLayout.addLayout(row)

	def _row_widget(self, text: str, hint: str, on_delete: Callable[[], None]) -> QWidget:
		"""Строка списка (виджет — чтобы перерисовка её корректно удаляла)."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 2, 0, 2)
		row.addWidget(BodyLabel(text, box))
		row.addWidget(CaptionLabel(hint, box))
		row.addStretch()
		delete = PushButton("Удалить", box)
		delete.clicked.connect(on_delete)
		row.addWidget(delete)
		return box

	def _show_fields(self, fields: list[FieldDto]) -> None:
		"""Перерисовывает список полей и набор для сборки шаблона."""
		self._fields = fields
		clear_layout(self._fields_box)
		for field in fields:
			flags = ("#" if field.hashtag else "текст") + (
				", несколько" if field.multiple else ""
			)
			self._fields_box.addWidget(self._row_widget(
				f"{field.name} ({flags})", f"словарь: {len(field.values)}",
				bind(self._on_delete_field, field),
			))
		self._fill_template_list()
		self.widget.adjustSize()  # данные пришли после показа окна

	def _on_add_field(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.captions.add_field(
				self._channel_id, str(self._field_name.text()),
				self._field_hashtag.isChecked(), self._field_multiple.isChecked(),
			),
			self, self._on_field_added, self._show_error,
		)

	def _on_field_added(self, _field: FieldDto) -> None:
		self._field_name.clear()
		self._reload()

	def _on_delete_field(self, field: FieldDto) -> None:
		run_in_engine(
			self._worker, self._worker.engine.captions.delete_field(field.id),
			self, lambda *_a: self._reload(), self._show_error,
		)

	# --- шаблоны ----------------------------------------------------------------

	def _build_templates_block(self) -> None:
		self.viewLayout.addWidget(BodyLabel(
			"Шаблоны (отметьте поля, порядок — перетаскиванием):", self
		))
		self._templates_box = QVBoxLayout()
		self.viewLayout.addLayout(self._templates_box)
		self._template_list = QListWidget(self)
		self._template_list.setDragDropMode(
			QListWidget.DragDropMode.InternalMove
		)
		self._template_list.setMaximumHeight(140)
		self.viewLayout.addWidget(self._template_list)
		row = QHBoxLayout()
		self._template_name = LineEdit(self)
		self._template_name.setPlaceholderText("Имя шаблона (например, Фильм)…")
		row.addWidget(self._template_name, stretch=1)
		save = PushButton("Сохранить шаблон", self)
		save.clicked.connect(self._on_save_template)
		row.addWidget(save)
		self.viewLayout.addLayout(row)

	def _fill_template_list(self) -> None:
		"""Заполняет набор полей для сборки нового шаблона."""
		self._template_list.clear()
		for field in self._fields:
			item = QListWidgetItem(field.name)
			item.setData(Qt.ItemDataRole.UserRole, field.id)
			item.setFlags(
				item.flags()
				| Qt.ItemFlag.ItemIsUserCheckable
				| Qt.ItemFlag.ItemIsDragEnabled
			)
			item.setCheckState(Qt.CheckState.Unchecked)
			self._template_list.addItem(item)

	def _show_templates(self, templates: list[TemplateDto]) -> None:
		clear_layout(self._templates_box)
		for template in templates:
			names = ", ".join(tf.field.name for tf in template.fields)
			self._templates_box.addWidget(self._row_widget(
				template.name, names, bind(self._on_delete_template, template),
			))
		self.widget.adjustSize()  # данные пришли после показа окна

	def _checked_field_ids(self) -> list[int]:
		"""Отмеченные поля в текущем порядке списка."""
		ids: list[int] = []
		for index in range(self._template_list.count()):
			item = self._template_list.item(index)
			if item.checkState() is Qt.CheckState.Checked:
				ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
		return ids

	def _on_save_template(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.captions.save_template(
				self._channel_id, str(self._template_name.text()),
				self._checked_field_ids(),
			),
			self, self._on_template_saved, self._show_error,
		)

	def _on_template_saved(self, _template: TemplateDto) -> None:
		self._template_name.clear()
		self._reload()

	def _on_delete_template(self, template: TemplateDto) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_template(template.id),
			self, lambda *_a: self._reload(), self._show_error,
		)

	# --- загрузка -----------------------------------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_fields(self._channel_id),
			self, self._show_fields, self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(self._channel_id),
			self, self._show_templates, self._show_error,
		)
