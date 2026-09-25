"""Пресеты подписи сообщества: список и экран пресета (ADR-0042).

Живут на вкладке «Настройки» страницы сообщества. Список — строки
пресетов и «Создать пресет»; клик по строке открывает **экран пресета**
в той же вкладке (строка пути «Настройки › пресет»), как настройка
задачи открывается из обзора задач.

Экран пресета правит всё, из чего собирается подпись: имя, поля
по порядку, их оформление и связи, правило «взять из имени файла»
у каждого поля, шаблон имени файла. Образец имени файла даёт живой
предпросмотр: результат разбора под каждым полем и итоговую подпись
с именем файла. Сохранение одно на весь экран (одна запись движка);
уход с несохранёнными правками — через вопрос.

Сразу, без кнопки «Сохранить», выполняется только то, что меняет пул
полей сообщества: новое поле заводится, удаление поля из сообщества —
с подтверждением; словарь правится в своём окне.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	BreadcrumbBar,
	CaptionLabel,
	CardWidget,
	CheckBox,
	FluentIcon,
	LineEdit,
	PushButton,
	RoundMenu,
	StrongBodyLabel,
	SubtitleLabel,
	TextEdit,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import (
	FILENAME_PLACEHOLDERS,
	CaptionLine,
	CaptionPresetDraft,
	CaptionPresetDto,
	FieldDto,
	FieldEdit,
	FieldStyle,
	PresetFieldSpec,
	SourceRule,
	build_caption,
	compose_filename,
	extract_values,
	filename_source,
)
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.telegram.rich_text import RichText
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.caption_rule import RuleEditor, rule_problem
from pxcontrol.ui.pages.captions import DictionaryDialog
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	FormDialog,
	bind,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
	leave_with_question,
	list_button,
	pick_file,
	plural,
	require_filled,
	row_card,
	section_header,
	show_success,
)
from pxcontrol.ui.pages.rich_edit import apply_rich

#: Вопрос при уходе с экрана пресета с несохранёнными правками.
SAVE_ON_LEAVE_HINT = (
	"В пресете подписи остались несохранённые изменения. Сохранить их перед уходом?"
)

#: Значение-заглушка предпросмотра у поля, которое вводит человек.
MANUAL_PLACEHOLDER = "…"

#: Ключ элемента «Настройки» строки пути экрана пресета.
_PATH_SETTINGS = "settings"


# --- чистые функции экрана --------------------------------------------------------


@dataclass(frozen=True)
class PresetForm:
	"""Снимок экрана пресета — по нему считается признак правок.

	Attributes:
		name: имя пресета (без крайних пробелов).
		fields: состав по порядку с правилами.
		pattern: шаблон имени файла (без крайних пробелов).
		edits: правки полей состава — пары «id поля, правка».
	"""

	name: str
	fields: tuple[PresetFieldSpec, ...]
	pattern: str
	edits: tuple[tuple[int, FieldEdit], ...]

	def draft(self) -> CaptionPresetDraft:
		"""Черновик движку: всё, что правится на экране, одной записью."""
		return CaptionPresetDraft(self.name, self.fields, self.pattern or None, dict(self.edits))


def without_field(form: PresetForm, field_id: int) -> PresetForm:
	"""Снимок после удаления поля из сообщества — как это сделал движок.

	Поле уходит из состава (каскад схемы), а поля, жившие внутри него,
	становятся независимыми (SET NULL у связи). Сохранённый снимок
	экрана сдвигается так же, иначе удаление выглядело бы правкой.
	"""
	edits = tuple(
		(item_id, replace(edit, parent_field_id=None) if edit.parent_field_id == field_id else edit)
		for item_id, edit in form.edits
		if item_id != field_id
	)
	fields = tuple(spec for spec in form.fields if spec.field_id != field_id)
	return replace(form, fields=fields, edits=edits)


def preset_summary(preset: CaptionPresetDto) -> str:
	"""Подстрочник строки пресета: состав, поля из имени файла, имя файла."""
	names = ", ".join(item.field.name for item in preset.fields) or "полей нет"
	parsed = [item.field.name for item in preset.fields if item.rule is not None]
	parts = [names]
	if parsed:
		parts.append(f"из имени файла: {', '.join(parsed)}")
	if preset.filename_pattern:
		parts.append("имя файла задано")
	return " · ".join(parts)


def preview_line(
	name: str, style: FieldStyle, rule: SourceRule | None, source: str | None
) -> CaptionLine:
	"""Строка предпросмотра поля.

	Поле с правилом при заданном образце показывает разобранное;
	остальные — заглушку без решётки: их значение введёт человек
	при сборке, а «#» от пустой заглушки только сбивал бы с толку.
	"""
	if rule is not None and source is not None:
		return CaptionLine(name, extract_values(source, rule, style.multiple), style)
	return CaptionLine(name, [MANUAL_PLACEHOLDER], replace(style, hashtag=False))


def sample_source(sample: str) -> str | None:
	"""Исходный текст разбора из образца имени файла; None — образца нет."""
	cleaned = sample.strip()
	return filename_source(cleaned) if cleaned else None


# --- список пресетов --------------------------------------------------------------


class PresetList(QWidget):
	"""Блок «Пресеты подписи»: строки пресетов и «Создать пресет».

	Сигнал ``open_requested`` несёт пресет (клик по строке) или None
	(«Создать пресет»).
	"""

	open_requested = Signal(object)  # CaptionPresetDto | None

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self.community = community
		self._show_error = error_reporter(self)
		self._presets: list[CaptionPresetDto] = []
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().list_spacing)
		create = list_button("Создать пресет", self)
		create.setIcon(FluentIcon.ADD)
		create.clicked.connect(lambda: self.open_requested.emit(None))
		layout.addWidget(section_header(self, "Пресеты подписи", trailing=[create]))
		self._rows = QVBoxLayout()
		self._rows.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._rows)

	@property
	def presets(self) -> list[CaptionPresetDto]:
		"""Прочитанные пресеты сообщества."""
		return list(self._presets)

	def reload(self) -> None:
		"""Перечитывает пресеты сообщества."""
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_presets(self.community.id),
			self,
			self._show,
			self._show_error,
		)

	def _show(self, presets: list[CaptionPresetDto]) -> None:
		self._presets = presets
		clear_layout(self._rows)
		if not presets:
			hint = CaptionLabel(
				"Пресетов нет. Пресет — набор полей подписи с правилами разбора "
				"имени файла; по нему «Публикация» собирает подпись.",
				self,
			)
			hint.setWordWrap(True)
			self._rows.addWidget(hint)
			return
		for preset in presets:
			card = row_card(self, preset.name, preset_summary(preset))
			card.setCursor(Qt.CursorShape.PointingHandCursor)
			card.clicked.connect(bind(self.open_requested.emit, preset))
			self._rows.addWidget(card)


# --- карточка поля ----------------------------------------------------------------


@dataclass(frozen=True)
class _CardActions:
	"""Что умеет экран по кнопкам карточки поля."""

	move: Callable[[_FieldCard, int], None]
	remove: Callable[[_FieldCard], None]
	delete_field: Callable[[_FieldCard], None]
	dictionary: Callable[[_FieldCard], None]
	changed: Callable[[], None]


class _FieldCard(CardWidget):
	"""Поле в составе пресета: оформление, связь, правило, результат разбора.

	Оформление и связь — общие для всех пресетов (у сообщества поле
	одно); правило разбора — своё у этого пресета.
	"""

	def __init__(
		self,
		field: FieldDto,
		rule: SourceRule | None,
		parent: QWidget,
		actions: _CardActions,
	) -> None:
		super().__init__(parent)
		self.field = field
		self._actions = actions
		self._source: str | None = None
		box = QVBoxLayout(self)
		box.setContentsMargins(*density.spacing().card_margins)
		box.setSpacing(density.spacing().card_body_spacing)
		box.addLayout(self._build_head())
		box.addLayout(self._build_style_row())
		self._from_name = CheckBox("Брать из имени файла", self)
		self._from_name.setToolTip(
			"Значение поля разбирается из имени файла по правилу ниже; "
			"такие значения словарь не пополняют"
		)
		box.addWidget(self._from_name)
		self._rule = RuleEditor(self)
		self._rule.set_rule(rule or SourceRule())
		self._rule.set_multiple(field.style.multiple)
		box.addWidget(self._rule)
		self._result = CaptionLabel(self)
		self._result.setWordWrap(True)
		box.addWidget(self._result)
		self._from_name.setChecked(rule is not None)
		self._rule.setVisible(rule is not None)
		# сигналы — после предустановки: начальное состояние не правка
		self._from_name.stateChanged.connect(self._on_from_name)
		self._rule.changed.connect(self._changed)

	def _build_head(self) -> QHBoxLayout:
		"""Шапка: имя, размер словаря, связь, порядок, «Словарь…» и меню.

		Выбор «внутри: …» — здесь, рядом со словарём: связь определяет,
		как устроен словарь поля (персонажи внутри тайтлов).
		"""
		row = QHBoxLayout()
		row.addWidget(StrongBodyLabel(self.field.name, self))
		self._count = CaptionLabel(self)
		row.addWidget(self._count)
		row.addStretch()
		self._parent: DtoComboBox[FieldDto] = DtoComboBox(self, "внутри: —")
		self._parent.setToolTip(
			"Поле живёт внутри значений другого поля: при сборке подписи "
			"показываются, например, персонажи выбранного тайтла"
		)
		self._parent.setMinimumWidth(150)
		self._parent.currentIndexChanged.connect(self._changed)
		row.addWidget(self._parent)
		self._up = TransparentToolButton(FluentIcon.UP, self)
		self._up.setToolTip("Выше")
		self._up.clicked.connect(lambda: self._actions.move(self, -1))
		row.addWidget(self._up)
		self._down = TransparentToolButton(FluentIcon.DOWN, self)
		self._down.setToolTip("Ниже")
		self._down.clicked.connect(lambda: self._actions.move(self, 1))
		row.addWidget(self._down)
		dictionary = list_button("Словарь…", self)
		dictionary.clicked.connect(lambda: self._actions.dictionary(self))
		row.addWidget(dictionary)
		more = TransparentToolButton(FluentIcon.MORE, self)
		more.setToolTip("Убрать из пресета или удалить поле из сообщества")
		more.clicked.connect(partial(self._show_menu, more))
		row.addWidget(more)
		self._show_count()
		return row

	def _build_style_row(self) -> QHBoxLayout:
		"""Оформление строки поля: общее для всех пресетов сообщества."""
		row = QHBoxLayout()
		style = self.field.style
		self._hashtag = self._style_box("решётки", style.hashtag, row)
		self._multiple = self._style_box("несколько значений", style.multiple, row)
		self._show_name = self._style_box("имя в подписи", style.show_name, row)
		self._bold = self._style_box("жирным", style.bold, row)
		row.addStretch()
		self._multiple.stateChanged.connect(
			lambda *_a: self._rule.set_multiple(self._multiple.isChecked())
		)
		return row

	def _style_box(self, text: str, checked: bool, row: QHBoxLayout) -> CheckBox:
		box = CheckBox(text, self)
		# галочка не сжимается ниже своей подписи: обрезанное «ре…»
		# вместо «решётки» не прочесть
		box.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
		box.setChecked(checked)
		box.stateChanged.connect(self._changed)
		row.addWidget(box)
		return box

	def _show_menu(self, anchor: QWidget) -> None:
		menu = RoundMenu(parent=self)
		remove = Action("Убрать из пресета", menu)
		remove.triggered.connect(lambda: self._actions.remove(self))
		menu.addAction(remove)
		menu.addSeparator()
		delete = Action(FluentIcon.DELETE, "Удалить поле из сообщества…", menu)
		delete.triggered.connect(lambda: self._actions.delete_field(self))
		menu.addAction(delete)
		menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

	# --- данные -----------------------------------------------------------------

	@property
	def field_id(self) -> int:
		return self.field.id

	def style(self) -> FieldStyle:
		"""Оформление по галочкам карточки."""
		return FieldStyle(
			hashtag=self._hashtag.isChecked(),
			multiple=self._multiple.isChecked(),
			show_name=self._show_name.isChecked(),
			bold=self._bold.isChecked(),
		)

	def rule(self) -> SourceRule | None:
		"""Правило разбора; None — значение вводит человек."""
		return self._rule.rule() if self._from_name.isChecked() else None

	def spec(self) -> PresetFieldSpec:
		return PresetFieldSpec(self.field.id, self.rule())

	def edit(self) -> FieldEdit:
		chosen = self._parent.selected()
		return FieldEdit(self.style(), chosen.id if chosen is not None else None)

	def problem(self) -> str | None:
		"""Причина, по которой правило поля не годится; None — годится."""
		rule = self.rule()
		return rule_problem(rule) if rule is not None else None

	def preview(self) -> CaptionLine:
		"""Строка предпросмотра поля по текущему образцу."""
		return preview_line(self.field.name, self.style(), self.rule(), self._source)

	def set_parent_options(self, fields: list[FieldDto], parent_id: int | None) -> None:
		"""Варианты «внутри: …» — остальные поля сообщества (без сигнала)."""
		self._parent.set_items(
			[other for other in fields if other.id != self.field.id],
			lambda other: f"внутри: {other.name}",
		)
		self._parent.blockSignals(True)
		try:
			if parent_id is None or not self._parent.select(lambda o: o.id == parent_id):
				self._parent.setCurrentIndex(0)
		finally:
			self._parent.blockSignals(False)

	def set_field(self, field: FieldDto) -> None:
		"""Свежее поле из движка (после правки словаря): только счётчик."""
		self.field = field
		self._show_count()

	def set_order(self, first: bool, last: bool) -> None:
		self._up.setEnabled(not first)
		self._down.setEnabled(not last)

	def show_sample(self, source: str | None) -> None:
		"""Результат разбора образца под правилом."""
		self._source = source
		rule = self.rule()
		if rule is None:
			self._result.hide()
			return
		self._result.show()
		if source is None:
			self._result.setText("Задайте образец имени файла — здесь появится результат.")
			return
		values = extract_values(source, rule, self._multiple.isChecked())
		shown = " | ".join(values) if values else "нет значения (совпадения нет)"
		self._result.setText(f"На образце: {shown}")

	def _show_count(self) -> None:
		count = len(self.field.values)
		self._count.setText(f"словарь: {count} {plural(count, 'значение', 'значения', 'значений')}")

	def _on_from_name(self, *_args: object) -> None:
		self._rule.setVisible(self._from_name.isChecked())
		self._changed()

	def _changed(self, *_args: object) -> None:
		self.show_sample(self._source)
		self._actions.changed()


# --- экран пресета ------------------------------------------------------------------


class PresetEditor(QWidget):
	"""Экран пресета: путь, имя, образец, поля, имя файла, предпросмотр.

	Сигналы: ``back_requested`` — клик по «Настройки» в строке пути
	(владелец уходит через :meth:`leave`); ``presets_changed`` —
	пресеты сообщества изменились (сохранение, удаление пресета или
	поля) и список надо перечитать; ``closed`` — пресет удалён,
	экран больше показывать нечего.
	"""

	back_requested = Signal()
	presets_changed = Signal()
	closed = Signal()

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self.community = community
		self._show_error = error_reporter(self)
		self._preset: CaptionPresetDto | None = None
		self._pool: list[FieldDto] = []
		self._others: list[CaptionPresetDto] = []
		self._cards: list[_FieldCard] = []
		self._saved: PresetForm | None = None
		self._dirty = False
		self._building_path = False
		self._build()

	# --- каркас -----------------------------------------------------------------

	def _build(self) -> None:
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().block_spacing)
		self._path = BreadcrumbBar(self)
		self._path.currentItemChanged.connect(self._on_path)
		layout.addWidget(self._path, alignment=Qt.AlignmentFlag.AlignLeft)
		self._title = SubtitleLabel(self)
		layout.addWidget(self._title)
		layout.addLayout(self._build_name_rows())
		layout.addWidget(section_header(self, "Поля пресета", trailing=[self._add_button()]))
		self._cards_box = QVBoxLayout()
		self._cards_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._cards_box)
		layout.addWidget(section_header(self, "Имя файла при отправке"))
		layout.addLayout(self._build_pattern_rows())
		layout.addWidget(section_header(self, "Предпросмотр"))
		self._preview = TextEdit(self)
		self._preview.setReadOnly(True)
		self._preview.setFixedHeight(120)
		layout.addWidget(self._preview)
		self._preview_name = CaptionLabel(self)
		self._preview_name.setWordWrap(True)
		layout.addWidget(self._preview_name)
		self._error = ErrorLabel(self)
		layout.addWidget(self._error)
		layout.addLayout(self._build_save_row())
		layout.addStretch()

	def _build_name_rows(self) -> QVBoxLayout:
		"""Имя пресета и образец имени файла для предпросмотра."""
		column = QVBoxLayout()
		column.setSpacing(density.spacing().row_spacing)
		self._name = LineEdit(self)
		self._name.setPlaceholderText("Имя пресета (например, Фильм)…")
		self._name.textChanged.connect(self._on_changed)
		column.addWidget(self._name)
		row = QHBoxLayout()
		self._sample = LineEdit(self)
		self._sample.setPlaceholderText(
			"Образец имени файла с расширением — для предпросмотра разбора (не сохраняется)"
		)
		self._sample.textChanged.connect(self._on_sample)
		row.addWidget(self._sample, stretch=1)
		pick = PushButton("Из файла…", self)
		pick.setToolTip("Взять имя настоящего файла образцом")
		pick.clicked.connect(self._on_pick_sample)
		row.addWidget(pick)
		column.addLayout(row)
		return column

	def _add_button(self) -> PushButton:
		button = list_button("Добавить поле", self)
		button.setIcon(FluentIcon.ADD)
		button.clicked.connect(partial(self._show_add_menu, button))
		return button

	def _build_pattern_rows(self) -> QVBoxLayout:
		column = QVBoxLayout()
		column.setSpacing(density.spacing().row_spacing)
		self._pattern = LineEdit(self)
		self._pattern.setPlaceholderText(
			"Шаблон имени файла (необязательно): {Author}, {Video} ({Genre}) {quality} (@{channel})"
		)
		self._pattern.textChanged.connect(self._on_changed)
		column.addWidget(self._pattern)
		self._pattern_hint = CaptionLabel(self)
		self._pattern_hint.setWordWrap(True)
		# подсказку можно выделять и копировать (плейсхолдеры — в шаблон)
		self._pattern_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		column.addWidget(self._pattern_hint)
		return column

	def _build_save_row(self) -> QHBoxLayout:
		row = QHBoxLayout()
		self._delete = PushButton(FluentIcon.DELETE, "Удалить пресет…", self)
		self._delete.clicked.connect(self._on_delete_preset)
		row.addWidget(self._delete)
		row.addStretch()
		self._revert = PushButton("Отменить изменения", self)
		self._revert.clicked.connect(self.discard)
		row.addWidget(self._revert)
		self._save = PushButton("Сохранить", self)
		self._save.clicked.connect(lambda: self.save())
		row.addWidget(self._save)
		return row

	# --- показ ------------------------------------------------------------------

	def open(
		self,
		preset: CaptionPresetDto | None,
		pool: list[FieldDto],
		others: Sequence[CaptionPresetDto] = (),
	) -> None:
		"""Показывает пресет (None — новый) с полями сообщества ``pool``.

		``others`` — все пресеты сообщества: удаление поля предупреждает,
		из каких пресетов оно уйдёт.
		"""
		self._preset = preset
		self._pool = list(pool)
		self._others = list(others)
		self._error.succeed()
		self._name.blockSignals(True)
		self._pattern.blockSignals(True)
		try:
			self._name.setText(preset.name if preset else "")
			self._pattern.setText((preset.filename_pattern or "") if preset else "")
		finally:
			self._name.blockSignals(False)
			self._pattern.blockSignals(False)
		clear_layout(self._cards_box)
		self._cards = []
		for item in preset.fields if preset else []:
			fresh = next((f for f in pool if f.id == item.field.id), item.field)
			self._append_card(fresh, item.rule)
		self._delete.setVisible(preset is not None)
		self.render_path()
		# снимок — с виджетов: нормализация полей (разделитель по умолчанию
		# и т. п.) не должна выглядеть правкой
		self._saved = self._form()
		self._refresh()

	def render_path(self) -> None:
		"""Строка пути: «Настройки» › имя пресета."""
		self._building_path = True
		try:
			self._path.clear()
			self._path.addItem(_PATH_SETTINGS, "Настройки")
			title = self._preset.name if self._preset else "Новый пресет"
			self._path.addItem("preset", title)
			self._title.setText(f"Пресет подписи «{title}»" if self._preset else "Новый пресет")
		finally:
			self._building_path = False

	def _on_path(self, route_key: str) -> None:
		if not self._building_path and route_key == _PATH_SETTINGS:
			self.back_requested.emit()

	def _append_card(self, field: FieldDto, rule: SourceRule | None) -> _FieldCard:
		actions = _CardActions(
			move=self._move_card,
			remove=self._remove_card,
			delete_field=self._on_delete_field,
			dictionary=self._open_dictionary,
			changed=self._on_changed,
		)
		card = _FieldCard(field, rule, self, actions)
		card.set_parent_options(self._pool, field.parent_field_id)
		self._cards.append(card)
		self._cards_box.addWidget(card)
		return card

	def _relayout_cards(self) -> None:
		"""Карточки в компоновке — в порядке списка, стрелки по краям выключены."""
		for card in self._cards:
			self._cards_box.removeWidget(card)
		for index, card in enumerate(self._cards):
			self._cards_box.addWidget(card)
			card.set_order(index == 0, index == len(self._cards) - 1)

	# --- состояние ----------------------------------------------------------------

	def _form(self) -> PresetForm:
		return PresetForm(
			name=str(self._name.text()).strip(),
			fields=tuple(card.spec() for card in self._cards),
			pattern=str(self._pattern.text()).strip(),
			edits=tuple((card.field_id, card.edit()) for card in self._cards),
		)

	@property
	def dirty(self) -> bool:
		"""Есть ли несохранённые правки."""
		return self._dirty

	def _on_changed(self, *_args: object) -> None:
		self._refresh()

	def _refresh(self) -> None:
		"""Признак правок, кнопки, подсказка имени файла и предпросмотр."""
		self._relayout_cards()
		self._dirty = self._saved is not None and self._form() != self._saved
		self._save.setEnabled(self._dirty)
		self._revert.setEnabled(self._dirty)
		self._show_pattern_hint()
		self._show_preview()

	def _show_pattern_hint(self) -> None:
		"""Плейсхолдеры имени файла — встроенные и поля этого пресета."""
		tokens = ", ".join("{" + card.field.name + "}" for card in self._cards) or "добавьте поля"
		builtin = ", ".join(f"{token} — {caption}" for token, caption in FILENAME_PLACEHOLDERS)
		self._pattern_hint.setText(
			f"Плейсхолдеры: {builtin}; поля — значения через запятую: {tokens}"
		)

	def _on_sample(self, *_args: object) -> None:
		source = sample_source(str(self._sample.text()))
		for card in self._cards:
			card.show_sample(source)
		self._show_preview()

	def _on_pick_sample(self) -> None:
		path = pick_file(self, "Образец имени файла", "Все файлы (*)")
		if path:
			self._sample.setText(Path(path).name)

	def _show_preview(self) -> None:
		"""Подпись и имя файла по образцу (поля без правила — заглушкой)."""
		sample = str(self._sample.text()).strip()
		source = sample_source(sample)
		caption: RichText = build_caption([card.preview() for card in self._cards])
		apply_rich(self._preview.document(), caption)
		pattern = str(self._pattern.text()).strip()
		if not pattern:
			self._preview_name.setText("Имя файла при отправке не меняется — шаблон не задан.")
			return
		mapping = {
			card.field.name: ", ".join(card.preview().values)
			for card in self._cards
			if card.rule() is not None and source is not None
		}
		mapping["channel"] = self.community.username or ""
		name = compose_filename(pattern, mapping, Path(sample).suffix if sample else "")
		self._preview_name.setText(f"Имя файла: {name or '(пусто)'}")

	# --- поля ---------------------------------------------------------------------

	def _show_add_menu(self, anchor: QWidget) -> None:
		"""Меню «Добавить поле»: поля сообщества вне пресета и «Новое поле…»."""
		menu = RoundMenu(parent=self)
		taken = {card.field_id for card in self._cards}
		for field in self._pool:
			if field.id in taken:
				continue
			action = Action(field.name, menu)
			action.triggered.connect(bind(self._add_existing, field))
			menu.addAction(action)
		if menu.actions():
			menu.addSeparator()
		new = Action(FluentIcon.ADD, "Новое поле…", menu)
		new.triggered.connect(self._on_new_field)
		menu.addAction(new)
		menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

	def _add_existing(self, field: FieldDto) -> None:
		card = self._append_card(field, None)
		card.show_sample(sample_source(str(self._sample.text())))
		self._refresh()

	def _on_new_field(self) -> None:
		"""Новое поле заводится в сообществе сразу и встаёт в конец пресета."""
		dialog = FormDialog(
			"Новое поле подписи",
			[("name", "Имя поля (например, Starring)…")],
			self.window(),
			validator=require_filled("name", message="Введите имя поля."),
			note="Поле появится у сообщества сразу и станет доступно всем пресетам; "
			"оформление настраивается в карточке поля.",
		)
		if not exec_dialog(dialog):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.add_field(
				self.community.id, dialog.value("name"), FieldStyle()
			),
			self,
			self._on_field_created,
			self._show_error,
		)

	def _on_field_created(self, field: FieldDto) -> None:
		self._pool.append(field)
		for card in self._cards:
			edit = card.edit()
			card.set_parent_options(self._pool, edit.parent_field_id)
		self._add_existing(field)

	def _move_card(self, card: _FieldCard, delta: int) -> None:
		index = self._cards.index(card)
		target = index + delta
		if 0 <= target < len(self._cards):
			self._cards[index], self._cards[target] = self._cards[target], self._cards[index]
			self._refresh()

	def _remove_card(self, card: _FieldCard) -> None:
		"""Убирает поле из пресета; у сообщества поле и словарь остаются."""
		self._cards.remove(card)
		self._cards_box.removeWidget(card)
		card.deleteLater()
		self._refresh()

	def _on_delete_field(self, card: _FieldCard) -> None:
		"""Удаляет поле из сообщества — с перечнем того, что пропадёт."""
		field = card.field
		used = [p.name for p in self._others if any(i.field.id == field.id for i in p.fields)]
		where = f" Оно уйдёт из пресетов: {', '.join(used)}." if used else ""
		values = len(field.values)
		lost = f" Словарь ({values} знач.) будет потерян." if values else ""
		if not confirm_delete(
			self, f"Удалить поле «{field.name}» из сообщества?{where}{lost} Это необратимо."
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_field(field.id),
			self,
			lambda *_a: self._on_field_deleted(card),
			self._show_error,
		)

	def _on_field_deleted(self, card: _FieldCard) -> None:
		"""Поле удалено движком: карточка, пул и сохранённый снимок — следом."""
		field_id = card.field_id
		self._pool = [f for f in self._pool if f.id != field_id]
		if self._saved is not None:
			self._saved = without_field(self._saved, field_id)
		if card in self._cards:
			self._remove_card(card)
		for other in self._cards:
			edit = other.edit()
			parent_id = None if edit.parent_field_id == field_id else edit.parent_field_id
			other.set_parent_options(self._pool, parent_id)
		self._refresh()
		self.presets_changed.emit()

	def _open_dictionary(self, card: _FieldCard) -> None:
		"""Окно словаря поля; после — свежие счётчики карточек."""
		field = card.field
		parent_field = next((f for f in self._pool if f.id == field.parent_field_id), None)
		dependents = [f.name for f in self._pool if f.parent_field_id == field.id]
		exec_dialog(DictionaryDialog(self._worker, field, self.window(), parent_field, dependents))
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_fields(self.community.id),
			self,
			self._on_pool_reloaded,
			self._show_error,
		)

	def _on_pool_reloaded(self, pool: list[FieldDto]) -> None:
		self._pool = pool
		by_id = {f.id: f for f in pool}
		for card in self._cards:
			if card.field_id in by_id:
				card.set_field(by_id[card.field_id])

	# --- сохранение и уход ------------------------------------------------------------

	def save(
		self,
		then: Callable[[], None] | None = None,
		failed: Callable[[], None] | None = None,
	) -> None:
		"""Сохраняет пресет одной записью движка.

		``then`` зовётся только после ответа движка (уход с экрана):
		при отказе человек остаётся с правками и видит причину, а
		``failed`` возвращает на место то, что уход успел сдвинуть.
		"""
		problem = next((p for p in (card.problem() for card in self._cards) if p), None)
		if problem is not None:
			self._error.fail(f"Правило разбора не годится: {problem}")
			if failed is not None:
				failed()
			return
		preset_id = self._preset.id if self._preset else None

		def on_failed(message: str) -> None:
			self._error.fail(message)
			if failed is not None:
				failed()

		run_in_engine(
			self._worker,
			self._worker.engine.captions.save_preset(
				self.community.id, self._form().draft(), preset_id
			),
			self,
			partial(self._on_saved, then),
			on_failed,
		)

	def _on_saved(self, then: Callable[[], None] | None, preset: CaptionPresetDto) -> None:
		"""Пресет сохранён: перечитать пул полей (оформление общее) и показать."""
		show_success(self, "Готово", f"Пресет «{preset.name}» сохранён.")
		self.presets_changed.emit()

		# список пресетов для предупреждений об удалении поля — со свежим
		# составом этого пресета (новый пресет в нём ещё не значился)
		others = [p for p in self._others if p.id != preset.id] + [preset]

		def reopen(pool: list[FieldDto]) -> None:
			self.open(preset, pool, others)
			if then is not None:
				then()

		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_fields(self.community.id),
			self,
			reopen,
			self._show_error,
		)

	def discard(self) -> None:
		"""Отбрасывает правки: экран возвращается к сохранённому."""
		self.open(self._preset, self._pool, self._others)

	def leave(self, then: Callable[[], None], *, stay: Callable[[], None] | None = None) -> None:
		"""Уход с экрана: сразу — без правок, иначе по ответу человека."""
		leave_with_question(
			self,
			SAVE_ON_LEAVE_HINT,
			dirty=self._dirty,
			save=lambda done, failed: self.save(then=done, failed=failed),
			discard=self.discard,
			then=then,
			stay=stay,
		)

	def _on_delete_preset(self) -> None:
		preset = self._preset
		if preset is None or not confirm_delete(
			self, f"Удалить пресет «{preset.name}»? Поля и словари останутся у сообщества."
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.delete_preset(preset.id),
			self,
			lambda *_a: self._on_preset_deleted(),
			self._show_error,
		)

	def _on_preset_deleted(self) -> None:
		self._preset = None
		self._saved = None
		self._dirty = False
		self.presets_changed.emit()
		self.closed.emit()
