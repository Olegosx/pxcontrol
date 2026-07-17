"""Политики внешних ключей и обязательные таймстемпы подписей.

Приложение включает проверку внешних ключей SQLite (PRAGMA foreign_keys=ON
на каждое соединение — Database), поэтому схема должна выражать политику
удаления явно: бот удаляется — каналы остаются без бота (SET NULL); канал
удаляется — его настройки и подписи уходят каскадом; поле или шаблон
удаляются — каскадом уходят значения словаря и строки состава. Заодно
закрывается расхождение с моделями из d9f3a6e42b71: created_at/updated_at
caption-таблиц становятся NOT NULL. Сироты, накопившиеся за время без
проверки ключей, удаляются до пересборки таблиц.

SQLite не умеет менять внешние ключи на месте; ограничения созданы без
имён, поэтому batch-режим пересобирает таблицы по явному определению
(``copy_from`` + ``recreate="always"``).

Revision ID: c1a4b83f7e29
Revises: d4a7c92f1b58
Create Date: 2026-07-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1a4b83f7e29"
down_revision = "d4a7c92f1b58"
branch_labels = None
depends_on = None

#: Таблицы с NULL-способными таймстемпами до этой миграции.
_CAPTION_TABLES = ("caption_fields", "caption_values", "caption_templates")


def _timestamps(*, nullable: bool) -> list[sa.Column]:
	return [
		sa.Column(
			"created_at", sa.DateTime(timezone=True),
			server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=nullable,
		),
		sa.Column(
			"updated_at", sa.DateTime(timezone=True),
			server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=nullable,
		),
	]


def _tables(*, strict: bool) -> dict[str, sa.Table]:
	"""Определения пересобираемых таблиц.

	``strict=True`` — целевая схема (политики удаления, NOT NULL);
	``strict=False`` — прежняя, для отката.
	"""
	meta = sa.MetaData()

	def fk(target: str, action: str) -> sa.ForeignKey:
		return sa.ForeignKey(target, ondelete=action if strict else None)

	channels = sa.Table(
		"channels", meta,
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("title", sa.String(255), nullable=False),
		sa.Column("tg_chat_id", sa.String(64), nullable=False, unique=True),
		sa.Column("username", sa.String(255), nullable=True),
		sa.Column("bot_id", sa.Integer(), fk("bots.id", "SET NULL"), nullable=True),
		*_timestamps(nullable=False),
		sa.Column(
			"userbot_admin", sa.Boolean(), server_default="0", nullable=False
		),
	)
	caption_fields = sa.Table(
		"caption_fields", meta,
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"channel_id", sa.Integer(), fk("channels.id", "CASCADE"),
			nullable=False,
		),
		sa.Column("name", sa.String(64), nullable=False),
		sa.Column("hashtag", sa.Boolean(), nullable=False),
		sa.Column("multiple", sa.Boolean(), nullable=False),
		*_timestamps(nullable=not strict),
	)
	caption_values = sa.Table(
		"caption_values", meta,
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"field_id", sa.Integer(), fk("caption_fields.id", "CASCADE"),
			nullable=False,
		),
		sa.Column("value", sa.String(128), nullable=False),
		*_timestamps(nullable=not strict),
	)
	caption_templates = sa.Table(
		"caption_templates", meta,
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"channel_id", sa.Integer(), fk("channels.id", "CASCADE"),
			nullable=False,
		),
		sa.Column("name", sa.String(64), nullable=False),
		sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
		*_timestamps(nullable=not strict),
		sa.Column("filename_pattern", sa.String(255), nullable=True),
	)
	caption_template_fields = sa.Table(
		"caption_template_fields", meta,
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"template_id", sa.Integer(), fk("caption_templates.id", "CASCADE"),
			nullable=False,
		),
		sa.Column(
			"field_id", sa.Integer(), fk("caption_fields.id", "CASCADE"),
			nullable=False,
		),
		sa.Column("position", sa.Integer(), nullable=False),
		sa.Column("enabled", sa.Boolean(), nullable=False),
	)
	return {
		"channels": channels,
		"caption_fields": caption_fields,
		"caption_values": caption_values,
		"caption_templates": caption_templates,
		"caption_template_fields": caption_template_fields,
	}


def _rebuild(*, strict: bool) -> None:
	"""Пересобирает таблицы по явному определению (nullable в _timestamps
	уже целевой, поэтому отдельных alter_column не нужно)."""
	for name, table in _tables(strict=strict).items():
		with op.batch_alter_table(name, copy_from=table, recreate="always"):
			pass


def upgrade() -> None:
	bind = op.get_bind()
	# сироты времён «ключи не проверялись»: сперва родители, потом дети
	bind.execute(sa.text(
		"UPDATE channels SET bot_id = NULL WHERE bot_id IS NOT NULL "
		"AND bot_id NOT IN (SELECT id FROM bots)"
	))
	bind.execute(sa.text(
		"DELETE FROM channel_settings "
		"WHERE channel_id NOT IN (SELECT id FROM channels)"
	))
	bind.execute(sa.text(
		"DELETE FROM caption_templates "
		"WHERE channel_id NOT IN (SELECT id FROM channels)"
	))
	bind.execute(sa.text(
		"DELETE FROM caption_fields "
		"WHERE channel_id NOT IN (SELECT id FROM channels)"
	))
	bind.execute(sa.text(
		"DELETE FROM caption_values "
		"WHERE field_id NOT IN (SELECT id FROM caption_fields)"
	))
	bind.execute(sa.text(
		"DELETE FROM caption_template_fields "
		"WHERE template_id NOT IN (SELECT id FROM caption_templates) "
		"OR field_id NOT IN (SELECT id FROM caption_fields)"
	))
	# NULL в таймстемпах (записи мимо ORM) — заполнить перед NOT NULL
	for table in _CAPTION_TABLES:
		for column in ("created_at", "updated_at"):
			bind.execute(sa.text(
				f"UPDATE {table} SET {column} = CURRENT_TIMESTAMP "  # noqa: S608 — имена из констант
				f"WHERE {column} IS NULL"
			))
	_rebuild(strict=True)


def downgrade() -> None:
	_rebuild(strict=False)
