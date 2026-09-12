"""Сервис подписей к постам: поля со словарями, шаблоны, сборка текста.

Поле канала («Genre», «Year»…) хранит свой словарь значений один раз;
шаблоны — именованные наборы полей с порядком. Сборка подписи — чистые
функции: жирное название (Markdown, Telethon парсит его по умолчанию)
и строки «Поле: значения» (с решётками или без).

Словари бывают связанными: поле объявляется зависимым от другого поля
канала («Character» внутри «Title»), и тогда его значения живут внутри
значений родителя — при сборке поста показываются персонажи выбранного
тайтла. Тайтл удаляется — его персонажи уходят вместе с ним (каскад
в схеме, решение от 15.08.2026).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import (
	CaptionField,
	CaptionTemplate,
	CaptionTemplateField,
	CaptionValue,
	Community,
)
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.video.ffmpeg import FfmpegSource, ffmpeg_source
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video

logger = logging.getLogger(__name__)

#: Суффикс имён файлов нашего конвейера: _<пресет>_<штамп>; вид штампа —
#: ``PIPELINE_STAMP_FORMAT`` сервиса видео (связка закреплена тестом
#: ``test_title_from_filename_matches_pipeline_stamp``).
_PIPELINE_SUFFIX = re.compile(r"_[^_]+_\d{8}-\d{6}$")

#: Плейсхолдер шаблона имени файла: {video}, {ИмяПоля}, {quality}, {channel}.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

#: Встроенные плейсхолдеры шаблона имени файла: токен → описание.
#: Единая точка для подсказок интерфейса (контракт ``render_filename``):
#: новый плейсхолдер попадает в подсказку сам, без правки страниц.
FILENAME_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
	("{video}", "название видео"),
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
	``show_name`` — вставлять ли имя поля в подпись: при False строка
	собирается без префикса «Имя: », только из значений.
	"""

	id: int
	name: str
	hashtag: bool
	multiple: bool
	values: list[ValueDto]
	parent_field_id: int | None = None
	show_name: bool = True

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


@dataclass(frozen=True)
class TemplateFieldDto:
	"""Поле в составе шаблона: само поле и включённость по умолчанию."""

	field: FieldDto
	enabled: bool


@dataclass(frozen=True)
class TemplateDto:
	"""Шаблон подписи: имя, состав полей и шаблон имени файла."""

	id: int
	name: str
	last_used_at: datetime | None
	fields: list[TemplateFieldDto]
	filename_pattern: str | None = None


@dataclass(frozen=True)
class CaptionLine:
	"""Строка подписи для сборки: имя поля, оформление, значения.

	``show_name=False`` — имя поля в подпись не вставляется, строка
	состоит только из значений.
	"""

	name: str
	hashtag: bool
	values: list[str]
	show_name: bool = True


# --- чистые функции сборки ---------------------------------------------------


def hashtag(value: str) -> str:
	"""Превращает значение в хэштег: «Tomb Raider» → «#TombRaider».

	Слова склеиваются с заглавной буквы (пробелы и знаки в хэштеге
	Telegram не допускает); не-буквенные символы отбрасываются.
	"""
	words = [w for w in re.split(r"[^\w]+|_", value) if w]
	return "#" + "".join(w[:1].upper() + w[1:] for w in words)


def build_caption(title: str, lines: list[CaptionLine]) -> str:
	"""Собирает текст подписи: жирное название + строки полей.

	Строки без значений пропускаются. Строка поля — «Имя: значения»;
	при выключенном ``show_name`` — только значения. Разметка — Markdown
	(``**название**``), Telethon применяет её по умолчанию.
	"""
	rows = [f"**{title.strip()}**"] if title.strip() else []
	for line in lines:
		values = [v for v in (raw.strip() for raw in line.values) if v]
		if not values:
			continue
		rendered = ", ".join(hashtag(v) if line.hashtag else v for v in values)
		rows.append(f"{line.name}: {rendered}" if line.show_name else rendered)
	return "\n".join(rows)


def title_from_filename(path: str) -> str:
	"""Название поста из имени файла (без суффикса нашего конвейера)."""
	stem = Path(path).stem
	return _PIPELINE_SUFFIX.sub("", stem).strip()


