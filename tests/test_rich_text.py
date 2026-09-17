"""Тесты размеченного текста поста (ADR-0033, подача C1).

Проверяются три вещи, каждая — чистая: сам тип и его правила, перевод
в сущности Telethon и обратно, перевод в HTML для Bot API. Сеть здесь
не нужна: переводы односторонние и от транспорта не зависят.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.telegram.bot_api import html_from_rich, post_html
from pxcontrol.engine.telegram.mtproto import entities_to_telethon, rich_from_telethon
from pxcontrol.engine.telegram.rich_text import (
	RichText,
	RichTextError,
	TextEntity,
	TextStyle,
	keep_entities,
	rich_from_json,
	rich_to_json,
	validate_rich_text,
)


def _bold(offset: int = 0, length: int = 4) -> TextEntity:
	return TextEntity(TextStyle.BOLD, offset, length)


def test_length_counts_visible_text_in_utf16() -> None:
	"""Длина — видимый текст в кодовых единицах UTF-16 (эмодзи за два).

	Прежняя разметка строкой считалась вместе с разделителями, и пост
	у предела отвергался там, где сервер бы его принял (ADR-0033).
	"""
	assert RichText("жирный", (_bold(0, 6),)).length == 6
	assert RichText("🙂").length == 2


def test_validate_rejects_broken_entities() -> None:
	"""Разъехавшуюся разметку отвергаем сами — с человеческой причиной."""
	validate_rich_text(RichText("текст", (_bold(0, 5),)))  # в границах — молчит
	with pytest.raises(RichTextError, match="границы"):
		validate_rich_text(RichText("текст", (_bold(3, 10),)))
	with pytest.raises(RichTextError, match="Пустой"):
		validate_rich_text(RichText("текст", (_bold(0, 0),)))
	with pytest.raises(RichTextError, match="перекрыв"):
		validate_rich_text(RichText("текст", (_bold(0, 3), _bold(2, 3))))
	with pytest.raises(RichTextError, match="Ссылка"):
		validate_rich_text(
			RichText("текст", (TextEntity(TextStyle.LINK, 0, 5, "javascript:alert(1)"),))
		)


def test_validate_allows_nesting_of_different_styles() -> None:
	"""Жирный поверх спойлера — законная вложенность, а не перекрытие."""
	rich = RichText(
		"тайный текст",
		(_bold(0, 12), TextEntity(TextStyle.SPOILER, 0, 6)),
	)
	validate_rich_text(rich)


def test_json_round_trip() -> None:
	"""Разметка переживает хранение в БД; её отсутствие — пустой список.

	Пустой список, а не NULL: незаполненная колонка означала поколение
	до ADR-0033 (текст с разделителями), и возвращать эту двусмысленность
	нельзя — поколение закрыто миграцией f9e2b47c3a81.
	"""
	rich = RichText("текст", (TextEntity(TextStyle.LINK, 0, 5, "https://telegram.org"),))
	raw = rich_to_json(rich)
	assert rich_from_json(rich.text, raw) == rich
	assert rich_to_json(RichText("текст")) == []
	assert rich_from_json("текст", []) == RichText("текст")


def test_json_survives_broken_record() -> None:
	"""Повреждённая запись не роняет восстановление очереди — пост без разметки."""
	assert rich_from_json("текст", [{"style": "нездешний", "offset": 0, "length": 1}]) == RichText(
		"текст"
	)
	assert rich_from_json("текст", "не список") == RichText("текст")


def test_keep_entities_drops_markup_when_text_changed() -> None:
	"""Разметка действительна только для своего текста (ADR-0033)."""
	entities = (_bold(0, 6),)
	assert keep_entities("жирный", "жирный", entities) == entities
	assert keep_entities("жирный", "жирный!", entities) == ()


def test_entities_to_telethon_and_back() -> None:
	"""Перевод в сущности Telethon и обратно сохраняет наши виды."""
	rich = RichText(
		"цитата и ссылка",
		(
			TextEntity(TextStyle.EXPANDABLE_QUOTE, 0, 6),
			TextEntity(TextStyle.LINK, 9, 6, "https://telegram.org"),
			TextEntity(TextStyle.SPOILER, 0, 6),
		),
	)
	telethon = entities_to_telethon(rich)
	assert [type(item).__name__ for item in telethon] == [
		"MessageEntityBlockquote",
		"MessageEntityTextUrl",
		"MessageEntitySpoiler",
	]
	assert telethon[0].collapsed is True  # раскрывающаяся цитата
	assert set(rich_from_telethon(rich.text, telethon).entities) == set(rich.entities)


def test_rich_from_telethon_skips_foreign_kinds() -> None:
	"""Чужие виды разметки пропускаем: управлять ими приложение не умеет."""
	from telethon.tl import types

	entities = [
		types.MessageEntityBold(0, 4),
		types.MessageEntityMention(5, 3),  # упоминание — не наш вид
		types.MessageEntityBlockquote(9, 6),
	]
	parsed = rich_from_telethon("жирн упо цитата", entities)
	assert [entity.style for entity in parsed.entities] == [TextStyle.BOLD, TextStyle.QUOTE]


def test_html_escapes_and_wraps_styles() -> None:
	"""HTML для бота: служебные символы экранируются, стили — тегами."""
	assert html_from_rich(RichText("a < b & c")) == "a &lt; b &amp; c"
	rich = RichText("тайна", (TextEntity(TextStyle.SPOILER, 0, 5),))
	assert html_from_rich(rich) == "<tg-spoiler>тайна</tg-spoiler>"
	quote = RichText("цитата", (TextEntity(TextStyle.EXPANDABLE_QUOTE, 0, 6),))
	assert html_from_rich(quote) == "<blockquote expandable>цитата</blockquote>"
	link = RichText("тут", (TextEntity(TextStyle.LINK, 0, 3, "https://telegram.org"),))
	assert html_from_rich(link) == '<a href="https://telegram.org">тут</a>'
	pre = RichText("код", (TextEntity(TextStyle.PRE, 0, 3, "python"),))
	assert html_from_rich(pre) == '<pre><code class="language-python">код</code></pre>'


def test_html_nests_overlapping_entities() -> None:
	"""Перекрытие кусков превращается в правильную вложенность тегов.

	Наивное «открыть на начале, закрыть на конце» давало
	``<tg-spoiler><b>…</tg-spoiler>…</b>`` — сервер такого не принимает.
	"""
	rich = RichText(
		"Тайна и ссылка",
		(
			TextEntity(TextStyle.SPOILER, 0, 5),
			TextEntity(TextStyle.LINK, 8, 6, "https://telegram.org"),
			TextEntity(TextStyle.BOLD, 0, 14),
		),
	)
	assert html_from_rich(rich) == (
		'<b><tg-spoiler>Тайна</tg-spoiler> и <a href="https://telegram.org">ссылка</a></b>'
	)


def test_html_keeps_emoji_whole() -> None:
	"""Эмодзи — суррогатная пара: резать текст по одной единице нельзя."""
	rich = RichText("🙂жирный", (_bold(2, 6),))
	assert html_from_rich(rich) == "🙂<b>жирный</b>"


def test_post_html_never_parses_separators() -> None:
	"""Оформление — только сущностями; разделители уходят как символы."""
	assert post_html("**жирный**") == "**жирный**"
	assert post_html("жирный", (_bold(0, 6),)) == "<b>жирный</b>"


def test_first_link_prefers_signed_links() -> None:
	"""Превью строится по первой ссылке: подписанная важнее голой."""
	from pxcontrol.engine.telegram.rich_text import first_link

	assert first_link(RichText("текст https://telegram.org хвост")) == "https://telegram.org"
	assert first_link(RichText("без ссылок")) == ""
	signed = RichText(
		"тут ссылка и https://later.example",
		(TextEntity(TextStyle.LINK, 4, 6, "https://first.example"),),
	)
	assert first_link(signed) == "https://first.example"


def test_preview_json_round_trip() -> None:
	"""Настройки превью переживают хранение; обычное поведение — NULL."""
	from pxcontrol.engine.telegram.types import (
		LinkPreview,
		preview_from_json,
		preview_to_json,
	)

	preview = LinkPreview(large=True, above=True, url="https://telegram.org")
	assert preview_from_json(preview_to_json(preview)) == preview
	assert preview_to_json(LinkPreview()) is None
	assert preview_from_json(None) == LinkPreview()
	assert preview_from_json("мусор") == LinkPreview()


def test_preview_needs_media_only_for_large_and_above() -> None:
	"""Сырой путь нужен только крупному превью и превью над текстом.

	Выключение превью — обычный флаг отправки, и гонять ради него
	сырой запрос незачем.
	"""
	from pxcontrol.engine.telegram.types import LinkPreview

	assert not LinkPreview().needs_media
	assert not LinkPreview(disabled=True).needs_media
	assert LinkPreview(large=True).needs_media
	assert LinkPreview(above=True).needs_media
	# выключенное превью крупным не бывает — противоречие решается в пользу «нет»
	assert not LinkPreview(disabled=True, large=True).needs_media
