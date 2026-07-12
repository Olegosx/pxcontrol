"""Окружение Alembic: связывает миграции с метаданными моделей."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from pxcontrol.engine.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
	"""Генерирует SQL без подключения к БД (режим --sql)."""
	context.configure(
		url=config.get_main_option("sqlalchemy.url"),
		target_metadata=target_metadata,
		literal_binds=True,
	)
	with context.begin_transaction():
		context.run_migrations()


def run_migrations_online() -> None:
	"""Применяет миграции через живое подключение к БД."""
	connectable = engine_from_config(
		config.get_section(config.config_ini_section, {}),
		prefix="sqlalchemy.",
		poolclass=pool.NullPool,
	)
	with connectable.connect() as connection:
		context.configure(connection=connection, target_metadata=target_metadata)
		with context.begin_transaction():
			context.run_migrations()


if context.is_offline_mode():
	run_migrations_offline()
else:
	run_migrations_online()
