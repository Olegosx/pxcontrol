"""Диалоги подписей: сборка по шаблону и настройка полей/шаблонов канала.

Сборка (`CaptionDialog`) работает на уже загруженных данных и ничего
не тянет из движка; настройка (`FieldsDialog`) выполняет CRUD через
`run_in_engine` прямо из диалога.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence

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
	ValueDto,
	build_caption,
)
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	bind,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
)


def _row_widget(
	parent: QWidget,
	text: str,
	hint: str,
	on_delete: Callable[[], None],
	extras: Sequence[QWidget] = (),
) -> QWidget:
	"""Строка списка (виджет — чтобы перерисовка её корректно удаляла).

	``extras`` — виджеты перед «Удалить» (например «Словарь…» и выбор
	родительского поля).
	"""
	box = QWidget(parent)
	row = QHBoxLayout(box)
	row.setContentsMargins(0, 2, 0, 2)
	row.addWidget(BodyLabel(text, box))
	row.addWidget(CaptionLabel(hint, box))
	row.addStretch()
	for widget in extras:
		row.addWidget(widget)
	delete = PushButton("Удалить", box)
	delete.clicked.connect(on_delete)
	row.addWidget(delete)
	return box


class _FieldRow:
	"""Строка поля в диалоге сборки: включённость и ввод значений.

	Раскладка — сетка: колонка имён (одинаковой ширины) и колонка
	значений; значения множественных полей — «пилюли»-теги, визуально
	отличимые от чекбокса включения поля.

	У зависимого поля («Character» внутри «Title») показываются не все
	значения словаря, а только принадлежащие выбранному значению
	родителя — :meth:`refresh` перестраивает список при его смене.
	"""

	def __init__(
		self,
		dialog: QWidget,
		grid: QGridLayout,
		row: int,
		tf: TemplateFieldDto,
		on_changed: Callable[[], None],
	) -> None:
		self.field = tf.field
		self._on_changed = on_changed
		# None — родителя в шаблоне нет, фильтровать не по чему (весь словарь)
		self._parent_ids: Collection[int] | None = None
		self.check = CheckBox(self.field.name, dialog)
		self.check.setChecked(tf.enabled)
		if self.field.multiple:
			grid.addWidget(
				self.check,
				row,
				0,
				Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
			)
			grid.addWidget(self._build_multi(dialog), row, 1)
		else:
			grid.addWidget(self.check, row, 0, Qt.AlignmentFlag.AlignLeft)
			grid.addWidget(self._build_single(dialog), row, 1)

	def _build_single(self, dialog: QWidget) -> QWidget:
		"""Одно значение: редактируемый список со словарём."""
		self._edit = EditableComboBox(dialog)
		self._edit.currentTextChanged.connect(self._changed)
		# qfluentwidgets не типизирован: без явной аннотации mypy видит Any
		widget: QWidget = self._edit
		self._fill_single()
		return widget

	def _build_multi(self, dialog: QWidget) -> QWidget:
		"""Несколько значений: «пилюли»-теги словаря + строка новых."""
		box = QWidget(dialog)
		column = QVBoxLayout(box)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(6)
		self._pills: list[PillPushButton] = []
		self._pills_box = QVBoxLayout()
		self._pills_box.setContentsMargins(0, 0, 0, 0)
		column.addLayout(self._pills_box)
		self._line = LineEdit(box)
		self._line.setPlaceholderText("новые значения через запятую…")
		column.addWidget(self._line)
		self._box = box
		self._fill_pills()
		return box

	def _visible(self) -> list[ValueDto]:
		"""Значения словаря, подходящие под выбранное значение родителя."""
		if self._parent_ids is None:
			return list(self.field.values)
		return self.field.available(self._parent_ids)

	def _fill_single(self) -> None:
		"""Пересобирает список значений, сохраняя введённый текст."""
		typed = str(self._edit.currentText())
		self._edit.blockSignals(True)
		try:
			self._edit.clear()
			self._edit.addItems([item.value for item in self._visible()])
			self._edit.setCurrentIndex(-1)
			self._edit.setText(typed)
		finally:
			self._edit.blockSignals(False)

	def _fill_pills(self) -> None:
		"""Пересобирает «пилюли», сохраняя отметки уцелевших значений."""
		checked = {str(p.text()) for p in self._pills if p.isChecked()}
		clear_layout(self._pills_box)
		self._pills = []
		visible = self._visible()
		if not visible:
			return
		host = QWidget(self._box)
		flow = FlowLayout(host, needAni=False)
		flow.setContentsMargins(0, 0, 0, 0)
		for item in visible:
			pill = PillPushButton(item.value, host)
			pill.setChecked(item.value in checked)  # до connect: без лишнего сигнала
			pill.toggled.connect(self._changed)
			flow.addWidget(pill)
			self._pills.append(pill)
		self._pills_box.addWidget(host)

	def _changed(self, *_args: object) -> None:
		"""Выбор изменился — диалог перестроит зависимые поля."""
		self._on_changed()

	def refresh(self, parent_ids: Collection[int] | None) -> None:
		"""Перестраивает список значений под выбранные значения родителя.

		``None`` — родительского поля в шаблоне нет: фильтровать не по чему,
		показывается весь словарь.
		"""
		self._parent_ids = parent_ids
		if self.field.multiple:
			self._fill_pills()
		else:
			self._fill_single()

	def values(self) -> list[str]:
		"""Введённые значения: отмеченные пилюли + строка (без дублей)."""
		if not self.field.multiple:
			value = str(self._edit.currentText()).strip()
			return [value] if value else []
		picked = [str(p.text()) for p in self._pills if p.isChecked()]
		typed = [v.strip() for v in str(self._line.text()).split(",") if v.strip()]
		return list(dict.fromkeys([*picked, *typed]))

	def selected_ids(self) -> list[int]:
		"""Идентификаторы выбранных значений словаря (для зависимых полей).

		Значение, введённое руками и ещё не попавшее в словарь,
		идентификатора не имеет — зависимое поле покажет только значения
		без привязки, а связь появится после отправки (``record_usage``).
		"""
		chosen = {value.lower() for value in self.values()}
		return [item.id for item in self.field.values if item.value.lower() in chosen]


class CaptionDialog(MessageBoxBase):
	"""Сборка подписи: шаблон, название, поля со словарями."""

	def __init__(self, templates: list[TemplateDto], suggested_title: str, parent: QWidget) -> None:
		super().__init__(parent)
		self._templates = templates
		self._rows: list[_FieldRow] = []
		self._refreshing = False
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
			(t.last_used_at, i) for i, t in enumerate(self._templates) if t.last_used_at is not None
		]
		return max(stamps)[1] if stamps else 0

	def _show_template(self, index: int) -> None:
		"""Перестраивает сетку полей под выбранный шаблон."""
		clear_layout(self._fields_grid)
		self._rows = [
			_FieldRow(self, self._fields_grid, row, tf, self._refresh_dependents)
			for row, tf in enumerate(self._templates[index].fields)
		]
		self._refresh_dependents()

	def _refresh_dependents(self) -> None:
		"""Перестраивает списки зависимых полей под выбор их родителей.

		Вызывается при смене шаблона и при любом изменении выбора: список
		персонажей должен отвечать выбранному тайтлу сразу, а не после
		переоткрытия диалога. Флаг ``_refreshing`` защищает от повторного
		захода: перестройка виджетов сама может излучать сигналы.
		"""
		if self._refreshing:
			return
		self._refreshing = True
		try:
			rows = {row.field.id: row for row in self._rows}
			for row in self._rows:
				parent_id = row.field.parent_field_id
				if parent_id is None:
					continue
				parent = rows.get(parent_id)
				row.refresh(parent.selected_ids() if parent is not None else None)
		finally:
			self._refreshing = False

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
			for row in self._rows
			if row.check.isChecked()
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

	У зависимого поля («Character» внутри «Title») у каждого значения
	есть выбор родителя: к какому тайтлу относится персонаж. Новые
	значения кладутся внутрь тайтла, выбранного в строке добавления.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		field: FieldDto,
		parent: QWidget,
		parent_field: FieldDto | None = None,
		dependent_names: Sequence[str] = (),
	) -> None:
		"""``parent_field`` — родительское поле (None — поле независимое);
		``dependent_names`` — поля, зависящие от этого: их значения уйдут
		вместе с удаляемым (предупреждение перед удалением)."""
		super().__init__(parent)
		self._worker = worker
		self._field = field
		self._parent_field = parent_field
		self._dependent_names = list(dependent_names)
		self._show_error = error_reporter(self)
		self.viewLayout.addWidget(SubtitleLabel(f"Словарь поля «{field.name}»", self))
		if parent_field is not None:
			self.viewLayout.addWidget(
				CaptionLabel(
					f"Значения живут внутри поля «{parent_field.name}»: "
					"выберите, к какому значению относится каждое.",
					self,
				)
			)
		self._values_box = QVBoxLayout()
		self.viewLayout.addLayout(self._values_box)
		self._build_add_row()
		self.yesButton.setText("Готово")
		self.cancelButton.hide()
		self.widget.setMinimumWidth(520)
		self._show_values(field)

	def _build_add_row(self) -> None:
		"""Строка добавления: значения через запятую и (для зависимого) родитель."""
		row = QHBoxLayout()
		self._new_values = LineEdit(self)
		self._new_values.setPlaceholderText("новые значения через запятую…")
		row.addWidget(self._new_values, stretch=1)
		self._new_parent: DtoComboBox[ValueDto] | None = None
		if self._parent_field is not None:
			combo: DtoComboBox[ValueDto] = DtoComboBox(self, "(без привязки)")
			combo.set_items(self._parent_field.values, lambda v: v.value)
			combo.setMinimumWidth(160)
			row.addWidget(combo)
			self._new_parent = combo
		add = PushButton("Добавить", self)
		add.clicked.connect(self._on_add)
		row.addWidget(add)
		self.viewLayout.addLayout(row)

	def _show_values(self, field: FieldDto) -> None:
		"""Перерисовывает список значений словаря."""
		self._field = field
		clear_layout(self._values_box)
		if not field.values:
			self._values_box.addWidget(
				CaptionLabel(
					"Словарь пуст — добавьте значения здесь или при сборке подписи.",
					self,
				)
			)
		for item in field.values:
			extras = [] if self._parent_field is None else [self._parent_combo(item)]
			self._values_box.addWidget(
				_row_widget(
					self,
					item.value,
					"",
					bind(self._on_delete_value, item),
					extras,
				)
			)
		self.widget.adjustSize()  # список меняется после показа окна

	def _parent_combo(self, item: ValueDto) -> QWidget:
		"""Выбор родительского значения для одного значения словаря."""
		combo: DtoComboBox[ValueDto] = DtoComboBox(self, "(без привязки)")
		assert self._parent_field is not None  # вызывается только у зависимого поля
		combo.set_items(self._parent_field.values, lambda v: v.value)
		if item.parent_id is not None:
			combo.select(lambda v: v.id == item.parent_id)
		combo.setMinimumWidth(160)
		# сигнал — после установки текущего значения: иначе предвыбор
		# сам себя сохранил бы обратно в движок
		combo.currentIndexChanged.connect(bind(self._on_parent_changed, (item, combo)))
		widget: QWidget = combo
		return widget

	def _on_parent_changed(self, pair: tuple[ValueDto, DtoComboBox[ValueDto]]) -> None:
		"""Пользователь сменил родителя значения — сохранить связь."""
		item, combo = pair
		chosen = combo.selected()
		run_in_engine(
			self._worker,
			self._worker.engine.captions.assign_value_parent(
				item.id, chosen.id if chosen is not None else None
			),
			self,
			self._on_changed,
			self._show_error,
		)

	def _on_add(self) -> None:
		"""Добавляет значения из строки ввода (через запятую)."""
		values = [v.strip() for v in str(self._new_values.text()).split(",") if v.strip()]
		if not values:
			return
		chosen = self._new_parent.selected() if self._new_parent is not None else None
		run_in_engine(
			self._worker,
			self._worker.engine.captions.add_values(
				self._field.id, values, chosen.id if chosen is not None else None
			),
			self,
			self._on_changed,
			self._show_error,
		)

	def _on_delete_value(self, item: ValueDto) -> None:
		"""Удаляет значение; у значения-родителя — вместе с зависимыми."""
		if self._dependent_names and not confirm_delete(
			self,
			f"Удалить «{item.value}»? Вместе с ним удалятся связанные "
			f"значения поля «{', '.join(self._dependent_names)}».",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_value(item.id),
			self,
			self._on_changed,
			self._show_error,
		)

	def _on_changed(self, field: FieldDto) -> None:
		"""Движок вернул обновлённое поле — перерисовать, очистить ввод."""
		self._new_values.clear()
		self._show_values(field)


class FieldsDialog(MessageBoxBase):
	"""Настройка канала: пул полей со словарями и шаблоны."""

	def __init__(
		self,
		worker: EngineWorker,
		channel_id: int,
		channel_title: str,
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._channel_id = channel_id
		self._show_error = error_reporter(self)
		self._fields: list[FieldDto] = []
		# шаблон, который сейчас правится (None — форма собирает новый)
		self._editing: TemplateDto | None = None
		self.viewLayout.addWidget(SubtitleLabel(f"Подписи канала «{channel_title}»", self))
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
			flags = ("#" if field.hashtag else "текст") + (", несколько" if field.multiple else "")
			dictionary = PushButton("Словарь…", self)
			dictionary.clicked.connect(bind(self._open_dictionary, field))
			self._fields_box.addWidget(
				_row_widget(
					self,
					f"{field.name} ({flags})",
					f"словарь: {len(field.values)}",
					bind(self._on_delete_field, field),
					[self._parent_field_combo(field), dictionary],
				)
			)
		# перерисовка не сбрасывает правку: состав правящегося шаблона
		# восстанавливается в списке (например, после добавления поля)
		self._fill_template_list(self._editing)
		self._update_pattern_hint(fields)
		self.widget.adjustSize()  # данные пришли после показа окна

	def _parent_field_combo(self, field: FieldDto) -> QWidget:
		"""Выбор родительского поля: внутри чьих значений живёт словарь.

		Например, «Character» внутри «Title» — при сборке поста
		показываются персонажи выбранного тайтла.
		"""
		combo: DtoComboBox[FieldDto] = DtoComboBox(self, "внутри: —")
		combo.set_items(
			[other for other in self._fields if other.id != field.id],
			lambda other: f"внутри: {other.name}",
		)
		if field.parent_field_id is not None:
			combo.select(lambda other: other.id == field.parent_field_id)
		combo.setMinimumWidth(150)
		# сигнал — после предвыбора: иначе выбор сохранился бы сам собой
		combo.currentIndexChanged.connect(bind(self._on_parent_field, (field, combo)))
		widget: QWidget = combo
		return widget

	def _on_parent_field(self, pair: tuple[FieldDto, DtoComboBox[FieldDto]]) -> None:
		"""Сохраняет связь поля с родительским полем."""
		field, combo = pair
		chosen = combo.selected()
		run_in_engine(
			self._worker,
			self._worker.engine.captions.set_field_parent(
				field.id, chosen.id if chosen is not None else None
			),
			self,
			lambda *_a: self._reload(),
			self._show_error,
		)

	def _open_dictionary(self, field: FieldDto) -> None:
		"""Открывает редактор словаря поля; после — обновляет счётчики."""
		parent_field = next((f for f in self._fields if f.id == field.parent_field_id), None)
		dependents = [f.name for f in self._fields if f.parent_field_id == field.id]
		exec_dialog(DictionaryDialog(self._worker, field, self.window(), parent_field, dependents))
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
				self._channel_id,
				str(self._field_name.text()),
				self._field_hashtag.isChecked(),
				self._field_multiple.isChecked(),
			),
			self,
			self._on_field_added,
			self._show_error,
		)

	def _on_field_added(self, _field: FieldDto) -> None:
		self._field_name.clear()
		self._reload()

	def _on_delete_field(self, field: FieldDto) -> None:
		# необратимая потеря словаря, копившегося автопополнением, —
		# как и везде в приложении, требует подтверждения
		details = f" вместе со словарём ({len(field.values)} знач.)" if field.values else ""
		if not confirm_delete(self, f"Удалить поле «{field.name}»{details}?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_field(field.id),
			self,
			lambda *_a: self._reload(),
			self._show_error,
		)

	# --- шаблоны ----------------------------------------------------------------

	def _build_templates_block(self) -> None:
		"""Блок шаблонов: список, набор полей, шаблон имени файла, сохранение."""
		self.viewLayout.addWidget(
			BodyLabel("Шаблоны (отметьте поля, порядок — перетаскиванием):", self)
		)
		self._templates_box = QVBoxLayout()
		self.viewLayout.addLayout(self._templates_box)
		self._template_list = QListWidget(self)
		self._template_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
		self._template_list.setMaximumHeight(140)
		self.viewLayout.addWidget(self._template_list)
		self._template_pattern = LineEdit(self)
		self._template_pattern.setPlaceholderText(
			"Шаблон имени файла (необязательно): {Author}, {video} ({Genre}) {quality} (@{channel})"
		)
		self.viewLayout.addWidget(self._template_pattern)
		self._pattern_hint = CaptionLabel("", self)
		self._pattern_hint.setWordWrap(True)
		# подсказку можно выделять и копировать (плейсхолдеры — в шаблон)
		self._pattern_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		self.viewLayout.addWidget(self._pattern_hint)
		self._edit_hint = CaptionLabel("", self)
		self._edit_hint.hide()
		self.viewLayout.addWidget(self._edit_hint)
		row = QHBoxLayout()
		self._template_name = LineEdit(self)
		self._template_name.setPlaceholderText("Имя шаблона (например, Фильм)…")
		row.addWidget(self._template_name, stretch=1)
		self._cancel_edit_button = PushButton("Отменить правку", self)
		self._cancel_edit_button.clicked.connect(self._cancel_edit)
		self._cancel_edit_button.hide()
		row.addWidget(self._cancel_edit_button)
		self._save_template_button = PushButton("Сохранить шаблон", self)
		self._save_template_button.clicked.connect(self._on_save_template)
		row.addWidget(self._save_template_button)
		self.viewLayout.addLayout(row)

	def _fill_template_list(self, template: TemplateDto | None = None) -> None:
		"""Заполняет набор полей формы шаблона.

		``template`` — правящийся шаблон: его поля идут первыми в порядке
		состава и отмечены; остальные поля канала — следом, без отметки.
		None — все поля без отметок (сборка нового шаблона).
		"""
		self._template_list.clear()
		template_ids = [tf.field.id for tf in template.fields] if template is not None else []
		ordered = sorted(
			self._fields,
			key=lambda field: (
				template_ids.index(field.id) if field.id in template_ids else len(template_ids)
			),
		)
		for field in ordered:
			item = QListWidgetItem(field.name)
			item.setData(Qt.ItemDataRole.UserRole, field.id)
			item.setFlags(
				item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled
			)
			item.setCheckState(
				Qt.CheckState.Checked if field.id in template_ids else Qt.CheckState.Unchecked
			)
			self._template_list.addItem(item)

	def _show_templates(self, templates: list[TemplateDto]) -> None:
		"""Перерисовывает список шаблонов с их составом."""
		clear_layout(self._templates_box)
		for template in templates:
			hint = ", ".join(tf.field.name for tf in template.fields)
			if template.filename_pattern:
				hint += " · шаблон имени файла задан"
			edit = PushButton("Изменить…", self)
			edit.clicked.connect(bind(self._start_edit_template, template))
			self._templates_box.addWidget(
				_row_widget(
					self,
					template.name,
					hint,
					bind(self._on_delete_template, template),
					[edit],
				)
			)
		self.widget.adjustSize()  # данные пришли после показа окна

	def _start_edit_template(self, template: TemplateDto) -> None:
		"""Загружает шаблон в форму: имя, состав с порядком, шаблон имени."""
		self._editing = template
		self._template_name.setText(template.name)
		self._template_pattern.setText(template.filename_pattern or "")
		self._fill_template_list(template)
		self._edit_hint.setText(
			f"Правка шаблона «{template.name}»: сохранение перезапишет его "
			"(смена имени — переименует)."
		)
		self._edit_hint.show()
		self._cancel_edit_button.show()
		self._save_template_button.setText("Сохранить изменения")

	def _cancel_edit(self) -> None:
		"""Возвращает форму в режим сборки нового шаблона."""
		self._editing = None
		self._template_name.clear()
		self._template_pattern.clear()
		self._fill_template_list()
		self._edit_hint.hide()
		self._cancel_edit_button.hide()
		self._save_template_button.setText("Сохранить шаблон")

	def _checked_field_ids(self) -> list[int]:
		"""Отмеченные поля в текущем порядке списка."""
		ids: list[int] = []
		for index in range(self._template_list.count()):
			item = self._template_list.item(index)
			if item.checkState() is Qt.CheckState.Checked:
				ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
		return ids

	def _on_save_template(self) -> None:
		"""Сохраняет форму: новый шаблон или перезапись правящегося."""
		run_in_engine(
			self._worker,
			self._worker.engine.captions.save_template(
				self._channel_id,
				str(self._template_name.text()),
				self._checked_field_ids(),
				str(self._template_pattern.text()).strip() or None,
				template_id=self._editing.id if self._editing else None,
			),
			self,
			self._on_template_saved,
			self._show_error,
		)

	def _on_template_saved(self, _template: TemplateDto) -> None:
		self._cancel_edit()  # форма снова собирает новый шаблон
		self._reload()

	def _on_delete_template(self, template: TemplateDto) -> None:
		if not confirm_delete(self, f"Удалить шаблон «{template.name}»?"):
			return
		if self._editing is not None and self._editing.id == template.id:
			self._cancel_edit()  # удаляемый шаблон не должен остаться в форме
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_template(template.id),
			self,
			lambda *_a: self._reload(),
			self._show_error,
		)

	# --- загрузка -----------------------------------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_fields(self._channel_id),
			self,
			self._show_fields,
			self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(self._channel_id),
			self,
			self._show_templates,
			self._show_error,
		)
