"""Хранение настроек (ADR-0013): app_settings, channel_settings.

Зарезервированная таблица ``settings`` переименовывается
в ``app_settings`` (колонка ``key`` → ``name``) и начинает
использоваться по назначению. Появляется ``channel_settings`` —
настройки канала строками «(канал, имя) → значение» с внешним ключом.
Колонка-заделка ``channels.video_preset_id`` (потребителя не было)
удаляется — её роль берёт настройка канала ``default_video_preset``.

Revision ID: a5d8f31c9b27
Revises: b9e2c74a51d3
Create Date: 2026-07-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a5d8f31c9b27"
down_revision = "b9e2c74a51d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.rename_table("settings", "app_settings")
	op.alter_column("app_settings", "key", new_column_name="name")
	op.create_table(
		"channel_settings",
		sa.Column(
			"channel_id", sa.Integer(),
			sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False,
		),
		sa.Column("name", sa.String(128), nullable=False),
		sa.Column("value", sa.JSON(), nullable=False),
		sa.PrimaryKeyConstraint("channel_id", "name"),
	)
	# SQLite не умеет DROP COLUMN с внешним ключом напрямую — batch-режим
	# пересобирает таблицу
	with op.batch_alter_table("channels") as batch:
		batch.drop_column("video_preset_id")


def downgrade() -> None:
	with op.batch_alter_table("channels") as batch:
		batch.add_column(sa.Column(
			"video_preset_id", sa.Integer(),
			sa.ForeignKey("video_presets.id"), nullable=True,
		))
	op.drop_table("channel_settings")
	op.alter_column("app_settings", "name", new_column_name="key")
	op.rename_table("app_settings", "settings")
