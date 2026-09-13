"""История статистики сообществ и справка «Обзора» (ADR-0027).

Кэш статистики получает моменты проходов по источникам (бот — часто,
userbot — редко), справочные поля (встроенная статистика доступна,
связанное сообщество, создано, последний пост) и итог последнего прохода
обслуживания по удалённым аккаунтам. Рядом — история снимков
(участники и онлайн в момент опроса) и последний ответ встроенной
статистики Telegram (одна строка на сообщество, JSON разобранных рядов).
Обе новые таблицы живут и умирают с сообществом (CASCADE).

Revision ID: b8d4f2a7c6e1
Revises: a7c3e91b5d24
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d4f2a7c6e1"
down_revision = "a7c3e91b5d24"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("community_stats") as batch:
		batch.add_column(sa.Column("bot_fetched_at", sa.DateTime(timezone=True), nullable=True))
		batch.add_column(sa.Column("full_fetched_at", sa.DateTime(timezone=True), nullable=True))
		batch.add_column(
			sa.Column(
				"can_view_stats",
				sa.Boolean(),
				nullable=False,
				server_default=sa.text("0"),
			)
		)
		batch.add_column(sa.Column("linked_chat_id", sa.String(64), nullable=True))
		batch.add_column(sa.Column("tg_created_at", sa.DateTime(timezone=True), nullable=True))
		batch.add_column(sa.Column("last_post_at", sa.DateTime(timezone=True), nullable=True))
		batch.add_column(sa.Column("deleted_found", sa.Integer(), nullable=True))
		batch.add_column(sa.Column("deleted_removed", sa.Integer(), nullable=True))
		batch.add_column(sa.Column("deleted_checked_at", sa.DateTime(timezone=True), nullable=True))
	op.create_table(
		"community_stats_history",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("participants", sa.Integer(), nullable=True),
		sa.Column("online", sa.Integer(), nullable=True),
	)
	op.create_index(
		"ix_community_stats_history_community_at",
		"community_stats_history",
		["community_id", "at"],
	)
	op.create_table(
		"community_analytics",
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			primary_key=True,
		),
		sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("payload", sa.JSON(), nullable=False),
	)


def downgrade() -> None:
	op.drop_table("community_analytics")
	op.drop_index("ix_community_stats_history_community_at", table_name="community_stats_history")
	op.drop_table("community_stats_history")
	with op.batch_alter_table("community_stats") as batch:
		for column in (
			"deleted_checked_at",
			"deleted_removed",
			"deleted_found",
			"last_post_at",
			"tg_created_at",
			"linked_chat_id",
			"can_view_stats",
			"full_fetched_at",
			"bot_fetched_at",
		):
			batch.drop_column(column)
