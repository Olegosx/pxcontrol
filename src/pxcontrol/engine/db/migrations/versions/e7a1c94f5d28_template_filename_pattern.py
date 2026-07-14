"""Добавляет caption_templates.filename_pattern — шаблон имени файла.

Плейсхолдеры: {title}, {ИмяПоля}, {quality}, {channel}. NULL — шаблон
имени не задан, переименование при отправке недоступно.

Revision ID: e7a1c94f5d28
Revises: d9f3a6e42b71
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7a1c94f5d28"
down_revision = "d9f3a6e42b71"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"caption_templates",
		sa.Column("filename_pattern", sa.String(length=255), nullable=True),
	)


def downgrade() -> None:
	op.drop_column("caption_templates", "filename_pattern")
