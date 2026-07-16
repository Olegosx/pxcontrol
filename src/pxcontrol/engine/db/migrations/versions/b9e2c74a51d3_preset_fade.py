"""Добавляет video_presets.fade_in/fade_out — затухание на краях ролика.

Длительность плавного появления из чёрного в начале и ухода в чёрное
в конце (сек; 0 — без эффекта). Эффект самостоятельный (не привязан
к обрезке), применяется к итоговому видео и звуку.

Revision ID: b9e2c74a51d3
Revises: d2b8e51c7f94
Create Date: 2026-07-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9e2c74a51d3"
down_revision = "d2b8e51c7f94"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column("fade_in", sa.Float(), nullable=False, server_default="0"),
	)
	op.add_column(
		"video_presets",
		sa.Column("fade_out", sa.Float(), nullable=False, server_default="0"),
	)


def downgrade() -> None:
	op.drop_column("video_presets", "fade_out")
	op.drop_column("video_presets", "fade_in")
