"""Добавляет bots.username — @имя бота, полученное при проверке токена.

Revision ID: b41c7a9d2e10
Revises: e5cc5457ed7a
Create Date: 2026-07-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b41c7a9d2e10"
down_revision = "e5cc5457ed7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column("bots", sa.Column("username", sa.String(length=255), nullable=True))


def downgrade() -> None:
	op.drop_column("bots", "username")
