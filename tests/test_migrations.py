"""Тест: миграции Alembic накатываются на пустую БД и создают все таблицы."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pxcontrol.engine.db.database import Database

EXPECTED_TABLES = {
	"settings", "bots", "tg_accounts", "ai_credentials", "video_presets", "channels",
	"caption_fields", "caption_values", "caption_templates", "caption_template_fields",
}


async def test_migrations_create_all_tables(tmp_path: Path) -> None:
	"""После init() в пустой БД есть все таблицы схемы и журнал Alembic."""
	db_file = tmp_path / "migrate.db"
	db = Database(f"sqlite+aiosqlite:///{db_file}")
	await db.init()
	await db.close()

	with sqlite3.connect(db_file) as conn:
		rows = conn.execute(
			"SELECT name FROM sqlite_master WHERE type='table'"
		).fetchall()
	tables = {name for (name,) in rows}
	assert EXPECTED_TABLES <= tables
	assert "alembic_version" in tables
