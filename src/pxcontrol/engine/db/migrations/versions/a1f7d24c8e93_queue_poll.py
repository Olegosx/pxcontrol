"""Опрос у поста очереди: колонка ``poll`` (ADR-0033, подача C5).

Опрос — третий вид содержимого поста наряду с текстом и файлами:
у Telegram это вложение без файла (``InputMediaPoll``), и хранить его
в тексте невозможно — у опроса есть вопрос, варианты и правила
голосования. Формат колонки — ``telegram.poll.poll_to_json``;
NULL означает «обычный пост», то есть все прежние элементы очереди
остаются собой.

Обратный переход просто снимает колонку: посты-опросы при этом теряют
своё содержимое целиком — выразить опрос прежними колонками нечем.
Такой элемент очереди станет пустым и будет отклонён проверкой при
отправке с понятной причиной, а не уйдёт в канал чем-то другим.

Revision ID: a1f7d24c8e93
Revises: e7c3a94b2d68
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f7d24c8e93"
down_revision = "e7c3a94b2d68"
branch_labels = None
depends_on = None


def upgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.add_column(sa.Column("poll", sa.JSON(), nullable=True))


def downgrade() -> None:
	with op.batch_alter_table("publish_queue_items") as batch:
		batch.drop_column("poll")
