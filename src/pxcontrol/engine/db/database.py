"""Доступ к базе данных (SQLAlchemy 2.0, асинхронный режим) и миграции."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import (
	AsyncEngine,
	AsyncSession,
	async_sessionmaker,
	create_async_engine,
)

logger = logging.getLogger(__name__)

#: Каталог с миграциями Alembic (внутри пакета).
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


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
		self._engine: AsyncEngine = create_async_engine(url, future=True)
		self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
			self._engine, expire_on_commit=False
		)

	async def init(self) -> None:
		"""Приводит схему БД к актуальной версии миграциями Alembic.

		Alembic работает синхронно, поэтому выполняется в отдельном потоке,
		чтобы не блокировать цикл событий движка.
		"""
		sync_url = self._url.replace("+aiosqlite", "")
		await asyncio.to_thread(_run_migrations, sync_url)
		logger.info("База данных готова (миграции применены).")

	async def close(self) -> None:
		"""Закрывает все соединения с базой."""
		await self._engine.dispose()
