"""Связанные словари подписей: значения живут внутри значений другого поля.

caption_fields.parent_field_id — поле объявлено зависимым от другого поля
того же канала (например, «Character» внутри «Title»); NULL — поле
независимое. caption_values.parent_value_id — конкретное значение
привязано к значению родительского словаря (персонаж — к своему тайтлу);
NULL — «без тайтла», показывается при любом выборе.

Политики удаления: родительское ПОЛЕ удаляется — зависимое поле становится
независимым (SET NULL); родительское ЗНАЧЕНИЕ (тайтл) удаляется — его
персонажи уходят каскадом (решение от 15.08.2026: персонажи существуют
только внутри тайтла).

SQLite не меняет внешние ключи на месте — batch-режим пересобирает таблицы
(copy_from + recreate="always"), как в c1a4b83f7e29. Колонка добавляется
в два шага: сперва простой ``add_column`` (безымянный внешний ключ через
ALTER Alembic добавить не может), затем пересборка по целевому определению
— она и создаёт сам ключ.

Revision ID: a8e4c73b9d52
Revises: c1a4b83f7e29
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8e4c73b9d52"
down_revision = "c1a4b83f7e29"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
	return [
		sa.Column(
			"created_at",
			sa.DateTime(timezone=True),
			server_default=sa.text("(CURRENT_TIMESTAMP)"),
			nullable=False,
		),
		sa.Column(
			"updated_at",
			sa.DateTime(timezone=True),
			server_default=sa.text("(CURRENT_TIMESTAMP)"),
			nullable=False,
		),
	]


def _tables(*, with_parents: bool) -> dict[str, sa.Table]:
	"""Определения пересобираемых таблиц.

	``with_parents=True`` — целевая схема (колонки связи);
	``with_parents=False`` — прежняя (по c1a4b83f7e29), для отката.
	"""
	meta = sa.MetaData()
	fields_columns = [
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"channel_id",
			sa.Integer(),
			sa.ForeignKey("channels.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("name", sa.String(64), nullable=False),
		sa.Column("hashtag", sa.Boolean(), nullable=False),
		sa.Column("multiple", sa.Boolean(), nullable=False),
		*_timestamps(),
	]
	values_columns = [
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"field_id",
			sa.Integer(),
			sa.ForeignKey("caption_fields.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("value", sa.String(128), nullable=False),
		*_timestamps(),
	]
	if with_parents:
		fields_columns.append(
			sa.Column(
				"parent_field_id",
				sa.Integer(),
				sa.ForeignKey("caption_fields.id", ondelete="SET NULL"),
				nullable=True,
			)
		)
		values_columns.append(
			sa.Column(
				"parent_value_id",
				sa.Integer(),
				sa.ForeignKey("caption_values.id", ondelete="CASCADE"),
				nullable=True,
			)
		)
	return {
		"caption_fields": sa.Table("caption_fields", meta, *fields_columns),
		"caption_values": sa.Table("caption_values", meta, *values_columns),
	}


def upgrade() -> None:
	target = _tables(with_parents=True)
	op.add_column("caption_fields", sa.Column("parent_field_id", sa.Integer(), nullable=True))
	op.add_column("caption_values", sa.Column("parent_value_id", sa.Integer(), nullable=True))
	# пересборка по целевому определению — внешние ключи появляются здесь
	for name in ("caption_fields", "caption_values"):
		with op.batch_alter_table(name, copy_from=target[name], recreate="always"):
			pass


def downgrade() -> None:
	target = _tables(with_parents=True)
	for name, column in (
		("caption_values", "parent_value_id"),
		("caption_fields", "parent_field_id"),
	):
		with op.batch_alter_table(name, copy_from=target[name], recreate="always") as batch:
			batch.drop_column(column)
