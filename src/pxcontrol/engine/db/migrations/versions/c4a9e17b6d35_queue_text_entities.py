"""Разметка текста у элемента очереди отправки (ADR-0033, подача C1).

Оформление поста Telegram держит не в самом тексте, а рядом —
сущностями со смещением и длиной. Приложение переходит на тот же
способ, значит разметку нужно хранить: у элемента очереди появляется
колонка ``entities`` (JSON, формат — ``rich_to_json``).

Существующие элементы получают NULL — «разметки нет». Их текст уходит
прежним путём: строку с разделителями (``**жирный**``) разбирает сам
транспорт, как разбирал до этого решения. Переносить данные не нужно
и нечего.

Revision ID: c4a9e17b6d35
Revises: b2f6c94e8d17
Create Date: 2026-09-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4a9e17b6d35"
down_revision = "b2f6c94e8d17"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("entities", sa.JSON(), nullable=True))


def downgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("entities")
