"""Подписи к постам: поля со словарями и шаблоны каналов.

caption_fields — пул полей канала (имя, решётки, множественность);
caption_values — словарь значений поля (общий для всех шаблонов канала);
caption_templates — именованные шаблоны; caption_template_fields — состав
шаблона (поле, порядок, включено ли по умолчанию).

Revision ID: d9f3a6e42b71
Revises: c7d2f4a91b33
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9f3a6e42b71"
down_revision = "c7d2f4a91b33"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
	return [
		sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
		sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
	]


def upgrade() -> None:
	op.create_table(
		"caption_fields",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
		sa.Column("name", sa.String(length=64), nullable=False),
		sa.Column("hashtag", sa.Boolean(), nullable=False),
		sa.Column("multiple", sa.Boolean(), nullable=False),
		*_timestamps(),
	)
	op.create_table(
		"caption_values",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"field_id", sa.Integer(), sa.ForeignKey("caption_fields.id"), nullable=False
		),
		sa.Column("value", sa.String(length=128), nullable=False),
		*_timestamps(),
	)
	op.create_table(
		"caption_templates",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
		sa.Column("name", sa.String(length=64), nullable=False),
		sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
		*_timestamps(),
	)
	op.create_table(
		"caption_template_fields",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"template_id", sa.Integer(),
			sa.ForeignKey("caption_templates.id"), nullable=False,
		),
		sa.Column(
			"field_id", sa.Integer(), sa.ForeignKey("caption_fields.id"), nullable=False
		),
		sa.Column("position", sa.Integer(), nullable=False),
		sa.Column("enabled", sa.Boolean(), nullable=False),
	)


def downgrade() -> None:
	op.drop_table("caption_template_fields")
	op.drop_table("caption_templates")
	op.drop_table("caption_values")
	op.drop_table("caption_fields")
