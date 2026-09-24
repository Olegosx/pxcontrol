"""Редактор правила «взять значение поля из имени файла» (ADR-0042).

Правило (``SourceRule`` движка) — извлечение выражением, цепочка замен,
регистр и разделитель значений. Виджет живёт в карточке поля на экране
пресета подписи; до ADR-0042 цепочка замен была блоком «Правила разбора
имени файла» на экране «Пакет» и давала одно лишь название.

Проверку правила делает движок (``check_rule`` — общая точка): экран
показывает причину рядом с полями и не даёт сохранить битое правило.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, LineEdit, PushButton

from pxcontrol.engine.services.captions import (
	DEFAULT_SEPARATOR,
	EXTRACT_PRESETS,
	STEP_PRESETS,
	CaptionsError,
	CaseMode,
	ReplaceStep,
	SourceRule,
	check_rule,
	compile_step,
)
from pxcontrol.ui import density
from pxcontrol.ui.pages.common import ErrorLabel, bind, clear_layout, elide_text

#: Наименьшая ширина полей шага очистки: в тесной карточке без неё
#: подсказка «что найти…» сжималась до многоточия.
_EDIT_MIN_WIDTH = 160

#: Режимы регистра: подпись → режим движка.
CASE_MODES: list[tuple[str, CaseMode]] = [
	("Как есть", CaseMode.KEEP),
	("Каждое Слово С Заглавной", CaseMode.EVERY_WORD),
	("Только первая буква", CaseMode.FIRST_WORD),
]


def replacement_text(replacement: str) -> str:
	"""Замена шага для списка: кавычки делают видимым пробел.

	Пустая замена — это удаление, так и пишем словом. Всё остальное
	берём в кавычки: без них шаг «[_-] → » выглядел бы оборванным,
	а пробел в замене — самый ходовой случай.
	"""
	return f"«{replacement}»" if replacement else "удалить"


def rule_problem(rule: SourceRule) -> str | None:
	"""Причина, по которой правило не годится; None — правило годное."""
	try:
		check_rule(rule)
	except CaptionsError as exc:
		return str(exc)
	return None


def _presets_combo(parent: QWidget, presets: tuple[tuple[str, str], ...]) -> ComboBox:
	"""Выпадающий помощник с заготовками выражений (пункт не «залипает»)."""
	combo = ComboBox(parent)
	combo.setPlaceholderText("Заготовки")
	for label, _pattern in presets:
		combo.addItem(label)
	combo.setCurrentIndex(-1)
	return combo


class RuleEditor(QWidget):
	"""Правка правила разбора: извлечение, цепочка замен, регистр, разделитель.

	Сигнал ``changed`` — после любой правки правила (карточка поля
	пересчитывает результат на образце, экран — признак правок).
	Разделитель виден только у поля с несколькими значениями
	(:meth:`set_multiple`).
	"""

	changed = Signal()

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._steps: list[ReplaceStep] = []
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().row_spacing)
		layout.addLayout(self._build_extract_row())
		layout.addLayout(self._build_step_row())
		self._step_error = ErrorLabel(self)
		layout.addWidget(self._step_error)
		self._steps_box = QVBoxLayout()
		self._steps_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._steps_box)
		layout.addLayout(self._build_tail_row())
		self._problem = ErrorLabel(self)
		layout.addWidget(self._problem)
		self._show_steps()

	# --- сборка ---------------------------------------------------------------

	def _build_extract_row(self) -> QHBoxLayout:
		"""Извлечение: выражение поиска и заготовки к нему."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Извлечь:", self))
		self._extract = LineEdit(self)
		self._extract.setPlaceholderText(
			r"выражение, например \(([^()]*)\)\s*$ — пусто: имя целиком"
		)
		self._extract.setToolTip(
			"Значение — группа (?P<value>…), иначе первая группа, иначе всё совпадение. "
			"Нет совпадения — у поля нет значения."
		)
		self._extract.textChanged.connect(self._on_changed)
		row.addWidget(self._extract, stretch=1)
		self._extract_presets = _presets_combo(self, EXTRACT_PRESETS)
		self._extract_presets.currentIndexChanged.connect(self._insert_extract_preset)
		row.addWidget(self._extract_presets)
		return row

	def _build_step_row(self) -> QHBoxLayout:
		"""Новый шаг очистки: что найти → на что заменить, заготовки, «Добавить»."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Очистка:", self))
		self._step_edit = LineEdit(self)
		self._step_edit.setPlaceholderText(r"что найти, например [_-]")
		self._step_edit.setToolTip(
			"Регулярное выражение поиска. Без учёта регистра — начните с (?i)"
		)
		self._step_edit.returnPressed.connect(self._add_step)
		self._step_edit.setMinimumWidth(_EDIT_MIN_WIDTH)
		row.addWidget(self._step_edit, stretch=2)
		row.addWidget(BodyLabel("→", self))
		self._replace_edit = LineEdit(self)
		self._replace_edit.setPlaceholderText("на что заменить (пусто — удалить)")
		self._replace_edit.setToolTip(
			"Чем заменить совпадение. Пусто — удаление; пробел — обычное "
			"значение: им разбивают слипшиеся слова (например [_-] → пробел). "
			"Допустимы ссылки на группы выражения: \\1, \\g<имя>"
		)
		self._replace_edit.returnPressed.connect(self._add_step)
		self._replace_edit.setMinimumWidth(_EDIT_MIN_WIDTH)
		row.addWidget(self._replace_edit, stretch=1)
		self._step_presets = _presets_combo(self, STEP_PRESETS)
		self._step_presets.currentIndexChanged.connect(self._insert_step_preset)
		row.addWidget(self._step_presets)
		add = PushButton("Добавить", self)
		add.setToolTip("Поставить шаг в конец цепочки очистки")
		add.clicked.connect(self._add_step)
		row.addWidget(add)
		return row

	def _build_tail_row(self) -> QHBoxLayout:
		"""Регистр значения и разделитель значений."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Регистр:", self))
		self._case = ComboBox(self)
		for label, _mode in CASE_MODES:
			self._case.addItem(label)
		self._case.currentIndexChanged.connect(self._on_changed)
		row.addWidget(self._case)
		self._separator_label = BodyLabel("Разделитель значений:", self)
		row.addWidget(self._separator_label)
		self._separator = LineEdit(self)
		self._separator.setPlaceholderText("выражение, например ,|&")
		self._separator.setFixedWidth(160)
		self._separator.textChanged.connect(self._on_changed)
		row.addWidget(self._separator)
		row.addStretch()
		return row

	# --- правило ----------------------------------------------------------------

	def set_rule(self, rule: SourceRule) -> None:
		"""Показывает правило (без сигнала ``changed``)."""
		self.blockSignals(True)
		try:
			self._extract.setText(rule.extract)
			self._steps = list(rule.steps)
			modes = [mode for _label, mode in CASE_MODES]
			self._case.setCurrentIndex(modes.index(rule.case))
			self._separator.setText(rule.separator)
		finally:
			self.blockSignals(False)
		self._show_steps()
		self._show_problem()

	def rule(self) -> SourceRule:
		"""Правило по состоянию полей."""
		return SourceRule(
			extract=str(self._extract.text()),
			steps=tuple(self._steps),
			case=CASE_MODES[max(int(self._case.currentIndex()), 0)][1],
			separator=str(self._separator.text()) or DEFAULT_SEPARATOR,
		)

	def set_multiple(self, multiple: bool) -> None:
		"""Разделитель нужен только полю с несколькими значениями."""
		self._separator_label.setVisible(multiple)
		self._separator.setVisible(multiple)

	# --- правка ------------------------------------------------------------------

	def _insert_extract_preset(self, index: int) -> None:
		"""Вставляет заготовку в поле извлечения — дальше её правят руками."""
		if 0 <= index < len(EXTRACT_PRESETS):
			self._extract.setText(EXTRACT_PRESETS[index][1])
			self._extract_presets.setCurrentIndex(-1)
			self._extract.setFocus()

	def _insert_step_preset(self, index: int) -> None:
		"""Вставляет заготовку в поле «что найти»; замену решает автор."""
		if 0 <= index < len(STEP_PRESETS):
			self._step_edit.setText(STEP_PRESETS[index][1])
			self._step_presets.setCurrentIndex(-1)
			self._step_edit.setFocus()

	def _add_step(self) -> None:
		"""Ставит шаг из полей в конец цепочки; битый — причина под полями.

		Замена берётся как есть, без обрезки краёв: пробел в ней —
		значащий (``[_-]`` → пробел разбивает слипшиеся слова).
		"""
		pattern = str(self._step_edit.text()).strip()
		if not pattern:
			return
		step = ReplaceStep(pattern, str(self._replace_edit.text()))
		try:
			compile_step(step)
		except CaptionsError as exc:
			self._step_error.fail(str(exc))
			return
		self._step_error.succeed()
		self._steps.append(step)
		self._step_edit.clear()
		self._replace_edit.clear()
		self._show_steps()
		self._on_changed()

	def _remove_step(self, index: int) -> None:
		"""Убирает шаг по номеру: два одинаковых шага не снимают друг друга."""
		if 0 <= index < len(self._steps):
			del self._steps[index]
			self._show_steps()
			self._on_changed()

	def _show_steps(self) -> None:
		"""Перерисовывает цепочку очистки (в порядке применения)."""
		clear_layout(self._steps_box)
		if not self._steps:
			self._steps_box.addWidget(CaptionLabel("Шагов очистки нет.", self))
			return
		for index, step in enumerate(self._steps):
			row = QHBoxLayout()
			text = f"{index + 1}. {step.pattern} → {replacement_text(step.replacement)}"
			label = BodyLabel(text, self)
			label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
			elide_text(label, text)
			row.addWidget(label, stretch=1)
			remove = PushButton("Убрать", self)
			remove.clicked.connect(bind(self._remove_step, index))
			row.addWidget(remove)
			self._steps_box.addLayout(row)

	def _on_changed(self, *_args: object) -> None:
		"""Правило изменилось: причина под полями и сигнал наружу."""
		self._show_problem()
		self.changed.emit()

	def _show_problem(self) -> None:
		problem = rule_problem(self.rule())
		if problem is None:
			self._problem.succeed()
		else:
			self._problem.fail(problem)
