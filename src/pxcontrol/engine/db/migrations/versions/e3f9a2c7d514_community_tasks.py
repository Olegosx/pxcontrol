"""Задачи сообщества и журнал их запусков (ADR-0038).

Обслуживание сообщества (ADR-0026) становится **задачами**: сохранённая
настройка на пару «сообщество + вид» с параметрами и расписанием
(``community_tasks``) и журнал запусков (``task_runs``) — кто запустил,
какой исполнитель вёл, чем кончилось, отчёт и события. Обе таблицы
живут и умирают с сообществом (CASCADE); строка запуска — с задачей.

Строка задачи заводится при первом обращении к виду в сообществе,
поэтому переносить из прежней модели нечего: у обслуживания
не было ни настроек, ни истории.

Revision ID: e3f9a2c7d514
Revises: c7d3f91e2b58
Create Date: 2026-09-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3f9a2c7d514"
down_revision = "c7d3f91e2b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
	"""Создаёт таблицы задач и журнала запусков."""
	op.create_table(
		"community_tasks",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"community_id",
			sa.Integer(),
			sa.ForeignKey("communities.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("kind", sa.String(length=32), nullable=False),
		sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
		sa.Column("params", sa.JSON(), nullable=False),
		sa.Column("schedule", sa.JSON(), nullable=False),
		sa.Column("cursor", sa.JSON(), nullable=True),
		sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
		sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
		sa.Column(
			"created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
		),
		sa.Column(
			"updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
		),
		sa.UniqueConstraint("community_id", "kind", name="uq_community_task_kind"),
	)
	op.create_table(
		"task_runs",
		sa.Column("id", sa.Integer(), primary_key=True),
		sa.Column(
			"task_id",
			sa.Integer(),
			sa.ForeignKey("community_tasks.id", ondelete="CASCADE"),
			nullable=False,
		),
		sa.Column("trigger", sa.String(length=16), nullable=False),
		sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("0")),
		sa.Column("executor_kind", sa.String(length=8), nullable=True),
		sa.Column("executor_id", sa.Integer(), nullable=True),
		sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
		sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
		sa.Column("outcome", sa.String(length=16), nullable=False),
		sa.Column("report", sa.JSON(), nullable=True),
		sa.Column("events", sa.JSON(), nullable=True),
		sa.Column("error", sa.Text(), nullable=True),
	)
	op.create_index("ix_task_runs_task_started", "task_runs", ["task_id", "started_at"])


def downgrade() -> None:
	"""Убирает журнал запусков и задачи."""
	op.drop_index("ix_task_runs_task_started", table_name="task_runs")
	op.drop_table("task_runs")
	op.drop_table("community_tasks")
