"""Обещание клавиатуры помнит номер вышедшего поста (ADR-0031, этап 2).

Пост «сейчас» может опубликоваться, а правка клавиатуры — не пройти
(у бота отняли право, его приостановили, пропала связь). Пост при этом
уже в канале: хоронить его ошибкой нельзя — повтор опубликовал бы второй.
Обещание остаётся в базе, и чтобы попытку можно было повторить, ему нужен
номер **вышедшего** поста; прежняя колонка хранит номер отложенной записи,
а это разные вещи.

Колонка добавляется отдельной ревизией, а не правкой прошлой: та уже
выпущена, а выпущенные миграции не переписываются.

Revision ID: f3a7c81d9e26
Revises: e4d1b92f7a63
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a7c81d9e26"
down_revision = "e4d1b92f7a63"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("post_markups") as batch:
		batch.add_column(sa.Column("message_id", sa.Integer(), nullable=True))


def downgrade() -> None:
	with op.batch_alter_table("post_markups") as batch:
		batch.drop_column("message_id")
