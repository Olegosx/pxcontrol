"""Опрос как содержимое поста: правила, хранение, форма (ADR-0033, C5).

Цена ошибки здесь выше обычной: отправленный опрос Telegram не даёт
править ничем — ни приложением, ни своим клиентом. Поэтому правила
проверяются до отправки, и каждое из них — под замком теста.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.telegram.poll import (
	MAX_POLL_OPTIONS,
	POLL_EXPLANATION_LIMIT,
	POLL_OPTION_LIMIT,
	POLL_QUESTION_LIMIT,
	PollDraft,
	PollError,
	poll_from_json,
	poll_to_json,
	trimmed_poll,
	validate_poll,
)
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.pages.poll_editor import poll_note


def _poll(**kwargs: object) -> PollDraft:
	base: dict[str, object] = {"question": "Любимый цвет?", "options": ("Синий", "Зелёный")}
	base.update(kwargs)
	return PollDraft(**base)  # type: ignore[arg-type]


def test_plain_poll_passes() -> None:
	"""Обычный опрос из вопроса и двух вариантов проходит проверку."""
	validate_poll(_poll())


def test_question_and_options_must_be_filled() -> None:
	"""Пустой вопрос и пустой вариант Telegram встречает ошибкой разбора."""
	with pytest.raises(PollError, match="нет вопроса"):
		validate_poll(_poll(question="   "))
	with pytest.raises(PollError, match="Вариант 2 пуст"):
		validate_poll(_poll(options=("Синий", "  ")))


def test_option_count_bounds() -> None:
	"""Вариантов — от двух (наше правило) до предела Telegram."""
	with pytest.raises(PollError, match="хотя бы 2"):
		validate_poll(_poll(options=("Один",)))
	many = tuple(f"Вариант {number}" for number in range(MAX_POLL_OPTIONS + 1))
	with pytest.raises(PollError, match=f"предел Telegram — {MAX_POLL_OPTIONS}"):
		validate_poll(_poll(options=many))
	# ровно предел — проходит: границу занижать нельзя
	validate_poll(_poll(options=many[:MAX_POLL_OPTIONS]))


def test_length_limits_count_utf16_units() -> None:
	"""Длина считается как у Telegram: эмодзи занимает две единицы."""
	validate_poll(_poll(question="я" * POLL_QUESTION_LIMIT))
	with pytest.raises(PollError, match="Вопрос опроса длиннее"):
		validate_poll(_poll(question="я" * (POLL_QUESTION_LIMIT + 1)))
	with pytest.raises(PollError, match="Вариант 1 длиннее"):
		validate_poll(_poll(options=("я" * (POLL_OPTION_LIMIT + 1), "Зелёный")))
	# эмодзи — суррогатная пара: половина предела длиной в две единицы
	emoji_question = "🙂" * (POLL_QUESTION_LIMIT // 2)
	validate_poll(_poll(question=emoji_question))
	with pytest.raises(PollError, match="Вопрос опроса длиннее"):
		validate_poll(_poll(question=emoji_question + "🙂"))


def test_quiz_needs_exactly_one_correct_answer() -> None:
	"""Викторина без правильного ответа и с чужим номером отвергается."""
	with pytest.raises(PollError, match="нужен правильный ответ"):
		validate_poll(_poll(quiz=True))
	with pytest.raises(PollError, match="несуществующий вариант"):
		validate_poll(_poll(quiz=True, correct_option=5))
	validate_poll(_poll(quiz=True, correct_option=1))


def test_quiz_and_multiple_choice_are_incompatible() -> None:
	"""Множественный выбор Bot API у викторины молча игнорирует — мы отказываем."""
	with pytest.raises(PollError, match="множественный выбор"):
		validate_poll(_poll(quiz=True, correct_option=0, multiple=True))


def test_quiz_fields_belong_to_quiz_only() -> None:
	"""Правильный ответ и пояснение вне викторины — знак ошибки формы."""
	with pytest.raises(PollError, match="только у викторины"):
		validate_poll(_poll(correct_option=0))
	with pytest.raises(PollError, match="только в викторине"):
		validate_poll(_poll(explanation="потому что"))
	with pytest.raises(PollError, match="Пояснение викторины длиннее"):
		validate_poll(
			_poll(quiz=True, correct_option=0, explanation="я" * (POLL_EXPLANATION_LIMIT + 1))
		)


def test_trimmed_poll_strips_every_text() -> None:
	"""В базу и в Telegram уезжает ровно то, что проверено."""
	trimmed = trimmed_poll(
		_poll(question="  Вопрос  ", options=("  А ", " Б "), quiz=True, correct_option=0)
	)
	assert trimmed.question == "Вопрос"
	assert trimmed.options == ("А", "Б")


def test_poll_survives_storage() -> None:
	"""Опрос переживает хранение в колонке БД без потерь."""
	poll = _poll(anonymous=False, quiz=True, correct_option=1, explanation="так вышло")
	assert poll_from_json(poll_to_json(poll)) == poll
	assert poll_to_json(None) is None
	assert poll_from_json(None) is None


def test_broken_poll_record_does_not_crash_recovery() -> None:
	"""Повреждённая запись не роняет восстановление очереди — пост без опроса."""
	assert poll_from_json({"нечто": "чужое"}) is None
	assert poll_from_json("строка") is None


def test_correct_text_names_the_right_answer() -> None:
	"""Текст правильного варианта — для подсказки формы (пусто вне викторины)."""
	assert _poll(quiz=True, correct_option=1).correct_text == "Зелёный"
	assert _poll().correct_text == ""
	assert _poll(quiz=True, correct_option=9).correct_text == ""


def test_poll_kind_has_no_file_and_no_caption() -> None:
	"""Опрос — вложение без файла и без подписи (правила вида, а не перечни)."""
	assert MediaKind.POLL.creatable  # приложение его создаёт
	assert not MediaKind.POLL.needs_file
	assert not MediaKind.POLL.has_caption
	assert MediaKind.VIDEO.needs_file and MediaKind.VIDEO.has_caption
	assert not MediaKind.NONE.needs_file and MediaKind.NONE.has_caption


def test_poll_note_says_what_will_happen() -> None:
	"""Подсказка формы называет состав, анонимность и необратимость."""
	note = poll_note(_poll())
	assert note.startswith("Опрос: 2 варианта · анонимно")
	assert "не правится" in note
	assert "видно, кто голосовал" in poll_note(_poll(anonymous=False))
	assert "можно выбрать несколько" in poll_note(_poll(multiple=True))
	quiz = poll_note(_poll(quiz=True, correct_option=1))
	assert "викторина, правильный — «Зелёный»" in quiz
	# незаполненные варианты не считаются: человек видит, сколько готово
	assert poll_note(_poll(options=("Синий", ""))).startswith("Опрос: 1 вариант ")
