"""Настройка логирования приложения."""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
	"""Конфигурирует корневой логгер.

	Args:
		level: Уровень логирования (например, ``"INFO"`` или ``"DEBUG"``).
	"""
	logging.basicConfig(level=level.upper(), format=_LOG_FORMAT)
	logging.getLogger("apscheduler").setLevel(logging.WARNING)
