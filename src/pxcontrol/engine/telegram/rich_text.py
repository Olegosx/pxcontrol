"""Размеченный текст поста: видимый текст и сущности разметки (ADR-0033).

Telegram хранит оформление не в самом тексте, а рядом: текст — обычная
строка, а жирный, ссылка или спойлер — **сущности** со смещением
и длиной. Приложение до сих пор жило иначе: в поле ввода лежала строка
с разделителями (``**жирный**``), её разбирала Telethon по дороге,
а бот-путь переводил в HTML. Такой строки не хватает — ни спойлера,
ни цитаты готовые парсеры Telethon не знают (проверено чтением её
исходников, ADR-0033), длина считается вместе с разделителями,
а правка поста стиль не сохраняет.

Модуль — о самом размеченном тексте: из чего он состоит, какие у него
правила и как он переживает хранение в БД. К транспортам не привязан:
перевод в сущности Telethon и в HTML для Bot API — дело шлюза.

Смещения и длины — в **кодовых единицах UTF-16**, как их считает
Telegram (та же единица, что у :func:`telegram_text_length`): эмодзи
занимает две единицы, и разметка вокруг него не должна разъезжаться.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.types import telegram_text_length

logger = logging.getLogger(__name__)

#: Схемы адресов, которые Telegram принимает у ссылки в тексте. Как
#: и у кнопки-ссылки (``markup.ALLOWED_URL_SCHEMES``), отсекаем сами:
#: сервер отвечает на прочие невнятной ошибкой разбора сущностей.
ALLOWED_URL_SCHEMES = ("https://", "http://", "tg://")


class RichTextError(EngineError):
	"""Разметка текста не годится для отправки (текст — для человека)."""


class TextStyle(StrEnum):
	"""Вид разметки куска текста.

	Набор — первый заход ADR-0033: всё, что умеют **оба** транспорта,
	userbot сущностями и бот HTML-тегами. Виды, которые приложение
	не создаёт (упоминание пользователя, банковская карта, кастомное
	эмодзи), сюда не входят: чужую разметку прочитанного поста мы
	показываем как есть, но сами её не собираем.
	"""

	BOLD = "bold"
	ITALIC = "italic"
	UNDERLINE = "underline"
	STRIKE = "strike"
	CODE = "code"  # моноширинный кусок внутри строки
	PRE = "pre"  # блок кода (``value`` — язык подсветки, необязателен)
	LINK = "link"  # ссылка с подписью (``value`` — адрес)
	SPOILER = "spoiler"
	QUOTE = "quote"
	EXPANDABLE_QUOTE = "expandable_quote"  # цитата, раскрываемая читателем


#: Виды, которым обязателен адрес в ``value``.
_NEEDS_URL = (TextStyle.LINK,)


@dataclass(frozen=True)
class TextEntity:
	"""Кусок текста с разметкой.

	Attributes:
		style: вид разметки.
		offset: начало куска в кодовых единицах UTF-16.
		length: длина куска в тех же единицах.
		value: адрес у ссылки, язык у блока кода; у остальных пусто.
	"""

	style: TextStyle
	offset: int
	length: int
	value: str = ""


@dataclass(frozen=True)
class RichText:
	"""Текст поста вместе с его разметкой.

	``text`` — то, что видит читатель: никаких звёздочек и тегов.
	Пустая разметка — обычный текст, и вести себя такой объект должен
	как обычный текст (в том числе в ложном контексте, когда текста
	нет вовсе).
	"""

	text: str
	entities: tuple[TextEntity, ...] = field(default_factory=tuple)

	def __bool__(self) -> bool:
		"""Есть ли в тексте хоть что-то (разметка без текста невозможна)."""
		return bool(self.text)

	@property
	def styled(self) -> bool:
		"""Есть ли разметка (иначе это обычный текст)."""
		return bool(self.entities)

	@property
	def length(self) -> int:
		"""Длина видимого текста — ею Telegram меряет пределы поста."""
		return telegram_text_length(self.text)


def validate_rich_text(rich: RichText) -> None:
	"""Проверяет разметку до отправки.

	Сервер на разъехавшиеся сущности отвечает невнятной ошибкой разбора,
	а иногда молча отбрасывает разметку целиком — поэтому границы
	проверяем сами и называем причину человеческими словами.

	Raises:
		RichTextError: Кусок выходит за границы текста, пуст, перекрывает
			соседний того же вида или у ссылки негодный адрес.
	"""
	limit = rich.length
	seen: dict[TextStyle, list[tuple[int, int]]] = {}
	for entity in rich.entities:
		if entity.length <= 0:
			raise RichTextError("Пустой кусок разметки — выделите текст и повторите.")
		if entity.offset < 0 or entity.offset + entity.length > limit:
			raise RichTextError(
				"Разметка выходит за границы текста — вероятно, текст правили "
				"мимо формы. Снимите оформление и наложите заново."
			)
		if entity.style in _NEEDS_URL and not _known_scheme(entity.value):
			raise RichTextError(
				f"Ссылка «{entity.value or '—'}» не годится: Telegram принимает "
				"адреса, начинающиеся с https://, http:// или tg://."
			)
		for start, length in seen.setdefault(entity.style, []):
			if entity.offset < start + length and start < entity.offset + entity.length:
				raise RichTextError(
					"Два куска одного вида разметки перекрываются — так Telegram "
					"разметку не примет."
				)
		seen[entity.style].append((entity.offset, entity.length))


def _known_scheme(url: str) -> bool:
	"""Начинается ли адрес со схемы, которую принимает Telegram."""
	return url.startswith(ALLOWED_URL_SCHEMES)


#: Как выглядит ссылка в тексте без разметки: до пробела или конца строки.
_BARE_URL = re.compile(r"(https?://|tg://)\S+")


def first_link(rich: RichText) -> str:
	"""Первая ссылка поста — та, по которой Telegram строит превью.

	Сначала ищется среди подписанных ссылок (сущности разметки), потом
	в самом тексте. Пустая строка означает «ссылок нет»: крупное превью
	и превью над текстом такому посту недоступны — Telegram собирает
	их по конкретному адресу, а не из воздуха.

	Чистая функция: правило выбора ссылки одно на форму (что показать
	человеку) и на транспорт (что отправить серверу).
	"""
	linked = [entity for entity in rich.entities if entity.style is TextStyle.LINK]
	if linked:
		return min(linked, key=lambda entity: entity.offset).value
	found = _BARE_URL.search(rich.text)
	return found.group(0) if found else ""


def trimmed(rich: RichText) -> RichText:
	"""Обрезает пробелы по краям вместе с разметкой.

	Раньше форма делала это простым ``strip()`` — с разметкой так
	нельзя: обрезка сдвигает весь текст, и куски оформления наезжают
	на чужие буквы. Здесь смещения сдвигаются на длину срезанного
	начала (в кодовых единицах UTF-16), а куски, оказавшиеся целиком
	в обрезанных краях, снимаются.
	"""
	stripped = rich.text.strip()
	if stripped == rich.text:
		return rich
	if not stripped:
		return RichText("")
	lead = telegram_text_length(rich.text[: len(rich.text) - len(rich.text.lstrip())])
	limit = telegram_text_length(stripped)
	entities: list[TextEntity] = []
	for entity in rich.entities:
		start = max(0, entity.offset - lead)
		end = min(limit, entity.offset + entity.length - lead)
		if end > start:
			entities.append(TextEntity(entity.style, start, end - start, entity.value))
	return RichText(stripped, tuple(entities))


def keep_entities(
	old_text: str, new_text: str, entities: tuple[TextEntity, ...]
) -> tuple[TextEntity, ...]:
	"""Разметка, которую можно оставить посту после правки текста.

	Сущности привязаны к смещениям в **своём** тексте: стоит тексту
	измениться — и жирный кусок наезжает на соседний, а ссылка
	охватывает не те буквы. Пока текст не трогали, разметка остаётся
	и обязана уйти на сервер заново (иначе Telegram сотрёт оформление,
	как стирает клавиатуру у поста, правленного без неё). Как только
	текст изменился, честнее снять разметку целиком, чем оставить
	разъехавшуюся: это правило живёт здесь, а не в форме, потому что
	оно предметное, а не оформительское.

	Правка **с** разметкой (визуальный редактор, подача C2) сюда
	не заходит — она передаёт свои сущности явно.
	"""
	return entities if new_text == old_text else ()


def _named(source: str) -> str:
	"""Пометка «чья это запись» для журнала (пусто — не сказали).

	По записи «не разобралось» без имени владельца нельзя понять,
	у какого поста пропало оформление: в очереди их сотни.
	"""
	return f" ({source})" if source else ""


def rich_to_json(rich: RichText) -> list[dict[str, Any]]:
	"""Переводит разметку в JSON для колонки БД (пустой список — её нет).

	Хранится только разметка: сам текст живёт в своей колонке и так.
	Текст без оформления даёт пустой список, а не NULL: незаполненная
	колонка означала текст поколения до ADR-0033 (разметка
	разделителями), и это поколение закрыто миграцией ``f9e2b47c3a81``.
	Писать NULL снова нельзя — вернулась бы та же двусмысленность.
	"""
	return [
		{
			"style": str(entity.style),
			"offset": entity.offset,
			"length": entity.length,
			"value": entity.value,
		}
		for entity in rich.entities
	]


def rich_from_json(text: str, raw: Any, source: str = "") -> RichText:
	"""Собирает размеченный текст из колонок БД.

	Пусто (пустой список) — оформления нет, текст уйдёт как набран.
	Незаполненная колонка после миграции ``f9e2b47c3a81`` не встречается
	и читается так же — текстом без оформления.

	Повреждённая запись (чужой формат, неизвестный вид) — не повод
	ронять восстановление очереди: пост уедет обычным текстом, а разбор
	останется в журнале. Молчать нельзя, падать здесь нечестно: пост уже
	принят, разметка — его оформление.
	"""
	if not raw:
		return RichText(text)
	try:
		entities = tuple(
			TextEntity(
				style=TextStyle(item["style"]),
				offset=int(item["offset"]),
				length=int(item["length"]),
				value=str(item.get("value", "")),
			)
			for item in raw
		)
	except (TypeError, KeyError, ValueError):
		logger.warning(
			"Разметка текста в БД не разобралась%s — пост уедет без неё.",
			_named(source),
			exc_info=True,
		)
		return RichText(text)
	return RichText(text, entities)
