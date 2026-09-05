"""Доступ к базе данных (SQLAlchemy 2.0, асинхронный режим) и миграции."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
	AsyncEngine,
	AsyncSession,
	async_sessionmaker,
	create_async_engine,
)
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

#: Каталог с миграциями Alembic (внутри пакета).
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
	"""Включает проверку внешних ключей на соединении.

	SQLite по умолчанию не проверяет внешние ключи: без прагмы объявленные
	каскады (например, настройки и подписи канала) — декоративные, а ссылки
	на удалённые строки остаются висеть. Прагма действует на соединение,
	поэтому выставляется обработчиком события ``connect`` движка.
	"""
	cursor = dbapi_connection.cursor()
	cursor.execute("PRAGMA foreign_keys=ON")
	cursor.close()


#: Сколько автокопий БД держать рядом с файлом (старшие удаляются).
_BACKUP_KEEP = 3

#: Суффикс автокопий: свой, отличный от ручных ``.bak-…`` владельца —
#: ротация не должна трогать чужие копии.
_BACKUP_SUFFIX = ".pre-migration-"


def _alembic_config(sync_url: str) -> Any:
	"""Конфигурация Alembic для нашей папки миграций."""
	from alembic.config import Config

	cfg = Config()
	cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
	cfg.set_main_option("sqlalchemy.url", sync_url)
	return cfg


def _backup_before_upgrade(cfg: Any, sync_url: str) -> Path | None:
	"""Копирует файл БД, если миграциям есть что применять.

	Прерванная миграция (питание, диск) — единственный сценарий потери
	невосполнимого: сессий аккаунтов, привязок каналов, очереди
	отправки. Копия перед ``upgrade`` делает его обратимым; держится
	``_BACKUP_KEEP`` последних копий, старшие удаляются (только наши,
	с суффиксом ``_BACKUP_SUFFIX`` — ручные копии не трогаются).

	Returns:
		Путь созданной копии; None — копия не нужна (БД свежая
		или уже на актуальной ревизии).
	"""
	import shutil
	from datetime import datetime

	from alembic.runtime.migration import MigrationContext
	from alembic.script import ScriptDirectory
	from sqlalchemy import create_engine

	db_path = Path(make_url(sync_url).database or "")
	if not db_path.is_file():
		return None  # свежая БД — терять нечего
	sync_engine = create_engine(sync_url)
	try:
		with sync_engine.connect() as conn:
			current = MigrationContext.configure(conn).get_current_revision()
	except Exception:  # noqa: BLE001 — битый файл: копия тем более нужна
		logger.warning(
			"Ревизию БД прочитать не удалось — копия делается на всякий случай.",
			exc_info=True,
		)
		current = None
	finally:
		sync_engine.dispose()
	if current == ScriptDirectory.from_config(cfg).get_current_head():
		return None  # применять нечего — копия не нужна
	stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
	target = db_path.with_name(f"{db_path.name}{_BACKUP_SUFFIX}{stamp}")
	shutil.copy2(db_path, target)
	logger.info("Перед миграциями сделана копия БД: %s", target.name)
	backups = sorted(db_path.parent.glob(f"{db_path.name}{_BACKUP_SUFFIX}*"))
	for old in backups[:-_BACKUP_KEEP]:
		old.unlink(missing_ok=True)
	return target


def _run_migrations(sync_url: str) -> None:
	"""Применяет миграции до последней версии (синхронно, для потока)."""
	from alembic import command

	cfg = _alembic_config(sync_url)
	_backup_before_upgrade(cfg, sync_url)
	command.upgrade(cfg, "head")


class Database:
	"""Обёртка над асинхронным движком SQLAlchemy и фабрикой сессий."""

	def __init__(self, url: str) -> None:
		self._url = url
		# без пула (NullPool): отмена asyncio-задачи посреди запроса
		# (отмена отправки в очереди, ADR-0016) обрывает сессию — с пулом
		# её соединение вернулось бы в пул с незавершённой транзакцией
		# и держало бы блокировку SQLite («database is locked» у следующей
		# записи). Свежее соединение к локальному файлу — микросекунды.
		self._engine: AsyncEngine = create_async_engine(url, poolclass=NullPool)
		event.listens_for(self._engine.sync_engine, "connect")(_enable_foreign_keys)
		self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
			self._engine, expire_on_commit=False
		)

	async def init(self) -> None:
		"""Приводит схему БД к актуальной версии миграциями Alembic.

		Alembic работает синхронно, поэтому выполняется в отдельном потоке,
		чтобы не блокировать цикл событий движка.
		"""
		# синхронный адрес — штатным разбором URL, а не строковой заменой:
		# замена подстроки молча отдавала бы асинхронный адрес для любого
		# драйвера, кроме aiosqlite, и миграции падали бы не о том
		url = make_url(self._url)
		sync_url = url.set(drivername=url.get_backend_name()).render_as_string(hide_password=False)
		await asyncio.to_thread(_run_migrations, sync_url)
		logger.info("База данных готова (миграции применены).")

	async def close(self) -> None:
		"""Закрывает все соединения с базой."""
		await self._engine.dispose()
