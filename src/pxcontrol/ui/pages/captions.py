"""Диалоги подписей: сборка по шаблону и настройка полей/шаблонов канала.

Сборка (`CaptionDialog`) работает на уже загруженных данных и ничего
не тянет из движка; настройка (`FieldsDialog`) выполняет CRUD через
`run_in_engine` прямо из диалога.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QGridLayout,
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
	FlowLayout,
	LineEdit,
	MessageBoxBase,
	PillPushButton,
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
from pxcontrol.ui.pages.common import bind, clear_layout, error_reporter, exec_dialog


def _row_widget(
	parent: QWidget,
	text: str,
	hint: str,
	on_delete: Callable[[], None],
	extra: QWidget | None = None,
) -> QWidget:
	"""Строка списка (виджет — чтобы перерисовка её корректно удаляла).

	``extra`` — необязательная кнопка перед «Удалить» (например «Словарь…»).
	"""
	box = QWidget(parent)
	row = QHBoxLayout(box)
	row.setContentsMargins(0, 2, 0, 2)
	row.addWidget(BodyLabel(text, box))
	row.addWidget(CaptionLabel(hint, box))
	row.addStretch()
	if extra is not None:
		row.addWidget(extra)
	delete = PushButton("Удалить", box)
	delete.clicked.connect(on_delete)
	row.addWidget(delete)
	return box


class _FieldRow:
	"""Строка поля в диалоге сборки: включённость и ввод значений.

	Раскладка — сетка: колонка имён (одинаковой ширины) и колонка
	значений; значения множественных полей — «пилюли»-теги, визуально
	отличимые от чекбокса включения поля.
	"""

	def __init__(
		self, dialog: QWidget, grid: QGridLayout, row: int, tf: TemplateFieldDto
	) -> None:
		self.field = tf.field
		self.check = CheckBox(self.field.name, dialog)
		self.check.setChecked(tf.enabled)
		if self.field.multiple:
			grid.addWidget(
				self.check, row, 0,
				Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
			)
			grid.addWidget(self._build_multi(dialog), row, 1)
		else:
			grid.addWidget(self.check, row, 0, Qt.AlignmentFlag.AlignLeft)
			grid.addWidget(self._build_single(dialog), row, 1)

	def _build_single(self, dialog: QWidget) -> QWidget:
		"""Одно значение: редактируемый список со словарём."""
		self._edit = EditableComboBox(dialog)
		self._edit.addItems(self.field.values)
		self._edit.setCurrentIndex(-1)
		# qfluentwidgets не типизирован: без явной аннотации mypy видит Any
		widget: QWidget = self._edit
		return widget

	def _build_multi(self, dialog: QWidget) -> QWidget:
		"""Несколько значений: «пилюли»-теги словаря + строка новых."""
		box = QWidget(dialog)
		column = QVBoxLayout(box)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(6)
		self._pills: list[PillPushButton] = []
		if self.field.values:
			pills_box = QWidget(box)
			flow = FlowLayout(pills_box, needAni=False)
			flow.setContentsMargins(0, 0, 0, 0)
			for value in self.field.values:
				pill = PillPushButton(value, pills_box)
				flow.addWidget(pill)
				self._pills.append(pill)
			column.addWidget(pills_box)
		self._line = LineEdit(box)
		self._line.setPlaceholderText("новые значения через запятую…")
		column.addWidget(self._line)
		return box

	def values(self) -> list[str]:
		"""Введённые значения: отмеченные пилюли + строка (без дублей)."""
		if not self.field.multiple:
			value = str(self._edit.currentText()).strip()
			return [value] if value else []
		picked = [str(p.text()) for p in self._pills if p.isChecked()]
		typed = [v.strip() for v in str(self._line.text()).split(",") if v.strip()]
		return list(dict.fromkeys([*picked, *typed]))


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
		self._fields_grid = QGridLayout()
		self._fields_grid.setHorizontalSpacing(16)
		self._fields_grid.setVerticalSpacing(10)
		self._fields_grid.setColumnStretch(1, 1)
		self.viewLayout.addLayout(self._fields_grid)
		# индекс и перерисовка — после сборки формы, сигнал подключаем последним
		index = self._last_used_index()
		self._combo.setCurrentIndex(index)
		self._show_template(index)
		self._combo.currentIndexChanged.connect(self._show_template)
		self.yesButton.setText("Вставить в подпись")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(560)

	def _build_template_combo(self) -> None:
		"""Выбор шаблона подписи (виден всегда, даже если шаблон один)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Шаблон подписи:", self))
		self._combo = ComboBox(self)
		for template in self._templates:
			self._combo.addItem(template.name)
		row.addWidget(self._combo, stretch=1)
		self.viewLayout.addLayout(row)

	def _last_used_index(self) -> int:
		"""Индекс последнего использованного шаблона (или первого)."""
		stamps = [
			(t.last_used_at, i) for i, t in enumerate(self._templates)
			if t.last_used_at is not None
		]
		return max(stamps)[1] if stamps else 0

	def _show_template(self, index: int) -> None:
		"""Перестраивает сетку полей под выбранный шаблон."""
		clear_layout(self._fields_grid)
		self._rows = [
			_FieldRow(self, self._fields_grid, row, tf)
			for row, tf in enumerate(self._templates[index].fields)
		]

	def template_id(self) -> int:
		"""Идентификатор выбранного шаблона."""
		return self._templates[int(self._combo.currentIndex())].id

	def title(self) -> str:
		"""Название поста (первая строка подписи)."""
		return str(self._title.text()).strip()

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


