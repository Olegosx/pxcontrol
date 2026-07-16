"""Подпапка пресета: ``video_presets.subdir``.

Подпапка — свойство пресета (контекст обработки — пресет, не канал):
одна и та же подпапка живёт внутри базовых папок видео (исходники /
результаты / опубликованные) из настроек приложения. Пустая строка —
без подпапки, файлы в корне базовых папок.

Revision ID: d4a7c92f1b58
Revises: c8f1d29e4a35
Create Date: 2026-07-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4a7c92f1b58"
down_revision = "c8f1d29e4a35"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column("video_presets", sa.Column(
		"subdir", sa.String(128), nullable=False, server_default="",
	))


def downgrade() -> None:
	with op.batch_alter_table("video_presets") as batch:
		batch.drop_column("subdir")
