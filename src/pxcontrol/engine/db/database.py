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


def _run_migrations(sync_url: str) -> None:
	"""Применяет миграции до последней версии (синхронно, для потока)."""
	from alembic import command
	from alembic.config import Config

	cfg = Config()
	cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
	cfg.set_main_option("sqlalchemy.url", sync_url)
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
