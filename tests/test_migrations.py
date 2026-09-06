"""Тесты миграций Alembic: полная схема на пустой БД и переносы данных."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pxcontrol.engine.db.database import MIGRATIONS_DIR, Database

EXPECTED_TABLES = {
	"app_settings",
	"community_settings",
	"bots",
	"tg_accounts",
	"tg_api_credentials",
	"ai_credentials",
	"video_presets",
	"communities",
	"publish_queue_items",
	"caption_fields",
	"caption_values",
	"caption_templates",
	"caption_template_fields",
}


async def test_migrations_create_all_tables(tmp_path: Path) -> None:
	"""После init() в пустой БД есть все таблицы схемы и журнал Alembic."""
	db_file = tmp_path / "migrate.db"
	db = Database(f"sqlite+aiosqlite:///{db_file}")
	await db.init()
	await db.close()

	with sqlite3.connect(db_file) as conn:
		rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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
		await session.execute(
			text(
				"INSERT INTO bots (label, token, created_at, updated_at) "
				"VALUES ('b', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
			)
		)
		await session.execute(
			text(
				"INSERT INTO tg_accounts (label, phone, created_at, updated_at) "
				"VALUES ('ub', '+7900', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
			)
		)
		await session.execute(
			text(
				"INSERT INTO communities (title, tg_chat_id, bot_id, tg_account_id,"
				" created_at, updated_at) VALUES ('c', '-1001', 1, 1,"
				" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
			)
		)
		await session.execute(
			text(
				"INSERT INTO community_settings (community_id, name, value) "
				"VALUES (1, 'enabled', 'false')"
			)
		)
		await session.execute(
			text(
				"INSERT INTO caption_fields (id, community_id, name, hashtag,"
				" multiple, created_at, updated_at) VALUES (1, 1, 'Genre', 1, 0,"
				" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
			)
		)
		await session.execute(
			text(
				"INSERT INTO caption_values (field_id, value, created_at,"
				" updated_at) VALUES (1, 'drama', CURRENT_TIMESTAMP,"
				" CURRENT_TIMESTAMP)"
			)
		)
		await session.commit()

	async with db.session_factory() as session:
		await session.execute(text("DELETE FROM bots WHERE id = 1"))
		await session.commit()
		bot_id = (
			await session.execute(text("SELECT bot_id FROM communities WHERE id = 1"))
		).scalar_one()
		assert bot_id is None  # SET NULL, а не висячая ссылка

		await session.execute(text("DELETE FROM tg_accounts WHERE id = 1"))
		await session.commit()
		account_id = (
			await session.execute(text("SELECT tg_account_id FROM communities WHERE id = 1"))
		).scalar_one()
		assert account_id is None  # удаление аккаунта отвязывает канал (ADR-0019)

		await session.execute(text("DELETE FROM communities WHERE id = 1"))
		await session.commit()
		for table in ("community_settings", "caption_fields", "caption_values"):
			count = (
				await session.execute(
					text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — имена из констант
				)
			).scalar_one()
			assert count == 0, f"{table}: сироты после удаления канала"
	await db.close()


def test_tg_api_columns_dropped_accounts_survive(tmp_path: Path) -> None:
	"""Миграция d8f2a61c4e93: колонки ключа уходят, аккаунты и сессии целы.

	Переноса данных в миграции нет сознательно (разовая ручная операция,
	ADR-0018) — проверяется только схема и сохранность строк аккаунтов.
	"""
	db_file = tmp_path / "api_key.db"
	_upgrade(db_file, "b7f3d92c5a41")  # состояние до выделения ключа
	with sqlite3.connect(db_file) as conn:
		conn.execute(
			"INSERT INTO tg_accounts (label, phone, api_id, api_hash, session,"
			" created_at, updated_at) VALUES ('ub', '+7900', 37612995, 'cipher',"
			" 'session-cipher', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		columns = [row[1] for row in conn.execute("PRAGMA table_info(tg_accounts)")]
		row = conn.execute("SELECT label, phone, session FROM tg_accounts").fetchone()
		api_rows = conn.execute("SELECT COUNT(*) FROM tg_api_credentials").fetchone()
	assert "api_id" not in columns and "api_hash" not in columns
	assert row == ("ub", "+7900", "session-cipher")  # аккаунт и сессия не тронуты
	assert api_rows == (0,)  # данные не переносятся — таблица пуста


def test_community_userbot_flag_becomes_binding_column(tmp_path: Path) -> None:
	"""Миграция e6b9d43a7f21: флаг уходит, колонка привязки появляется пустой.

	Переноса данных в миграции нет сознательно (разовая ручная привязка,
	ADR-0019) — проверяется схема и сохранность строки канала.
	"""
	db_file = tmp_path / "binding.db"
	_upgrade(db_file, "d8f2a61c4e93")  # состояние до привязки
	with sqlite3.connect(db_file) as conn:
		conn.execute(
			# имя таблицы до переименования a9d4c37e8b15
			"INSERT INTO channels (title, tg_chat_id, userbot_admin,"
			" created_at, updated_at) VALUES ('c', '-1001', 1,"
			" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		columns = [row[1] for row in conn.execute("PRAGMA table_info(communities)")]
		row = conn.execute("SELECT title, tg_account_id FROM communities").fetchone()
	assert "userbot_admin" not in columns and "tg_account_id" in columns
	assert row == ("c", None)  # канал цел, привязка не переносится — ручная


def test_community_enabled_moves_to_settings(tmp_path: Path) -> None:
	"""Перенос c8f1d29e4a35: выключенный канал — строкой, колонка удаляется.

	Переносятся только отличия от умолчания: включённый канал строки
	не получает (читается как умолчание ключа — True).
	"""
	db_file = tmp_path / "carry.db"
	_upgrade(db_file, "a5d8f31c9b27")  # состояние до переноса enabled
	with sqlite3.connect(db_file) as conn:
		for title, chat_id, enabled in (("Выкл", "-1001", 0), ("Вкл", "-1002", 1)):
			conn.execute(
				# имя таблицы до переименования a9d4c37e8b15
				"INSERT INTO channels (title, tg_chat_id, enabled, userbot_admin,"
				" created_at, updated_at) VALUES (?, ?, ?, 1,"
				" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
				(title, chat_id, enabled),
			)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		rows = conn.execute(
			"SELECT community_id, value FROM community_settings WHERE name = 'enabled'"
		).fetchall()
		columns = [row[1] for row in conn.execute("PRAGMA table_info(communities)")]
	assert rows == [(1, "false")]  # JSON-текст значения False
	assert "enabled" not in columns


async def test_backup_before_upgrade_copies_and_rotates(tmp_path: Path) -> None:
	"""Автокопия БД: делается при непримененных ревизиях, ротация — только своих."""
	from sqlalchemy.engine import make_url

	from pxcontrol.engine.db.database import (
		_alembic_config,
		_backup_before_upgrade,
		_run_migrations,
	)

	db_file = tmp_path / "app.db"
	url = f"sqlite:///{db_file}"
	cfg = _alembic_config(url)
	assert make_url(url).database == str(db_file)  # разбор адреса честный
	# свежей БД нет — копировать нечего
	assert _backup_before_upgrade(cfg, url) is None
	# «старая» БД (пустой файл — ревизия None, применять есть что) — копия
	db_file.write_bytes(b"old")
	manual = tmp_path / "app.db.bak-20260101-000000"  # ручная копия владельца
	manual.write_bytes(b"manual")
	for stamp in ("20260102-000000", "20260103-000000", "20260104-000000"):
		(tmp_path / f"app.db.pre-migration-{stamp}").write_bytes(b"x")
	created = _backup_before_upgrade(cfg, url)
	assert created is not None and created.read_bytes() == b"old"
	ours = sorted(p.name for p in tmp_path.glob("app.db.pre-migration-*"))
	assert len(ours) == 3  # ротация: старшая своя копия удалена
	assert "app.db.pre-migration-20260102-000000" not in ours
	assert manual.exists()  # ручные копии владельца не трогаются
	# БД на актуальной ревизии — копия больше не делается
	db_file.unlink()
	_run_migrations(url)
	before = sorted(p.name for p in tmp_path.glob("app.db.pre-migration-*"))
	assert _backup_before_upgrade(cfg, url) is None
	assert sorted(p.name for p in tmp_path.glob("app.db.pre-migration-*")) == before
