"""Вид сообщества и признак форума (этап 1 ADR-0021).

Колонка ``kind`` («channel»/«group») — вид определяется при
подключении и не меняется жизнью записи; существующие строки получают
``channel`` (валидация до ADR-0021 пропускала только каналы).
Колонка ``forum`` — темы включены; изменчивое свойство группы,
обновляется при подключении и перепроверке доступов.

Обе колонки — обычный ``ADD COLUMN`` с умолчанием на сервере,
пересборка таблицы не нужна.

Revision ID: b3e7d51f9a24
Revises: a9d4c37e8b15
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3e7d51f9a24"
down_revision = "a9d4c37e8b15"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.add_column(
		"communities",
		sa.Column("kind", sa.String(16), nullable=False, server_default="channel"),
	)
	op.add_column(
		"communities",
		sa.Column("forum", sa.Boolean(), nullable=False, server_default=sa.text("0")),
	)


def downgrade() -> None:
	with op.batch_alter_table("communities") as batch:
		batch.drop_column("forum")
		batch.drop_column("kind")
