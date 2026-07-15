"""Добавляет channels.userbot_admin — userbot админ канала (способ управления).

Канал можно администрировать ботом (bot_id), userbot-ом (этот флаг)
или обоими; подключение через userbot не требует бота в канале.

Revision ID: f4e8b3c92a17
Revises: e9c2a71f4d36
Create Date: 2026-07-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4e8b3c92a17"
down_revision = "e9c2a71f4d36"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"channels",
		sa.Column(
			"userbot_admin", sa.Boolean(), nullable=False, server_default="0"
		),
	)


def downgrade() -> None:
	op.drop_column("channels", "userbot_admin")
