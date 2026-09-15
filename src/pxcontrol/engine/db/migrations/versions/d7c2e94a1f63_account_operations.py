"""Операции пользователей и ботов в Telegram — учёт активности (ADR-0030).

Запись на каждое обращение через дорожку шлюза: владелец (пользователь
или бот — одна из двух ссылок), вид операции, начало, конец, исход
и срок флуд-лимита. Живёт и умирает с владельцем (CASCADE); строки
старше года убирает сервис активности.

Revision ID: d7c2e94a1f63
Revises: c9e4a71d5b28
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7c2e94a1f63"
down_revision = "c9e4a71d5b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"account_operations",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"tg_account_id",
			sa.Integer(),
			sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"),
			nullable=True,
		),
		sa.Column(
			"bot_id", sa.Integer(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=True
		),
		sa.Column("kind", sa.String(16), nullable=False),
		sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("outcome", sa.String(16), nullable=False),
		sa.Column("wait_s", sa.Integer(), nullable=False, server_default=sa.text("0")),
	)
	op.create_index(
		"ix_account_operations_account_finished",
		"account_operations",
		["tg_account_id", "finished_at"],
	)
	op.create_index(
		"ix_account_operations_bot_finished", "account_operations", ["bot_id", "finished_at"]
	)
	op.create_index("ix_account_operations_finished", "account_operations", ["finished_at"])


def downgrade() -> None:
	op.drop_index("ix_account_operations_finished", table_name="account_operations")
	op.drop_index("ix_account_operations_bot_finished", table_name="account_operations")
	op.drop_index("ix_account_operations_account_finished", table_name="account_operations")
	op.drop_table("account_operations")
