"""Привязка userbot-аккаунта к каналу (ADR-0019).

Флаг ``channels.userbot_admin`` («какой-то userbot — админ») заменяется
честной ссылкой ``tg_account_id`` на конкретный аккаунт: постинг идёт
из сессии привязанного пользователя. Удаление аккаунта отвязывает
каналы (SET NULL) — политика та же, что у ботов.

SQLite не меняет внешние ключи на месте: таблица пересобирается
по явному определению (``copy_from`` + ``recreate`` — паттерн
c1a4b83f7e29); новая колонка у существующих строк — NULL.

Переноса данных здесь нет сознательно: операция разовая, привязка
существующих каналов выполняется вручную (решение от 2026-09-04,
как и перенос ключа API в d8f2a61c4e93).

Revision ID: e6b9d43a7f21
Revises: d8f2a61c4e93
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6b9d43a7f21"
down_revision = "d8f2a61c4e93"
branch_labels = None
depends_on = None


def _channels_table(*, with_binding: bool) -> sa.Table:
	"""Определение channels: целевое (привязка) или прежнее (флаг)."""
	meta = sa.MetaData()
	extra = (
		sa.Column(
			"tg_account_id",
			sa.Integer(),
			sa.ForeignKey("tg_accounts.id", ondelete="SET NULL"),
			nullable=True,
		)
		if with_binding
		else sa.Column("userbot_admin", sa.Boolean(), server_default="0", nullable=False)
	)
	return sa.Table(
		"channels",
		meta,
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column("title", sa.String(255), nullable=False),
		sa.Column("tg_chat_id", sa.String(64), nullable=False, unique=True),
		sa.Column("username", sa.String(255), nullable=True),
		sa.Column(
			"bot_id",
			sa.Integer(),
			sa.ForeignKey("bots.id", ondelete="SET NULL"),
			nullable=True,
		),
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
		extra,
	)


def upgrade() -> None:
	# сначала простая колонка (native ALTER ADD): copy_from-пересборка
	# копирует данные и требует, чтобы все колонки определения существовали
	with op.batch_alter_table("channels") as batch:
		batch.add_column(sa.Column("tg_account_id", sa.Integer(), nullable=True))
	# пересборка к целевому определению добавляет внешний ключ привязки
	# и отбрасывает userbot_admin (его нет в определении)
	with op.batch_alter_table(
		"channels", copy_from=_channels_table(with_binding=True), recreate="always"
	):
		pass


def downgrade() -> None:
	# привязка сворачивается обратно во флаг «админ есть»: флаг добавляется
	# рядом, заполняется из привязки, затем пересборка убирает привязку
	with op.batch_alter_table("channels") as batch:
		batch.add_column(
			sa.Column("userbot_admin", sa.Boolean(), nullable=False, server_default=sa.text("0"))
		)
	op.execute("UPDATE channels SET userbot_admin = 1 WHERE tg_account_id IS NOT NULL")
	with op.batch_alter_table(
		"channels", copy_from=_channels_table(with_binding=False), recreate="always"
	):
		pass
