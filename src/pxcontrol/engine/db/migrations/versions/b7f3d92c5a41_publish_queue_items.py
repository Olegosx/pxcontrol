"""Персистентная очередь отправки (ADR-0016).

publish_queue_items — черновики, ждущие отправки: поля PostDraft +
статус (pending / waiting — ждёт слота отложек / error). Успешные
и отменённые не хранятся; очередь живёт и умирает вместе с каналом.

Revision ID: b7f3d92c5a41
Revises: a8e4c73b9d52
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7f3d92c5a41"
down_revision = "a8e4c73b9d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"publish_queue_items",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"channel_id",
			sa.Integer(),
			sa.ForeignKey("channels.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("text", sa.Text(), nullable=False),
		sa.Column("media_path", sa.String(length=1024), nullable=True),
		sa.Column("media_kind", sa.String(length=16), nullable=False),
		sa.Column("when", sa.DateTime(timezone=True), nullable=True),
		sa.Column("rename_to", sa.String(length=255), nullable=True),
		sa.Column("status", sa.String(length=16), nullable=False),
		sa.Column("error", sa.Text(), nullable=True),
		sa.Column(
			"created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
		),
		sa.Column(
			"updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
		),
	)


def downgrade() -> None:
	op.drop_table("publish_queue_items")
