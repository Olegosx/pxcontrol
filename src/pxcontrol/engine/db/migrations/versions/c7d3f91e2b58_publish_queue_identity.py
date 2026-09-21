"""Лицо публикации у элемента очереди отправки (ADR-0036, этап D).

Пост в очереди помнит, **от чьего имени** уйти: обе колонки пустые —
от имени сообщества (умолчание: в канале пост и так от имени канала,
в группе публикует анонимный администратор с явным ``send_as``);
иначе — явно названный человеком исполнитель: вид (``user`` / ``bot``)
и его id в нашей базе. Две колонки, а не одна строка «вид:id»: разбирать
нечего, а типы держит схема.

Существующие элементы получают пустые колонки — то есть умолчание,
которое и действовало до этого решения по факту (публиковало умолчание
сообщества, а в группах анонимный администратор писал от имени группы).

Revision ID: c7d3f91e2b58
Revises: b5c1e83d7a64
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d3f91e2b58"
down_revision = "b5c1e83d7a64"
branch_labels = None
depends_on = None


def upgrade() -> None:
	"""Добавляет колонки лица публикации (пустые — от имени сообщества)."""
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("identity_kind", sa.String(length=8), nullable=True))
		batch.add_column(sa.Column("identity_id", sa.Integer(), nullable=True))


def downgrade() -> None:
	"""Убирает колонки лица публикации."""
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("identity_id")
		batch.drop_column("identity_kind")
