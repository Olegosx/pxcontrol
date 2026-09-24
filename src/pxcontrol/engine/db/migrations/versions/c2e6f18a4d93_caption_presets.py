"""Пресеты подписи: название — обычное поле, разбор имени файла — у поля (ADR-0042).

Шаблоны подписи становятся **пресетами** — именем, которым их зовёт
интерфейс: таблицы ``caption_templates`` → ``caption_presets``
и ``caption_template_fields`` → ``caption_preset_fields``, колонка
``template_id`` → ``preset_id``.

Новые колонки:

- ``caption_fields.bold`` — строка поля в подписи выделяется жирным;
- ``caption_preset_fields.source_rule`` — правило «взять значение
  из имени файла» в JSON (NULL — значение вводит человек).

Перенос данных. До этой ревизии каждая подпись начиналась с жирного
названия, которое не было полем: строка ввода в окне сборки, особый
аргумент сборки и плейсхолдер ``{video}`` в шаблоне имени файла.
Чтобы подписи не лишились первой строки, у каждого сообщества
с пресетами заводится поле ``Video`` (жирным, без имени, без решёток,
одно значение), оно встаёт первым в каждый пресет с правилом «всё имя
файла» (прежнее поведение: имя без расширения и суффикса конвейера),
а ``{video}`` в шаблоне имени файла становится ``{Video}``. Если имя
``Video`` у сообщества уже занято, берётся ``Video 2``, ``Video 3``…

Настройка ``title_parse_rules`` (цепочка разбора названия на экране
«Пакет») удаляется: разбор переехал в правила полей пресета.

SQLite (с версии 3.25) переименовывает таблицы и колонки на месте,
ссылки внешних ключей в зависимых таблицах обновляются сами (как
в a9d4c37e8b15).

Обратный ход структурный: имена и колонки возвращаются, а созданное
поле названия остаётся обычным полем шаблонов, и ``{Video}`` обратно
в ``{video}`` не превращается — отличить перенесённое поле от поля,
заведённого человеком, после правок уже нечем.

Revision ID: c2e6f18a4d93
Revises: e3f9a2c7d514
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2e6f18a4d93"
down_revision = "e3f9a2c7d514"
branch_labels = None
depends_on = None

#: Имя поля названия, которое заводит перенос.
_TITLE_FIELD = "Video"


def upgrade() -> None:
	"""Переименовывает таблицы, добавляет колонки, переносит название в поле."""
	op.rename_table("caption_templates", "caption_presets")
	op.rename_table("caption_template_fields", "caption_preset_fields")
	op.execute("ALTER TABLE caption_preset_fields RENAME COLUMN template_id TO preset_id")
	op.add_column(
		"caption_fields",
		sa.Column("bold", sa.Boolean(), nullable=False, server_default=sa.false()),
	)
	op.add_column("caption_preset_fields", sa.Column("source_rule", sa.JSON(), nullable=True))
	conn = op.get_bind()
	communities = conn.execute(sa.text("SELECT DISTINCT community_id FROM caption_presets"))
	for (community_id,) in communities.fetchall():
		_move_title_to_field(conn, int(community_id))
	op.execute("DELETE FROM community_settings WHERE name = 'title_parse_rules'")


def _move_title_to_field(conn: sa.Connection, community_id: int) -> None:
	"""Заводит сообществу поле названия и ставит его первым в каждый пресет."""
	taken = {
		str(name)
		for (name,) in conn.execute(
			sa.text("SELECT name FROM caption_fields WHERE community_id = :c"),
			{"c": community_id},
		)
	}
	name = _TITLE_FIELD
	suffix = 2
	while name in taken:
		name = f"{_TITLE_FIELD} {suffix}"
		suffix += 1
	field_id = conn.execute(
		sa.text(
			"INSERT INTO caption_fields (community_id, name, hashtag, multiple, show_name,"
			" bold, created_at, updated_at) VALUES (:c, :n, 0, 0, 0, 1,"
			" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		),
		{"c": community_id, "n": name},
	).lastrowid
	presets = conn.execute(
		sa.text("SELECT id, filename_pattern FROM caption_presets WHERE community_id = :c"),
		{"c": community_id},
	).fetchall()
	for preset_id, pattern in presets:
		conn.execute(
			sa.text(
				"UPDATE caption_preset_fields SET position = position + 1 WHERE preset_id = :p"
			),
			{"p": preset_id},
		)
		# правило «всё имя файла» — пустой объект: все части правила
		# по умолчанию (SourceRule сервиса подписей)
		conn.execute(
			sa.text(
				"INSERT INTO caption_preset_fields (preset_id, field_id, position, enabled,"
				" source_rule) VALUES (:p, :f, 0, 1, '{}')"
			),
			{"p": preset_id, "f": field_id},
		)
		if pattern and "{video}" in pattern:
			conn.execute(
				sa.text("UPDATE caption_presets SET filename_pattern = :v WHERE id = :p"),
				{"v": pattern.replace("{video}", "{" + name + "}"), "p": preset_id},
			)


def downgrade() -> None:
	"""Возвращает прежние имена и убирает новые колонки (перенос не отменяется)."""
	with op.batch_alter_table("caption_preset_fields") as batch:
		batch.drop_column("source_rule")
	with op.batch_alter_table("caption_fields") as batch:
		batch.drop_column("bold")
	op.execute("ALTER TABLE caption_preset_fields RENAME COLUMN preset_id TO template_id")
	op.rename_table("caption_preset_fields", "caption_template_fields")
	op.rename_table("caption_presets", "caption_templates")
