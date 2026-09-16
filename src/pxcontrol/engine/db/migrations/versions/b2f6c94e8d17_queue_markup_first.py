"""Режим «кнопки важнее» у элемента очереди отправки (ADR-0031, п. 4).

У отложенного поста с кнопками два исхода на выбор человека: важнее
публикация (отложку держит сервер Telegram, кнопки бот дорисует после
выхода) или важнее кнопки (пост ждёт в нашей очереди и уходит ботом
в назначенную минуту — кнопки с первой секунды, но приложение должно
работать). Выбор живёт с постом, поэтому переживает перезапуск.

Существующие элементы получают False — прежнее поведение.

Revision ID: b2f6c94e8d17
Revises: f3a7c81d9e26
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2f6c94e8d17"
down_revision = "f3a7c81d9e26"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(
			sa.Column("markup_first", sa.Boolean(), nullable=False, server_default=sa.text("0"))
		)


def downgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("markup_first")
