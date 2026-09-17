"""Очередь переходит на разметку сущностями целиком (ADR-0033).

До этой ревизии колонка ``entities`` пустовала у элементов, поставленных
в очередь раньше ADR-0033: их текст нёс разметку разделителями
(``**жирный**``), и разбирал её транспорт при отправке. После подачи C2
поле поста стало визуальным — разделители в нём обычные символы, —
и пустая колонка стала означать сразу две несовместимые вещи:
«старый текст, разбери разделители» и «человек не применял оформление».
Разделить их задним числом нечем, поэтому поколение закрывается
переносом данных, а не признаком.

Текст каждой такой строки разбирается тем же разбором, который применил
бы к ней userbot (основной путь публикации, ADR-0011), и раскладывается
на видимый текст и сущности. Что уйдёт в канал, от этого не меняется —
меняется лишь то, что теперь это записано явно и видно человеку в поле
правки. Побочно чинится опознание вышедшего поста: дозор кнопок
сверяет текст точно (ADR-0031, п. 9), а сверял он текст с разделителями
с постом без них.

Обратный ход собирает текст с разделителями назад. Виды, которых
в разделителях не существует (подчёркивание, спойлер, цитата),
при этом теряются: у перенесённых сюда строк их быть не может (разбор
их не порождает), а у написанных после — может, и честнее назвать это
здесь, чем обнаружить потерей оформления.

Обещания кнопок (``post_markups.match_text``) миграция намеренно
не трогает: обещание живёт не дольше суток, а поколения в нём
не различить — у поста, написанного после ADR-0033, разделители
могут быть обычными символами, и разбор испортил бы опознание.

Revision ID: f9e2b47c3a81
Revises: a1f7d24c8e93
Create Date: 2026-09-17
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "f9e2b47c3a81"
down_revision = "a1f7d24c8e93"
branch_labels = None
depends_on = None

#: Виды, которые порождает разбор разделителей, — в наши имена
#: (формат колонки — ``rich_text.rich_to_json``). Полный набор видов
#: приложения шире, но разделителями остальные не записываются.
_FROM_TELETHON = {
	"MessageEntityBold": "bold",
	"MessageEntityItalic": "italic",
	"MessageEntityStrike": "strike",
	"MessageEntityCode": "code",
	"MessageEntityPre": "pre",
	"MessageEntityTextUrl": "link",
}


def _split(text: str) -> tuple[str, list[dict[str, Any]]]:
	"""Раскладывает текст с разделителями на видимый текст и сущности."""
	from telethon.extensions import markdown

	visible, entities = markdown.parse(text)
	result: list[dict[str, Any]] = []
	for entity in entities or ():
		style = _FROM_TELETHON.get(type(entity).__name__)
		if style is None:
			# разбор такого вида не порождает; молча терять нельзя
			raise RuntimeError(f"Неизвестный вид разметки при переносе: {type(entity).__name__}")
		result.append(
			{
				"style": style,
				"offset": int(entity.offset),
				"length": int(entity.length),
				"value": str(getattr(entity, "url", None) or getattr(entity, "language", "") or ""),
			}
		)
	return visible, result


def _join(text: str, raw: Any) -> str:
	"""Собирает текст с разделителями обратно (виды сверх разбора теряются)."""
	from telethon.extensions import markdown
	from telethon.tl import types

	back = {
		"bold": types.MessageEntityBold,
		"italic": types.MessageEntityItalic,
		"strike": types.MessageEntityStrike,
		"code": types.MessageEntityCode,
	}
	entities = []
	for item in raw or ():
		style = item.get("style")
		offset, length = int(item["offset"]), int(item["length"])
		if style == "link":
			entities.append(types.MessageEntityTextUrl(offset, length, item.get("value") or ""))
			continue
		if style == "pre":
			entities.append(types.MessageEntityPre(offset, length, item.get("value") or ""))
			continue
		kind = back.get(str(style))
		if kind is not None:
			entities.append(kind(offset, length))
	return str(markdown.unparse(text, entities))


def upgrade() -> None:
	bind = op.get_bind()
	rows = bind.execute(
		sa.text("SELECT id, text FROM publish_queue_items WHERE entities IS NULL")
	).fetchall()
	for row in rows:
		visible, entities = _split(row.text or "")
		bind.execute(
			sa.text(
				"UPDATE publish_queue_items SET text = :text, entities = :entities WHERE id = :id"
			),
			{
				"text": visible,
				"entities": json.dumps(entities, ensure_ascii=False),
				"id": row.id,
			},
		)


def downgrade() -> None:
	bind = op.get_bind()
	rows = bind.execute(
		sa.text("SELECT id, text, entities FROM publish_queue_items WHERE entities IS NOT NULL")
	).fetchall()
	for row in rows:
		raw = row.entities
		if isinstance(raw, str):
			raw = json.loads(raw)
		bind.execute(
			sa.text("UPDATE publish_queue_items SET text = :text, entities = NULL WHERE id = :id"),
			{"text": _join(row.text or "", raw), "id": row.id},
		)
