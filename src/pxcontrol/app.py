"""Сборка приложения: порядок запуска движка и интерфейса.

Порядок запуска:
1. Загрузка настроек (``.env`` → :class:`Settings`).
2. Настройка логирования.
3. Старт движка в фоновом потоке (:class:`EngineWorker`):
   инициализация БД → подключение userbot → запуск шлюза Telegram.
4. Создание Qt-приложения и главного окна.
5. Запуск цикла событий Qt.

Порядок остановки (обратный) выполняется при закрытии окна:
шлюз Telegram → база данных → остановка потока движка.
"""

from __future__ import annotations

import logging

from pxcontrol.config import get_settings
from pxcontrol.engine import EngineWorker
from pxcontrol.logging_config import setup_logging

logger = logging.getLogger(__name__)


def run() -> int:
	"""Запускает приложение с графическим интерфейсом.

	Returns:
		Код выхода процесса.
	"""
	settings = get_settings()
	setup_logging(settings.log_level)
	worker = EngineWorker(settings)
	worker.start()
	try:
		return _run_qt(worker)
	finally:
		worker.stop()


def _run_qt(worker: EngineWorker) -> int:
	"""Создаёт Qt-приложение, показывает окно и крутит цикл событий."""
	from PySide6.QtWidgets import QApplication  # ленивый импорт интерфейса

	from pxcontrol.engine.services.settings import THEME_DARK
	from pxcontrol.ui.main_window import MainWindow
	from pxcontrol.ui.theme import apply_theme

	app = QApplication.instance() or QApplication([])
	# сохранённая тема — до создания окна (движок уже готов, ожидание — мс);
	# сбой чтения не валит запуск — откат к тёмной теме (умолчание ключа)
	try:
		dark = bool(
			worker.submit(worker.engine.settings.get(THEME_DARK)).result(timeout=5)
		)
	except Exception:  # noqa: BLE001 — тема не стоит отказа в запуске
		logger.warning("Не удалось прочитать тему — использую умолчание.", exc_info=True)
		dark = THEME_DARK.default
	apply_theme(dark=dark)
	window = MainWindow(worker)
	window.show()
	logger.info("Интерфейс запущен.")
	return int(app.exec())


def run_headless(seconds: float = 0.0) -> None:
	"""Запускает только движок без интерфейса (для проверки и тестов).

	Args:
		seconds: Сколько секунд держать движок запущенным перед остановкой.
	"""
	import time

	settings = get_settings()
	setup_logging(settings.log_level)
	worker = EngineWorker(settings)
	worker.start()
	try:
		if seconds:
			time.sleep(seconds)
	finally:
		worker.stop()
