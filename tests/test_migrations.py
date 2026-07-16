"""Тесты миграций Alembic: полная схема на пустой БД и переносы данных."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pxcontrol.engine.db.database import MIGRATIONS_DIR, Database

EXPECTED_TABLES = {
	"app_settings", "channel_settings", "bots", "tg_accounts", "ai_credentials",
	"video_presets", "channels",
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
	assert tables >= EXPECTED_TABLES
	assert "alembic_version" in tables


def _upgrade(db_file: Path, revision: str) -> None:
	"""Накатывает миграции до указанной ревизии (синхронно)."""
	from alembic import command
	from alembic.config import Config

	cfg = Config()
	cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
	cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
	command.upgrade(cfg, revision)


def test_channel_enabled_moves_to_settings(tmp_path: Path) -> None:
	"""Перенос c8f1d29e4a35: выключенный канал — строкой, колонка удаляется.

	Переносятся только отличия от умолчания: включённый канал строки
	не получает (читается как умолчание ключа — True).
	"""
	db_file = tmp_path / "carry.db"
	_upgrade(db_file, "a5d8f31c9b27")  # состояние до переноса enabled
	with sqlite3.connect(db_file) as conn:
		for title, chat_id, enabled in (("Выкл", "-1001", 0), ("Вкл", "-1002", 1)):
			conn.execute(
				"INSERT INTO channels (title, tg_chat_id, enabled, userbot_admin,"
				" created_at, updated_at) VALUES (?, ?, ?, 1,"
				" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
				(title, chat_id, enabled),
			)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		rows = conn.execute(
			"SELECT channel_id, value FROM channel_settings WHERE name = 'enabled'"
		).fetchall()
		columns = [row[1] for row in conn.execute("PRAGMA table_info(channels)")]
	assert rows == [(1, "false")]  # JSON-текст значения False
	assert "enabled" not in columns
