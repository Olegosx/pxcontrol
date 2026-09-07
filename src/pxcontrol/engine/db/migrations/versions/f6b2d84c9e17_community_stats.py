"""Кэш статистики сообщества для карточек дашборда.

Подписчики, онлайн и число отложенных приезжают из Telegram; дашборд
рисует мгновенно из этого кэша, обновление идёт фоном с TTL. Аватар
хранится файлом на диске — в БД только путь. Удаление сообщества
уносит строку каскадом (файл аватара убирает движок).

Revision ID: f6b2d84c9e17
Revises: e2c7b58d4a96
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6b2d84c9e17"
down_revision = "e2c7b58d4a96"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"community_stats",
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			primary_key=True,
		),
		sa.Column("participants", sa.Integer(), nullable=True),
		sa.Column("online", sa.Integer(), nullable=True),
		sa.Column("scheduled_count", sa.Integer(), nullable=True),
		sa.Column("avatar_path", sa.String(1024), nullable=True),
		sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
	)


def downgrade() -> None:
	op.drop_table("community_stats")
