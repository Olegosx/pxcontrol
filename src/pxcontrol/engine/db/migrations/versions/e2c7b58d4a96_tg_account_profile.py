"""Профиль userbot-аккаунта из Telegram; пометка — необязательна.

У аккаунта появляются @имя и имя (first_name/last_name — раздельно,
как отдаёт Telegram): заполняются и актуализируются автоматически
(вход, старт приложения, зонды прав). Ручная пометка ``label`` теперь
необязательна — карточку подписывают данные Telegram, пометку можно
снять или переназначить в любой момент.

Revision ID: e2c7b58d4a96
Revises: f8b3d67c1a49
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2c7b58d4a96"
down_revision = "f8b3d67c1a49"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("tg_accounts") as batch:
		batch.alter_column("label", existing_type=sa.String(128), nullable=True)
		batch.add_column(sa.Column("username", sa.String(255), nullable=True))
		batch.add_column(sa.Column("first_name", sa.String(255), nullable=True))
		batch.add_column(sa.Column("last_name", sa.String(255), nullable=True))


def downgrade() -> None:
	# пустые пометки заполняются заглушкой: вернуть NOT NULL иначе нельзя
	op.execute("UPDATE tg_accounts SET label = 'аккаунт' WHERE label IS NULL")
	with op.batch_alter_table("tg_accounts") as batch:
		batch.drop_column("last_name")
		batch.drop_column("first_name")
		batch.drop_column("username")
		batch.alter_column("label", existing_type=sa.String(128), nullable=False)
