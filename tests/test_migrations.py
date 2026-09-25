"""Тесты миграций Alembic: полная схема на пустой БД и переносы данных."""

from __future__ import annotations

import json
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
	"community_executors",
	"community_stats",
	"publish_queue_items",
	"caption_fields",
	"caption_values",
	"caption_presets",
	"caption_preset_fields",
	"community_stats_history",
	"community_analytics",
	"account_operations",
	"post_markups",
	"community_tasks",
	"task_runs",
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
	assert "alembic_version" in tables
	# равенство, а не «не меньше»: иначе список молча устаревает —
	# так и потерялась таблица кэша статистики, добавленная миграцией
	assert tables - {"alembic_version"} == EXPECTED_TABLES


def _upgrade(db_file: Path, revision: str) -> None:
	"""Накатывает миграции до указанной ревизии (синхронно)."""
	from alembic import command
	from alembic.config import Config

	cfg = Config()
	cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
	cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
	command.upgrade(cfg, revision)


def _downgrade(db_file: Path, revision: str) -> None:
	"""Откатывает миграции до указанной ревизии (синхронно)."""
	from alembic import command
	from alembic.config import Config

	cfg = Config()
	cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
	cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
	command.downgrade(cfg, revision)


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
				"INSERT INTO communities (title, tg_chat_id, default_bot_id,"
				" default_tg_account_id, created_at, updated_at) VALUES ('c', '-1001', 1, 1,"
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
			await session.execute(text("SELECT default_bot_id FROM communities WHERE id = 1"))
		).scalar_one()
		assert bot_id is None  # SET NULL, а не висячая ссылка

		await session.execute(text("DELETE FROM tg_accounts WHERE id = 1"))
		await session.commit()
		account_id = (
			await session.execute(
				text("SELECT default_tg_account_id FROM communities WHERE id = 1")
			)
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
		row = conn.execute("SELECT title, default_tg_account_id FROM communities").fetchone()
	assert "userbot_admin" not in columns and "default_tg_account_id" in columns
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


def test_community_kind_defaults_for_existing_rows(tmp_path: Path) -> None:
	"""Миграция b3e7d51f9a24: существующие сообщества — каналы без форума.

	Валидация до ADR-0021 пропускала только каналы, поэтому умолчание
	``kind = channel`` для старых строк честное; ``forum`` появляется
	выключенным и дальше живёт перепроверками доступов.
	"""
	db_file = tmp_path / "kind.db"
	_upgrade(db_file, "a9d4c37e8b15")  # состояние до вида и форума
	with sqlite3.connect(db_file) as conn:
		conn.execute(
			"INSERT INTO communities (title, tg_chat_id, created_at, updated_at)"
			" VALUES ('c', '-1001', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		row = conn.execute("SELECT kind, forum FROM communities").fetchone()
	assert row == ("channel", 0)


def test_bindings_become_memberships(tmp_path: Path) -> None:
	"""Миграция f8b3d67c1a49: привязка → членство + умолчание (ADR-0022).

	Участие заполняется по виду: каналу — admin (инвариант проверки
	подключения до ADR-0022), группе — member (наименьшие права,
	фактическое участие поднимет перепроверка). Колонка с тех пор
	переименована в ``status`` (ADR-0035, миграция a4e9c72f1b85):
	прогон идёт до головы цепочки, поэтому проверяется её нынешнее имя.
	"""
	db_file = tmp_path / "members.db"
	_upgrade(db_file, "d6a9c48e2f57")  # состояние до членств
	with sqlite3.connect(db_file) as conn:
		conn.execute(
			"INSERT INTO tg_accounts (label, phone, created_at, updated_at)"
			" VALUES ('ub', '+7900', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		)
		for title, chat_id, kind, account in (
			("Канал", "-1001", "channel", 1),
			("Группа", "-1002", "group", 1),
			("Без привязки", "-1003", "channel", None),
		):
			conn.execute(
				"INSERT INTO communities (title, tg_chat_id, kind, forum,"
				" tg_account_id, created_at, updated_at)"
				" VALUES (?, ?, ?, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
				(title, chat_id, kind, account),
			)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		members = conn.execute(
			"SELECT community_id, tg_account_id, status FROM community_executors"
			" WHERE tg_account_id IS NOT NULL ORDER BY community_id"
		).fetchall()
		defaults = conn.execute(
			"SELECT id, default_tg_account_id FROM communities ORDER BY id"
		).fetchall()
	assert members == [(1, 1, "admin"), (2, 1, "member")]
	assert defaults == [(1, 1), (2, 1), (3, None)]


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


def test_preset_resolution_defaults_to_fullhd(tmp_path: Path) -> None:
	"""Миграция a7c3e91b5d24: существующим пресетам проставляется 1080.

	До ступеней разрешения конвейер вписывал кадр в рамку FullHD, так что
	1080 — фактическое поведение сохранённых пресетов. NULL означал бы
	«не масштабировать» и молча сменил бы их всем разом.
	"""
	db_file = tmp_path / "resolution.db"
	_upgrade(db_file, "f6b2d84c9e17")  # состояние до ступеней разрешения
	with sqlite3.connect(db_file) as conn:
		conn.execute(
			"INSERT INTO video_presets (name, wm_corner, wm_margin, wm_opacity, wm_scale,"
			" intro, intro_source, intro_hold, xfade, cover, no_audio, wm_fade,"
			" trim_start, trim_end, fade_in, fade_out, subdir, created_at, updated_at)"
			" VALUES ('Старый', 'tr', 24, 1.0, 0.15, 0, 'random-middle', 1.0, 0.5, 0, 0, 0.0,"
			" 0.0, 0.0, 0.0, 0.0, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		row = conn.execute("SELECT name, target_resolution FROM video_presets").fetchone()
	assert row == ("Старый", 1080)


def test_preset_rescale_mode_defaults_to_constant_quality(tmp_path: Path) -> None:
	"""Миграция b8d4e27a6c15: существующие пресеты получают режим ``crf``.

	Прежнее поведение — битрейт исходника без пересчёта при смене размера
	кадра — не сохраняется сознательно (ADR-0044): при уменьшении кадра
	оно раздувало итог в разы.
	"""
	db_file = tmp_path / "rescale.db"
	_upgrade(db_file, "c2e6f18a4d93")  # состояние до режима
	with sqlite3.connect(db_file) as conn:
		conn.execute(
			"INSERT INTO video_presets (name, wm_corner, wm_margin, wm_opacity, wm_scale,"
			" intro, intro_source, intro_hold, xfade, cover, no_audio, wm_fade,"
			" trim_start, trim_end, fade_in, fade_out, subdir, target_resolution,"
			" created_at, updated_at)"
			" VALUES ('Старый', 'tr', 24, 1.0, 0.15, 0, 'random-middle', 1.0, 0.5, 0, 0, 0.0,"
			" 0.0, 0.0, 0.0, 0.0, '', 1080, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
		)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		row = conn.execute("SELECT name, rescale_bitrate_mode FROM video_presets").fetchone()
	assert row == ("Старый", "crf")


def _queue_row(conn: sqlite3.Connection, text: str, entities: str | None = None) -> int:
	"""Кладёт элемент очереди с заданным текстом и разметкой."""
	cur = conn.execute(
		"INSERT INTO publish_queue_items (community_id, text, status, markup_first,"
		" entities, created_at, updated_at) VALUES (1, ?, 'pending', 0, ?,"
		" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
		(text, entities),
	)
	return int(cur.lastrowid or 0)


def test_queue_text_moves_to_entities(tmp_path: Path) -> None:
	"""Миграция f9e2b47c3a81: текст с разделителями становится сущностями.

	Поколение до ADR-0033 несло разметку прямо в тексте (``**жирный**``),
	и разбирал её транспорт при отправке. Перенос раскладывает такой
	текст на видимый текст и сущности тем же разбором — что уйдёт
	в канал, не меняется. Строки нового поколения (колонка заполнена,
	пусть и пустым списком) не трогаются: их разделители — обычные
	символы.
	"""
	db_file = tmp_path / "entities.db"
	_upgrade(db_file, "a1f7d24c8e93")  # состояние до переноса
	with sqlite3.connect(db_file) as conn:
		plain = _queue_row(conn, "обычный текст без разделителей")
		styled = _queue_row(conn, "**жирный** и __курсив__")
		linked = _queue_row(conn, "см. [тут](https://example.com)")
		emoji = _queue_row(conn, "🙂**жирный**")
		unpaired = _queue_row(conn, "незакрытая **звёздочка")
		fresh = _queue_row(conn, "новый пост про __init__", entities="[]")
		conn.commit()

	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		rows = {
			row[0]: (row[1], json.loads(row[2]))
			for row in conn.execute("SELECT id, text, entities FROM publish_queue_items")
		}

	assert rows[plain] == ("обычный текст без разделителей", [])
	assert rows[styled][0] == "жирный и курсив"
	assert [(e["style"], e["offset"], e["length"]) for e in rows[styled][1]] == [
		("bold", 0, 6),
		("italic", 9, 6),
	]
	assert rows[linked][0] == "см. тут"
	assert rows[linked][1] == [
		{"style": "link", "offset": 4, "length": 3, "value": "https://example.com"}
	]
	# смещение — в кодовых единицах UTF-16: эмодзи занимает две
	assert rows[emoji] == ("🙂жирный", [{"style": "bold", "offset": 2, "length": 6, "value": ""}])
	assert rows[unpaired] == ("незакрытая **звёздочка", [])
	# новое поколение не тронуто: разделители остались символами
	assert rows[fresh] == ("новый пост про __init__", [])


def test_queue_text_downgrade_returns_separators(tmp_path: Path) -> None:
	"""Обратный ход собирает разделители назад — без потерь для своих видов."""
	db_file = tmp_path / "entities_back.db"
	_upgrade(db_file, "a1f7d24c8e93")
	with sqlite3.connect(db_file) as conn:
		item = _queue_row(conn, "**жирный** и `код` и [тут](https://example.com)")
		conn.commit()
	_upgrade(db_file, "head")
	_downgrade(db_file, "a1f7d24c8e93")
	with sqlite3.connect(db_file) as conn:
		row = conn.execute(
			"SELECT text, entities FROM publish_queue_items WHERE id = ?", (item,)
		).fetchone()
	assert row == ("**жирный** и `код` и [тут](https://example.com)", None)


def _caption_state_before_presets(conn: sqlite3.Connection) -> None:
	"""Два сообщества с шаблонами подписи и настройкой разбора — до c2e6f18a4d93.

	У первого поле «Video» уже занято человеком — перенос обязан взять
	другое имя, а не приклеиться к чужому полю.
	"""
	for community_id in (1, 2):
		conn.execute(
			"INSERT INTO communities (id, title, tg_chat_id, created_at, updated_at)"
			" VALUES (?, 'c', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
			(community_id, f"-100{community_id}"),
		)
	fields = [(10, 1, "Video"), (11, 1, "Genre"), (20, 2, "Title")]
	for field_id, community_id, name in fields:
		conn.execute(
			"INSERT INTO caption_fields (id, community_id, name, hashtag, multiple, show_name,"
			" created_at, updated_at) VALUES (?, ?, ?, 1, 0, 1,"
			" CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
			(field_id, community_id, name),
		)
	templates = [(1, 1, "{video} ({Genre})"), (2, 1, None), (3, 2, "{Title} {video}")]
	for template_id, community_id, pattern in templates:
		conn.execute(
			"INSERT INTO caption_templates (id, community_id, name, filename_pattern,"
			" created_at, updated_at) VALUES (?, ?, 't', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
			(template_id, community_id, pattern),
		)
	rows = [(1, 11, 0), (1, 10, 1), (2, 11, 0), (3, 20, 0)]
	for template_id, field_id, position in rows:
		conn.execute(
			"INSERT INTO caption_template_fields (template_id, field_id, position, enabled)"
			" VALUES (?, ?, ?, 1)",
			(template_id, field_id, position),
		)
	conn.execute(
		"INSERT INTO community_settings (community_id, name, value)"
		" VALUES (1, 'title_parse_rules', '[\"case:first_word\"]')"
	)


def test_caption_title_becomes_video_field(tmp_path: Path) -> None:
	"""Миграция c2e6f18a4d93: название — поле, первым в каждом пресете.

	Подписи не должны лишиться жирной первой строки: у каждого сообщества
	с шаблонами заводится поле названия (жирным, без имени, без решёток)
	с правилом «всё имя файла», ``{video}`` становится подстановкой этого
	поля, а настройка прежнего разбора названия удаляется.
	"""
	db_file = tmp_path / "presets.db"
	_upgrade(db_file, "e3f9a2c7d514")
	with sqlite3.connect(db_file) as conn:
		_caption_state_before_presets(conn)
		conn.commit()
	_upgrade(db_file, "head")
	with sqlite3.connect(db_file) as conn:
		created = {
			community_id: (field_id, name, flags)
			for field_id, community_id, name, *flags in conn.execute(
				"SELECT id, community_id, name, hashtag, multiple, show_name, bold"
				" FROM caption_fields WHERE id NOT IN (10, 11, 20)"
			)
		}
		patterns = dict(conn.execute("SELECT id, filename_pattern FROM caption_presets"))
		composition = conn.execute(
			"SELECT preset_id, field_id, position, source_rule FROM caption_preset_fields"
			" ORDER BY preset_id, position"
		).fetchall()
		settings = conn.execute("SELECT COUNT(*) FROM community_settings").fetchone()
	# имя «Video» у первого сообщества занято — берётся следующее
	assert created[1][1] == "Video 2" and created[2][1] == "Video"
	assert created[1][2] == [0, 0, 0, 1]  # без решёток, одно, без имени, жирным
	first, second = created[1][0], created[2][0]
	assert patterns == {1: "{Video 2} ({Genre})", 2: None, 3: "{Title} {Video}"}
	assert composition == [
		(1, first, 0, "{}"),
		(1, 11, 1, None),
		(1, 10, 2, None),
		(2, first, 0, "{}"),
		(2, 11, 1, None),
		(3, second, 0, "{}"),
		(3, 20, 1, None),
	]
	assert settings == (0,)
