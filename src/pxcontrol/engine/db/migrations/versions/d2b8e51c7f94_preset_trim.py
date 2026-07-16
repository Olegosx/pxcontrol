"""Добавляет video_presets.trim_start/trim_end — обрезка видео с краёв.

Сколько секунд отрезать в начале и в конце ролика (0 — не резать);
остальные параметры обработки считаются от обрезанной версии.

Revision ID: d2b8e51c7f94
Revises: f4e8b3c92a17
Create Date: 2026-07-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d2b8e51c7f94"
down_revision = "f4e8b3c92a17"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column("trim_start", sa.Float(), nullable=False, server_default="0"),
	)
	op.add_column(
		"video_presets",
		sa.Column("trim_end", sa.Float(), nullable=False, server_default="0"),
	)


def downgrade() -> None:
	op.drop_column("video_presets", "trim_end")
	op.drop_column("video_presets", "trim_start")
