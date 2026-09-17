"""Превью ссылки у элемента очереди отправки (ADR-0033, подача C3).

Превью Telegram собирает сам по первой ссылке поста, но человек может
попросить иначе: не показывать вовсе, показать крупно или поставить
над текстом. Выбор живёт с постом, поэтому переживает перезапуск —
у элемента очереди появляется колонка ``preview`` (JSON).

Существующие элементы получают NULL — «как решит Telegram», то есть
прежнее поведение.

Revision ID: d5b8c23e7f41
Revises: c4a9e17b6d35
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d5b8c23e7f41"
down_revision = "c4a9e17b6d35"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("preview", sa.JSON(), nullable=True))


def downgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("preview")
