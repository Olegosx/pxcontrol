"""Добавляет video_presets.rescale_bitrate_mode — битрейт при смене размера кадра.

Режим действует, когда битрейт пресета не задан («как в оригинале»),
а кадр меняет размер (ADR-0044): ``crf`` — постоянное качество,
``scale`` — битрейт исходника, пересчитанный под площадь нового кадра.

Существующим пресетам проставляется ``crf``. Их прежнее поведение —
битрейт исходника без пересчёта — сознательно не сохраняется: при
уменьшении кадра (4K → 1080 при ступени по умолчанию) оно раздувало
итог в разы, и исправление этой расточительности — цель ревизии.

Revision ID: b8d4e27a6c15
Revises: c2e6f18a4d93
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d4e27a6c15"
down_revision = "c2e6f18a4d93"
branch_labels = None
depends_on = None

#: Режим существующих пресетов. Литерал, а не константа движка:
#: миграция описывает прошлое состояние схемы и не должна меняться
#: вслед за кодом.
_CONSTANT_QUALITY = "crf"


def upgrade() -> None:
	op.add_column(
		"video_presets",
		sa.Column(
			"rescale_bitrate_mode",
			sa.String(16),
			nullable=False,
			server_default=_CONSTANT_QUALITY,
		),
	)


def downgrade() -> None:
	with op.batch_alter_table("video_presets") as batch:
		batch.drop_column("rescale_bitrate_mode")
