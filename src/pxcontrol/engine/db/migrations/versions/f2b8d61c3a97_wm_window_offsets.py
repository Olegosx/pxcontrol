"""Окно показа вотермарка — отступы от краёв ролика вместо абсолютных времён.

wm_start → wm_start_offset (сек от начала), wm_end → wm_end_offset
(сек ДО КОНЦА): пресет применяется к роликам разной длины, «за 10 секунд
до конца» переносимо, абсолютная секунда — нет. Данных в колонках нет
(в интерфейс не выносились), поэтому перенос значений не требуется.

Revision ID: f2b8d61c3a97
Revises: e7a1c94f5d28
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2b8d61c3a97"
down_revision = "e7a1c94f5d28"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("video_presets") as batch:
		batch.alter_column(
			"wm_start", new_column_name="wm_start_offset",
			existing_type=sa.Float(), existing_nullable=True,
		)
		batch.alter_column(
			"wm_end", new_column_name="wm_end_offset",
			existing_type=sa.Float(), existing_nullable=True,
		)


def downgrade() -> None:
	with op.batch_alter_table("video_presets") as batch:
		batch.alter_column(
			"wm_start_offset", new_column_name="wm_start",
			existing_type=sa.Float(), existing_nullable=True,
		)
		batch.alter_column(
			"wm_end_offset", new_column_name="wm_end",
			existing_type=sa.Float(), existing_nullable=True,
		)
