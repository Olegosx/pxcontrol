"""Пул исполнителей сообщества обоих видов (ADR-0035, этап B).

Членства userbot-аккаунтов (``community_members``) и единственная ссылка
на бота (``communities.bot_id``) сходятся в одну таблицу
``community_executors``: строка на пару «сообщество + исполнитель»,
где владелец — ровно одна из двух ссылок (приём таблицы операций,
ADR-0030). Назначения остаются у сообщества, по одному на вид:
``default_tg_account_id`` и ``default_bot_id`` (наследник ``bot_id``).

Колонка ``communities.bot_can_edit`` уходит: право бота править чужие
сообщения принадлежит паре «сообщество + бот» и живёт теперь в снимке
прав этого бота.

**Перенос данных — только подтверждённое.** Прежняя модель пускала
в membership и в назначение ботом лишь тех, чьи права проверил зонд:
в канале — администратора с правом публиковать, в группе — участника,
не ограниченного в отправке. Это знание и переезжает: участие, право
публиковать (канал), возможность писать (группа) и право править чужие
сообщения, если оно было записано. Администраторам вдобавок
проставляются разрешения участника — на них ограничения не действуют
по правилам Telegram, это вывод, а не догадка. ``checked_at`` остаётся
пустым: полного снимка не было, и первая же перепроверка доступов
заменит перенесённое целиком.

Revision ID: b5c1e83d7a64
Revises: a4e9c72f1b85
Create Date: 2026-09-20
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "b5c1e83d7a64"
down_revision = "a4e9c72f1b85"
branch_labels = None
depends_on = None

#: Разрешения участника — состав ``MemberRights`` на момент миграции.
#: Список записан здесь целиком намеренно: миграция — исторический
#: слепок, и она не должна меняться вслед за кодом.
_ALL_MEMBER_RIGHTS = [
	"send_plain",
	"send_photos",
	"send_videos",
	"send_roundvideos",
	"send_audios",
	"send_voices",
	"send_docs",
	"send_stickers",
	"send_gifs",
	"send_games",
	"send_inline",
	"embed_links",
	"send_polls",
	"send_reactions",
	"invite_users",
	"pin_messages",
	"change_info",
	"manage_topics",
	"edit_rank",
]


def _payload(kind: str, status: str, *, can_edit: bool = False) -> str:
	"""Снимок прав из того, что подтверждала прежняя модель."""
	admin: list[str] = []
	allowed: list[str] = []
	if status in ("creator", "admin"):
		if kind == "channel":
			admin.append("post_messages")
		if can_edit:
			admin.append("edit_messages")
		allowed = list(_ALL_MEMBER_RIGHTS)
	else:
		allowed = ["send_plain"]
	return json.dumps({"admin": admin, "allowed": allowed}, ensure_ascii=False)


def upgrade() -> None:
	op.create_table(
		"community_executors",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column(
			"tg_account_id",
			sa.Integer(),
			sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"),
			nullable=True,
		),
		sa.Column(
			"bot_id", sa.Integer(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=True
		),
		sa.Column("status", sa.String(16), nullable=False),
		sa.Column("rights", sa.JSON(), nullable=False),
		sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
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
		sa.UniqueConstraint("community_id", "tg_account_id", name="uq_executor_account"),
		sa.UniqueConstraint("community_id", "bot_id", name="uq_executor_bot"),
		sa.CheckConstraint(
			"(tg_account_id IS NULL) <> (bot_id IS NULL)", name="ck_executor_single_owner"
		),
	)
	bind = op.get_bind()
	rows = bind.execute(
		sa.text(
			"SELECT m.community_id, m.tg_account_id, m.status, c.kind "
			"FROM community_members AS m JOIN communities AS c ON c.id = m.community_id"
		)
	).fetchall()
	for community_id, account_id, status, kind in rows:
		bind.execute(
			sa.text(
				"INSERT INTO community_executors (community_id, tg_account_id, status, rights) "
				"VALUES (:community, :account, :status, :rights)"
			),
			{
				"community": community_id,
				"account": account_id,
				"status": status,
				"rights": _payload(str(kind), str(status)),
			},
		)
	bots = bind.execute(
		sa.text("SELECT id, kind, bot_id, bot_can_edit FROM communities WHERE bot_id IS NOT NULL")
	).fetchall()
	for community_id, kind, bot_id, can_edit in bots:
		# бот в канале существует только администратором, в группе ему
		# довольно участия — тем же правилом заполняла роли миграция
		# f8b3d67c1a49, и здесь оно применяется к другому виду исполнителя
		status = "admin" if str(kind) == "channel" else "member"
		bind.execute(
			sa.text(
				"INSERT INTO community_executors (community_id, bot_id, status, rights) "
				"VALUES (:community, :bot, :status, :rights)"
			),
			{
				"community": community_id,
				"bot": bot_id,
				"status": status,
				"rights": _payload(str(kind), status, can_edit=bool(can_edit)),
			},
		)
	op.drop_table("community_members")
	op.execute("ALTER TABLE communities RENAME COLUMN bot_id TO default_bot_id")
	with op.batch_alter_table("communities") as batch:
		batch.drop_column("bot_can_edit")


def downgrade() -> None:
	# пул сворачивается обратно: пользователи — в членства, назначенный
	# бот — в ссылку сообщества; прочие боты прежней схеме неизвестны
	# и теряются осознанно, как и снимки прав
	op.execute("ALTER TABLE communities RENAME COLUMN default_bot_id TO bot_id")
	with op.batch_alter_table("communities") as batch:
		batch.add_column(
			sa.Column("bot_can_edit", sa.Boolean(), nullable=False, server_default=sa.text("0"))
		)
	op.create_table(
		"community_members",
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			primary_key=True,
		),
		sa.Column(
			"tg_account_id",
			sa.Integer(),
			sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"),
			primary_key=True,
		),
		sa.Column("status", sa.String(16), nullable=False),
		sa.Column("rights", sa.JSON(), nullable=True),
		sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
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
	)
	op.execute(
		"INSERT INTO community_members (community_id, tg_account_id, status, rights, checked_at) "
		"SELECT community_id, tg_account_id, status, rights, checked_at "
		"FROM community_executors WHERE tg_account_id IS NOT NULL"
	)
	op.execute(
		"UPDATE communities SET bot_can_edit = 1 WHERE id IN ("
		"SELECT community_id FROM community_executors "
		"WHERE bot_id IS NOT NULL AND rights LIKE '%edit_messages%')"
	)
	op.drop_table("community_executors")
