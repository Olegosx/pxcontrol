"""Тесты портативных путей: БД — в папке приложения, а не в папке запуска."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pxcontrol.config import Settings
from pxcontrol.paths import app_dir, default_db_url


def test_app_dir_is_project_root() -> None:
	"""В режиме разработки каталог приложения — корень проекта."""
	assert (app_dir() / "pyproject.toml").exists()


def test_db_url_ignores_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Путь к БД не зависит от каталога, из которого запустили приложение."""
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("DATABASE_URL", raising=False)
	url = Settings(_env_file=None).database_url
	assert url == default_db_url()
	assert str(app_dir()) in url
	assert str(tmp_path) not in url
	assert os.path.isabs(url.removeprefix("sqlite+aiosqlite:///"))
