"""Снимок прав участника сообщества (ADR-0035, этап A).

Членство перестаёт хранить одну роль и начинает хранить то, что о правах
известно целиком: ``status`` — как аккаунт участвует (наследник колонки
``role``: значения ``admin``/``member`` переносятся как есть, а новые —
«владелец», «ограничен», «не состоит», «исключён» — появляются с первой
же перепроверкой доступов), ``rights`` — полный снимок прав в JSON,
``checked_at`` — когда его сняли.

Существующим записям снимок **не выдумывается**: ``rights`` остаётся
пустым (NULL — «ещё не читался»), и его наполнит первая перепроверка.
Роль при этом знанием была, поэтому она и переезжает в статус.

Revision ID: a4e9c72f1b85
Revises: f9e2b47c3a81
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a4e9c72f1b85"
down_revision = "f9e2b47c3a81"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("community_members") as batch:
		batch.alter_column("role", new_column_name="status", existing_type=sa.String(16))
		batch.add_column(sa.Column("rights", sa.JSON(), nullable=True))
		batch.add_column(sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
	with op.batch_alter_table("community_members") as batch:
		batch.drop_column("checked_at")
		batch.drop_column("rights")
		batch.alter_column("status", new_column_name="role", existing_type=sa.String(16))
