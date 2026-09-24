"""Окна подписей: сборка подписи по пресету и редактор словаря поля.

Сборка (`CaptionDialog`) работает на уже загруженных данных и ничего
не тянет из движка: пресет приходит с полями и словарями, а значения
полей с правилом разбора считаются чистой функцией движка из имени
файла (ADR-0042). Редактор словаря (`DictionaryDialog`) правит словарь
через `run_in_engine` прямо из окна; открывается он с экрана пресета.

Настройка полей и пресетов живёт на вкладке «Настройки» сообщества
(:mod:`caption_presets`), а не в отдельном окне.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	BodyLabel,
	CaptionLabel,
	CheckBox,
	ComboBox,
	EditableComboBox,
	FlowLayout,
	FluentIcon,
	LineEdit,
	PillPushButton,
	PushButton,
	RoundMenu,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import (
	CaptionPresetDto,
	FieldDto,
	PresetFieldDto,
	ValueDto,
	build_caption,
)
from pxcontrol.engine.telegram.rich_text import RichText
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	WorkDialog,
	bind,
	clear_layout,
	confirm_delete,
	error_reporter,
	list_area,
)

#: Подпись поля, чьи значения в пакете берутся из имени каждого файла.
PER_FILE_HINT = "из имени каждого файла"


def split_prefill(field: FieldDto, values: list[str]) -> tuple[set[str], list[str]]:
	"""Раскладывает разобранные значения: отметить в словаре или вписать строкой.

	Значение, которое есть в словаре поля (без учёта регистра), отмечается
	пилюлей; остальные уходят в строку новых значений.

	Returns:
		Отмечаемые значения (в нижнем регистре) и значения для строки.
	"""
	known = {name.lower() for name in field.names()}
	picked = {value.lower() for value in values if value.lower() in known}
	typed = [value for value in values if value.lower() not in known]
	return picked, typed


class _FieldRow:
	"""Строка поля в окне сборки: включённость и ввод значений.

	Раскладка — сетка: колонка имён (одинаковой ширины) и колонка
	значений; значения множественных полей — «пилюли»-теги, визуально
	отличимые от чекбокса включения поля.

	У зависимого поля («Character» внутри «Title») показываются не все
	значения словаря, а только принадлежащие выбранному значению
	родителя — :meth:`refresh` перестраивает список при его смене.

	В пакете поле с правилом разбора не правится: у каждого файла
	значение своё, и строка только сообщает об этом.
	"""

	def __init__(
		self,
		dialog: QWidget,
		grid: QGridLayout,
		row: int,
		item: PresetFieldDto,
		prefill: list[str],
		on_changed: Callable[[], None],
		*,
		per_file: bool = False,
	) -> None:
		self.field = item.field
		self.per_file = per_file and item.rule is not None
		self._on_changed = on_changed
		# None — родителя в пресете нет, фильтровать не по чему (весь словарь)
		self._parent_ids: Collection[int] | None = None
		self._picked, typed = split_prefill(self.field, prefill)
		self.check = CheckBox(self.field.name, dialog)
		self.check.setChecked(item.enabled)
		if self.per_file:
			grid.addWidget(self.check, row, 0, Qt.AlignmentFlag.AlignLeft)
			grid.addWidget(CaptionLabel(PER_FILE_HINT, dialog), row, 1)
		elif self.field.style.multiple:
			grid.addWidget(
				self.check,
				row,
				0,
				Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
			)
			grid.addWidget(self._build_multi(dialog, typed), row, 1)
		else:
			grid.addWidget(self.check, row, 0, Qt.AlignmentFlag.AlignLeft)
			grid.addWidget(self._build_single(dialog, prefill), row, 1)

	def _build_single(self, dialog: QWidget, prefill: list[str]) -> QWidget:
		"""Одно значение: редактируемый список со словарём."""
		self._edit = EditableComboBox(dialog)
		self._edit.currentTextChanged.connect(self._changed)
		# qfluentwidgets не типизирован: без явной аннотации mypy видит Any
		widget: QWidget = self._edit
		self._fill_single()
		if prefill:
			self._edit.setText(prefill[0])
		return widget

	def _build_multi(self, dialog: QWidget, typed: list[str]) -> QWidget:
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
		self._line.setText(", ".join(typed))
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
		"""Пересобирает «пилюли», сохраняя отметки уцелевших значений.

		Разобранные из имени файла значения отмечаются при первой сборке;
		дальше отметки ведёт человек.
		"""
		checked = {str(p.text()).lower() for p in self._pills if p.isChecked()} | self._picked
		self._picked = set()
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
			pill.setChecked(item.value.lower() in checked)  # до connect: без лишнего сигнала
			pill.toggled.connect(self._changed)
			flow.addWidget(pill)
			self._pills.append(pill)
		self._pills_box.addWidget(host)

	def _changed(self, *_args: object) -> None:
		"""Выбор изменился — окно перестроит зависимые поля."""
		self._on_changed()

	def refresh(self, parent_ids: Collection[int] | None) -> None:
		"""Перестраивает список значений под выбранные значения родителя.

		``None`` — родительского поля в пресете нет: фильтровать не по чему,
		показывается весь словарь.
		"""
		self._parent_ids = parent_ids
		if self.per_file:
			return
		if self.field.style.multiple:
			self._fill_pills()
		else:
			self._fill_single()

	def values(self) -> list[str]:
		"""Введённые значения: отмеченные пилюли + строка (без дублей).

		У поля «из имени каждого файла» значений в окне нет.
		"""
		if self.per_file:
			return []
		if not self.field.style.multiple:
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


class CaptionDialog(WorkDialog):
	"""Сборка подписи по пресету: поля со словарями и разбором имени файла.

	``source`` — имя файла без расширения и суффикса конвейера
	(``filename_source`` движка); None — файла нет. Поля с правилом
	разбора заполняются из него заранее и правятся руками.

	``per_file`` — режим пакета (ADR-0015): поля с правилом разбора
	не правятся — значения у каждого файла свои, — а окно собирает
	общие значения остальных полей.
	"""

	def __init__(
		self,
		presets: list[CaptionPresetDto],
		source: str | None,
		parent: QWidget,
		*,
		per_file: bool = False,
	) -> None:
		super().__init__("Собрать подпись", parent, size=(680, 640))
		self._presets = presets
		self._source = source
		self._per_file = per_file
		self._rows: list[_FieldRow] = []
		self._refreshing = False
		self._build_preset_combo()
		# поля с их словарями — в прокручиваемой области: у пресета
		# с десятком полей, да ещё с пилюлями значений, они не влезают
		area, box = list_area(self, spacing=density.spacing().row_spacing)
		fields_host = QWidget(self)
		self._fields_grid = QGridLayout(fields_host)
		self._fields_grid.setContentsMargins(0, 0, 0, 0)
		self._fields_grid.setHorizontalSpacing(16)
		self._fields_grid.setVerticalSpacing(10)
		self._fields_grid.setColumnStretch(1, 1)
		box.addWidget(fields_host)
		box.addStretch()
		self.content.addWidget(area, stretch=1)
		# индекс и перерисовка — после сборки формы, сигнал подключаем последним
		index = self._last_used_index()
		self._combo.setCurrentIndex(index)
		self._show_preset(index)
		self._combo.currentIndexChanged.connect(self._show_preset)
		self.add_accept_buttons("Вставить в подпись" if not per_file else "Готово")

	def _build_preset_combo(self) -> None:
		"""Выбор пресета подписи (виден всегда, даже если пресет один)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Пресет подписи:", self))
		self._combo = ComboBox(self)
		for preset in self._presets:
			self._combo.addItem(preset.name)
		row.addWidget(self._combo, stretch=1)
		self.content.addLayout(row)

	def _last_used_index(self) -> int:
		"""Индекс последнего использованного пресета (или первого)."""
		stamps = [
			(p.last_used_at, i) for i, p in enumerate(self._presets) if p.last_used_at is not None
		]
		return max(stamps)[1] if stamps else 0

	def _show_preset(self, index: int) -> None:
		"""Перестраивает сетку полей под выбранный пресет."""
		clear_layout(self._fields_grid)
		preset = self._presets[index]
		parsed = preset.parsed(self._source) if self._source is not None else {}
		self._rows = [
			_FieldRow(
				self,
				self._fields_grid,
				row,
				item,
				parsed.get(item.field.id, []),
				self._refresh_dependents,
				per_file=self._per_file,
			)
			for row, item in enumerate(preset.fields)
		]
		self._refresh_dependents()

	def _refresh_dependents(self) -> None:
		"""Перестраивает списки зависимых полей под выбор их родителей.

		Вызывается при смене пресета и при любом изменении выбора: список
		персонажей должен отвечать выбранному тайтлу сразу, а не после
		переоткрытия окна. Флаг ``_refreshing`` защищает от повторного
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

	def preset(self) -> CaptionPresetDto:
		"""Выбранный пресет."""
		return self._presets[int(self._combo.currentIndex())]

	def enabled_ids(self) -> list[int]:
		"""Поля, отмеченные для этой подписи (в порядке пресета)."""
		return [row.field.id for row in self._rows if row.check.isChecked()]

	def values(self) -> dict[int, list[str]]:
		"""Значения отмеченных полей по их id (поля без значений — мимо).

		Годятся и для сборки, и для пополнения словарей: поля с правилом
		разбора движок в словарь не пускает сам (``record_usage``).
		"""
		return {
			row.field.id: row.values()
			for row in self._rows
			if row.check.isChecked() and row.values()
		}

	def caption(self) -> RichText:
		"""Собранная подпись: текст и его разметка (ADR-0033)."""
		return build_caption(self.preset().lines(self.values(), self.enabled_ids()))


#: Имя группы значений, не привязанных ни к какому значению родителя.
_NO_PARENT_GROUP = "Без привязки"


class _ValueChip(PillPushButton):
	"""Значение словаря пилюлей с крестиком удаления.

	Внутри пилюли — обычная компоновка Qt: надпись и кнопка-крестик.
	Всё расставляет и меряет она; своих расчётов размеров, положения
	и обрезки текста здесь нет.

	Две оговорки, из-за которых класс вообще существует:

	1. Кнопка сама компоновку о размере не спрашивает (``sizeHint``
	у неё свой, по тексту), поэтому ширину берём у компоновки,
	а высоту — у самой кнопки, чтобы совпасть с пилюлями выбора.
	2. Свой лист стилей задавать нельзя: библиотека вешает на каждую
	кнопку собственный (``FluentStyleSheet.BUTTON``), наш заменил бы
	его целиком — вместе с правилом, которое гасит рисование коробки,
	и поверх нарисованной пилюли встала бы вторая рамка. Поэтому
	отступы задаются полями компоновки, а не стилем.
	"""

	#: Поля внутри пилюли: слева — под надпись, справа — под крестик.
	MARGINS = (12, 0, 6, 0)
	#: Сторона кнопки-крестика и её значка (точек).
	CLOSE_SIZE = 20
	CLOSE_ICON_SIZE = 10

	def __init__(self, text: str, parent: QWidget, on_delete: Callable[[], None]) -> None:
		# конструктор родителя — в форме «только родитель»: форма
		# с текстом у библиотеки перевызывает self.__init__(parent=…),
		# и наша сигнатура с обязательными аргументами её ломает
		super().__init__(parent=parent)
		self.setCheckable(False)  # это не выбор, а показ значения
		self.setToolTip(text)
		row = QHBoxLayout(self)
		row.setContentsMargins(*self.MARGINS)
		row.setSpacing(density.spacing().list_spacing)
		self._label = BodyLabel(text, self)
		row.addWidget(self._label)
		close = TransparentToolButton(FluentIcon.CLOSE, self)
		close.setFixedSize(self.CLOSE_SIZE, self.CLOSE_SIZE)
		close.setIconSize(QSize(self.CLOSE_ICON_SIZE, self.CLOSE_ICON_SIZE))
		close.setToolTip(f"Удалить «{text}»")
		close.clicked.connect(on_delete)
		row.addWidget(close)

	def value_text(self) -> str:
		"""Показанное значение (``text()`` у самой кнопки пуст)."""
		return str(self._label.text())

	def sizeHint(self) -> QSize:  # noqa: N802 — API Qt
		"""Ширина — от компоновки, высота — штатная кнопочная."""
		return QSize(int(self.layout().sizeHint().width()), int(super().sizeHint().height()))


class DictionaryDialog(WorkDialog):
	"""Редактор словаря поля: значения пилюлями, удаление, добавление.

	Словарь пополняется и сам — из значений, введённых при сборке
	подписи; здесь он правится руками: опечатки и устаревшие значения
	удаляются, новые добавляются пачкой через запятую.

	Значения показываются пилюлями в поточной раскладке: словарь легко
	вырастает до сотен значений, и строка на каждое занимала бы экраны
	пустого места справа. Прокрутка — у области с пилюлями: окно имеет
	собственный размер, поэтому область показывает полосу сама, когда
	значения не влезли.

	У зависимого поля («Character» внутри «Title») значения сгруппированы
	по родителю: заголовок группы — тайтл, под ним его персонажи, в конце
	группа «Без привязки». Перенести значение в другой тайтл — правый
	щелчок по пилюле.
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
		super().__init__(f"Словарь поля «{field.name}»", parent)
		self._worker = worker
		self._field = field
		self._parent_field = parent_field
		self._dependent_names = list(dependent_names)
		self._show_error = error_reporter(self)
		if parent_field is not None:
			self.content.addWidget(
				CaptionLabel(
					f"Значения живут внутри поля «{parent_field.name}» и сгруппированы "
					"по нему. Правый щелчок по значению — перенести в другую группу.",
					self,
				)
			)
		self._build_values_area()
		self._build_add_row()
		self.add_close_button("Готово")
		self._show_values(field)

	# --- сборка окна ----------------------------------------------------------

	def _build_values_area(self) -> None:
		"""Прокручиваемая область значений (растёт на всю высоту окна).

		Полоса прокрутки появляется сама, когда пилюли не помещаются:
		высоту содержимого считает поточная раскладка
		(``heightForWidth``), а область — общая для рабочих окон
		(ADR-0023), собранная руками она повторяла бы её построчно.
		"""
		area, self._values_box = list_area(self, density.spacing().row_spacing)
		self.content.addWidget(area, stretch=1)

	def _build_add_row(self) -> None:
		"""Строка добавления: значения через запятую и (для зависимого) родитель."""
		row = QHBoxLayout()
		self._new_values = LineEdit(self)
		self._new_values.setPlaceholderText("новые значения через запятую…")
		self._new_values.returnPressed.connect(self._on_add)
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
		self.content.addLayout(row)

	# --- показ ------------------------------------------------------------------

	def _show_values(self, field: FieldDto) -> None:
		"""Перерисовывает значения: одна лента или группы по родителю."""
		self._field = field
		clear_layout(self._values_box)
		if not field.values:
			self._values_box.addWidget(
				CaptionLabel(
					"Словарь пуст — добавьте значения здесь или при сборке подписи.",
					self,
				)
			)
			self._values_box.addStretch()
			return
		if self._parent_field is None:
			self._values_box.addWidget(self._chips_row(field.values))
		else:
			for title, values in self._grouped(field.values):
				self._values_box.addWidget(CaptionLabel(title, self))
				self._values_box.addWidget(self._chips_row(values))
		# распорка прижимает содержимое кверху: короткий словарь
		# не растягивается на всю высоту окна
		self._values_box.addStretch()

	def _grouped(self, values: list[ValueDto]) -> list[tuple[str, list[ValueDto]]]:
		"""Значения по группам «родитель → его значения»; пустые группы — мимо.

		Порядок групп — как у значений родительского поля; значения без
		привязки (и с исчезнувшим родителем) идут последней группой.
		"""
		assert self._parent_field is not None  # группы есть только у зависимого
		by_parent: dict[int | None, list[ValueDto]] = {}
		known = {parent.id for parent in self._parent_field.values}
		for value in values:
			key = value.parent_id if value.parent_id in known else None
			by_parent.setdefault(key, []).append(value)
		groups = [
			(parent.value, by_parent[parent.id])
			for parent in self._parent_field.values
			if parent.id in by_parent
		]
		if None in by_parent:
			groups.append((_NO_PARENT_GROUP, by_parent[None]))
		return groups

	def _chips_row(self, values: list[ValueDto]) -> QWidget:
		"""Пилюли значений в поточной раскладке (переносятся по ширине)."""
		host = QWidget(self)
		flow = FlowLayout(host, needAni=False)
		flow.setContentsMargins(0, 0, 0, 0)
		for value in values:
			flow.addWidget(self._chip(value))
		return host

	def _chip(self, value: ValueDto) -> _ValueChip:
		"""Пилюля значения: крестик удаляет, правый щелчок переносит."""
		chip = _ValueChip(value.value, self, bind(self._on_delete_value, value))
		if self._parent_field is not None:
			chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
			chip.customContextMenuRequested.connect(bind(self._on_chip_menu, (value, chip)))
		return chip

	# --- операции ----------------------------------------------------------------

	def _on_chip_menu(self, pair: tuple[ValueDto, _ValueChip]) -> None:
		"""Меню переноса значения в другую группу (правый щелчок)."""
		assert self._parent_field is not None  # меню вешается только у зависимого
		value, chip = pair
		menu = RoundMenu(parent=self)
		for parent in self._parent_field.values:
			if parent.id == value.parent_id:
				continue
			action = Action(f"Перенести в «{parent.value}»", self)
			action.triggered.connect(bind(self._on_reparent, (value, parent.id)))
			menu.addAction(action)
		if value.parent_id is not None:
			detach = Action("Убрать привязку", self)
			detach.triggered.connect(bind(self._on_reparent, (value, None)))
			menu.addAction(detach)
		menu.exec(chip.mapToGlobal(chip.rect().bottomLeft()))

	def _on_reparent(self, pair: tuple[ValueDto, int | None]) -> None:
		"""Сохраняет новую привязку значения."""
		value, parent_id = pair
		run_in_engine(
			self._worker,
			self._worker.engine.captions.assign_value_parent(value.id, parent_id),
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
