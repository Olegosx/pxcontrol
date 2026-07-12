"""Пути приложения. Режим — портативный: данные лежат рядом с приложением.

БД (и в будущем медиа-хранилище) находятся в каталоге приложения,
из какого бы каталога его ни запускали (решение от 06.07.2026).
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
	"""Возвращает каталог приложения.

	В собранном виде (PyInstaller и т.п.) — папка исполняемого файла;
	в разработке — корень проекта (там, где ``pyproject.toml``).
	"""
	if getattr(sys, "frozen", False):
		return Path(sys.executable).resolve().parent
	return Path(__file__).resolve().parents[2]


def default_db_url() -> str:
	"""URL базы данных по умолчанию — файл SQLite в каталоге приложения."""
	return f"sqlite+aiosqlite:///{app_dir() / 'pxcontrol.db'}"


def logs_dir() -> Path:
	"""Каталог файлов логов — подпапка ``logs`` в каталоге приложения."""
	return app_dir() / "logs"


def media_dir() -> Path:
	"""Каталог медиа-файлов — подпапка ``media`` в каталоге приложения."""
	return app_dir() / "media"
