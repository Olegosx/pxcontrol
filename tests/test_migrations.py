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


async def test_schema_matches_models(tmp_path: Path) -> None:
	"""Схема после всех миграций совпадает с ORM-моделями.

	Autogenerate-сравнение Alembic: любой дрейф (новая колонка в модели
	без миграции, разошедшийся nullable и т.п.) даст непустой список
	отличий. Ловит расхождения навсегда — вместо ручной сверки.
	"""
	from alembic.autogenerate import compare_metadata
	from alembic.migration import MigrationContext
	from sqlalchemy import create_engine

	from pxcontrol.engine.db.models import Base

	db_file = tmp_path / "schema.db"
	db = Database(f"sqlite+aiosqlite:///{db_file}")
	await db.init()
	await db.close()

	engine = create_engine(f"sqlite:///{db_file}")
	try:
		with engine.connect() as conn:
			ctx = MigrationContext.configure(conn)
			diffs = compare_metadata(ctx, Base.metadata)
	finally:
		engine.dispose()
	assert diffs == [], f"Схема БД разошлась с моделями: {diffs}"


async def test_foreign_key_policies(tmp_path: Path) -> None:
	"""Политики внешних ключей работают: каскады и SET NULL.

	Проверяется связка «PRAGMA foreign_keys=ON на соединении (Database) +
	политики в схеме (c1a4b83f7e29)»: удаление канала уносит настройки
	и подписи, удаление бота отвязывает канал, а не оставляет висячую
	ссылку (SQLite переиспользует id — канал мог «прилипнуть» к чужому
	боту).
	"""
	from sqlalchemy import text

	db_file = tmp_path / "fk.db"
	db = Database(f"sqlite+aiosqlite:///{db_file}")
	await db.init()
	async with db.session_factory() as session:
		await session.execute(text(
			"INSERT INTO bots (label, token, created_at, updated_at) "
			"VALUES ('b', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		))
		await session.execute(text(
			"INSERT INTO channels (title, tg_chat_id, bot_id, userbot_admin,"
			" created_at, updated_at) VALUES ('c', '-1001', 1, 0,"
			" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		))
		await session.execute(text(
			"INSERT INTO channel_settings (channel_id, name, value) "
			"VALUES (1, 'enabled', 'false')"
		))
		await session.execute(text(
			"INSERT INTO caption_fields (id, channel_id, name, hashtag,"
			" multiple, created_at, updated_at) VALUES (1, 1, 'Genre', 1, 0,"
			" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		))
		await session.execute(text(
			"INSERT INTO caption_values (field_id, value, created_at,"
			" updated_at) VALUES (1, 'drama', CURRENT_TIMESTAMP,"
			" CURRENT_TIMESTAMP)"
		))
		await session.commit()

	async with db.session_factory() as session:
		await session.execute(text("DELETE FROM bots WHERE id = 1"))
		await session.commit()
		bot_id = (await session.execute(
			text("SELECT bot_id FROM channels WHERE id = 1")
		)).scalar_one()
		assert bot_id is None  # SET NULL, а не висячая ссылка

		await session.execute(text("DELETE FROM channels WHERE id = 1"))
		await session.commit()
		for table in ("channel_settings", "caption_fields", "caption_values"):
			count = (await session.execute(
				text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — имена из констант
			)).scalar_one()
			assert count == 0, f"{table}: сироты после удаления канала"
	await db.close()


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
