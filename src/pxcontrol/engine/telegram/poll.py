"""Опрос как содержимое поста: тип, пределы Telegram, проверка (ADR-0033, C5).

Опрос — не текст и не файл, а **третий вид содержимого**: у Telegram он
устроен как вложение (`InputMediaPoll` у MTProto, `sendPoll` у Bot API),
и пост с опросом состоит из вопроса и вариантов, а не из подписи.
Поэтому опрос живёт своим полем черновика, а не притворяется текстом.

Что важно знать про опросы, прежде чем читать код:

- **опрос не правится.** Отправленный опрос Telegram менять не даёт
  ни одним транспортом — ни вопрос, ни варианты. Поэтому правка есть
  только пока пост ждёт в нашей очереди (ADR-0016), а у вышедшего поста
  остаются лишь кнопки и удаление (ADR-0032);
- **оформления в вопросе и вариантах нет.** Bot API прямо говорит про
  вариант ответа: «Currently, only custom emoji entities are allowed»,
  а кастомных эмодзи приложение не умеет (ADR-0033). Отдавать разметку
  одним транспортом и терять другим — хуже, чем не давать её вовсе,
  поэтому вопрос, варианты и пояснение — обычный текст;
- **викторина — это опрос с правильным ответом** (`quiz`): у неё ровно
  один правильный вариант и необязательное пояснение, которое читатель
  видит, ошибившись. Множественный выбор с викториной несовместим —
  Bot API молча игнорирует его у викторины, а молчаливое игнорирование
  человеку не объяснишь;
- **анонимность решает автор.** Неанонимный опрос показывает, кто как
  проголосовал (`public_voters` у MTProto, ``is_anonymous=False``
  у Bot API).

Пределы взяты из документации Bot API (вопрос 1–300, вариант 1–100,
пояснение 0–200) и клиентского конфига MTProto (`poll_answers_max`,
по умолчанию 12). Нижняя граница в две штуки — наша: опрос с одним
вариантом Telegram с недавних пор принимает, но голосовать в нём не
за что.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.types import telegram_text_length

logger = logging.getLogger(__name__)

#: Длина вопроса (документация Bot API: 1–300 символов).
POLL_QUESTION_LIMIT = 300

#: Длина одного варианта ответа (документация Bot API: 1–100 символов).
POLL_OPTION_LIMIT = 100

#: Длина пояснения викторины (документация Bot API: 0–200 символов).
POLL_EXPLANATION_LIMIT = 200

#: Вариантов в опросе: предел Telegram (`poll_answers_max`, Bot API —
#: «2-12 answer options»).
MAX_POLL_OPTIONS = 12

#: Вариантов в опросе минимум. Правило наше, а не Telegram: сервер
#: принимает и один, но опрос без выбора — не опрос.
MIN_POLL_OPTIONS = 2


class PollError(EngineError):
	"""Опрос не годится для отправки (текст — для человека)."""


@dataclass(frozen=True)
class PollDraft:
	"""Опрос поста: вопрос, варианты и правила голосования.

	Attributes:
		question: вопрос — он же заголовок поста в ленте.
		options: варианты ответа в том порядке, в каком их увидит
			читатель.
		anonymous: голоса анонимны (умолчание Telegram). Ложь —
			читатели видят, кто как проголосовал; отменить это
			у вышедшего опроса нельзя.
		multiple: можно выбрать несколько вариантов.
		quiz: викторина — у опроса есть правильный ответ.
		correct_option: номер правильного варианта с нуля (только
			у викторины; у обычного опроса — None).
		explanation: что читатель увидит, ответив неправильно (только
			у викторины; пусто — ничего не показывать).
	"""

	question: str
	options: tuple[str, ...]
	anonymous: bool = True
	multiple: bool = False
	quiz: bool = False
	correct_option: int | None = None
	explanation: str = ""

	@property
	def correct_text(self) -> str:
		"""Текст правильного варианта (пусто — это не викторина)."""
		if self.correct_option is None or not 0 <= self.correct_option < len(self.options):
			return ""
		return self.options[self.correct_option]


def validate_poll(poll: PollDraft) -> None:
	"""Проверяет опрос по пределам Telegram и по смыслу.

	До отправки, а не после: на пустой вариант или на викторину без
	правильного ответа сервер отвечает невнятной ошибкой разбора,
	а у отложенного опроса она всплыла бы в момент выхода — когда
	человека уже нет у экрана.

	Raises:
		PollError: Опрос не пройдёт — с указанием, что именно поправить.
	"""
	question = poll.question.strip()
	if not question:
		raise PollError("У опроса нет вопроса — напишите, о чём спрашиваете.")
	_check_length(question, POLL_QUESTION_LIMIT, "Вопрос опроса")
	options = [option.strip() for option in poll.options]
	if len(options) < MIN_POLL_OPTIONS:
		raise PollError(
			f"Вариантов ответа должно быть хотя бы {MIN_POLL_OPTIONS} — иначе голосовать не за что."
		)
	if len(options) > MAX_POLL_OPTIONS:
		raise PollError(f"Вариантов ответа {len(options)}, предел Telegram — {MAX_POLL_OPTIONS}.")
	for number, option in enumerate(options, start=1):
		if not option:
			raise PollError(f"Вариант {number} пуст — заполните его или удалите.")
		_check_length(option, POLL_OPTION_LIMIT, f"Вариант {number}")
	_validate_quiz(poll, len(options))


def _validate_quiz(poll: PollDraft, count: int) -> None:
	"""Проверяет правила викторины (или их отсутствие у обычного опроса).

	Raises:
		PollError: Викторина без правильного ответа, правильный ответ
			у обычного опроса, викторина с множественным выбором
			или слишком длинное пояснение.
	"""
	if not poll.quiz:
		if poll.correct_option is not None:
			raise PollError(
				"Правильный ответ бывает только у викторины — включите её или снимите ответ."
			)
		if poll.explanation.strip():
			raise PollError("Пояснение показывается только в викторине — включите её.")
		return
	if poll.multiple:
		# Bot API молча игнорирует множественный выбор у викторины —
		# человек собрал бы одно, а читатель увидел другое
		raise PollError("У викторины один правильный ответ — множественный выбор в ней невозможен.")
	if poll.correct_option is None:
		raise PollError("У викторины нужен правильный ответ — отметьте его.")
	if not 0 <= poll.correct_option < count:
		raise PollError("Правильный ответ указывает на несуществующий вариант.")
	explanation = poll.explanation.strip()
	if explanation:
		_check_length(explanation, POLL_EXPLANATION_LIMIT, "Пояснение викторины")


def _check_length(text: str, limit: int, what: str) -> None:
	"""Проверяет длину по счёту Telegram (UTF-16, эмодзи — за два).

	Raises:
		PollError: Текст длиннее предела.
	"""
	length = telegram_text_length(text)
	if length > limit:
		raise PollError(f"{what} длиннее {limit} символов (сейчас {length}).")


def trimmed_poll(poll: PollDraft) -> PollDraft:
	"""Опрос с обрезанными пробелами по краям всех его текстов.

	Одна точка для формы и для постановки в очередь: в базу и в Telegram
	уезжает ровно то, что проверено, а не «почти то же самое».
	"""
	return PollDraft(
		question=poll.question.strip(),
		options=tuple(option.strip() for option in poll.options),
		anonymous=poll.anonymous,
		multiple=poll.multiple,
		quiz=poll.quiz,
		correct_option=poll.correct_option,
		explanation=poll.explanation.strip(),
	)


def poll_to_json(poll: PollDraft | None) -> dict[str, Any] | None:
	"""Переводит опрос в JSON для колонки БД (None — опроса нет)."""
	if poll is None:
		return None
	return {
		"question": poll.question,
		"options": list(poll.options),
		"anonymous": poll.anonymous,
		"multiple": poll.multiple,
		"quiz": poll.quiz,
		"correct_option": poll.correct_option,
		"explanation": poll.explanation,
	}


def poll_from_json(raw: Any, source: str = "") -> PollDraft | None:
	"""Собирает опрос из значения колонки БД.

	Повреждённая запись (чужой формат, испорченный JSON) не роняет
	восстановление очереди: такой элемент останется без опроса и будет
	отклонён проверкой при отправке как пустой пост — с понятной
	причиной, а не падением движка. Разбор пишется в журнал: молчать
	о потерянном содержимом нельзя.

	Returns:
		Опрос или None, если его нет (или запись не разобралась).
	"""
	if raw is None:
		return None
	try:
		correct = raw.get("correct_option")
		return PollDraft(
			question=str(raw["question"]),
			options=tuple(str(option) for option in raw["options"]),
			anonymous=bool(raw.get("anonymous", True)),
			multiple=bool(raw.get("multiple", False)),
			quiz=bool(raw.get("quiz", False)),
			correct_option=None if correct is None else int(correct),
			explanation=str(raw.get("explanation", "")),
		)
	except (TypeError, ValueError, KeyError, AttributeError):
		logger.warning(
			"Опрос в базе не разобрался%s — пост остался без него.",
			f" ({source})" if source else "",
			exc_info=True,
		)
		return None
