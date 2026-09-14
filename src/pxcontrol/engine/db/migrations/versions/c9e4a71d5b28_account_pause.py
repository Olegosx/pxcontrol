"""Приостановка пользователей и ботов (ADR-0029).

У userbot-аккаунта и у бота появляется признак ``paused``: приостановленного
приложение не использует ни для чего (публикация, фоновые чтения,
зонды прав), но помнит целиком — сессию, пометку, членства и назначения
в сообществах. Существующие записи — не приостановлены.

Revision ID: c9e4a71d5b28
Revises: b8d4f2a7c6e1
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9e4a71d5b28"
down_revision = "b8d4f2a7c6e1"
branch_labels = None
depends_on = None


def _paused_column() -> sa.Column[bool]:
	"""Колонка признака паузы — одна и та же у обеих таблиц."""
	return sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("0"))


def upgrade() -> None:
	with op.batch_alter_table("tg_accounts") as batch:
		batch.add_column(_paused_column())
	with op.batch_alter_table("bots") as batch:
		batch.add_column(_paused_column())


def downgrade() -> None:
	with op.batch_alter_table("bots") as batch:
		batch.drop_column("paused")
	with op.batch_alter_table("tg_accounts") as batch:
		batch.drop_column("paused")
