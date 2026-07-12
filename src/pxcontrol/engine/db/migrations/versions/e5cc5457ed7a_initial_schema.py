"""Начальная схема: настройки, боты, аккаунты MTProto, ключи ИИ,
пресеты видео, каналы.

Секретные колонки (token, api_hash, session, api_key) на уровне БД —
обычные строки: в них лежит шифртекст, шифрование выполняет приложение
(тип EncryptedStr, ADR-0009). Миграция от кода приложения не зависит.

Revision ID: e5cc5457ed7a
Revises:
Create Date: 2026-07-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5cc5457ed7a"
down_revision = None
branch_labels = None
depends_on = None

_TIMESTAMPS = (
	sa.Column(
		"created_at", sa.DateTime(timezone=True),
		server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False,
	),
	sa.Column(
		"updated_at", sa.DateTime(timezone=True),
		server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False,
	),
)


def _timestamps() -> tuple[sa.Column, ...]:
	"""Возвращает свежие колонки времени (Column нельзя переиспользовать)."""
	return tuple(col._copy() for col in _TIMESTAMPS)


def upgrade() -> None:
	op.create_table(
		"settings",
		sa.Column("key", sa.String(length=128), primary_key=True),
		sa.Column("value", sa.JSON(), nullable=False),
	)
	op.create_table(
		"bots",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("label", sa.String(length=128), nullable=False),
		sa.Column("token", sa.String(length=512), nullable=False),
		*_timestamps(),
	)
	op.create_table(
		"tg_accounts",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("label", sa.String(length=128), nullable=False),
		sa.Column("phone", sa.String(length=32), nullable=True),
		sa.Column("api_id", sa.Integer(), nullable=False),
		sa.Column("api_hash", sa.String(length=512), nullable=False),
		sa.Column("session", sa.String(length=2048), nullable=True),
		*_timestamps(),
	)
	op.create_table(
		"ai_credentials",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("provider", sa.String(length=64), nullable=False),
		sa.Column("label", sa.String(length=128), nullable=False),
		sa.Column("api_key", sa.String(length=512), nullable=False),
		*_timestamps(),
	)
	op.create_table(
		"video_presets",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("name", sa.String(length=128), nullable=False),
		sa.Column("watermark_path", sa.String(length=1024), nullable=True),
		sa.Column("wm_corner", sa.String(length=2), nullable=False),
		sa.Column("wm_margin", sa.Integer(), nullable=False),
		sa.Column("wm_opacity", sa.Float(), nullable=False),
		sa.Column("wm_scale", sa.Float(), nullable=False),
		sa.Column("wm_start", sa.Float(), nullable=True),
		sa.Column("wm_end", sa.Float(), nullable=True),
		sa.Column("intro", sa.Boolean(), nullable=False),
		sa.Column("intro_source", sa.String(length=255), nullable=False),
		sa.Column("intro_hold", sa.Float(), nullable=False),
		sa.Column("xfade", sa.Float(), nullable=False),
		sa.Column("cover", sa.Boolean(), nullable=False),
		sa.Column("no_audio", sa.Boolean(), nullable=False),
		*_timestamps(),
	)
	op.create_table(
		"channels",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("title", sa.String(length=255), nullable=False),
		sa.Column("tg_chat_id", sa.String(length=64), nullable=False, unique=True),
		sa.Column("username", sa.String(length=255), nullable=True),
		sa.Column("enabled", sa.Boolean(), nullable=False),
		sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id"), nullable=True),
		sa.Column(
			"video_preset_id", sa.Integer(),
			sa.ForeignKey("video_presets.id"), nullable=True,
		),
		*_timestamps(),
	)


def downgrade() -> None:
	op.drop_table("channels")
	op.drop_table("video_presets")
	op.drop_table("ai_credentials")
	op.drop_table("tg_accounts")
	op.drop_table("bots")
	op.drop_table("settings")
