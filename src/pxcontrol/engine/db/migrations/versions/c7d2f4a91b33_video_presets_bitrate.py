"""Добавляет video_presets.video_bitrate_kbps — целевой битрейт видео.

NULL — «как в оригинале»: при обработке берётся битрейт исходника.

Revision ID: c7d2f4a91b33
Revises: b41c7a9d2e10
Create Date: 2026-07-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d2f4a91b33"
down_revision = "b41c7a9d2e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column("video_bitrate_kbps", sa.Integer(), nullable=True),
	)


def downgrade() -> None:
	op.drop_column("video_presets", "video_bitrate_kbps")
