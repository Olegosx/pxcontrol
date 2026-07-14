"""Переименовывает плейсхолдер названия в шаблонах имени: {title} → {video}.

У каналов бывает поле «Title» — различие с встроенным {title} только
регистром путало. Замена строгая по регистру: пользовательский {Title}
не затрагивается.

Revision ID: b6d4f82e9c13
Revises: a3c5e97d21b4
Create Date: 2026-07-15
"""
from __future__ import annotations

from alembic import op

revision = "b6d4f82e9c13"
down_revision = "a3c5e97d21b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.execute(
		"UPDATE caption_templates SET filename_pattern = "
		"REPLACE(filename_pattern, '{title}', '{video}') "
		"WHERE filename_pattern IS NOT NULL"
	)


def downgrade() -> None:
	op.execute(
		"UPDATE caption_templates SET filename_pattern = "
		"REPLACE(filename_pattern, '{video}', '{title}') "
		"WHERE filename_pattern IS NOT NULL"
	)
