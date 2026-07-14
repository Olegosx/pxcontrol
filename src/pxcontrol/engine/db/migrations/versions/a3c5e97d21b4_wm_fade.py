"""Добавляет video_presets.wm_fade — плавность появления/исчезания вотермарка.

Секунды перехода по альфа-каналу на краях окна показа; 0 — резко
(как раньше). Переход возможен только на краю с ненулевым отступом.

Revision ID: a3c5e97d21b4
Revises: f2b8d61c3a97
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3c5e97d21b4"
down_revision = "f2b8d61c3a97"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column("wm_fade", sa.Float(), nullable=False, server_default="0"),
	)


def downgrade() -> None:
	op.drop_column("video_presets", "wm_fade")
