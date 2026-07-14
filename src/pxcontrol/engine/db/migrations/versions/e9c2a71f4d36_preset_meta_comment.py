"""Добавляет video_presets.meta_comment — комментарий в метаданные файла.

Свободный текст (обычно «ссылка на канал — описание»), пишется тегом
``comment`` контейнера MP4 при кодировании. NULL/пусто — тег не пишется.

Revision ID: e9c2a71f4d36
Revises: b6d4f82e9c13
Create Date: 2026-07-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e9c2a71f4d36"
down_revision = "b6d4f82e9c13"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column("meta_comment", sa.String(length=512), nullable=True),
	)


def downgrade() -> None:
	op.drop_column("video_presets", "meta_comment")
