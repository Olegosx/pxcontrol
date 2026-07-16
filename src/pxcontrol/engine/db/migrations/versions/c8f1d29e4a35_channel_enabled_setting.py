"""Флаг «канал активен» — в настройки канала (ADR-0013).

Колонка ``channels.enabled`` переезжает строкой ``enabled``
в ``channel_settings``: в БД хранятся только отличия от умолчания,
поэтому переносятся лишь выключенные каналы (enabled = 0), остальные
читаются как умолчание ключа (True). Колонка удаляется.

Revision ID: c8f1d29e4a35
Revises: a5d8f31c9b27
Create Date: 2026-07-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8f1d29e4a35"
down_revision = "a5d8f31c9b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
	# JSON-значение хранится сериализованным текстом: False → 'false'
	op.execute(
		"INSERT OR IGNORE INTO channel_settings (channel_id, name, value) "
		"SELECT id, 'enabled', 'false' FROM channels WHERE enabled = 0"
	)
	# SQLite не умеет DROP COLUMN напрямую — batch-режим пересобирает таблицу
	with op.batch_alter_table("channels") as batch:
		batch.drop_column("enabled")


def downgrade() -> None:
	with op.batch_alter_table("channels") as batch:
		batch.add_column(sa.Column(
			"enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"),
		))
	op.execute(
		"UPDATE channels SET enabled = 0 WHERE id IN "
		"(SELECT channel_id FROM channel_settings "
		"WHERE name = 'enabled' AND value = 'false')"
	)
	op.execute("DELETE FROM channel_settings WHERE name = 'enabled'")
