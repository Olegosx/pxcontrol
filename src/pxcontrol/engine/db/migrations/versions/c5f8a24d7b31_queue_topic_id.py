"""Тема форума у элемента очереди отправки (этап 2 ADR-0021).

Черновик поста получил необязательную тему форума (``topic_id`` —
id корневого сообщения темы); элемент персистентной очереди переживает
перезапуск и обязан её помнить. NULL — общая лента: у каналов, обычных
групп и постов без выбранной темы.

Revision ID: c5f8a24d7b31
Revises: b3e7d51f9a24
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c5f8a24d7b31"
down_revision = "b3e7d51f9a24"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column("publish_queue_items", sa.Column("topic_id", sa.Integer(), nullable=True))


def downgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("topic_id")