# --- элементарный разбор имени файла в название (пакетная публикация) ---------
#
# Модель одна: разбор — это цепочка замен по регулярным выражениям.
# Каждый шаг применяется к результату предыдущего, совпадения заменяются
# пробелом (для имён файлов «удалить» и «заменить пробелом» — одно и то
# же: лишние пробелы схлопываются в конце). Единственное, что заменой
# не выражается, — регистр: он остался отдельным полем правил.

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

#: Заготовки для помощника в интерфейсе: подпись → выражение. Единая
#: точка: список пунктов и их шаблоны не должны жить в двух местах.
#: Выбранная заготовка вставляется в поле выражения и правится руками;
#: замену автор задаёт сам (пусто — удаление). Разделителей (``_``, ``-``)
#: среди заготовок нет намеренно: это замена на пробел, а не удаление,
#: и пишется она парой «``[_-]``  →  пробел» без всякой заготовки.
TITLE_STEP_PRESETS: tuple[tuple[str, str], ...] = (
	("Скобки с содержимым", BRACKETS_STEP),
	("Даты", DATE_STEP),
	("Слова из одних цифр", DIGIT_WORDS_STEP),
	("Номера по краям", EDGE_NUMBERS_STEP),
	("Перечень слов", r"(?i)\b(?:слово1|слово2)\b"),
	("Метки качества и релиза", r"(?i)\d{3,4}p|WEB-?DL|BluRay|x26[45]|HDR"),
)


class TitleCaseMode(StrEnum):
	"""Режим регистра названия после разбора имени файла."""

	KEEP = "keep"  # как есть
	EVERY_WORD = "every_word"  # Каждое Слово С Заглавной
	FIRST_WORD = "first_word"  # Только первая буква фразы