class DictionaryDialog(MessageBoxBase):
	"""Редактор словаря поля: список значений, удаление, добавление.

	Словарь пополняется и сам — из значений, введённых при сборке
	подписи; здесь он правится руками: опечатки и устаревшие значения
	удаляются, новые добавляются пачкой через запятую.
	"""

	def __init__(
		self, worker: EngineWorker, field: FieldDto, parent: QWidget
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._field = field
		self._show_error = error_reporter(self)
		self.viewLayout.addWidget(SubtitleLabel(f"Словарь поля «{field.name}»", self))
		self._values_box = QVBoxLayout()
		self.viewLayout.addLayout(self._values_box)
		row = QHBoxLayout()
		self._new_values = LineEdit(self)
		self._new_values.setPlaceholderText("новые значения через запятую…")
		row.addWidget(self._new_values, stretch=1)
		add = PushButton("Добавить", self)
		add.clicked.connect(self._on_add)
		row.addWidget(add)
		self.viewLayout.addLayout(row)
		self.yesButton.setText("Готово")
		self.cancelButton.hide()
		self.widget.setMinimumWidth(460)
		self._show_values(field)

	def _show_values(self, field: FieldDto) -> None:
		"""Перерисовывает список значений словаря."""
		self._field = field
		clear_layout(self._values_box)
		if not field.values:
			self._values_box.addWidget(CaptionLabel(
				"Словарь пуст — добавьте значения здесь или при сборке подписи.",
				self,
			))
		for value in field.values:
			self._values_box.addWidget(_row_widget(
				self, value, "", bind(self._on_delete_value, value),
			))
		self.widget.adjustSize()  # список меняется после показа окна

	def _on_add(self) -> None:
		"""Добавляет значения из строки ввода (через запятую)."""
		values = [
			v.strip() for v in str(self._new_values.text()).split(",") if v.strip()
		]
		if not values:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.add_values(self._field.id, values),
			self, self._on_changed, self._show_error,
		)

	def _on_delete_value(self, value: str) -> None:
		"""Удаляет значение из словаря."""
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_value(self._field.id, value),
			self, self._on_changed, self._show_error,
		)

	def _on_changed(self, field: FieldDto) -> None:
		"""Движок вернул обновлённое поле — перерисовать, очистить ввод."""
		self._new_values.clear()
		self._show_values(field)


class FieldsDialog(MessageBoxBase):
	"""Настройка канала: пул полей со словарями и шаблоны."""

	def __init__(
		self, worker: EngineWorker, channel_id: int, channel_title: str,
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._channel_id = channel_id
		self._show_error = error_reporter(self)
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

	# --- поля -----------------------------------------------------------------

	def _build_fields_block(self) -> None:
		"""Блок пула полей: список и строка добавления нового поля."""
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

	def _show_fields(self, fields: list[FieldDto]) -> None:
		"""Перерисовывает список полей и набор для сборки шаблона."""
		self._fields = fields
		clear_layout(self._fields_box)
		for field in fields:
			flags = ("#" if field.hashtag else "текст") + (
				", несколько" if field.multiple else ""
			)
			dictionary = PushButton("Словарь…", self)
			dictionary.clicked.connect(bind(self._open_dictionary, field))
			self._fields_box.addWidget(_row_widget(
				self, f"{field.name} ({flags})", f"словарь: {len(field.values)}",
				bind(self._on_delete_field, field), extra=dictionary,
			))
		self._fill_template_list()
		self._update_pattern_hint(fields)
		self.widget.adjustSize()  # данные пришли после показа окна

	def _open_dictionary(self, field: FieldDto) -> None:
		"""Открывает редактор словаря поля; после — обновляет счётчики."""
		exec_dialog(DictionaryDialog(self._worker, field, self.window()))
		self._reload()

	def _update_pattern_hint(self, fields: list[FieldDto]) -> None:
		"""Подсказка плейсхолдеров имени файла — с актуальными полями канала."""
		tokens = ", ".join("{" + f.name + "}" for f in fields) or "добавьте поля выше"
		self._pattern_hint.setText(
			"Плейсхолдеры имени файла: {video} — название видео, "
			"{quality} — качество видео, {channel} — @имя канала; "
			"поля со значениями через запятую: " + tokens
		)

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
		"""Блок шаблонов: список, набор полей, шаблон имени файла, сохранение."""
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
		self._template_pattern = LineEdit(self)
		self._template_pattern.setPlaceholderText(
			"Шаблон имени файла (необязательно): "
			"{Author}, {video} ({Genre}) {quality} (@{channel})"
		)
		self.viewLayout.addWidget(self._template_pattern)
		self._pattern_hint = CaptionLabel("", self)
		self._pattern_hint.setWordWrap(True)
		# подсказку можно выделять и копировать (плейсхолдеры — в шаблон)
		self._pattern_hint.setTextInteractionFlags(
			Qt.TextInteractionFlag.TextSelectableByMouse
		)
		self.viewLayout.addWidget(self._pattern_hint)
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
		"""Перерисовывает список шаблонов с их составом."""
		clear_layout(self._templates_box)
		for template in templates:
			hint = ", ".join(tf.field.name for tf in template.fields)
			if template.filename_pattern:
				hint += " · шаблон имени файла задан"
			self._templates_box.addWidget(_row_widget(
				self, template.name, hint, bind(self._on_delete_template, template),
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
				str(self._template_pattern.text()).strip() or None,
			),
			self, self._on_template_saved, self._show_error,
		)

	def _on_template_saved(self, _template: TemplateDto) -> None:
		self._template_name.clear()
		self._template_pattern.clear()
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
