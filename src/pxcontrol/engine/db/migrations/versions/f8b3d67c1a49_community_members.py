"""Членства userbot-аккаунтов и публикатор по умолчанию (этап A ADR-0022).

Единственная привязка ``communities.tg_account_id`` (ADR-0019)
обобщается: колонка меняет смысл и имя на ``default_tg_account_id``
(публикатор по умолчанию), а состав аккаунтов сообщества переезжает
в таблицу ``community_members`` с ролью.

Перенос данных: каждая существующая привязка становится членством.
Роль заполняется по виду сообщества: каналам — ``admin`` (инвариант
проверки подключения до ADR-0022), группам — ``member`` (наименьшие
права; фактическую роль поднимет первая перепроверка доступов).

Revision ID: f8b3d67c1a49
Revises: d6a9c48e2f57
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f8b3d67c1a49"
down_revision = "d6a9c48e2f57"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.execute("ALTER TABLE communities RENAME COLUMN tg_account_id TO default_tg_account_id")
	op.create_table(
		"community_members",
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			primary_key=True,
		),
		sa.Column(
			"tg_account_id",
			sa.Integer(),
			sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"),
			primary_key=True,
		),
		sa.Column("role", sa.String(16), nullable=False),
		sa.Column(
			"created_at",
			sa.DateTime(timezone=True),
			server_default=sa.text("(CURRENT_TIMESTAMP)"),
			nullable=False,
		),
		sa.Column(
			"updated_at",
			sa.DateTime(timezone=True),
			server_default=sa.text("(CURRENT_TIMESTAMP)"),
			nullable=False,
		),
	)
	op.execute(
		"INSERT INTO community_members (community_id, tg_account_id, role) "
		"SELECT id, default_tg_account_id, "
		"CASE kind WHEN 'channel' THEN 'admin' ELSE 'member' END "
		"FROM communities WHERE default_tg_account_id IS NOT NULL"
	)


def downgrade() -> None:
	# членства сворачиваются обратно в единственную привязку-умолчание;
	# прочие участники прежней схеме неизвестны и теряются осознанно
	op.drop_table("community_members")
	op.execute("ALTER TABLE communities RENAME COLUMN default_tg_account_id TO tg_account_id")
