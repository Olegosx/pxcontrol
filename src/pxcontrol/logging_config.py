"""Настройка логирования: консоль + файл с ротацией.

Файл — ``logs/pxcontrol.log`` в папке приложения. Ротация по размеру:
при достижении ~2 МБ файл переименовывается в ``.1``…``.5``, старейший
удаляется. Уровень задаётся переменной окружения ``LOG_LEVEL``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pxcontrol import __version__
from pxcontrol.paths import logs_dir

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> Path:
	"""Конфигурирует корневой логгер: консоль + файл с ротацией.

	Args:
		level: Уровень логирования (``DEBUG``/``INFO``/``WARNING``…).
		log_dir: Каталог логов; по умолчанию — ``logs/`` в папке приложения
			(параметр нужен тестам).

	Returns:
		Путь к файлу лога.
	"""
	target_dir = log_dir if log_dir is not None else logs_dir()
	target_dir.mkdir(parents=True, exist_ok=True)
	log_file = target_dir / "pxcontrol.log"

	formatter = logging.Formatter(_LOG_FORMAT)
	console = logging.StreamHandler()
	console.setFormatter(formatter)
	file_handler = RotatingFileHandler(
		log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
	)
	file_handler.setFormatter(formatter)

	root = logging.getLogger()
	root.setLevel(level.upper())
	root.handlers.clear()
	root.addHandler(console)
	root.addHandler(file_handler)
	logging.getLogger(__name__).info(
		"pXcontrol v%s: логирование настроено (уровень %s, файл %s).",
		__version__, level.upper(), log_file,
	)
	return log_file
