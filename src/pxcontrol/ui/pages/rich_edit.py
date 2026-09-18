"""Поле текста поста с оформлением: выделил — нажал стиль (ADR-0033, C2).

Звёздочек человек больше не пишет. Поле показывает пост таким, каким
его увидит читатель, а разметка живёт рядом — сущностями со смещениями
(:mod:`pxcontrol.engine.telegram.rich_text`). Модуль отвечает за перевод
между документом Qt и этими сущностями в обе стороны.

**Почему перевод сходится.** Qt считает позиции в документе теми же
кодовыми единицами UTF-16, что и Telegram (проверено: эмодзи занимает
две, конец абзаца — одну), поэтому арифметики пересчёта здесь нет
вовсе — это и есть причина, по которой смещения не разъезжаются
на эмодзи. Опасная часть перевода — склейка соседних кусков одного
стиля — вынесена чистой функцией (:func:`merge_runs`) и покрыта
тестами: ошибка здесь испортила бы чужой пост.

Стили, которых у Qt нет (спойлер, цитата, раскрывающаяся цитата,
моноширинный кусок, блок кода), держатся своими свойствами формата;
жирный, курсив, подчёркивание, зачёркивание и ссылка — штатными.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import (
	QColor,
	QFont,
	QKeySequence,
	QShortcut,
	QTextCharFormat,
	QTextCursor,
	QTextDocument,
	QTextFormat,
)
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, LineEdit, MessageBoxBase, TextEdit, ToolButton

from pxcontrol.engine.telegram.rich_text import (
	RichText,
	TextEntity,
	TextStyle,
)
from pxcontrol.engine.telegram.types import known_scheme
from pxcontrol.ui.pages.common import ErrorLabel, exec_dialog

#: Свойства формата под стили, которых у Qt нет. Значения начинаются
#: от ``UserProperty``: пространство до него принадлежит Qt.
_OWN_PROPERTIES: dict[TextStyle, int] = {
	TextStyle.SPOILER: QTextFormat.Property.UserProperty + 1,
	TextStyle.QUOTE: QTextFormat.Property.UserProperty + 2,
	TextStyle.EXPANDABLE_QUOTE: QTextFormat.Property.UserProperty + 3,
	TextStyle.CODE: QTextFormat.Property.UserProperty + 4,
	TextStyle.PRE: QTextFormat.Property.UserProperty + 5,
}

#: Подложка спойлера и цитат: полупрозрачный серый виден и на светлой,
#: и на тёмной теме — свой цвет темы тут заводить незачем.
_TINT = QColor(128, 128, 128, 60)

#: Подпись и подсказка кнопки каждого стиля (порядок — порядок кнопок).
_TOOLS: list[tuple[TextStyle, str, str]] = [
	(TextStyle.BOLD, "Ж", "Жирный (Ctrl+B)"),
	(TextStyle.ITALIC, "К", "Курсив (Ctrl+I)"),
	(TextStyle.UNDERLINE, "Ч", "Подчёркнутый (Ctrl+U)"),
	(TextStyle.STRIKE, "З", "Зачёркнутый"),
	(TextStyle.CODE, "</>", "Моноширинный кусок"),
	(TextStyle.SPOILER, "Спойлер", "Скрытый текст — читатель раскрывает нажатием"),
	(TextStyle.QUOTE, "Цитата", "Цитата"),
	(TextStyle.EXPANDABLE_QUOTE, "Цитата ▾", "Цитата, которую читатель раскрывает"),
	(TextStyle.LINK, "Ссылка", "Ссылка с подписью (Ctrl+K)"),
]

#: Горячие клавиши стилей — привычные по любому текстовому редактору.
_SHORTCUTS: dict[TextStyle, QKeySequence.StandardKey | str] = {
	TextStyle.BOLD: QKeySequence.StandardKey.Bold,
	TextStyle.ITALIC: QKeySequence.StandardKey.Italic,
	TextStyle.UNDERLINE: QKeySequence.StandardKey.Underline,
	TextStyle.LINK: "Ctrl+K",
}


@dataclass(frozen=True)
class StyleRun:
	"""Отрезок текста с набором стилей — промежуточный вид перевода.

	Attributes:
		start: начало в кодовых единицах UTF-16.
		length: длина в тех же единицах.
		styles: стили отрезка (вид → значение: адрес ссылки, язык блока
			кода; у остальных пустая строка).
	"""

	start: int
	length: int
	styles: tuple[tuple[TextStyle, str], ...]


def merge_runs(runs: Sequence[StyleRun]) -> tuple[TextEntity, ...]:
	"""Склеивает соседние отрезки одного стиля в сущности разметки.

	Документ Qt дробится на отрезки по любой смене формата: жирный кусок
	внутри спойлера разрежет спойлер надвое. Отдавать Telegram два куска
	вместо одного нельзя — он их примет, но правка и сравнение с тем, что
	вернёт сервер, перестанут сходиться. Поэтому отрезки с одинаковым
	стилем **и одинаковым значением**, идущие встык, склеиваются.

	Чистая функция: здесь живёт вся арифметика перевода, и здесь же —
	его тесты.
	"""
	opened: dict[tuple[TextStyle, str], int] = {}
	result: list[TextEntity] = []
	position = 0
	for run in sorted(runs, key=lambda item: item.start):
		if run.start > position:  # разрыв — всё открытое закрывается
			_close(opened, position, result, keep=())
		current = dict(run.styles)
		_close(opened, run.start, result, keep=tuple(current.items()))
		for key in current.items():
			opened.setdefault(key, run.start)
		position = run.start + run.length
	_close(opened, position, result, keep=())
	return tuple(sorted(result, key=lambda entity: (entity.offset, entity.length)))


def _close(
	opened: dict[tuple[TextStyle, str], int],
	position: int,
	result: list[TextEntity],
	keep: tuple[tuple[TextStyle, str], ...],
) -> None:
	"""Закрывает открытые стили, которых нет в ``keep``, на позиции."""
	for key in [key for key in opened if key not in keep]:
		start = opened.pop(key)
		style, value = key
		if position > start:
			result.append(TextEntity(style, start, position - start, value))


def format_for(styles: Iterable[tuple[TextStyle, str]]) -> QTextCharFormat:
	"""Формат Qt для набора стилей (обратный перевод — для показа)."""
	fmt = QTextCharFormat()
	for style, value in styles:
		if style is TextStyle.BOLD:
			fmt.setFontWeight(QFont.Weight.Bold)
		elif style is TextStyle.ITALIC:
			fmt.setFontItalic(True)
		elif style is TextStyle.UNDERLINE:
			fmt.setFontUnderline(True)
		elif style is TextStyle.STRIKE:
			fmt.setFontStrikeOut(True)
		elif style is TextStyle.LINK:
			fmt.setAnchor(True)
			fmt.setAnchorHref(value)
			fmt.setFontUnderline(True)
		else:
			fmt.setProperty(_OWN_PROPERTIES[style], value or True)
			if style in (TextStyle.CODE, TextStyle.PRE):
				fmt.setFontFamilies(["monospace"])
			if style is not TextStyle.CODE:
				fmt.setBackground(_TINT)
	return fmt


def styles_of(fmt: QTextCharFormat) -> tuple[tuple[TextStyle, str], ...]:
	"""Стили куска текста по его формату Qt."""
	styles: list[tuple[TextStyle, str]] = []
	if fmt.fontWeight() >= QFont.Weight.Bold:
		styles.append((TextStyle.BOLD, ""))
	if fmt.fontItalic():
		styles.append((TextStyle.ITALIC, ""))
	if fmt.fontStrikeOut():
		styles.append((TextStyle.STRIKE, ""))
	if fmt.isAnchor() and fmt.anchorHref():
		styles.append((TextStyle.LINK, str(fmt.anchorHref())))
	elif fmt.fontUnderline():
		# подчёркивание ссылки — её вид, а не отдельный стиль
		styles.append((TextStyle.UNDERLINE, ""))
	for style, prop in _OWN_PROPERTIES.items():
		value = fmt.property(prop)
		if value:
			styles.append((style, value if isinstance(value, str) else ""))
	return tuple(styles)


def rich_of_document(document: QTextDocument) -> RichText:
	"""Собирает размеченный текст из документа Qt.

	Позиции Qt — те же кодовые единицы UTF-16, что у Telegram, поэтому
	смещение куска это просто «начало абзаца плюс начало куска».
	"""
	runs: list[StyleRun] = []
	block = document.firstBlock()
	while block.isValid():
		for piece in block.textFormats():
			styles = styles_of(piece.format)
			if styles:
				runs.append(StyleRun(block.position() + piece.start, piece.length, styles))
		block = block.next()
	return RichText(document.toPlainText(), merge_runs(runs))


def apply_rich(document: QTextDocument, rich: RichText) -> None:
	"""Показывает размеченный текст в документе Qt (обратный перевод)."""
	document.setPlainText(rich.text)
	for entity in rich.entities:
		cursor = QTextCursor(document)
		cursor.setPosition(entity.offset)
		cursor.setPosition(entity.offset + entity.length, QTextCursor.MoveMode.KeepAnchor)
		cursor.mergeCharFormat(format_for(((entity.style, entity.value),)))


class _LinkDialog(MessageBoxBase):
	"""Короткий вопрос: адрес для выделенного куска текста."""

	def __init__(self, parent: QWidget, current: str = "") -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(BodyLabel("Адрес ссылки", self))
		self._edit = LineEdit(self)
		self._edit.setPlaceholderText("https://…")
		self._edit.setText(current)
		self.viewLayout.addWidget(self._edit)
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("Поставить")
		self.cancelButton.setText("Отмена")

	def validate(self) -> bool:  # noqa: N802 — API MessageBoxBase
		"""Не пускает адрес, который Telegram всё равно отвергнет."""
		if not self.url():
			return self._error.fail("Укажите адрес — иначе ссылку ставить не на что.")
		if not known_scheme(self.url()):
			return self._error.fail(
				"Telegram принимает адреса, начинающиеся с https://, http:// или tg://."
			)
		return self._error.succeed()

	def url(self) -> str:
		"""Введённый адрес."""
		return str(self._edit.text()).strip()


class RichPostEdit(QWidget):
	"""Поле текста поста со строкой оформления.

	Форма работает с ним как с полем: ``edit`` — то же ``TextEdit``,
	что было раньше (к нему цепляются счётчик символов и подсказка),
	а :meth:`rich` отдаёт текст вместе с разметкой.
	"""

	def __init__(self, parent: QWidget, height: int | None = None) -> None:
		"""Args:
		parent: виджет-владелец.
		height: фиксированная высота поля (None — обычная политика).
		"""
		super().__init__(parent)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(4)
		self._tools = QHBoxLayout()
		layout.addLayout(self._tools)
		self.edit = TextEdit(self)
		if height is not None:
			self.edit.setFixedHeight(height)
		layout.addWidget(self.edit)
		self._build_tools()

	def _build_tools(self) -> None:
		"""Кнопки стилей и горячие клавиши к ним."""
		for style, label, tip in _TOOLS:
			button = ToolButton(self)
			button.setText(label)
			button.setToolTip(tip)
			button.clicked.connect(_bind_style(self.apply_style, style))
			self._tools.addWidget(button)
		clear = ToolButton(self)
		clear.setText("✕")
		clear.setToolTip("Снять оформление с выделенного")
		clear.clicked.connect(self.clear_style)
		self._tools.addWidget(clear)
		self._tools.addStretch()
		for style, key in _SHORTCUTS.items():
			shortcut = QShortcut(QKeySequence(key), self.edit)
			shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
			shortcut.activated.connect(_bind_style(self.apply_style, style))

	# --- работа со стилями ---------------------------------------------------

	def apply_style(self, style: TextStyle) -> None:
		"""Накладывает или снимает стиль на выделенном куске.

		Без выделения делать нечего: стиль живёт на куске текста,
		а не на курсоре — и молча промолчать честнее, чем делать вид,
		что что-то произошло.
		"""
		cursor = self.edit.textCursor()
		if not cursor.hasSelection():
			return
		if style is TextStyle.LINK:
			self._apply_link(cursor)
			return
		if _has_style(cursor.charFormat(), style):
			self._strip(cursor, style)
			return
		cursor.mergeCharFormat(format_for(((style, ""),)))

	def clear_style(self) -> None:
		"""Снимает всё оформление с выделенного куска."""
		cursor = self.edit.textCursor()
		if cursor.hasSelection():
			cursor.setCharFormat(QTextCharFormat())

	def _apply_link(self, cursor: QTextCursor) -> None:
		"""Спрашивает адрес и делает выделенное ссылкой."""
		dialog = _LinkDialog(self.window(), str(cursor.charFormat().anchorHref() or ""))
		if exec_dialog(dialog):
			cursor.mergeCharFormat(format_for(((TextStyle.LINK, dialog.url()),)))

	def _strip(self, cursor: QTextCursor, style: TextStyle) -> None:
		"""Снимает один стиль, не трогая остальные на том же куске.

		Документ пересобирается целиком: снять одно свойство формата,
		не задев соседние, штатными средствами Qt нельзя. Выделение
		после пересборки возвращается на место — иначе человек терял бы
		его на каждом нажатии и не мог снять два стиля подряд.
		"""
		text = self.rich()
		kept = tuple(
			entity
			for entity in text.entities
			if not (
				entity.style is style
				and entity.offset < cursor.selectionEnd()
				and cursor.selectionStart() < entity.offset + entity.length
			)
		)
		start, end = cursor.selectionStart(), cursor.selectionEnd()
		self.set_rich(RichText(text.text, kept))
		restored = self.edit.textCursor()
		restored.setPosition(start)
		restored.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
		self.edit.setTextCursor(restored)

	# --- содержимое -----------------------------------------------------------

	def rich(self) -> RichText:
		"""Текст поста вместе с разметкой."""
		return rich_of_document(self.edit.document())

	def set_rich(self, rich: RichText) -> None:
		"""Показывает готовый размеченный текст (правка поста)."""
		apply_rich(self.edit.document(), rich)

	def clear(self) -> None:
		"""Очищает поле (форма освобождается под следующий пост)."""
		self.edit.clear()


def _has_style(fmt: QTextCharFormat, style: TextStyle) -> bool:
	"""Стоит ли стиль на куске под курсором."""
	return any(item[0] is style for item in styles_of(fmt))


def _bind_style(action: Callable[[TextStyle], None], style: TextStyle) -> Callable[[], None]:
	"""Ранняя привязка стиля к обработчику (как ``common.bind``)."""

	def handler() -> None:
		action(style)

	return handler
