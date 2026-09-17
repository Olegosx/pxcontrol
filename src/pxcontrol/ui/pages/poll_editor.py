"""Блок опроса в форме поста (ADR-0033, подача C5).

Опрос — не текст и не файл: у него вопрос, варианты и правила
голосования. Блок один на обе формы (новый пост и правка элемента
очереди): правила у них общие, и расходиться им незачем — ровно как
у списка файлов (`media_picker.py`) и клавиатуры (`markup_editor.py`).

Что блок обещает форме:

- собранный опрос (:meth:`poll`) или None, если опрос не выбран;
- пределы Telegram не дают перешагнуть в поле: длина вопроса, варианта
  и пояснения обрезается вводом, а вариантов нельзя добавить больше
  дозволенного (правила — движок, `telegram/poll.py`);
- честная строка о том, что получится: анонимность, множественный
  выбор, викторина с правильным ответом. Отправленный опрос **не
  правится ничем** — ни приложением, ни клиентом Telegram, — поэтому
  цена ошибки тут выше обычной, и итог называется словами до отправки.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, LineEdit, PushButton, RadioButton

from pxcontrol.engine.telegram.poll import (
	MAX_POLL_OPTIONS,
	MIN_POLL_OPTIONS,
	POLL_EXPLANATION_LIMIT,
	POLL_OPTION_LIMIT,
	POLL_QUESTION_LIMIT,
	PollDraft,
	trimmed_poll,
)
from pxcontrol.ui.pages.common import DIM_TEXT, bind, clear_layout, plural, tinted

#: Сколько пустых вариантов показать в новом опросе: минимум, с которого
#: опрос вообще имеет смысл.
START_OPTIONS = MIN_POLL_OPTIONS


def poll_note(poll: PollDraft) -> str:
	"""Строка под блоком: что получится, если отправить сейчас.

	Чистая функция — её проверяет тест, а не глаз. Говорит о том, что
	человек **не видит** в полях: как Telegram покажет опрос читателю
	и что из этого необратимо.
	"""
	filled = [option for option in poll.options if option.strip()]
	parts = [f"{len(filled)} {plural(len(filled), 'вариант', 'варианта', 'вариантов')}"]
	parts.append("анонимно" if poll.anonymous else "видно, кто голосовал")
	if poll.quiz:
		correct = poll.correct_text
		parts.append(f"викторина, правильный — «{correct}»" if correct else "викторина")
	elif poll.multiple:
		parts.append("можно выбрать несколько")
	return "Опрос: " + " · ".join(parts) + ". Отправленный опрос не правится."


class PollEditor(QWidget):
	"""Поля опроса: вопрос, варианты, правила голосования."""

	#: Опрос изменился (форма пересобирает подсказки и правила).
	changed = Signal()

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._options: list[LineEdit] = []
		self._correct = QButtonGroup(self)
		self._correct.setExclusive(True)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(4)
		self._build_question(layout)
		self._options_box = QVBoxLayout()
		self._options_box.setSpacing(2)
		layout.addLayout(self._options_box)
		self._build_add_row(layout)
		self._build_rules(layout)
		self._note = tinted(CaptionLabel("", self), DIM_TEXT)
		self._note.setWordWrap(True)
		layout.addWidget(self._note)
		self.set_poll(None)

	# --- сборка ------------------------------------------------------------------

	def _build_question(self, layout: QVBoxLayout) -> None:
		"""Поле вопроса: его читатель видит заголовком поста."""
		layout.addWidget(BodyLabel("Вопрос", self))
		self._question = LineEdit(self)
		self._question.setMaxLength(POLL_QUESTION_LIMIT)
		self._question.setPlaceholderText("О чём спрашиваем читателей")
		self._question.textChanged.connect(self._on_changed)
		layout.addWidget(self._question)

	def _build_add_row(self, layout: QVBoxLayout) -> None:
		"""Кнопка добавления варианта и счёт оставшихся мест."""
		row = QHBoxLayout()
		self._add = PushButton("Добавить вариант", self)
		self._add.setToolTip(f"Вариантов в опросе может быть до {MAX_POLL_OPTIONS}")
		self._add.clicked.connect(self._add_option)
		row.addWidget(self._add)
		row.addStretch()
		layout.addLayout(row)

	def _build_rules(self, layout: QVBoxLayout) -> None:
		"""Правила голосования: анонимность, несколько ответов, викторина."""
		row = QHBoxLayout()
		self._anonymous = CheckBox("Анонимный", self)
		self._anonymous.setToolTip("Читатели не увидят, кто как проголосовал")
		self._anonymous.setChecked(True)
		self._multiple = CheckBox("Несколько ответов", self)
		self._multiple.setToolTip("Можно выбрать больше одного варианта")
		self._quiz = CheckBox("Викторина", self)
		self._quiz.setToolTip("У опроса есть правильный ответ — отметьте его точкой у варианта")
		for check in (self._anonymous, self._multiple, self._quiz):
			check.stateChanged.connect(self._on_rules_changed)
			row.addWidget(check)
		row.addStretch()
		layout.addLayout(row)
		self._explanation = LineEdit(self)
		self._explanation.setMaxLength(POLL_EXPLANATION_LIMIT)
		self._explanation.setPlaceholderText("Пояснение: его увидит тот, кто ответил неправильно")
		self._explanation.textChanged.connect(self._on_changed)
		layout.addWidget(self._explanation)

	# --- состав -------------------------------------------------------------------

	def poll(self) -> PollDraft:
		"""Собранный опрос (пробелы по краям обрезаны, как при отправке)."""
		correct = self._correct.checkedId()
		return trimmed_poll(
			PollDraft(
				question=self._question.text(),
				options=tuple(str(field.text()) for field in self._options),
				anonymous=self._anonymous.isChecked(),
				multiple=self._multiple.isChecked(),
				quiz=self._quiz.isChecked(),
				correct_option=correct if self._quiz.isChecked() and correct >= 0 else None,
				explanation=self._explanation.text(),
			)
		)

	def set_poll(self, poll: PollDraft | None) -> None:
		"""Показывает готовый опрос (правка элемента очереди) или пустой.

		None — новый опрос: вопрос пуст, вариантов :data:`START_OPTIONS`,
		правила по умолчанию (анонимный обычный опрос — так Telegram
		создаёт опрос сам).
		"""
		source = poll or PollDraft(question="", options=("",) * START_OPTIONS)
		self._question.setText(source.question)
		self._explanation.setText(source.explanation)
		for check, value in (
			(self._anonymous, source.anonymous),
			(self._multiple, source.multiple),
			(self._quiz, source.quiz),
		):
			check.blockSignals(True)
			check.setChecked(value)
			check.blockSignals(False)
		options = list(source.options) or [""] * START_OPTIONS
		self._rebuild(options, source.correct_option)

	def clear(self) -> None:
		"""Забывает опрос (форма освободилась под следующий пост)."""
		self.set_poll(None)

	# --- варианты -----------------------------------------------------------------

	def _add_option(self) -> None:
		"""Добавляет пустой вариант (до предела Telegram)."""
		if len(self._options) >= MAX_POLL_OPTIONS:
			return
		self._rebuild([*self._values(), ""], self._correct.checkedId())

	def _drop_option(self, index: int) -> None:
		"""Убирает вариант; ниже минимума список не опускается."""
		if len(self._options) <= MIN_POLL_OPTIONS:
			return
		values = self._values()
		del values[index]
		correct = self._correct.checkedId()
		if correct == index:
			correct = -1  # правильный ответ убрали — пусть отметят заново
		elif correct > index:
			correct -= 1
		self._rebuild(values, correct if correct >= 0 else None)

	def _values(self) -> list[str]:
		"""Тексты вариантов как они набраны сейчас."""
		return [str(field.text()) for field in self._options]

	def _rebuild(self, values: list[str], correct: int | None) -> None:
		"""Пересобирает ряды вариантов (их число меняется вводом человека)."""
		for button in list(self._correct.buttons()):
			self._correct.removeButton(button)
		clear_layout(self._options_box)
		self._options = []
		for index, value in enumerate(values[:MAX_POLL_OPTIONS]):
			self._options_box.addLayout(self._option_row(index, value, correct))
		self._render()

	def _option_row(self, index: int, value: str, correct: int | None) -> QHBoxLayout:
		"""Один вариант: точка «правильный», поле текста и «убрать»."""
		row = QHBoxLayout()
		mark = RadioButton("", self)
		mark.setToolTip("Правильный ответ викторины")
		mark.setChecked(correct == index)
		mark.setVisible(self._quiz.isChecked())
		mark.toggled.connect(self._on_changed)
		self._correct.addButton(mark, index)
		row.addWidget(mark)
		field = LineEdit(self)
		field.setMaxLength(POLL_OPTION_LIMIT)
		field.setPlaceholderText(f"Вариант {index + 1}")
		field.setText(value)
		field.textChanged.connect(self._on_changed)
		self._options.append(field)
		row.addWidget(field, stretch=1)
		drop = PushButton("✕", self)
		drop.setToolTip("Убрать вариант")
		drop.setFixedWidth(36)
		drop.clicked.connect(bind(self._drop_option, index))
		row.addWidget(drop)
		return row

	# --- показ --------------------------------------------------------------------

	def _on_rules_changed(self) -> None:
		"""Сменились правила голосования: викторина показывает свои поля."""
		quiz = self._quiz.isChecked()
		if quiz and self._multiple.isChecked():
			# у викторины один правильный ответ — движок такой опрос
			# отвергает, поэтому несовместимое снимается сразу
			self._multiple.blockSignals(True)
			self._multiple.setChecked(False)
			self._multiple.blockSignals(False)
		self._rebuild(self._values(), self._correct.checkedId())

	def _on_changed(self) -> None:
		"""Поля изменились: подсказка пересобирается, форма узнаёт."""
		self._render()

	def _render(self) -> None:
		"""Приводит блок к своему состоянию: доступность кнопок и подсказка."""
		quiz = self._quiz.isChecked()
		for button in self._correct.buttons():
			button.setVisible(quiz)
		self._explanation.setVisible(quiz)
		self._multiple.setEnabled(not quiz)
		self._add.setEnabled(len(self._options) < MAX_POLL_OPTIONS)
		self._note.setText(poll_note(self.poll()))
		self.changed.emit()
