"""Кнопки под постом: право правки у бота и хранение клавиатуры (ADR-0031).

Три изменения, все — про кнопки:

1. ``communities.bot_can_edit`` — может ли бот сообщества править чужие
   сообщения. Без этого права он не дорисует клавиатуру к посту
   публикателя; право изменчивое (владелец канала может его отобрать),
   поэтому обновляется зондами вместе с названием и признаком форума.
   Существующие сообщества получают False — перепроверка доступов
   выяснит истину.
2. ``publish_queue_items.markup`` — обещанная клавиатура едет вместе
   с элементом очереди, потому что у элемента есть своя строка.
3. Таблица ``post_markups`` — обещанная клавиатура для постов, у которых
   своей строки нет: отложенные записи живут на сервере Telegram
   (ADR-0010), а клавиатуру мы обещали, и до публикации применить её
   нельзя. Текст храним целиком, а не хешем: постов в ожидании единицы,
   и при разборе инцидента по строке должно быть понятно, о каком посте
   речь.

Revision ID: e4d1b92f7a63
Revises: d7c2e94a1f63
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4d1b92f7a63"
down_revision = "d7c2e94a1f63"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("communities") as batch:
		batch.add_column(
			sa.Column("bot_can_edit", sa.Boolean(), nullable=False, server_default=sa.text("0"))
		)
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("markup", sa.JSON(), nullable=True))
	op.create_table(
		"post_markups",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("scheduled_message_id", sa.Integer(), nullable=True),
		sa.Column("when", sa.DateTime(timezone=True), nullable=True),
		sa.Column("match_text", sa.Text(), nullable=False),
		sa.Column("markup", sa.JSON(), nullable=False),
		sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
		sa.Column("error", sa.Text(), nullable=True),
		sa.Column(
			"created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
		),
		sa.Column(
			"updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
		),
	)
	# читают обещания по сообществу и в порядке времени публикации:
	# дозор берёт ближайшие, страница сообщества — все его
	op.create_index("ix_post_markups_community_when", "post_markups", ["community_id", "when"])


def downgrade() -> None:
	op.drop_index("ix_post_markups_community_when", table_name="post_markups")
	op.drop_table("post_markups")
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("markup")
	with op.batch_alter_table("communities") as batch:
		batch.drop_column("bot_can_edit")