@dataclass(frozen=True)
class TitleStep:
	r"""Шаг разбора: что найти и на что заменить.

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


def compile_step(step: TitleStep) -> re.Pattern[str] | None:
	"""Готовит шаг к применению; None — пустое выражение (шага нет).

	Проверяются обе части: выражение и шаблон замены. Публичная:
	интерфейс проверяет ею шаг до применения и показывает причину отказа
	рядом с полем ввода — разбирать сообщения ``re`` на двух сторонах
	не нужно.

	Raises:
		CaptionsError: Выражение или шаблон замены не разбираются
			(с текстом от ``re``).
	"""
	if not step.pattern:
		return None
	try:
		expression = re.compile(step.pattern)
	except re.error as exc:
		raise CaptionsError(f"Выражение не разобрано: {exc}") from exc
	try:
		# шаблон замены разбирается при первой же подстановке — даже
		# без совпадений, поэтому пустая строка годится в пробники.
		# IndexError ловим наравне с re.error: ссылку на несуществующую
		# именованную группу (\g<нет>) Python поднимает именно им
		expression.sub(step.replacement, "")
	except (re.error, IndexError) as exc:
		raise CaptionsError(f"Замена не разобрана: {exc}") from exc
	return expression


#: Токен шага: JSON-пара «выражение, замена».
_STEP_PREFIX = "sub:"

#: Токен шага прежней модели — только выражение.
_LEGACY_STEP_PREFIX = "step:"

#: Замена шагов прежних моделей: пробел (другой они не знали).
_LEGACY_REPLACEMENT = " "

#: Прежние правила-галочки → выражения. Порядок фиксирован и повторяет
#: порядок применения старой версии: настройка канала, записанная ею,
#: должна разбирать имена ровно так же, как разбирала.
_LEGACY_STEPS: tuple[tuple[str, str], ...] = (
	("brackets", BRACKETS_STEP),
	("dates", DATE_STEP),
	("separators", "_"),  # одна галочка на оба разделителя
	("underscores", "_"),
	("hyphens", "-"),
	("edge_numbers", EDGE_NUMBERS_STEP),
	("digit_words", DIGIT_WORDS_STEP),
)


def _legacy_steps(tokens: Collection[str]) -> list[TitleStep]:
	"""Шаги из токенов прежней модели (галочки и список слов).

	Токены прежних версий читаются, а не отбрасываются: у каналов
	сохранены наборы правил, и после обновления они обязаны продолжать
	работать — уже шагами. Замена у всех — пробел: прежняя модель
	другой и не знала.
	"""
	steps: list[TitleStep] = []
	for name, pattern in _LEGACY_STEPS:
		if name in tokens:
			steps.append(TitleStep(pattern, _LEGACY_REPLACEMENT))
			if name == "separators":
				# прежняя галочка меняла оба разделителя сразу
				steps.append(TitleStep("-", _LEGACY_REPLACEMENT))
	words = [
		word
		for token in tokens
		if token.startswith("remove:") and (word := token.removeprefix("remove:").strip())
	]
	if words:
		# прежнее удаление слов не различало регистр и работало по целым
		# словам — то же самое выражением
		pattern = r"(?i)\b(?:" + "|".join(re.escape(word) for word in words) + r")\b"
		steps.append(TitleStep(pattern, _LEGACY_REPLACEMENT))
	return steps


def _step_from_token(raw: str) -> TitleStep | None:
	"""Шаг из JSON-пары токена; None — токен испорчен (след в логе)."""
	try:
		pair = json.loads(raw)
	except ValueError:
		logger.warning("Токен шага разбора не разобран: %s", raw)
		return None
	if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
		logger.warning("Токен шага разбора — не пара «выражение, замена»: %s", raw)
		return None
	return TitleStep(pair[0], pair[1])


@dataclass(frozen=True)
class TitleParseRules:
	"""Разбор имени файла в название поста: цепочка замен и регистр.

	Осознанно простые и детерминированные правила (полный смысловой
	разбор — будущая задача ИИ, ей эти правила не мешают). Хранятся
	настройкой канала как список токенов
	(:meth:`to_tokens`/:meth:`from_tokens`).

	Attributes:
		steps: шаги по порядку применения; каждый следующий работает
			по результату предыдущего.
		case: режим регистра итоговой фразы.
	"""

	steps: tuple[TitleStep, ...] = ()
	case: TitleCaseMode = TitleCaseMode.KEEP

	def to_tokens(self) -> list[str]:
		"""Сериализация в список токенов для настройки канала.

		Шаг — пара, поэтому в одну строку токена он пишется как JSON:
		и выражение, и замена бывают любыми, и разделитель-символ
		рано или поздно встретился бы внутри них самих.
		"""
		tokens = [
			_STEP_PREFIX + json.dumps([step.pattern, step.replacement], ensure_ascii=False)
			for step in self.steps
		]
		if self.case is not TitleCaseMode.KEEP:
			tokens.append(f"case:{self.case.value}")
		return tokens

	@classmethod
	def from_tokens(cls, tokens: list[str]) -> TitleParseRules:
		"""Правила из списка токенов; незнакомые токены игнорируются.

		Терпимость к незнакомому — прямая совместимость: настройка,
		записанная более новой версией, не ломает старую. Токены
		прежних моделей превращаются в шаги: галочки и список слов —
		:func:`_legacy_steps`, шаги-выражения без замены
		(``step:``) — с заменой на пробел, как они и работали.
		"""
		case = TitleCaseMode.KEEP
		steps: list[TitleStep] = []
		legacy: list[str] = []
		for token in tokens:
			if token.startswith(_STEP_PREFIX):
				step = _step_from_token(token.removeprefix(_STEP_PREFIX))
				if step is not None:
					steps.append(step)
			elif token.startswith(_LEGACY_STEP_PREFIX):
				# прежняя модель заменяла совпадения пробелом и другой
				# замены не знала — сохраняем смысл сохранённых настроек
				steps.append(
					TitleStep(token.removeprefix(_LEGACY_STEP_PREFIX), _LEGACY_REPLACEMENT)
				)
			elif token.startswith("case:"):
				try:
					case = TitleCaseMode(token.removeprefix("case:"))
				except ValueError:
					logger.warning("Неизвестный режим регистра в настройке: %s", token)
			elif token.startswith("remove:") or token in {name for name, _ in _LEGACY_STEPS}:
				legacy.append(token)
			else:
				logger.warning("Неизвестный токен правил разбора: %s", token)
		# шаги прежней модели идут первыми: своих у неё быть не могло
		return cls(steps=tuple(_legacy_steps(legacy) + steps), case=case)


def parse_title(raw: str, rules: TitleParseRules) -> str:
	"""Применяет разбор к названию, взятому из имени файла.

	Шаги идут по порядку, каждый — по результату предыдущего;
	совпадение заменяется тем, что задано шагом (пустая замена —
	удаление). В конце пробелы схлопываются, последним применяется
	регистр (по итоговой фразе). Пустой результат откатывается
	к исходному названию: пост без названия хуже поста с сырым.

	Битый шаг пропускается (след — в логе): разбор сотни имён не должен
	падать из-за одной опечатки, а причину пользователь уже видит
	в форме (:func:`compile_step`).
	"""
	text = raw
	for step in rules.steps:
		try:
			expression = compile_step(step)
		except CaptionsError as exc:
			logger.warning("Разбор имени: %s", exc)
			continue
		if expression is not None:
			text = expression.sub(step.replacement, text)
	words = text.split()
	if rules.case is TitleCaseMode.EVERY_WORD:
		# только первая буква каждого слова: title() ломал бы «iPhone»
		words = [word[:1].upper() + word[1:] for word in words]
	text = " ".join(words)
	if rules.case is TitleCaseMode.FIRST_WORD:
		text = text[:1].upper() + text[1:]
	return text if text else raw.strip()


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

	Правила те же, по которым чистится имя, собранное по шаблону
	подписи (:func:`sanitize_filename`, :data:`TELEGRAM_MAX_STEM_CHARS`):
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
	"""Поля, словари и шаблоны подписей каналов."""

	def __init__(self, db: Database, ffmpeg_path: FfmpegSource = "ffmpeg") -> None:
		self._db = db
		self._ffmpeg = ffmpeg_source(ffmpeg_path)  # провайдер пути (настройки)

	# --- поля и словари ---------------------------------------------------

	async def list_fields(self, community_id: int) -> list[FieldDto]:
		"""Возвращает поля канала со словарями значений."""
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

	async def add_field(
		self, community_id: int, name: str, hashtag: bool, multiple: bool, show_name: bool = True
	) -> FieldDto:
		"""Добавляет поле в пул канала.

		``show_name`` — вставлять ли имя поля в подпись (см. :class:`FieldDto`);
		позже флаг переключается через :meth:`set_field_show_name`.
		Связь с родительским полем задаётся отдельно
		(:meth:`set_field_parent`): её выбирают уже среди существующих
		полей канала.

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
				raise CaptionsError(f"Поле «{name}» уже есть у канала.")
			field = CaptionField(
				community_id=community_id,
				name=name,
				hashtag=hashtag,
				multiple=multiple,
				show_name=show_name,
			)
			session.add(field)
			await session.commit()
			await session.refresh(field)
		logger.info("Поле подписи «%s» добавлено (канал id=%s).", name, community_id)
		return FieldDto(
			field.id, field.name, field.hashtag, field.multiple, [], show_name=field.show_name
		)

	async def set_field_show_name(self, field_id: int, show_name: bool) -> FieldDto:
		"""Включает или выключает вставку имени поля в подпись.

		Флаг действует на все будущие сборки подписи (у существующего
		поля переключается без пересоздания — словарь сохраняется).

		Returns:
			Поле с обновлённым флагом и словарём.

		Raises:
			CaptionsError: Поле не найдено.
		"""
		async with self._db.session_factory() as session:
			field = await session.get(CaptionField, field_id)
			if field is None:
				raise CaptionsError("Поле не найдено — обновите список.")
			field.show_name = show_name
			await session.commit()
		logger.info("Поле id=%s: имя в подписи — %s.", field_id, "да" if show_name else "нет")
		return await self._get_field(field_id)

	async def set_field_parent(self, field_id: int, parent_field_id: int | None) -> FieldDto:
		"""Объявляет поле зависимым от другого поля канала (None — снимает связь).

		Смена связи сбрасывает привязки значений: они указывали на словарь
		прежнего родителя и после смены ничего не значат.

		Returns:
			Поле с обновлённой связью и словарём.

		Raises:
			CaptionsError: Поле не найдено, родитель не годится (другой
				канал, само поле, кольцо связей).
		"""
		async with self._db.session_factory() as session:
			field = await session.get(CaptionField, field_id)
			if field is None:
				raise CaptionsError("Поле не найдено — обновите список.")
			if parent_field_id is not None:
				await self._validate_parent(session, field, parent_field_id)
			if field.parent_field_id != parent_field_id:
				await session.execute(
					update(CaptionValue)
					.where(CaptionValue.field_id == field_id)
					.values(parent_value_id=None)
				)
			field.parent_field_id = parent_field_id
			await session.commit()
		logger.info("Поле id=%s: родитель — %s.", field_id, parent_field_id or "нет")
		return await self._get_field(field_id)

	@staticmethod
	async def _validate_parent(
		session: AsyncSession, field: CaptionField, parent_field_id: int
	) -> None:
		"""Проверяет пригодность родительского поля.

		Raises:
			CaptionsError: Родитель — само поле, из другого канала,
				не найден или связь замкнулась бы в кольцо.
		"""
		if parent_field_id == field.id:
			raise CaptionsError("Поле не может зависеть само от себя.")
		parent = await session.get(CaptionField, parent_field_id)
		if parent is None or parent.community_id != field.community_id:
			raise CaptionsError("Родительское поле не найдено у этого канала.")
		ancestor: CaptionField | None = parent
		while ancestor is not None and ancestor.parent_field_id is not None:
			if ancestor.parent_field_id == field.id:
				raise CaptionsError("Связь полей замкнулась бы в кольцо.")
			ancestor = await session.get(CaptionField, ancestor.parent_field_id)

	async def delete_field(self, field_id: int) -> None:
		"""Удаляет поле, его словарь и строки состава шаблонов.

		Значения зависимых полей перед этим отвязываются (как при смене
		связи в :meth:`set_field_parent`): зависимое поле становится
		независимым с целым словарём — а не остаётся пустым из-за каскада
		``parent_value_id``. Сам словарь поля и строки состава шаблонов
		убирают каскады схемы (внешние ключи включены).
		"""
		async with self._db.session_factory() as session:
			doomed_values = select(CaptionValue.id).where(CaptionValue.field_id == field_id)
			await session.execute(
				update(CaptionValue)
				.where(CaptionValue.parent_value_id.in_(doomed_values))
				.values(parent_value_id=None)
			)
			field = await session.get(CaptionField, field_id)
			if field is None:
				# идемпотентность сознательная (повторный клик), но след
				# нужен: удаление словаря необратимо (как в delete_community)
				logger.info("Поле подписи id=%s уже отсутствует — удалять нечего.", field_id)
				return
			name = field.name
			await session.delete(field)
			await session.commit()
		logger.info("Поле подписи «%s» (id=%s) удалено вместе со словарём.", name, field_id)

	async def add_values(
		self, field_id: int, values: list[str], parent_value_id: int | None = None
	) -> FieldDto:
		"""Пополняет словарь поля (редактор словаря в «Полях подписи»).

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
			field = await session.get(CaptionField, field_id)
			if field is None:
				raise CaptionsError("Поле не найдено — обновите список.")
			if parent_value_id is not None:
				await self._validate_parent_value(session, field, parent_value_id)
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
			row = await session.get(CaptionValue, value_id)
			if row is None:
				raise CaptionsError("Значение не найдено — обновите список.")
			field = await session.get(CaptionField, row.field_id)
			if field is None or field.parent_field_id is None:
				raise CaptionsError(
					"Поле не зависит от другого поля — привязывать значение не к чему."
				)
			if parent_value_id is not None:
				await self._validate_parent_value(session, field, parent_value_id)
			row.parent_value_id = parent_value_id
			await session.commit()
			field_id = row.field_id
		return await self._get_field(field_id)

	@staticmethod
	async def _validate_parent_value(
		session: AsyncSession, field: CaptionField, parent_value_id: int
	) -> None:
		"""Проверяет, что значение принадлежит словарю родительского поля.

		Raises:
			CaptionsError: Поле независимое или значение из чужого словаря.
		"""
		if field.parent_field_id is None:
			raise CaptionsError(
				f"Поле «{field.name}» не зависит от другого поля — привязывать значение не к чему."
			)
		parent = await session.get(CaptionValue, parent_value_id)
		if parent is None or parent.field_id != field.parent_field_id:
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

	# --- шаблоны -----------------------------------------------------------

	async def list_templates(self, community_id: int) -> list[TemplateDto]:
		"""Возвращает шаблоны канала с полным составом полей."""
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(CaptionTemplate)
						.options(
							selectinload(CaptionTemplate.fields)
							.selectinload(CaptionTemplateField.field)
							.selectinload(CaptionField.values)
						)
						.where(CaptionTemplate.community_id == community_id)
						.order_by(CaptionTemplate.id)
					)
				)
				.scalars()
				.all()
			)
			return [self._template_dto(t) for t in rows]

	async def save_template(
		self,
		community_id: int,
		name: str,
		field_ids: list[int],
		filename_pattern: str | None = None,
		template_id: int | None = None,
	) -> TemplateDto:
		"""Создаёт или перезаписывает шаблон (состав — в порядке списка).

		``filename_pattern`` — необязательный шаблон имени файла при
		отправке ({video}, {ИмяПоля}, {quality}, {channel}).

		Raises:
			CaptionsError: Пустое имя, пустой состав, чужое поле
				или шаблон не найден.
		"""
		name = name.strip()
		if not name:
			raise CaptionsError("У шаблона должно быть имя.")
		if not field_ids:
			raise CaptionsError("Выберите хотя бы одно поле для шаблона.")
		async with self._db.session_factory() as session:
			# состав — только из полей этого канала: внешний ключ гарантирует
			# лишь существование поля, и промах вызывающего пришил бы шаблону
			# поле чужого канала
			owned = set(
				(
					await session.execute(
						select(CaptionField.id).where(
							CaptionField.community_id == community_id,
							CaptionField.id.in_(field_ids),
						)
					)
				).scalars()
			)
			if any(field_id not in owned for field_id in field_ids):
				raise CaptionsError("В составе шаблона поле другого канала — обновите список.")
			template = await self._get_or_create_template(session, community_id, name, template_id)
			template.filename_pattern = (filename_pattern or "").strip() or None
			saved_id = template.id
			await session.execute(
				delete(CaptionTemplateField).where(CaptionTemplateField.template_id == saved_id)
			)
			for position, field_id in enumerate(field_ids):
				session.add(
					CaptionTemplateField(
						template_id=saved_id,
						field_id=field_id,
						position=position,
						enabled=True,
					)
				)
			await session.commit()
		logger.info("Шаблон подписи «%s» сохранён (канал id=%s).", name, community_id)
		templates = await self.list_templates(community_id)
		return next(t for t in templates if t.id == saved_id)

	async def delete_template(self, template_id: int) -> None:
		"""Удаляет шаблон; строки состава убирают каскады схемы
		(внешние ключи включены) — как при удалении поля."""
		async with self._db.session_factory() as session:
			template = await session.get(CaptionTemplate, template_id)
			if template is None:
				logger.info("Шаблон подписи id=%s уже отсутствует — удалять нечего.", template_id)
				return
			name = template.name
			await session.delete(template)
			await session.commit()
		logger.info("Шаблон подписи «%s» (id=%s) удалён.", name, template_id)

	async def render_filename(
		self,
		template_id: int,
		community_id: int,
		title: str,
		used_values: dict[int, list[str]],
		media_path: str,
	) -> str:
		"""Собирает имя файла по шаблону имени выбранного шаблона подписи.

		Плейсхолдеры: ``{video}`` — название видео/поста, ``{ИмяПоля}`` —
		значения поля через запятую (без решёток), ``{quality}`` — меньшая
		сторона кадра видео (ffprobe), ``{channel}`` — @имя канала без
		``@``. Неизвестные плейсхолдеры остаются как есть — видно
		и правится руками. Название нарочно не ``{title}``: у каналов
		бывает поле «Title», и различие только регистром путало.

		Стем вписывается в лимит Telegram
		(:data:`TELEGRAM_MAX_STEM_CHARS`, иначе сервер молча режет
		и чистит имя): срез до конца последнего законченного слова
		(:func:`_cut_readable`); сплошное слово без разделителей —
		срез как есть.

		Raises:
			CaptionsError: Шаблон не найден, шаблон имени не задан
				или имя получилось пустым.
		"""
		async with self._db.session_factory() as session:
			template = await session.get(CaptionTemplate, template_id)
			if template is None or not template.filename_pattern:
				raise CaptionsError("У шаблона не задан шаблон имени файла.")
			pattern = template.filename_pattern
			community = await session.get(Community, community_id)
		mapping: dict[str, str] = {}
		for field in await self.list_fields(community_id):
			mapping[field.name] = ", ".join(used_values.get(field.id, []))
		# встроенные плейсхолдеры — поверх полей: поле, названное «video»,
		# не должно молча подменять название поста (приоритет закреплён
		# тестом; сами имена перечисляет FILENAME_PLACEHOLDERS)
		mapping["video"] = title.strip()
		# ffprobe — блокирующий подпроцесс: в отдельном потоке,
		# чтобы не останавливать цикл событий движка
		mapping["quality"] = await asyncio.to_thread(self._probe_quality, media_path)
		mapping["channel"] = (community.username or "") if community else ""
		rendered = _PLACEHOLDER.sub(lambda m: mapping.get(m.group(1), m.group(0)), pattern)
		# байтовый бюджет — предел ФС минус расширение (оно едет как есть);
		# поверх — лимит Telegram: срез до законченного слова
		suffix = Path(media_path).suffix
		stem = sanitize_filename(rendered, MAX_FILENAME_BYTES - len(suffix.encode("utf-8")))
		stem = _cut_readable(stem, TELEGRAM_MAX_STEM_CHARS)
		if not stem:
			raise CaptionsError("Имя файла по шаблону получилось пустым.")
		return stem + suffix

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

	async def record_usage(self, template_id: int, used_values: dict[int, list[str]]) -> None:
		"""Фиксирует использование шаблона: словари пополняются сами.

		``used_values`` — значения по id полей; новые (без учёта регистра)
		добавляются в словарь. Поля обрабатываются от родителей к зависимым:
		новое значение зависимого поля привязывается к значению родителя
		из этой же сборки (новый персонаж — к выбранному тайтлу). Привязка
		возможна, когда у родителя выбрано ровно одно значение: иначе
		«внутри какого тайтла» — вопрос без ответа.

		Шаблону отмечается момент использования — для предвыбора в диалоге.
		"""
		async with self._db.session_factory() as session:
			fields = await self._fields_by_id(session, list(used_values))
			merged: dict[int, list[int]] = {}
			for field_id in _parents_first(fields):
				merged[field_id] = await self._merge_values(
					session,
					field_id,
					used_values[field_id],
					_single_parent(merged, fields[field_id].parent_field_id),
				)
			template = await session.get(CaptionTemplate, template_id)
			if template is not None:
				template.last_used_at = datetime.now(UTC)
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
			field = (
				await session.execute(
					select(CaptionField)
					.options(selectinload(CaptionField.values))
					.where(CaptionField.id == field_id)
				)
			).scalar_one_or_none()
		if field is None:
			raise CaptionsError("Поле не найдено — обновите список.")
		return self._field_dto(field)

	@staticmethod
	async def _get_or_create_template(
		session: AsyncSession, community_id: int, name: str, template_id: int | None
	) -> CaptionTemplate:
		"""Находит шаблон для перезаписи или создаёт новый.

		Raises:
			CaptionsError: Шаблон для обновления не найден.
		"""
		if template_id is None:
			template = CaptionTemplate(community_id=community_id, name=name)
			session.add(template)
			await session.flush()
			return template
		existing = await session.get(CaptionTemplate, template_id)
		if existing is None:
			raise CaptionsError("Шаблон не найден — обновите список.")
		existing.name = name
		return existing

	@staticmethod
	def _field_dto(field: CaptionField) -> FieldDto:
		return FieldDto(
			field.id,
			field.name,
			field.hashtag,
			field.multiple,
			[ValueDto(v.id, v.value, v.parent_value_id) for v in field.values],
			field.parent_field_id,
			field.show_name,
		)

	@classmethod
	def _template_dto(cls, template: CaptionTemplate) -> TemplateDto:
		return TemplateDto(
			template.id,
			template.name,
			template.last_used_at,
			[TemplateFieldDto(cls._field_dto(row.field), row.enabled) for row in template.fields],
			template.filename_pattern,
		)
