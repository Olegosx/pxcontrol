"""Редакторы значений настроек сообщества — по одному на вид значения (ADR-0043).

Экран «Настройки › В Telegram» строит себя по каталогу движка: для
каждой настройки берёт редактор её вида из :data:`EDITORS`. Новая
настройка Telegram, вид значения которой уже есть (переключатель,
выбор, текст…), появляется на экране без правки интерфейса; новый вид
значения — это новый редактор здесь и строка в таблице.

Общий контракт — :class:`SettingEditor`: сигнал ``changed``, чтение
``value()`` и показ ``set_value()`` (без сигнала — показ не правка).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	BodyLabel,
	CaptionLabel,
	CheckBox,
	ComboBox,
	FlowLayout,
	LineEdit,
	PillPushButton,
	PlainTextEdit,
	PushButton,
	RoundMenu,
	SpinBox,
	SwitchButton,
)

from pxcontrol.engine.community_settings.catalog import SettingSpec
from pxcontrol.engine.community_settings.model import (
	Choice,
	LinkedChat,
	PhotoValue,
	ReactionsValue,
	SettingsContext,
	SettingValue,
	ValueKind,
)
from pxcontrol.engine.telegram.rights import MemberRights
from pxcontrol.engine.telegram.types import ChatReactionsMode
from pxcontrol.ui import density
from pxcontrol.ui.pages.common import clear_layout, pick_file

#: Подписи разрешений участников — в порядке показа.
PERMISSION_LABELS: dict[str, str] = {
	"send_plain": "Текст",
	"send_photos": "Фото",
	"send_videos": "Видео",
	"send_roundvideos": "Видеосообщения",
	"send_audios": "Музыка",
	"send_voices": "Голосовые",
	"send_docs": "Файлы",
	"send_stickers": "Стикеры",
	"send_gifs": "GIF",
	"send_games": "Игры",
	"send_inline": "Встроенные боты",
	"embed_links": "Превью ссылок",
	"send_polls": "Опросы",
	"send_reactions": "Реакции",
	"invite_users": "Добавлять участников",
	"pin_messages": "Закреплять сообщения",
	"change_info": "Менять информацию группы",
	"manage_topics": "Создавать темы",
	"edit_rank": "Менять свой тег",
}

#: Режимы реакций: подпись → режим движка.
REACTION_MODES: tuple[tuple[str, ChatReactionsMode], ...] = (
	("Любые", ChatReactionsMode.ALL),
	("Только выбранные", ChatReactionsMode.SOME),
	("Никаких", ChatReactionsMode.NONE),
)

#: Картинки, которые Telegram принимает фото сообщества.
_IMAGE_FILTER = "Картинки (*.jpg *.jpeg *.png)"


@dataclass(frozen=True)
class EditorContext:
	"""Что редактору нужно знать о сообществе, кроме своего значения.

	Attributes:
		settings: контекст снимка (справочник реакций, пределы сервера).
		load_discussion: запросить у движка группы для обсуждения —
			ответ приходит в переданную функцию (None — выбирать нельзя).
	"""

	settings: SettingsContext
	load_discussion: Callable[[Callable[[list[LinkedChat]], None]], None] | None = None


class SettingEditor(QWidget):
	"""Общий контракт редактора: значение, показ и сигнал правки.

	Конструктор у всех редакторов один — ``(spec, ctx, parent)``: так
	таблица :data:`EDITORS` строит любой из них одинаково.
	"""

	changed = Signal()

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(parent)
		self.spec = spec
		self.ctx = ctx

	def value(self) -> SettingValue:
		raise NotImplementedError

	def set_value(self, value: SettingValue) -> None:
		raise NotImplementedError

	def _emit(self, *_args: object) -> None:
		self.changed.emit()


def _row(parent: QWidget) -> QHBoxLayout:
	row = QHBoxLayout(parent)
	row.setContentsMargins(0, 0, 0, 0)
	row.setSpacing(density.spacing().row_spacing)
	return row


class ToggleEditor(SettingEditor):
	"""Переключатель да / нет."""

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		row = _row(self)
		self._switch = SwitchButton(self)
		self._switch.setOnText("вкл.")
		self._switch.setOffText("выкл.")
		self._switch.checkedChanged.connect(self._emit)
		row.addWidget(self._switch)
		row.addStretch()

	def value(self) -> SettingValue:
		return bool(self._switch.isChecked())

	def set_value(self, value: SettingValue) -> None:
		self._switch.blockSignals(True)
		try:
			self._switch.setChecked(bool(value))
		finally:
			self._switch.blockSignals(False)


class TextEditor(SettingEditor):
	"""Строка или многострочный текст со счётчиком предела."""

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		self._limit = spec.max_length
		column = QVBoxLayout(self)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(2)
		if spec.multiline:
			self._plain: PlainTextEdit | None = PlainTextEdit(self)
			self._plain.setFixedHeight(90)
			self._plain.textChanged.connect(self._on_text)
			column.addWidget(self._plain)
			self._line: LineEdit | None = None
		else:
			self._plain = None
			self._line = LineEdit(self)
			self._line.textChanged.connect(self._on_text)
			column.addWidget(self._line)
		self._counter = CaptionLabel(self)
		self._counter.setVisible(self._limit is not None)
		column.addWidget(self._counter)

	def value(self) -> SettingValue:
		if self._plain is not None:
			return str(self._plain.toPlainText())
		assert self._line is not None
		return str(self._line.text())

	def set_value(self, value: SettingValue) -> None:
		text = value if isinstance(value, str) else ""
		widget = self._plain if self._plain is not None else self._line
		assert widget is not None
		widget.blockSignals(True)
		try:
			if self._plain is not None:
				self._plain.setPlainText(text)
			elif self._line is not None:
				self._line.setText(text)
		finally:
			widget.blockSignals(False)
		self._show_counter()

	def _on_text(self, *_args: object) -> None:
		self._show_counter()
		self.changed.emit()

	def _show_counter(self) -> None:
		if self._limit is not None:
			self._counter.setText(f"{len(str(self.value()))} / {self._limit}")


class ChoiceEditor(SettingEditor):
	"""Одно значение из перечня каталога."""

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		self._choices = list(spec.choices)
		row = _row(self)
		self._combo = ComboBox(self)
		for choice in self._choices:
			self._combo.addItem(choice.label)
		self._combo.currentIndexChanged.connect(self._emit)
		row.addWidget(self._combo)
		row.addStretch()

	def value(self) -> SettingValue:
		index = int(self._combo.currentIndex())
		return self._choices[index].value if 0 <= index < len(self._choices) else None

	def set_value(self, value: SettingValue) -> None:
		"""Показывает значение; незнакомое каталогу — отдельным пунктом.

		В Telegram могли выставить значение, которого нет в перечне
		(другим клиентом): без своего пункта оно показалось бы пустым,
		и первое же сохранение затёрло бы его как «изменённое».
		"""
		values = [choice.value for choice in self._choices]
		if isinstance(value, int | str) and not isinstance(value, bool) and value not in values:
			self._choices.append(Choice(value, f"{value} — задано вне приложения"))
			self._combo.addItem(self._choices[-1].label)
			values.append(value)
		self._combo.blockSignals(True)
		try:
			self._combo.setCurrentIndex(values.index(value) if value in values else -1)
		finally:
			self._combo.blockSignals(False)


class PermissionsEditor(SettingEditor):
	"""Разрешения участников галочками в три колонки."""

	COLUMNS = 3

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		grid = QGridLayout(self)
		grid.setContentsMargins(0, 0, 0, 0)
		self._boxes: dict[str, CheckBox] = {}
		for index, (name, label) in enumerate(PERMISSION_LABELS.items()):
			box = CheckBox(label, self)
			box.stateChanged.connect(self._emit)
			grid.addWidget(box, index // self.COLUMNS, index % self.COLUMNS)
			self._boxes[name] = box

	def value(self) -> SettingValue:
		return MemberRights(**{name: box.isChecked() for name, box in self._boxes.items()})

	def set_value(self, value: SettingValue) -> None:
		for name, box in self._boxes.items():
			box.blockSignals(True)
			try:
				box.setChecked(bool(getattr(value, name, False)))
			finally:
				box.blockSignals(False)


class ReactionsEditor(SettingEditor):
	"""Режим реакций, выбранные эмодзи и предел разных реакций под сообщением."""

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		self._catalog = ctx.settings.reaction_catalog
		column = QVBoxLayout(self)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(density.spacing().row_spacing)
		head = QHBoxLayout()
		self._mode = ComboBox(self)
		for label, _mode in REACTION_MODES:
			self._mode.addItem(label)
		self._mode.currentIndexChanged.connect(self._on_mode)
		head.addWidget(self._mode)
		head.addWidget(BodyLabel("разных под сообщением не больше:", self))
		self._limit = SpinBox(self)
		self._limit.setRange(0, ctx.settings.reactions_max or 11)
		self._limit.setSpecialValueText("по умолчанию")
		self._limit.valueChanged.connect(self._emit)
		head.addWidget(self._limit)
		head.addStretch()
		column.addLayout(head)
		self._pills_host = QWidget(self)
		self._flow = FlowLayout(self._pills_host, needAni=False)
		self._flow.setContentsMargins(0, 0, 0, 0)
		self._pills: dict[str, PillPushButton] = {}
		column.addWidget(self._pills_host)

	def value(self) -> SettingValue:
		mode = REACTION_MODES[max(int(self._mode.currentIndex()), 0)][1]
		emojis = tuple(emoji for emoji, pill in self._pills.items() if pill.isChecked())
		limit = int(self._limit.value()) or None
		return ReactionsValue(mode, emojis if mode is ChatReactionsMode.SOME else (), limit)

	def set_value(self, value: SettingValue) -> None:
		default = ReactionsValue(ChatReactionsMode.ALL)
		current = value if isinstance(value, ReactionsValue) else default
		modes = [mode for _label, mode in REACTION_MODES]
		self.blockSignals(True)
		try:
			self._mode.setCurrentIndex(modes.index(current.mode))
			self._limit.setValue(current.limit or 0)
			self._fill_pills(current.emojis)
		finally:
			self.blockSignals(False)
		self._pills_host.setVisible(current.mode is ChatReactionsMode.SOME)

	def _fill_pills(self, chosen: tuple[str, ...]) -> None:
		"""Пилюли всех стандартных реакций; выбранные — отмечены.

		Выбранная реакция, которой нет в справочнике (сервер снял её
		из списка), всё равно показывается — молча терять её нельзя.
		"""
		clear_layout(self._flow)
		self._pills = {}
		for emoji in dict.fromkeys((*self._catalog, *chosen)):
			pill = PillPushButton(emoji, self._pills_host)
			pill.setChecked(emoji in chosen)
			pill.toggled.connect(self._emit)
			self._flow.addWidget(pill)
			self._pills[emoji] = pill

	def _on_mode(self, *_args: object) -> None:
		mode = REACTION_MODES[max(int(self._mode.currentIndex()), 0)][1]
		self._pills_host.setVisible(mode is ChatReactionsMode.SOME)
		self.changed.emit()


class PhotoEditor(SettingEditor):
	"""Фото сообщества: есть ли, выбрать новую картинку, убрать."""

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		self._value = PhotoValue(False)
		row = _row(self)
		self._state = BodyLabel(self)
		row.addWidget(self._state)
		choose = PushButton("Выбрать…", self)
		choose.clicked.connect(self._on_choose)
		row.addWidget(choose)
		self._remove = PushButton("Убрать", self)
		self._remove.clicked.connect(self._on_remove)
		row.addWidget(self._remove)
		row.addStretch()

	def value(self) -> SettingValue:
		return self._value

	def set_value(self, value: SettingValue) -> None:
		self._value = value if isinstance(value, PhotoValue) else PhotoValue(False)
		self._show()

	def _show(self) -> None:
		if self._value.upload is not None:
			self._state.setText(f"Новое: {Path(self._value.upload).name}")
		else:
			self._state.setText("Фото есть" if self._value.present else "Фото нет")
		self._remove.setEnabled(self._value.present or self._value.upload is not None)

	def _on_choose(self) -> None:
		path = pick_file(self, "Фото сообщества", _IMAGE_FILTER)
		if path:
			self._value = PhotoValue(True, path)
			self._show()
			self.changed.emit()

	def _on_remove(self) -> None:
		self._value = PhotoValue(False)
		self._show()
		self.changed.emit()


class LinkedChatEditor(SettingEditor):
	"""Группа обсуждения канала: выбор из того, что разрешает сервер."""

	def __init__(self, spec: SettingSpec, ctx: EditorContext, parent: QWidget) -> None:
		super().__init__(spec, ctx, parent)
		self._load = ctx.load_discussion
		self._value = LinkedChat(None)
		row = _row(self)
		self._state = BodyLabel(self)
		row.addWidget(self._state)
		self._choose = PushButton("Выбрать…", self)
		self._choose.setEnabled(self._load is not None)
		self._choose.clicked.connect(self._on_choose)
		row.addWidget(self._choose)
		row.addStretch()

	def value(self) -> SettingValue:
		return self._value

	def set_value(self, value: SettingValue) -> None:
		self._value = value if isinstance(value, LinkedChat) else LinkedChat(None)
		self._show()

	def _show(self) -> None:
		if self._value.chat_id is None:
			self._state.setText("не связана")
		else:
			self._state.setText(self._value.title or self._value.chat_id)

	def _on_choose(self) -> None:
		if self._load is not None:
			self._load(self._show_menu)

	def _show_menu(self, groups: list[LinkedChat]) -> None:
		menu = RoundMenu(parent=self)
		for group in groups:
			action = Action(group.title or group.chat_id or "?", menu)
			action.triggered.connect(partial(self._pick, group))
			menu.addAction(action)
		if not groups:
			empty = Action("Подходящих групп нет", menu)
			empty.setEnabled(False)
			menu.addAction(empty)
		menu.addSeparator()
		unlink = Action("Отвязать", menu)
		unlink.triggered.connect(partial(self._pick, LinkedChat(None)))
		menu.addAction(unlink)
		menu.exec(self._choose.mapToGlobal(self._choose.rect().bottomLeft()))

	def _pick(self, group: LinkedChat, *_args: object) -> None:
		self._value = group
		self._show()
		self.changed.emit()


#: Редактор каждого вида значения.
EDITORS: dict[ValueKind, type[SettingEditor]] = {
	ValueKind.TOGGLE: ToggleEditor,
	ValueKind.TEXT: TextEditor,
	ValueKind.CHOICE: ChoiceEditor,
	ValueKind.PERMISSIONS: PermissionsEditor,
	ValueKind.REACTIONS: ReactionsEditor,
	ValueKind.PHOTO: PhotoEditor,
	ValueKind.LINKED_CHAT: LinkedChatEditor,
}


def build_editor(
	spec: SettingSpec, value: SettingValue, ctx: EditorContext, parent: QWidget
) -> SettingEditor:
	"""Редактор настройки по виду её значения — с показанным значением."""
	editor = EDITORS[spec.kind](spec, ctx, parent)
	editor.set_value(value)
	return editor
