"""Дымовые тесты каркаса: настройки и запуск/остановка движка без интерфейса."""

from __future__ import annotations

from pathlib import Path

from pxcontrol.app import run_headless
from pxcontrol.config import Settings
from pxcontrol.config.settings import get_settings


def test_settings_defaults() -> None:
	"""Настройки создаются и имеют ожидаемые значения по умолчанию."""
	settings = Settings(_env_file=None)
	assert settings.database_url.startswith("sqlite")
	assert settings.log_level == "INFO"


def test_stop_after_failed_start_is_safe(tmp_path: Path) -> None:
	"""stop() после неудачного start() не должен падать (цикл уже закрыт)."""
	import pytest

	from pxcontrol.engine import EngineWorker

	bad_url = f"sqlite+aiosqlite:///{tmp_path}/no_such_dir/x.db"
	worker = EngineWorker(Settings(_env_file=None, database_url=bad_url))
	with pytest.raises(RuntimeError):
		worker.start()
	worker.stop()  # не должно бросить исключение


def test_engine_starts_and_stops(tmp_path: Path, monkeypatch) -> None:
	"""Движок запускается в фоновом потоке и корректно останавливается."""
	db_file = tmp_path / "smoke.db"
	monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
	get_settings.cache_clear()
	run_headless(seconds=0.0)
	get_settings.cache_clear()
