"""Добавляет video_presets.target_resolution — ступень разрешения кадра.

Число ступени присваивается короткой стороне итогового кадра
(720/1080/1440/2160); NULL — «как в оригинале», без масштабирования.

Существующим пресетам проставляется 1080: прежний конвейер вписывал
кадр в рамку FullHD, и это их фактическое поведение — оставить им NULL
значило бы молча перевести все пресеты на «не масштабировать».

Revision ID: a7c3e91b5d24
Revises: f6b2d84c9e17
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c3e91b5d24"
down_revision = "f6b2d84c9e17"
branch_labels = None
depends_on = None

#: Ступень существующих пресетов. Литерал, а не константа движка:
#: миграция описывает прошлое состояние схемы и не должна меняться
#: вслед за кодом.
_FULLHD = 1080


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column("target_resolution", sa.Integer(), nullable=True),
	)
	op.execute(sa.text(f"UPDATE video_presets SET target_resolution = {_FULLHD}"))


def downgrade() -> None:
	op.drop_column("video_presets", "target_resolution")
