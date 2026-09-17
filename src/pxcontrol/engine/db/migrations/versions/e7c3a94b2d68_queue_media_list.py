"""Файлы поста списком: альбом (ADR-0033, подача C4).

Пост перестал быть «один файл»: теперь это текст и **список** файлов —
ноль (текстовый пост), один (обычное вложение) или несколько (альбом).
Три колонки одиночного файла (``media_path``, ``media_kind``,
``rename_to``) заменяются одной ``media`` (JSON, формат —
``posts.media_to_json``).

Данные переносятся: у каждого элемента с файлом получается список
из одного элемента с теми же путём, видом и именем переименования;
у текстового поста — NULL. Обратный переход тоже переносит данные,
но берёт только **первый** файл: альбом в трёх колонках не выражается,
и это честно назвать здесь, а не обнаружить потерей файлов.

Revision ID: e7c3a94b2d68
Revises: d5b8c23e7f41
Create Date: 2026-09-17
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "e7c3a94b2d68"
down_revision = "d5b8c23e7f41"
branch_labels = None
depends_on = None


def upgrade() -> None:
	bind = op.get_bind()
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("media", sa.JSON(), nullable=True))
	rows = bind.execute(
		sa.text("SELECT id, media_path, media_kind, rename_to FROM publish_queue_items")
	).fetchall()
	for row in rows:
		if not row.media_path:
			continue
		payload = json.dumps(
			[{"path": row.media_path, "kind": row.media_kind, "rename_to": row.rename_to}],
			ensure_ascii=False,
		)
		bind.execute(
			sa.text("UPDATE publish_queue_items SET media = :media WHERE id = :id"),
			{"media": payload, "id": row.id},
		)
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("media_path")
		batch.drop_column("media_kind")
		batch.drop_column("rename_to")


def downgrade() -> None:
	bind = op.get_bind()
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("media_path", sa.String(1024), nullable=True))
		batch.add_column(
			sa.Column("media_kind", sa.String(16), nullable=False, server_default="none")
		)
		batch.add_column(sa.Column("rename_to", sa.String(255), nullable=True))
	rows = bind.execute(sa.text("SELECT id, media FROM publish_queue_items")).fetchall()
	for row in rows:
		if not row.media:
			continue
		files = json.loads(row.media) if isinstance(row.media, str) else row.media
		if not files:
			continue
		first = files[0]  # альбом тремя колонками не выражается — берём первый
		bind.execute(
			sa.text(
				"UPDATE publish_queue_items "
				"SET media_path = :path, media_kind = :kind, rename_to = :rename "
				"WHERE id = :id"
			),
			{
				"path": first.get("path"),
				"kind": first.get("kind", "none"),
				"rename": first.get("rename_to"),
				"id": row.id,
			},
		)
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("media")
