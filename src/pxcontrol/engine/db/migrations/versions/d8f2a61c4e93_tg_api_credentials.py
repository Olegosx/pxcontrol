"""Ключ API Telegram — один на приложение (ADR-0018).

Пара api_id/api_hash — реквизиты приложения с my.telegram.org, а не
аккаунта: с одной парой входят все userbot-аккаунты. Заводится таблица
``tg_api_credentials`` (одна запись, api_hash шифруется), колонки
``api_id``/``api_hash`` удаляются из ``tg_accounts``.

Переноса данных здесь нет сознательно: операция разовая, значения
переносятся вручную (решение от 2026-09-04; шифртекст копируется
как есть — ключ шифрования тот же).

Revision ID: d8f2a61c4e93
Revises: b7f3d92c5a41
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8f2a61c4e93"
down_revision = "b7f3d92c5a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.create_table(
		"tg_api_credentials",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("api_id", sa.Integer(), nullable=False),
		sa.Column("api_hash", sa.String(length=512), nullable=False),
		sa.Column(
			"created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
		),
		sa.Column(
			"updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
		),
	)
	# SQLite не умеет DROP COLUMN напрямую — batch-режим пересобирает таблицу
	with op.batch_alter_table("tg_accounts") as batch:
		batch.drop_column("api_id")
		batch.drop_column("api_hash")


def downgrade() -> None:
	# значения ключа при откате не возвращаются в аккаунты (переноса данных
	# в миграции нет в обе стороны); колонки — с заглушками, вход потребует
	# заново указать реквизиты
	with op.batch_alter_table("tg_accounts") as batch:
		batch.add_column(sa.Column("api_id", sa.Integer(), nullable=False, server_default="0"))
		batch.add_column(
			sa.Column("api_hash", sa.String(length=512), nullable=False, server_default="")
		)
	op.drop_table("tg_api_credentials")
