"""Блок кнопок под постом: редактор клавиатуры и его правила (ADR-0031).

Кнопки — не часть текста, а отдельное поле сообщения, и ставит их только
бот. Для человека это значит две неочевидные вещи, о которых форма
обязана сказать **до** отправки, а не после:

1. пост с кнопками уходит от лица бота (если файл ему по силам) — значит
   и предел подписи у него бот-овский, вчетверо меньше Premium-ного;
2. у крупного файла кнопки появляются через секунду после выхода поста —
   его отправляет публикатор, а бот дорисовывает разметку.

Пределы самой клавиатуры (8 кнопок в ряду, 100 рядов, 128 символов
в подписи) Telegram не объявляет и молча обрезает лишнее, поэтому
редактор не даёт их перешагнуть: ряд перестаёт принимать кнопки,
а поля ограничены по длине.

Чистые правила (:func:`limits_for_route`, :func:`markup_notice`) отделены
от виджетов и покрыты тестами — интерфейс Qt тестами не покрывается
(см. `docs/08-development/testing.md`).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, LineEdit, PushButton, ToolButton

from pxcontrol.engine.services.posts import TextLimits
from pxcontrol.engine.services.publish_route import PublishRoute
from pxcontrol.engine.telegram.markup import (
	BUTTON_TEXT_LIMIT,
	COPY_TEXT_LIMIT,
	MAX_BUTTONS_IN_ROW,
	MAX_ROWS,
	ButtonKind,
	MarkupError,
	PostButton,
	PostMarkup,
	validate_markup,
)
from pxcontrol.engine.telegram.types import CAPTION_LENGTH_LIMIT, TEXT_LENGTH_LIMIT

#: Подписи видов кнопок для человека (порядок — порядок в списке).
KIND_LABELS: dict[ButtonKind, str] = {
	ButtonKind.LINK: "Ссылка",
	ButtonKind.COPY: "Скопировать текст",
}

#: Подсказки в поле значения — своя на каждый вид.
VALUE_PLACEHOLDERS: dict[ButtonKind, str] = {
	ButtonKind.LINK: "https://…",
	ButtonKind.COPY: "Текст, который скопируется по нажатию",
}

#: Предел длины поля значения по виду кнопки. У ссылки предел не наш,
#: а Telegram (он проверяет адрес сам), поэтому просто не мешаем.
VALUE_LIMITS: dict[ButtonKind, int] = {
	ButtonKind.LINK: 2048,
	ButtonKind.COPY: COPY_TEXT_LIMIT,
}


def limits_for_route(limits: TextLimits, route: PublishRoute) -> TextLimits:
	"""Пределы длины текста, действующие на этом маршруте.

	Пост, который отправляет бот, ограничен базовыми пределами Telegram:
	подписки у ботов не бывает, и Premium-пределы публикателя к нему
	не относятся. Правило то же, что у движка (он проверяет пределы
	по маршруту), — здесь оно нужно счётчику символов, чтобы тот
	не обещал больше, чем пройдёт.
	"""
	if route is not PublishRoute.BOT:
		return limits
	return TextLimits(text=TEXT_LENGTH_LIMIT, caption=CAPTION_LENGTH_LIMIT)


def markup_notice(route: PublishRoute, bot_label: str | None, *, scheduled: bool = False) -> str:
	"""Что меняется в посте из-за кнопок (пустая строка — ничего).

	Текст показывается рядом с блоком кнопок: человек должен узнать
	о смене лица поста, о задержке кнопок и о том, что в режиме «кнопки
	важнее» пост уходит из приложения, — заранее, а не из канала.
	"""
	who = f"бот «{bot_label}»" if bot_label else "бот"
	if route is PublishRoute.BOT and scheduled:
		return (
			f"Пост отправит {who} в назначенную минуту: кнопки будут с первой "
			"секунды, но приложение в это время должно работать. Предел подписи — "
			f"{CAPTION_LENGTH_LIMIT} знаков."
		)
	if route is PublishRoute.BOT:
		return (
			f"Пост с кнопками отправит {who} — предел подписи у него "
			f"{CAPTION_LENGTH_LIMIT} знаков, Premium-пределы не действуют."
		)
	if route is PublishRoute.USERBOT_MARKUP and scheduled:
		return (
			f"Отложку держит сервер Telegram (выйдет даже при закрытом приложении), "
			f"а {who} поставит кнопки после выхода — недолго пост будет без них."
		)
	if route is PublishRoute.USERBOT_MARKUP:
		return (
			f"Файл крупный, поэтому пост отправит публикатор, а {who} "
			"дорисует кнопки сразу после выхода — секунду пост будет без них."
		)
	return ""


class _ButtonEditor(QWidget):
	"""Одна кнопка: вид, подпись, значение и удаление."""

	changed = Signal()
	removed = Signal(QWidget)

	def __init__(self, parent: QWidget, button: PostButton | None = None) -> None:
		super().__init__(parent)
		row = QHBoxLayout(self)
		row.setContentsMargins(0, 0, 0, 0)
		self._kind = ComboBox(self)
		for kind, label in KIND_LABELS.items():
			self._kind.addItem(label, userData=kind.value)
		self._kind.currentIndexChanged.connect(self._on_kind_changed)
		self._text = LineEdit(self)
		self._text.setPlaceholderText("Подпись на кнопке")
		self._text.setMaxLength(BUTTON_TEXT_LIMIT)
		self._text.textChanged.connect(self.changed)
		self._value = LineEdit(self)
		self._value.textChanged.connect(self.changed)
		drop = ToolButton(FluentIcon.DELETE, self)
		drop.setToolTip("Убрать кнопку")
		drop.clicked.connect(lambda: self.removed.emit(self))
		row.addWidget(self._kind)
		row.addWidget(self._text, 2)
		row.addWidget(self._value, 3)
		row.addWidget(drop)
		if button is not None:
			self._kind.setCurrentIndex(list(KIND_LABELS).index(button.kind))
			self._text.setText(button.text)
			self._value.setText(button.value)
		self._apply_kind()

	def _on_kind_changed(self, _index: int = 0) -> None:
		"""Смена вида меняет подсказку и предел значения."""
		self._apply_kind()
		self.changed.emit()

	def _apply_kind(self) -> None:
		"""Приводит поле значения к выбранному виду кнопки."""
		kind = self.kind()
		self._value.setPlaceholderText(VALUE_PLACEHOLDERS[kind])
		self._value.setMaxLength(VALUE_LIMITS[kind])

	def kind(self) -> ButtonKind:
		"""Выбранный вид кнопки."""
		return ButtonKind(str(self._kind.currentData()))

	def button(self) -> PostButton | None:
		"""Кнопка из полей (None — поля пусты, кнопки нет)."""
		text = str(self._text.text()).strip()
		value = str(self._value.text()).strip()
		if not text and not value:
			return None
		return PostButton(self.kind(), text, value)


class _RowEditor(QWidget):
	"""Ряд клавиатуры: свои кнопки, добавление и удаление ряда."""

	changed = Signal()
	removed = Signal(QWidget)

	def __init__(self, parent: QWidget, buttons: tuple[PostButton, ...] = ()) -> None:
		super().__init__(parent)
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._buttons: list[_ButtonEditor] = []
		header = QHBoxLayout()
		self._title = CaptionLabel("Ряд", self)
		self._add = PushButton(FluentIcon.ADD, "Кнопка", self)
		self._add.clicked.connect(lambda: self.add_button())
		drop_row = ToolButton(FluentIcon.DELETE, self)
		drop_row.setToolTip("Убрать ряд целиком")
		drop_row.clicked.connect(lambda: self.removed.emit(self))
		header.addWidget(self._title)
		header.addStretch()
		header.addWidget(self._add)
		header.addWidget(drop_row)
		self._layout.addLayout(header)
		for button in buttons or (None,):  # пустой ряд — с одной заготовкой
			self.add_button(button, notify=False)

	def set_number(self, number: int) -> None:
		"""Показывает номер ряда (нумерация пересчитывается при удалении)."""
		self._title.setText(f"Ряд {number}")

	def add_button(self, button: PostButton | None = None, notify: bool = True) -> None:
		"""Добавляет кнопку в ряд (до предела Telegram — 8 в ряду)."""
		if len(self._buttons) >= MAX_BUTTONS_IN_ROW:
			return
		editor = _ButtonEditor(self, button)
		editor.changed.connect(self.changed)
		editor.removed.connect(self._drop_button)
		self._buttons.append(editor)
		self._layout.addWidget(editor)
		self._sync_add()
		if notify:
			self.changed.emit()

	def _drop_button(self, editor: QWidget) -> None:
		"""Убирает кнопку; опустевший ряд просит снять себя целиком."""
		if not isinstance(editor, _ButtonEditor):
			return
		self._buttons.remove(editor)
		editor.setParent(None)
		editor.deleteLater()
		self._sync_add()
		if not self._buttons:
			self.removed.emit(self)
			return
		self.changed.emit()

	def _sync_add(self) -> None:
		"""Гасит «Кнопка», когда ряд полон: сверх предела Telegram обрежет молча."""
		full = len(self._buttons) >= MAX_BUTTONS_IN_ROW
		self._add.setEnabled(not full)
		self._add.setToolTip(
			f"В ряду не больше {MAX_BUTTONS_IN_ROW} кнопок — лишние Telegram отбросит"
			if full
			else ""
		)

	def buttons(self) -> tuple[PostButton, ...]:
		"""Заполненные кнопки ряда (пустые заготовки пропускаются)."""
		return tuple(button for editor in self._buttons if (button := editor.button()))


class MarkupEditor(QWidget):
	"""Блок кнопок под постом: ряды, проверка пределов и объяснения.

	Общий для страницы «Публикация» и правки элемента очереди — правила
	кнопок обеим формам нужны одни и те же.
	"""

	changed = Signal()

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		outer = QVBoxLayout(self)
		outer.setContentsMargins(0, 0, 0, 0)
		self._body = QWidget(self)
		self._rows_layout = QVBoxLayout(self._body)
		self._rows_layout.setContentsMargins(0, 0, 0, 0)
		self._rows: list[_RowEditor] = []
		outer.addWidget(self._body)
		bottom = QHBoxLayout()
		self._add_row = PushButton(FluentIcon.ADD, "Ряд кнопок", self)
		self._add_row.clicked.connect(lambda: self.add_row())
		bottom.addWidget(self._add_row)
		bottom.addStretch()
		outer.addLayout(bottom)
		self._mode_box = QWidget(self)
		mode_row = QHBoxLayout(self._mode_box)
		mode_row.setContentsMargins(0, 0, 0, 0)
		mode_row.addWidget(CaptionLabel("Что важнее у отложенного поста:", self._mode_box))
		self._mode = ComboBox(self._mode_box)
		self._mode.addItem("публикация — кнопки появятся после выхода", userData=False)
		self._mode.addItem("кнопки — пост уйдёт из приложения точно в срок", userData=True)
		self._mode.currentIndexChanged.connect(lambda _index: self._on_changed())
		mode_row.addWidget(self._mode, 1)
		self._mode_box.setVisible(False)
		outer.addWidget(self._mode_box)
		self._hint = CaptionLabel("", self)
		self._hint.setWordWrap(True)
		outer.addWidget(self._hint)
		self._blocked: str | None = None
		self._notice = ""

	# --- состав -----------------------------------------------------------

	def add_row(self, buttons: tuple[PostButton, ...] = (), notify: bool = True) -> None:
		"""Добавляет ряд кнопок (до предела Telegram — 100 рядов)."""
		if len(self._rows) >= MAX_ROWS:
			return
		row = _RowEditor(self._body, buttons)
		row.changed.connect(self._on_changed)
		row.removed.connect(self._drop_row)
		self._rows.append(row)
		self._rows_layout.addWidget(row)
		self._renumber()
		if notify:
			self._on_changed()

	def _drop_row(self, row: QWidget) -> None:
		"""Убирает ряд целиком."""
		if not isinstance(row, _RowEditor):
			return
		self._rows.remove(row)
		row.setParent(None)
		row.deleteLater()
		self._renumber()
		self._on_changed()

	def _renumber(self) -> None:
		"""Обновляет номера рядов и доступность «Ряд кнопок»."""
		for number, row in enumerate(self._rows, start=1):
			row.set_number(number)
		full = len(self._rows) >= MAX_ROWS
		self._add_row.setEnabled(not full and self._blocked is None)
		if full:
			self._add_row.setToolTip(f"Больше {MAX_ROWS} рядов Telegram не покажет")

	def markup(self) -> PostMarkup | None:
		"""Клавиатура из полей (None — кнопок нет)."""
		rows = tuple(buttons for row in self._rows if (buttons := row.buttons()))
		markup = PostMarkup(rows)
		return markup if markup else None

	def set_markup(self, markup: PostMarkup | None) -> None:
		"""Заполняет редактор готовой клавиатурой (правка элемента очереди)."""
		for row in list(self._rows):
			self._drop_row(row)
		for buttons in markup.rows if markup else ():
			self.add_row(buttons, notify=False)
		self._refresh_hint()

	# --- доступность и подсказки -------------------------------------------

	def markup_first(self) -> bool:
		"""Выбран ли режим «кнопки важнее» (ADR-0031, п. 4).

		У поста «сейчас» выбора нет и он не спрашивается: кнопки и так
		уходят вместе с постом.
		"""
		return bool(self._mode_box.isVisibleTo(self) and self._mode.currentData())

	def set_markup_first(self, value: bool) -> None:
		"""Показывает сохранённый выбор режима (правка элемента очереди)."""
		self._mode.setCurrentIndex(1 if value else 0)

	def set_mode_available(self, available: bool) -> None:
		"""Показывает выбор режима — он есть только у отложенного поста с кнопками."""
		self._mode_box.setVisible(available)

	def set_blocked(self, reason: str | None) -> None:
		"""Запрещает кнопки с названной причиной (None — снова можно).

		Причину даёт движок (`markup_blocker`) — одна на движок
		и интерфейс, чтобы форма объясняла отказ теми же словами,
		которыми потом отказала бы отправка.
		"""
		self._blocked = reason
		self._body.setEnabled(reason is None)
		self._add_row.setEnabled(reason is None and len(self._rows) < MAX_ROWS)
		self._refresh_hint()

	def set_notice(self, notice: str) -> None:
		"""Показывает, что меняется в посте из-за кнопок (:func:`markup_notice`)."""
		self._notice = notice
		self._refresh_hint()

	def _on_changed(self) -> None:
		"""Любая правка: обновить подсказку и сообщить наружу."""
		self._refresh_hint()
		self.changed.emit()

	def _refresh_hint(self) -> None:
		"""Собирает подсказку: запрет, ошибка пределов или последствие кнопок."""
		if self._blocked is not None:
			self._hint.setText(self._blocked)
			return
		markup = self.markup()
		if markup is not None:
			try:
				validate_markup(markup)
			except MarkupError as exc:
				self._hint.setText(str(exc))
				return
		self._hint.setText(self._notice if markup is not None else "")
