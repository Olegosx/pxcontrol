"""Сервис подписей к постам: поля со словарями, пресеты, сборка текста.

Поле сообщества («Genre», «Year», «Video»…) хранит оформление и словарь
значений один раз; пресеты — именованные наборы полей с порядком
(ADR-0042). Название ролика — обычное поле с оформлением «жирным»,
а не особая первая строка.

Значение поля пресета бывает взято из имени файла: у строки состава
пресета есть правило разбора (:class:`SourceRule`) — извлечение
выражением, цепочка замен, регистр и разделение на значения. Разбор
и сборка подписи — чистые функции.

Словари бывают связанными: поле объявляется зависимым от другого поля
сообщества («Character» внутри «Title»), и тогда его значения живут
внутри значений родителя — при сборке поста показываются персонажи
выбранного тайтла. Тайтл удаляется — его персонажи уходят вместе с ним
(каскад в схеме, решение от 15.08.2026).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import (
	CaptionField,
	CaptionPreset,
	CaptionPresetField,
	CaptionValue,
	Community,
)
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.rich_text import RichText, TextEntity, TextStyle
from pxcontrol.engine.telegram.types import telegram_text_length
from pxcontrol.engine.video.ffmpeg import FfmpegSource, ffmpeg_source
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video

logger = logging.getLogger(__name__)

#: Суффикс имён файлов нашего конвейера: _<пресет>_<штамп>; вид штампа —
#: ``PIPELINE_STAMP_FORMAT`` сервиса видео (связка закреплена тестом
#: ``test_filename_source_matches_pipeline_stamp``).
_PIPELINE_SUFFIX = re.compile(r"_[^_]+_\d{8}-\d{6}$")

#: Плейсхолдер шаблона имени файла: {ИмяПоля}, {quality}, {channel}.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

#: Встроенные плейсхолдеры шаблона имени файла: токен → описание.
#: Единая точка для подсказок интерфейса (контракт ``render_filename``):
#: новый плейсхолдер попадает в подсказку сам, без правки страниц.
#: Название ролика здесь больше не встроенное — это обычное поле.
FILENAME_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
	("{quality}", "качество видео"),
	("{channel}", "@имя канала"),
)

#: Символы, недопустимые в именах файлов и папок (Windows + Unix) —
#: единый перечень для всех очисток имён: здесь чистятся имена файлов
#: (плюс управляющие символы ниже), в сервисе видео — имена подпапок.
FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'

#: Символы, недопустимые в именах файлов (плюс управляющие).
_FORBIDDEN_IN_FILENAME = re.compile(f"[{re.escape(FORBIDDEN_NAME_CHARS)}\\x00-\\x1f]")

#: Предел имени файла в байтах UTF-8: лимит файловых систем байтовый
#: (ext4 — 255 байт на имя; NTFS — 255 символов), а кириллица занимает
#: два байта на букву. 240 — запас под любые ФС и перезалив.
MAX_FILENAME_BYTES = 240

#: Лимит Telegram на стем имени файла в символах Юникода: длиннее —
#: сервер молча режет до 63–64 символов и чистит символы (вычислено
#: опытом, см. docs/03-modules/telegram-filenames.md).
TELEGRAM_MAX_STEM_CHARS = 78

#: «Словесный» символ — буква или цифра любого алфавита (без ``_``).
_WORD_CHAR = re.compile(r"[^\W_]")


class CaptionsError(EngineError):
	"""Ошибка работы с подписями (с понятным человеку текстом)."""


# --- поля и пресеты: объекты передачи ------------------------------------------


@dataclass(frozen=True)
class FieldStyle:
	"""Оформление строки поля в подписи.

	Attributes:
		hashtag: значения — хэштегами («#TombRaider»), иначе текстом.
		multiple: у поля бывает несколько значений.
		show_name: строка начинается с имени поля («Genre: …»);
			выключено — в строке только значения.
		bold: строка выделяется жирным (так выглядит название ролика).
	"""

	hashtag: bool = True
	multiple: bool = False
	show_name: bool = True
	bold: bool = False


@dataclass(frozen=True)
class ValueDto:
	"""Значение словаря: идентификатор, текст и привязка к родителю.

	``parent_id`` — значение родительского словаря, внутри которого живёт
	это значение (персонаж внутри тайтла); None — привязки нет.
	"""

	id: int
	value: str
	parent_id: int | None = None


@dataclass(frozen=True)
class FieldDto:
	"""Поле подписи со словарём значений (для интерфейса).

	``parent_field_id`` — поле, от которого зависит это поле: его значения
	живут внутри значений родителя; None — поле независимое.
	"""

	id: int
	name: str
	style: FieldStyle
	values: list[ValueDto]
	parent_field_id: int | None = None

	def names(self) -> list[str]:
		"""Тексты значений словаря (в порядке словаря)."""
		return [item.value for item in self.values]

	def available(self, parent_ids: Collection[int]) -> list[ValueDto]:
		"""Значения, доступные при выбранных значениях родительского поля.

		Независимое поле отдаёт весь словарь. У зависимого показываются
		значения выбранных родителей и значения без привязки: последние
		видны всегда, иначе их нельзя было бы ни выбрать, ни привязать.
		"""
		if self.parent_field_id is None:
			return list(self.values)
		return [
			item for item in self.values if item.parent_id is None or item.parent_id in parent_ids
		]

	def line(self, values: list[str]) -> CaptionLine:
		"""Строка подписи этого поля с данными значениями."""
		return CaptionLine(self.name, values, self.style)


@dataclass(frozen=True)
class FieldEdit:
	"""Правка поля сообщества: оформление и связь с родительским полем."""

	style: FieldStyle
	parent_field_id: int | None = None


@dataclass(frozen=True)
class CaptionLine:
	"""Строка подписи для сборки: имя поля, значения и оформление."""

	name: str
	values: list[str]
	style: FieldStyle = FieldStyle()


# --- правило разбора имени файла ------------------------------------------------


class CaseMode(StrEnum):
	"""Режим регистра значения после разбора имени файла."""

	KEEP = "keep"  # как есть
	EVERY_WORD = "every_word"  # Каждое Слово С Заглавной
	FIRST_WORD = "first_word"  # Только первая буква значения


@dataclass(frozen=True)
class ReplaceStep:
	r"""Шаг очистки: что найти и на что заменить.

	Attributes:
		pattern: регулярное выражение поиска; пустое — шага нет.
		replacement: чем заменить совпадение; пустая строка — удаление.
			Пробел здесь — обычное значение, а не «ничего»: замена
			разделителей (``_``, ``-``) пробелом разбивает слипшиеся
			слова. Допустимы ссылки на группы выражения (``\1``,
			``\g<имя>``) — совпадение можно не выбрасывать, а
			переписать.
	"""

	pattern: str
	replacement: str = ""


#: Разделитель значений по умолчанию (для полей с несколькими значениями).
DEFAULT_SEPARATOR = ","

#: Имя группы извлечения, которая главнее прочих групп выражения.
VALUE_GROUP = "value"


@dataclass(frozen=True)
class SourceRule:
	"""Правило «взять значение поля из имени файла» (ADR-0042).

	Применяется к имени файла без расширения и без суффикса конвейера
	(:func:`filename_source`) по шагам: извлечение → цепочка замен →
	разделение на значения (у поля с несколькими значениями) → регистр
	каждого значения. Правило с частями по умолчанию берёт имя целиком.

	Attributes:
		extract: выражение поиска; значение — группа ``value``, иначе
			первая группа, иначе всё совпадение. Пустое — имя целиком.
			Нет совпадения — у поля нет значения.
		steps: цепочка замен по результату извлечения.
		case: регистр каждого значения.
		separator: выражение-разделитель значений (только для поля
			с несколькими значениями).
	"""

	extract: str = ""
	steps: tuple[ReplaceStep, ...] = ()
	case: CaseMode = CaseMode.KEEP
	separator: str = DEFAULT_SEPARATOR

	def to_json(self) -> dict[str, Any]:
		"""Правило для колонки ``source_rule`` (JSON)."""
		return {
			"extract": self.extract,
			"steps": [[step.pattern, step.replacement] for step in self.steps],
			"case": self.case.value,
			"separator": self.separator,
		}

	@classmethod
	def from_json(cls, raw: object) -> SourceRule:
		"""Правило из колонки; испорченная часть — по умолчанию, со следом в логе.

		Незнакомые ключи пропускаются: правило, записанное более новой
		версией, не ломает старую (прямая совместимость).
		"""
		if not isinstance(raw, dict):
			logger.warning("Правило разбора имени файла — не объект: %r", raw)
			return cls()
		return cls(
			extract=_text_part(raw, "extract", ""),
			steps=_steps_part(raw.get("steps", [])),
			case=_case_part(raw.get("case", CaseMode.KEEP.value)),
			separator=_text_part(raw, "separator", DEFAULT_SEPARATOR),
		)


def _text_part(raw: dict[str, Any], key: str, default: str) -> str:
	"""Строковая часть правила; не строка — умолчание со следом в логе."""
	value = raw.get(key, default)
	if isinstance(value, str):
		return value
	logger.warning("Часть «%s» правила разбора — не строка: %r", key, value)
	return default


def _steps_part(raw: object) -> tuple[ReplaceStep, ...]:
	"""Цепочка замен правила; испорченный шаг пропускается со следом в логе."""
	if not isinstance(raw, list):
		logger.warning("Цепочка замен правила разбора — не список: %r", raw)
		return ()
	steps: list[ReplaceStep] = []
	for pair in raw:
		if isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair):
			steps.append(ReplaceStep(pair[0], pair[1]))
		else:
			logger.warning("Шаг правила разбора — не пара «выражение, замена»: %r", pair)
	return tuple(steps)


def _case_part(raw: object) -> CaseMode:
	"""Режим регистра правила; незнакомый — «как есть» со следом в логе."""
	try:
		return CaseMode(str(raw))
	except ValueError:
		logger.warning("Неизвестный режим регистра в правиле разбора: %r", raw)
		return CaseMode.KEEP


@dataclass(frozen=True)
class PresetFieldDto:
	"""Поле в составе пресета: само поле, включённость и правило разбора.

	``rule`` — правило «взять из имени файла»; None — значение вводит
	человек при сборке подписи.
	"""

	field: FieldDto
	enabled: bool
	rule: SourceRule | None = None


@dataclass(frozen=True)
class CaptionPresetDto:
	"""Пресет подписи: имя, состав полей и шаблон имени файла."""

	id: int
	name: str
	last_used_at: datetime | None
	fields: list[PresetFieldDto]
	filename_pattern: str | None = None

	def parsed(self, source: str) -> dict[int, list[str]]:
		"""Значения полей с правилом, разобранные из имени файла.

		``source`` — имя без расширения и суффикса конвейера
		(:func:`filename_source`). Поле без правила в ответ не входит,
		поле без совпадения — входит с пустым списком.
		"""
		return {
			item.field.id: extract_values(source, item.rule, item.field.style.multiple)
			for item in self.fields
			if item.rule is not None
		}

	def lines(
		self, values: Mapping[int, list[str]], enabled: Collection[int] | None = None
	) -> list[CaptionLine]:
		"""Строки подписи по составу пресета — в его порядке.

		``values`` — значения по id полей (разобранные и введённые
		вместе); ``enabled`` — поля, вошедшие в эту подпись (None — все).
		Поле без значений даёт пустую строку: её пропустит сборка.
		"""
		return [
			item.field.line(values.get(item.field.id, []))
			for item in self.fields
			if enabled is None or item.field.id in enabled
		]


@dataclass(frozen=True)
class PresetFieldSpec:
	"""Строка состава пресета к сохранению: поле и его правило разбора."""

	field_id: int
	rule: SourceRule | None = None


@dataclass(frozen=True)
class CaptionPresetDraft:
	"""Пресет к сохранению целиком — всё, что правится на его экране.

	Attributes:
		name: имя пресета.
		fields: состав по порядку с правилами разбора.
		filename_pattern: шаблон имени файла (пусто — не задан).
		field_edits: правки полей сообщества по их id — оформление
			и связь; они общие для всех пресетов и сохраняются той же
			записью, что и пресет.
	"""

	name: str
	fields: tuple[PresetFieldSpec, ...]
	filename_pattern: str | None = None
	field_edits: Mapping[int, FieldEdit] = field(default_factory=dict)


# --- чистые функции сборки ---------------------------------------------------


def hashtag(value: str) -> str:
	"""Превращает значение в хэштег: «Tomb Raider» → «#TombRaider».

	Слова склеиваются с заглавной буквы (пробелы и знаки в хэштеге
	Telegram не допускает); не-буквенные символы отбрасываются.
	"""
	words = [w for w in re.split(r"[^\w]+|_", value) if w]
	return "#" + "".join(w[:1].upper() + w[1:] for w in words)


def _render_line(line: CaptionLine) -> str:
	"""Текст строки поля; пустая строка — значений нет."""
	values = [v for v in (raw.strip() for raw in line.values) if v]
	if not values:
		return ""
	rendered = ", ".join(hashtag(v) if line.style.hashtag else v for v in values)
	return f"{line.name}: {rendered}" if line.style.show_name else rendered


def build_caption(lines: list[CaptionLine]) -> RichText:
	"""Собирает подпись из строк полей; строки без значений пропускаются.

	Строка поля — «Имя: значения»; при выключенном ``show_name`` — только
	значения. Строка поля с оформлением «жирным» выделяется **сущностью
	разметки** (ADR-0033), а не звёздочками: текст подписи — то, что
	увидит читатель, оформление живёт рядом. Смещения — в кодовых
	единицах UTF-16, как их считает Telegram.
	"""
	rows: list[str] = []
	entities: list[TextEntity] = []
	offset = 0
	for line in lines:
		text = _render_line(line)
		if not text:
			continue
		if rows:
			offset += 1  # перевод строки перед этой строкой
		length = telegram_text_length(text)
		if line.style.bold:
			entities.append(TextEntity(TextStyle.BOLD, offset, length))
		rows.append(text)
		offset += length
	return RichText("\n".join(rows), tuple(entities))


def filename_source(path: str) -> str:
	"""Исходный текст разбора: имя файла без расширения и суффикса конвейера.

	Суффикс ``_<пресет>_<штамп>`` — технический след нашей обработки
	видео, а не содержимое, поэтому срезается до любых правил.
	"""
	stem = Path(path).stem
	return _PIPELINE_SUFFIX.sub("", stem).strip()


# --- разбор имени файла в значения полей ------------------------------------------
#
# Правило поля: извлечение → цепочка замен → разделение → регистр.
# Извлечение — поиск, а не замена: нет совпадения — нет значения
# (замена без совпадения оставила бы имя целиком, и поле «Starring»
# получило бы всё имя файла).

#: Заготовка дат: ходовые написания в именах файлов. Внутри одной даты
#: разделитель одинаковый (обратная ссылка по имени): «31.01-24» датой
#: не считается. По краям запрещена цифра — иначе выкусывались бы куски
#: длинных чисел. Год бывает полным (2024) и коротким (24); одиночный
#: год датой НЕ считается — «Blade Runner 2049» должен пережить разбор
#: (кому мешает и год — берёт заготовку «слова из одних цифр»).
#: Группы именованные: без имён их номера разъехались бы при склейке
#: вариантов через «|».
DATE_STEP = (
	# год впереди четырьмя цифрами: 2024-01-31, 2024.1.5
	r"(?<!\d)(?:19|20)\d{2}(?P<ds_lead>[-._/])\d{1,2}(?P=ds_lead)\d{1,2}(?!\d)"
	# год в конце четырьмя цифрами: 31.01.2024
	r"|(?<!\d)\d{1,2}(?P<ds_tail>[-._/])\d{1,2}(?P=ds_tail)(?:19|20)\d{2}(?!\d)"
	# все три части по две цифры — год короткий: 31.01.24, 24-01-31
	r"|(?<!\d)\d{2}(?P<ds_short>[-._/])\d{2}(?P=ds_short)\d{2}(?!\d)"
	# слитно восемь цифр: 20240131 (век и границы месяца/дня — якорь
	# против случайных длинных чисел вроде битрейта или идентификатора)
	r"|(?<!\d)(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)

#: Заготовка скобок с содержимым: частый мусор источников — [1080p], (official).
BRACKETS_STEP = r"\[[^\[\]]*\]|\([^()]*\)"

#: Заготовка номеров по краям: «01. » в начале, « - 2» в конце.
EDGE_NUMBERS_STEP = r"^\s*\d+[\s.\-–—)]+|[\s.\-–—(]+\d+\s*$"

#: Заготовка слов из одних цифр: «2024», «1080», «007».
DIGIT_WORDS_STEP = r"\b\d+\b"

#: Заготовки выражений очистки: подпись → выражение. Единая точка:
#: список пунктов и их шаблоны не должны жить в двух местах. Выбранная
#: заготовка вставляется в поле выражения и правится руками; замену
#: автор задаёт сам (пусто — удаление). Разделителей (``_``, ``-``)
#: среди заготовок нет намеренно: это замена на пробел, а не удаление,
#: и пишется она парой «``[_-]``  →  пробел» без всякой заготовки.
STEP_PRESETS: tuple[tuple[str, str], ...] = (
	("Скобки с содержимым", BRACKETS_STEP),
	("Даты", DATE_STEP),
	("Слова из одних цифр", DIGIT_WORDS_STEP),
	("Номера по краям", EDGE_NUMBERS_STEP),
	("Перечень слов", r"(?i)\b(?:слово1|слово2)\b"),
	("Метки качества и релиза", r"(?i)\d{3,4}p|WEB-?DL|BluRay|x26[45]|HDR"),
)

#: Заготовки выражений извлечения: подпись → выражение. У даты группа
#: ``value`` обязательна: внутри заготовки дат есть свои группы
#: разделителей, и «первая группа» вернула бы разделитель, а не дату.
EXTRACT_PRESETS: tuple[tuple[str, str], ...] = (
	("Текст в последних скобках", r"\(([^()]*)\)\s*$"),
	("Текст до первой скобки", r"^([^(\[]+?)\s*[(\[]"),
	("Первая дата", f"(?P<{VALUE_GROUP}>{DATE_STEP})"),
	("Год", r"(?<!\d)(?:19|20)\d{2}(?!\d)"),
)


def _compile(pattern: str, what: str) -> re.Pattern[str]:
	"""Компилирует выражение; битое — понятная ошибка с текстом ``re``."""
	try:
		return re.compile(pattern)
	except re.error as exc:
		raise CaptionsError(f"{what} не разобрано: {exc}") from exc


def compile_step(step: ReplaceStep) -> re.Pattern[str] | None:
	"""Готовит шаг к применению; None — пустое выражение (шага нет).

	Проверяются обе части: выражение и шаблон замены. Публичная:
	интерфейс проверяет ею шаг до добавления и показывает причину отказа
	рядом с полем ввода — разбирать сообщения ``re`` на двух сторонах
	не нужно.

	Raises:
		CaptionsError: Выражение или шаблон замены не разбираются
			(с текстом от ``re``).
	"""
	if not step.pattern:
		return None
	expression = _compile(step.pattern, "Выражение")
	try:
		# шаблон замены разбирается при первой же подстановке — даже
		# без совпадений, поэтому пустая строка годится в пробники.
		# IndexError ловим наравне с re.error: ссылку на несуществующую
		# именованную группу (\g<нет>) Python поднимает именно им
		expression.sub(step.replacement, "")
	except (re.error, IndexError) as exc:
		raise CaptionsError(f"Замена не разобрана: {exc}") from exc
	return expression


def check_rule(rule: SourceRule) -> None:
	"""Проверяет правило целиком: извлечение, шаги и разделитель.

	Общая точка проверки: экран пресета показывает причину до сохранения,
	сервис не сохраняет битое правило.

	Raises:
		CaptionsError: Какая-то часть правила не разбирается.
	"""
	if rule.extract:
		_compile(rule.extract, "Выражение извлечения")
	for step in rule.steps:
		compile_step(step)
	if rule.separator:
		_compile(rule.separator, "Разделитель")


def _extracted(source: str, pattern: str) -> str | None:
	"""Извлечённый текст: группа ``value``, первая группа или всё совпадение.

	None — совпадения нет (или выбранная группа не участвовала в нём).
	"""
	if not pattern:
		return source
	found = re.search(pattern, source)
	if found is None:
		return None
	if VALUE_GROUP in found.re.groupindex:
		return found.group(VALUE_GROUP)
	return found.group(1) if found.re.groups else found.group(0)


def _cased(text: str, case: CaseMode) -> str:
	"""Схлопывает пробелы и применяет регистр к одному значению.

	«Каждое Слово» поднимает только первую букву слова: ``title()``
	ломал бы «iPhone».
	"""
	words = text.split()
	if case is CaseMode.EVERY_WORD:
		words = [word[:1].upper() + word[1:] for word in words]
	joined = " ".join(words)
	if case is CaseMode.FIRST_WORD:
		joined = joined[:1].upper() + joined[1:]
	return joined


def _applied_steps(text: str, steps: tuple[ReplaceStep, ...]) -> str:
	"""Цепочка замен; битый шаг пропускается со следом в логе.

	Разбор сотни имён не должен падать из-за одной опечатки, а причину
	человек видит на экране пресета (:func:`check_rule`).
	"""
	for step in steps:
		try:
			expression = compile_step(step)
		except CaptionsError as exc:
			logger.warning("Разбор имени: %s", exc)
			continue
		if expression is not None:
			text = expression.sub(step.replacement, text)
	return text


def _split(text: str, separator: str) -> list[str]:
	"""Делит текст на значения; битый разделитель — одно значение (след в логе)."""
	if not separator:
		return [text]
	try:
		return re.split(separator, text)
	except re.error as exc:
		logger.warning("Разбор имени: разделитель не разобран: %s", exc)
		return [text]


def extract_values(source: str, rule: SourceRule, multiple: bool) -> list[str]:
	"""Значения поля, разобранные из имени файла по правилу.

	Порядок: извлечение (нет совпадения — пустой список), цепочка замен,
	разделение (только у поля с несколькими значениями), регистр
	и схлопывание пробелов каждого значения. Пустые значения
	отбрасываются, повторы — тоже (порядок первого появления).
	Битое извлечение даёт пустой список со следом в логе: поле без
	значения честнее поля, заполненного всем именем файла.
	"""
	try:
		text = _extracted(source, rule.extract)
	except (re.error, IndexError) as exc:
		logger.warning("Разбор имени: выражение извлечения не разобрано: %s", exc)
		return []
	if text is None:
		return []
	text = _applied_steps(text, rule.steps)
	parts = _split(text, rule.separator) if multiple else [text]
	values = (_cased(part, rule.case) for part in parts)
	return list(dict.fromkeys(value for value in values if value))


def sanitize_filename(name: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
	"""Чистит имя файла: недопустимые символы → пробел, предел — в байтах.

	Предел считается в байтах UTF-8 (см. :data:`MAX_FILENAME_BYTES`);
	обрезка не рвёт многобайтовый символ посередине.
	"""
	cleaned = _FORBIDDEN_IN_FILENAME.sub(" ", name)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	if len(cleaned.encode("utf-8")) <= max_bytes:
		return cleaned
	cut = cleaned.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
	return cut.strip()


def filename_complaint(name: str) -> str | None:
	"""Претензия к имени файла, набранному человеком (None — имя годное).

	Правила те же, по которым чистится имя, собранное по пресету
	(:func:`sanitize_filename`, :data:`TELEGRAM_MAX_STEM_CHARS`):
	один набор запрещённых символов, один байтовый предел файловых
	систем, один предел Telegram на стем. Разница лишь в том, что
	собранное имя чистится молча, а набранное человеком — отклоняется
	с объяснением: незаметно менять то, что человек только что напечатал,
	хуже, чем попросить поправить.

	Returns:
		Текст претензии для показа человеку или None, если имя годное.
	"""
	found = sorted(set(_FORBIDDEN_IN_FILENAME.findall(name)))
	if found:
		visible = " ".join(ch if ch.isprintable() else "·" for ch in found)
		return f"В имени файла недопустимы символы: {visible}"
	size = len(name.encode("utf-8"))
	if size > MAX_FILENAME_BYTES:
		return (
			f"Имя файла слишком длинное: {size} байт при пределе "
			f"{MAX_FILENAME_BYTES} (кириллица — два байта на букву)."
		)
	stem = Path(name).stem
	if len(stem) > TELEGRAM_MAX_STEM_CHARS:
		return (
			f"Имя файла длиннее предела Telegram: {len(stem)} символов "
			f"при {TELEGRAM_MAX_STEM_CHARS} (без расширения) — сервер "
			"молча урезал бы его сам."
		)
	return None


def compose_filename(pattern: str, mapping: Mapping[str, str], suffix: str) -> str:
	"""Имя файла по шаблону: подстановка, очистка, пределы, расширение.

	Неизвестные плейсхолдеры остаются как есть — видно и правится
	руками (экран пресета показывает так ``{quality}`` без файла).
	Байтовый бюджет — предел ФС минус расширение (оно едет как есть);
	поверх — лимит Telegram на стем (:data:`TELEGRAM_MAX_STEM_CHARS`,
	иначе сервер молча режет и чистит имя): срез до конца последнего
	законченного слова (:func:`_cut_readable`).

	Returns:
		Имя с расширением; пустая строка — по шаблону ничего не вышло.
	"""
	rendered = _PLACEHOLDER.sub(lambda m: mapping.get(m.group(1), m.group(0)), pattern)
	stem = sanitize_filename(rendered, MAX_FILENAME_BYTES - len(suffix.encode("utf-8")))
	stem = _cut_readable(stem, TELEGRAM_MAX_STEM_CHARS)
	return stem + suffix if stem else ""


def filename_mapping(fields: list[FieldDto], values: Mapping[int, list[str]]) -> dict[str, str]:
	"""Подстановки полей для шаблона имени файла: значения через запятую."""
	return {item.name: ", ".join(values.get(item.id, [])) for item in fields}


def _parents_first(fields: dict[int, CaptionField]) -> list[int]:
	"""Идентификаторы полей в порядке «родитель раньше зависимого».

	Порядок нужен сборке: значение зависимого поля привязывается
	к значению родителя, а оно должно быть создано (и получить id)
	раньше. Родители вне набора пропускаются — их в этой сборке нет.
	Кольца невозможны (проверка при задании связи), но обход всё равно
	защищён от повторного захода: битые данные не должны его подвесить.
	"""
	order: list[int] = []
	visiting: set[int] = set()

	def visit(field_id: int) -> None:
		if field_id in order or field_id in visiting or field_id not in fields:
			return
		visiting.add(field_id)
		parent = fields[field_id].parent_field_id
		if parent is not None:
			visit(parent)
		visiting.discard(field_id)
		order.append(field_id)

	for field_id in fields:
		visit(field_id)
	return order


def _single_parent(merged: dict[int, list[int]], parent_field_id: int | None) -> int | None:
	"""Значение родителя для привязки — только если оно ровно одно.

	Несколько выбранных значений родителя (или ни одного) оставляют
	новое значение без привязки: «внутри какого тайтла» — вопрос
	без однозначного ответа, а угадывать нельзя.
	"""
	if parent_field_id is None:
		return None
	picked = merged.get(parent_field_id, [])
	return picked[0] if len(picked) == 1 else None


def _cut_readable(stem: str, limit: int) -> str:
	"""Укорачивает стем до ``limit`` символов, не оставляя огрызков слов.

	Если срез пришёлся на середину слова — откат до последнего
	разделителя; висячие разделители на конце отбрасываются. Крайний
	случай (одно сплошное слово без разделителей) — жёсткий срез.
	"""
	if len(stem) <= limit:
		return stem
	head = stem[:limit]
	if _WORD_CHAR.match(stem[limit]) and _WORD_CHAR.match(head[-1]):
		separators = [i for i, ch in enumerate(head) if not _WORD_CHAR.match(ch)]
		if separators and separators[-1] > 0:
			head = head[: separators[-1]]
	while head and not _WORD_CHAR.match(head[-1]):
		head = head[:-1]
	return head or stem[:limit].strip()


# --- сервис -------------------------------------------------------------------


class CaptionsService:
	"""Поля, словари и пресеты подписей сообществ."""

	def __init__(self, db: Database, ffmpeg_path: FfmpegSource = "ffmpeg") -> None:
		self._db = db
		self._ffmpeg = ffmpeg_source(ffmpeg_path)  # провайдер пути (настройки)

	# --- поля и словари ---------------------------------------------------

	async def list_fields(self, community_id: int) -> list[FieldDto]:
		"""Возвращает поля сообщества со словарями значений."""
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(CaptionField)
						.options(selectinload(CaptionField.values))
						.where(CaptionField.community_id == community_id)
						.order_by(CaptionField.id)
					)
				)
				.scalars()
				.all()
			)
			return [self._field_dto(f) for f in rows]

	async def add_field(self, community_id: int, name: str, style: FieldStyle) -> FieldDto:
		"""Добавляет поле в пул сообщества.

		Связь с родительским полем задаётся правкой (:meth:`update_field`
		или правкой в составе :meth:`save_preset`): её выбирают уже среди
		существующих полей сообщества.

		Raises:
			CaptionsError: Пустое имя или поле с таким именем уже есть.
		"""
		name = name.strip()
		if not name:
			raise CaptionsError("У поля должно быть имя.")
		async with self._db.session_factory() as session:
			exists = (
				await session.execute(
					select(CaptionField).where(
						CaptionField.community_id == community_id,
						CaptionField.name == name,
					)
				)
			).scalar_one_or_none()
			if exists is not None:
				raise CaptionsError(f"Поле «{name}» уже есть у сообщества.")
			row = CaptionField(community_id=community_id, name=name)
			self._apply_style(row, style)
			session.add(row)
			await session.commit()
			await session.refresh(row)
			field_id = row.id
		logger.info("Поле подписи «%s» добавлено (сообщество id=%s).", name, community_id)
		return await self._get_field(field_id)

	async def update_field(self, field_id: int, edit: FieldEdit) -> FieldDto:
		"""Меняет оформление поля и его связь с родительским полем.

		Действует на все пресеты сообщества: поле и словарь у сообщества
		одни. Смена связи сбрасывает привязки значений — они указывали
		в словарь прежнего родителя и после смены ничего не значат.

		Raises:
			CaptionsError: Поле не найдено или родитель не годится
				(другое сообщество, само поле, кольцо связей).
		"""
		async with self._db.session_factory() as session:
			row = await session.get(CaptionField, field_id)
			if row is None:
				raise CaptionsError("Поле не найдено — обновите список.")
			await self._apply_edit(session, row, edit)
			await session.commit()
		return await self._get_field(field_id)

	@classmethod
	async def _apply_edit(cls, session: AsyncSession, row: CaptionField, edit: FieldEdit) -> None:
		"""Применяет правку поля в открытой сессии (с проверкой родителя)."""
		if edit.parent_field_id is not None:
			await cls._validate_parent(session, row, edit.parent_field_id)
		if row.parent_field_id != edit.parent_field_id:
			await session.execute(
				update(CaptionValue)
				.where(CaptionValue.field_id == row.id)
				.values(parent_value_id=None)
			)
			logger.info("Поле id=%s: родитель — %s.", row.id, edit.parent_field_id or "нет")
		row.parent_field_id = edit.parent_field_id
		cls._apply_style(row, edit.style)
		await session.flush()  # следующая правка проверяет кольца по новому состоянию

	@staticmethod
	def _apply_style(row: CaptionField, style: FieldStyle) -> None:
		"""Переносит оформление в строку поля."""
		row.hashtag = style.hashtag
		row.multiple = style.multiple
		row.show_name = style.show_name
		row.bold = style.bold

	@staticmethod
	async def _validate_parent(
		session: AsyncSession, row: CaptionField, parent_field_id: int
	) -> None:
		"""Проверяет пригодность родительского поля.

		Raises:
			CaptionsError: Родитель — само поле, из другого сообщества,
				не найден или связь замкнулась бы в кольцо.
		"""
		if parent_field_id == row.id:
			raise CaptionsError("Поле не может зависеть само от себя.")
		parent = await session.get(CaptionField, parent_field_id)
		if parent is None or parent.community_id != row.community_id:
			raise CaptionsError("Родительское поле не найдено у этого сообщества.")
		ancestor: CaptionField | None = parent
		while ancestor is not None and ancestor.parent_field_id is not None:
			if ancestor.parent_field_id == row.id:
				raise CaptionsError("Связь полей замкнулась бы в кольцо.")
			ancestor = await session.get(CaptionField, ancestor.parent_field_id)

	async def delete_field(self, field_id: int) -> None:
		"""Удаляет поле, его словарь и строки состава пресетов.

		Значения зависимых полей перед этим отвязываются (как при смене
		связи в :meth:`update_field`): зависимое поле становится
		независимым с целым словарём — а не остаётся пустым из-за каскада
		``parent_value_id``. Сам словарь поля и строки состава пресетов
		убирают каскады схемы (внешние ключи включены).
		"""
		async with self._db.session_factory() as session:
			doomed_values = select(CaptionValue.id).where(CaptionValue.field_id == field_id)
			await session.execute(
				update(CaptionValue)
				.where(CaptionValue.parent_value_id.in_(doomed_values))
				.values(parent_value_id=None)
			)
			row = await session.get(CaptionField, field_id)
			if row is None:
				# идемпотентность сознательная (повторный клик), но след
				# нужен: удаление словаря необратимо (как в delete_community)
				logger.info("Поле подписи id=%s уже отсутствует — удалять нечего.", field_id)
				return
			name = row.name
			await session.delete(row)
			await session.commit()
		logger.info("Поле подписи «%s» (id=%s) удалено вместе со словарём.", name, field_id)

	async def add_values(
		self, field_id: int, values: list[str], parent_value_id: int | None = None
	) -> FieldDto:
		"""Пополняет словарь поля (редактор словаря).

		Дубли значений (без учёта регистра) и пустые строки пропускаются —
		правила те же, что при автопополнении из сборки подписи.
		``parent_value_id`` — значение родительского словаря, внутрь
		которого кладутся новые значения (например, тайтл для персонажей).

		Returns:
			Поле с обновлённым словарём.

		Raises:
			CaptionsError: Поле не найдено или родительское значение
				не из словаря родительского поля.
		"""
		async with self._db.session_factory() as session:
			row = await session.get(CaptionField, field_id)
			if row is None:
				raise CaptionsError("Поле не найдено — обновите список.")
			if parent_value_id is not None:
				await self._validate_parent_value(session, row, parent_value_id)
			await self._merge_values(session, field_id, values, parent_value_id)
			await session.commit()
		return await self._get_field(field_id)

	async def assign_value_parent(self, value_id: int, parent_value_id: int | None) -> FieldDto:
		"""Привязывает значение словаря к значению родителя (None — отвязывает).

		Ручная правка связей из редактора словаря: персонажу назначается
		тайтл, внутри которого он живёт.

		Returns:
			Поле значения с обновлённым словарём.

		Raises:
			CaptionsError: Значение не найдено, поле независимое или
				родительское значение не из словаря родительского поля.
		"""
		async with self._db.session_factory() as session:
			value = await session.get(CaptionValue, value_id)
			if value is None:
				raise CaptionsError("Значение не найдено — обновите список.")
			row = await session.get(CaptionField, value.field_id)
			if row is None or row.parent_field_id is None:
				raise CaptionsError(
					"Поле не зависит от другого поля — привязывать значение не к чему."
				)
			if parent_value_id is not None:
				await self._validate_parent_value(session, row, parent_value_id)
			value.parent_value_id = parent_value_id
			await session.commit()
			field_id = value.field_id
		return await self._get_field(field_id)

	@staticmethod
	async def _validate_parent_value(
		session: AsyncSession, row: CaptionField, parent_value_id: int
	) -> None:
		"""Проверяет, что значение принадлежит словарю родительского поля.

		Raises:
			CaptionsError: Поле независимое или значение из чужого словаря.
		"""
		if row.parent_field_id is None:
			raise CaptionsError(
				f"Поле «{row.name}» не зависит от другого поля — привязывать значение не к чему."
			)
		parent = await session.get(CaptionValue, parent_value_id)
		if parent is None or parent.field_id != row.parent_field_id:
			raise CaptionsError("Родительское значение не из словаря родительского поля.")

	async def delete_value(self, value_id: int) -> FieldDto:
		"""Удаляет значение словаря.

		У значения-родителя (тайтла) вместе с ним уходят привязанные
		значения зависимого поля — каскадом в схеме.

		Returns:
			Поле с обновлённым словарём.

		Raises:
			CaptionsError: Значение не найдено.
		"""
		async with self._db.session_factory() as session:
			row = await session.get(CaptionValue, value_id)
			if row is None:
				raise CaptionsError("Значение не найдено — обновите список.")
			field_id = row.field_id
			value = row.value
			await session.delete(row)
			await session.commit()
		logger.info("Значение «%s» удалено из словаря поля id=%s.", value, field_id)
		return await self._get_field(field_id)

	# --- пресеты -----------------------------------------------------------

	async def list_presets(self, community_id: int) -> list[CaptionPresetDto]:
		"""Возвращает пресеты сообщества с полным составом полей."""
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						self._preset_query().where(CaptionPreset.community_id == community_id)
					)
				)
				.scalars()
				.all()
			)
			return [self._preset_dto(p) for p in rows]

	async def get_preset(self, preset_id: int) -> CaptionPresetDto:
		"""Возвращает пресет с составом полей.

		Raises:
			CaptionsError: Пресет не найден.
		"""
		async with self._db.session_factory() as session:
			row = (
				await session.execute(self._preset_query().where(CaptionPreset.id == preset_id))
			).scalar_one_or_none()
			if row is None:
				raise CaptionsError("Пресет не найден — обновите список.")
			return self._preset_dto(row)

	@staticmethod
	def _preset_query() -> Any:
		"""Запрос пресетов с составом, полями и словарями (одним заходом)."""
		return (
			select(CaptionPreset)
			.options(
				selectinload(CaptionPreset.fields)
				.selectinload(CaptionPresetField.field)
				.selectinload(CaptionField.values)
			)
			.order_by(CaptionPreset.id)
		)

	async def save_preset(
		self, community_id: int, draft: CaptionPresetDraft, preset_id: int | None = None
	) -> CaptionPresetDto:
		"""Создаёт или перезаписывает пресет целиком — одной записью.

		В ту же запись уходят правки полей сообщества из черновика
		(оформление и связи): экран пресета сохраняется одной кнопкой,
		и половина правок не должна пережить сбой второй половины.

		Raises:
			CaptionsError: Пустое имя, пустой состав, поле повторяется
				или из другого сообщества, битое правило разбора,
				негодная связь полей, пресет не найден.
		"""
		name = draft.name.strip()
		self._check_draft(name, draft)
		wanted = {*(spec.field_id for spec in draft.fields), *draft.field_edits}
		async with self._db.session_factory() as session:
			owned = await self._owned_fields(session, community_id, wanted)
			for field_id, edit in draft.field_edits.items():
				await self._apply_edit(session, owned[field_id], edit)
			preset = await self._get_or_create_preset(session, community_id, name, preset_id)
			preset.filename_pattern = (draft.filename_pattern or "").strip() or None
			saved_id = preset.id
			await self._replace_composition(session, saved_id, draft.fields)
			await session.commit()
		logger.info("Пресет подписи «%s» сохранён (сообщество id=%s).", name, community_id)
		return await self.get_preset(saved_id)

	@staticmethod
	def _check_draft(name: str, draft: CaptionPresetDraft) -> None:
		"""Проверки черновика, не требующие базы.

		Raises:
			CaptionsError: Пустое имя или состав, повтор поля, битое правило.
		"""
		if not name:
			raise CaptionsError("У пресета должно быть имя.")
		if not draft.fields:
			raise CaptionsError("Добавьте в пресет хотя бы одно поле.")
		field_ids = [spec.field_id for spec in draft.fields]
		if len(set(field_ids)) != len(field_ids):
			raise CaptionsError("Поле встречается в пресете дважды.")
		for position, spec in enumerate(draft.fields, start=1):
			if spec.rule is None:
				continue
			try:
				check_rule(spec.rule)
			except CaptionsError as exc:
				raise CaptionsError(f"Поле №{position}: {exc}") from exc

	@staticmethod
	async def _owned_fields(
		session: AsyncSession, community_id: int, field_ids: Collection[int]
	) -> dict[int, CaptionField]:
		"""Поля сообщества по id; чужое или пропавшее — ошибка.

		Внешний ключ гарантирует лишь существование поля, и промах
		вызывающего пришил бы пресету поле чужого сообщества.

		Raises:
			CaptionsError: Поле не найдено у этого сообщества.
		"""
		rows = (
			(
				await session.execute(
					select(CaptionField).where(
						CaptionField.community_id == community_id,
						CaptionField.id.in_(list(field_ids)),
					)
				)
			)
			.scalars()
			.all()
		)
		owned = {row.id: row for row in rows}
		if any(field_id not in owned for field_id in field_ids):
			raise CaptionsError("Поле не найдено у этого сообщества — обновите список.")
		return owned

	@staticmethod
	async def _replace_composition(
		session: AsyncSession, preset_id: int, specs: tuple[PresetFieldSpec, ...]
	) -> None:
		"""Перезаписывает состав пресета по порядку черновика."""
		await session.execute(
			delete(CaptionPresetField).where(CaptionPresetField.preset_id == preset_id)
		)
		for position, spec in enumerate(specs):
			session.add(
				CaptionPresetField(
					preset_id=preset_id,
					field_id=spec.field_id,
					position=position,
					enabled=True,
					source_rule=spec.rule.to_json() if spec.rule is not None else None,
				)
			)

	async def delete_preset(self, preset_id: int) -> None:
		"""Удаляет пресет; строки состава убирают каскады схемы
		(внешние ключи включены) — как при удалении поля. Поля
		и словари остаются у сообщества."""
		async with self._db.session_factory() as session:
			preset = await session.get(CaptionPreset, preset_id)
			if preset is None:
				logger.info("Пресет подписи id=%s уже отсутствует — удалять нечего.", preset_id)
				return
			name = preset.name
			await session.delete(preset)
			await session.commit()
		logger.info("Пресет подписи «%s» (id=%s) удалён.", name, preset_id)

	async def render_filename(
		self,
		preset_id: int,
		community_id: int,
		used_values: Mapping[int, list[str]],
		media_path: str,
	) -> str:
		"""Собирает имя файла по шаблону имени пресета.

		Плейсхолдеры: ``{ИмяПоля}`` — значения поля через запятую (без
		решёток), ``{quality}`` — меньшая сторона кадра видео (ffprobe),
		``{channel}`` — @имя сообщества без ``@``. Встроенные
		плейсхолдеры главнее полей-тёзок. Остальные правила —
		:func:`compose_filename`.

		Raises:
			CaptionsError: Пресет не найден, шаблон имени не задан
				или имя получилось пустым.
		"""
		async with self._db.session_factory() as session:
			preset = await session.get(CaptionPreset, preset_id)
			if preset is None or not preset.filename_pattern:
				raise CaptionsError("У пресета не задан шаблон имени файла.")
			pattern = preset.filename_pattern
			community = await session.get(Community, community_id)
		mapping = filename_mapping(await self.list_fields(community_id), used_values)
		# ffprobe — блокирующий подпроцесс: в отдельном потоке,
		# чтобы не останавливать цикл событий движка
		mapping["quality"] = await asyncio.to_thread(self._probe_quality, media_path)
		mapping["channel"] = (community.username or "") if community else ""
		name = compose_filename(pattern, mapping, Path(media_path).suffix)
		if not name:
			raise CaptionsError("Имя файла по шаблону получилось пустым.")
		return name

	def _probe_quality(self, media_path: str) -> str:
		"""Качество видео (меньшая сторона кадра) или пустая строка.

		Сбой ffprobe — не повод ронять сборку имени: ``{quality}``
		становится пустым (закреплено тестом), но причина уходит в лог —
		иначе «имя без качества» было бы неразбираемым молча.
		"""
		try:
			info = probe_video(media_path, ffprobe_bin_for(self._ffmpeg()))
		except (OSError, RuntimeError, ValueError):
			logger.warning("Качество видео %s не прочитано ffprobe.", media_path, exc_info=True)
			return ""
		return str(min(info.width, info.height))

	async def record_usage(self, preset_id: int, used_values: Mapping[int, list[str]]) -> None:
		"""Фиксирует использование пресета: словари пополняются сами.

		``used_values`` — значения по id полей; новые (без учёта регистра)
		добавляются в словарь. **Поля с правилом разбора словарь
		не пополняют**: их значения — данные конкретного файла (название
		ролика, состав), а не словарь, из которого выбирают. Поля
		обрабатываются от родителей к зависимым: новое значение
		зависимого поля привязывается к значению родителя из этой же
		сборки (новый персонаж — к выбранному тайтлу). Привязка возможна,
		когда у родителя выбрано ровно одно значение: иначе «внутри
		какого тайтла» — вопрос без ответа.

		Пресету отмечается момент использования — для предвыбора в диалоге.
		"""
		async with self._db.session_factory() as session:
			parsed = set(
				(
					await session.execute(
						select(CaptionPresetField.field_id).where(
							CaptionPresetField.preset_id == preset_id,
							CaptionPresetField.source_rule.is_not(None),
						)
					)
				).scalars()
			)
			wanted = [field_id for field_id in used_values if field_id not in parsed]
			fields = await self._fields_by_id(session, wanted)
			merged: dict[int, list[int]] = {}
			for field_id in _parents_first(fields):
				merged[field_id] = await self._merge_values(
					session,
					field_id,
					used_values[field_id],
					_single_parent(merged, fields[field_id].parent_field_id),
				)
			preset = await session.get(CaptionPreset, preset_id)
			if preset is not None:
				preset.last_used_at = datetime.now(UTC)
			await session.commit()

	@staticmethod
	async def _fields_by_id(session: AsyncSession, field_ids: list[int]) -> dict[int, CaptionField]:
		"""Поля по идентификаторам (для связей внутри одной сборки)."""
		if not field_ids:
			return {}
		rows = (
			(await session.execute(select(CaptionField).where(CaptionField.id.in_(field_ids))))
			.scalars()
			.all()
		)
		return {row.id: row for row in rows}

	# --- внутреннее ---------------------------------------------------------

	@classmethod
	async def _merge_values(
		cls,
		session: AsyncSession,
		field_id: int,
		values: list[str],
		parent_value_id: int | None = None,
	) -> list[int]:
		"""Добавляет в словарь поля новые значения (в открытой сессии).

		Пустые строки пропускаются. Дубли считаются в пределах родителя:
		один персонаж живёт внутри своего тайтла, тёзка в другом тайтле —
		отдельная запись словаря.

		Returns:
			Идентификаторы значений (существующих и созданных) по порядку.
		"""
		rows = (
			(await session.execute(select(CaptionValue).where(CaptionValue.field_id == field_id)))
			.scalars()
			.all()
		)
		known = {(row.parent_value_id, row.value.lower()): row for row in rows}
		ids: list[int] = []
		for value in dict.fromkeys(v.strip() for v in values):
			if not value:
				continue
			row = cls._existing_value(known, value, parent_value_id)
			if row is None:
				row = CaptionValue(
					field_id=field_id,
					value=value,
					parent_value_id=parent_value_id,
				)
				session.add(row)
				await session.flush()  # нужен id: к нему привяжутся зависимые
			known[(row.parent_value_id, value.lower())] = row
			ids.append(row.id)
		return ids

	@staticmethod
	def _existing_value(
		known: dict[tuple[int | None, str], CaptionValue],
		value: str,
		parent_value_id: int | None,
	) -> CaptionValue | None:
		"""Ищет в словаре подходящую запись под нужным родителем.

		Точное совпадение «родитель + значение» — используется как есть.
		Значение без привязки, использованное вместе с родителем,
		привязывается к нему (словарь связывается по мере работы).
		Без выбранного родителя годится любая запись с таким текстом —
		дубль-двойник не заводится.
		"""
		key = value.lower()
		exact = known.get((parent_value_id, key))
		if exact is not None:
			return exact
		if parent_value_id is not None:
			orphan = known.get((None, key))
			if orphan is not None:
				orphan.parent_value_id = parent_value_id
				return orphan
			return None
		return next((row for (_parent, name), row in known.items() if name == key), None)

	async def _get_field(self, field_id: int) -> FieldDto:
		"""Возвращает поле со словарём значений.

		Raises:
			CaptionsError: Поле не найдено.
		"""
		async with self._db.session_factory() as session:
			row = (
				await session.execute(
					select(CaptionField)
					.options(selectinload(CaptionField.values))
					.where(CaptionField.id == field_id)
				)
			).scalar_one_or_none()
		if row is None:
			raise CaptionsError("Поле не найдено — обновите список.")
		return self._field_dto(row)

	@staticmethod
	async def _get_or_create_preset(
		session: AsyncSession, community_id: int, name: str, preset_id: int | None
	) -> CaptionPreset:
		"""Находит пресет для перезаписи или создаёт новый.

		Raises:
			CaptionsError: Пресет для обновления не найден или он
				другого сообщества.
		"""
		if preset_id is None:
			preset = CaptionPreset(community_id=community_id, name=name)
			session.add(preset)
			await session.flush()
			return preset
		existing = await session.get(CaptionPreset, preset_id)
		if existing is None or existing.community_id != community_id:
			raise CaptionsError("Пресет не найден — обновите список.")
		existing.name = name
		return existing

	@staticmethod
	def _field_dto(row: CaptionField) -> FieldDto:
		return FieldDto(
			row.id,
			row.name,
			FieldStyle(row.hashtag, row.multiple, row.show_name, row.bold),
			[ValueDto(v.id, v.value, v.parent_value_id) for v in row.values],
			row.parent_field_id,
		)

	@classmethod
	def _preset_dto(cls, preset: CaptionPreset) -> CaptionPresetDto:
		return CaptionPresetDto(
			preset.id,
			preset.name,
			preset.last_used_at,
			[
				PresetFieldDto(
					cls._field_dto(row.field),
					row.enabled,
					SourceRule.from_json(row.source_rule) if row.source_rule is not None else None,
				)
				for row in preset.fields
			],
			preset.filename_pattern,
		)
