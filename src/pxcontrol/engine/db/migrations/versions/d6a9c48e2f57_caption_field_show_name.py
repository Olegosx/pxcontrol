"""Флаг «имя в подписи» у поля подписи.

Строка поля в подписи всегда собиралась как «Имя: значения»; теперь
имя опционально: при выключенном флаге в подпись уходят только значения.
По умолчанию флаг включён — существующие поля ведут себя как раньше.

Revision ID: d6a9c48e2f57
Revises: c5f8a24d7b31
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d6a9c48e2f57"
down_revision = "c5f8a24d7b31"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"caption_fields",
		sa.Column("show_name", sa.Boolean(), nullable=False, server_default=sa.true()),
	)


def downgrade() -> None:
	with op.batch_alter_table("caption_fields") as batch:
		batch.drop_column("show_name")
