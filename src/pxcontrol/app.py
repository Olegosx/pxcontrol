"""Сборка приложения: порядок запуска движка и интерфейса.

Порядок запуска:
1. Загрузка настроек (``.env`` → :class:`Settings`).
2. Настройка логирования.
3. Старт движка в фоновом потоке (:class:`EngineWorker`):
   инициализация БД → прогрев настроек → активация userbot
   по сохранённой сессии (неудача подключения не мешает запуску).
4. Создание Qt-приложения и главного окна.
5. Запуск цикла событий Qt.

Перед созданием окна читается оформление (тема и плотность) — иначе
окно мигнуло бы чужими размерами при первом же применении настроек.

Порядок остановки (обратный) выполняется при закрытии окна: три очереди
(обслуживание → отправка → обработка видео) → сервис видео → шлюз
Telegram → база данных → остановка потока движка.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pxcontrol.config import get_settings
from pxcontrol.engine import EngineWorker
from pxcontrol.logging_config import setup_logging

logger = logging.getLogger(__name__)

#: Предел ожидания чтения оформления из цикла движка перед созданием
#: окна: движок уже готов, штатно это миллисекунды — предел лишь
#: страхует от зависшего цикла, не задерживая запуск заметно.
_APPEARANCE_READ_TIMEOUT_S = 5


def run() -> int:
	"""Запускает приложение с графическим интерфейсом.

	Returns:
		Код выхода процесса.
	"""
	settings = get_settings()
	log_file = setup_logging(settings.log_level)
	worker = EngineWorker(settings)
	try:
		worker.start()
	except Exception as exc:  # noqa: BLE001 — единственное место показа ошибки старта
		# без этого ошибка старта (миграция, keyring) уходила только
		# в stderr: при запуске из ярлыка окно «просто не открывалось»,
		# а лог-файл оставался пустым (ADR-0009 обещает честное падение)
		logger.critical("Движок не запустился.", exc_info=True)
		_show_startup_error(exc, log_file)
		return 1
	try:
		return _run_qt(worker)
	finally:
		worker.stop()


def _show_startup_error(exc: BaseException, log_file: Path) -> None:
	"""Показывает ошибку старта системным диалогом (терминала может не быть)."""
	try:
		from PySide6.QtWidgets import QApplication, QMessageBox

		from pxcontrol.engine.errors import user_message

		QApplication.instance() or QApplication([])
		QMessageBox.critical(
			None,
			"pXcontrol — ошибка запуска",
			f"Движок не запустился: {user_message(exc.__cause__ or exc)}\n\n"
			# фактический путь возвращает setup_logging — литерал разъезжался
			f"Подробности — в {log_file}.",
		)
	except Exception:  # noqa: BLE001 — диалог вспомогательный, лог уже записан
		logger.debug("Диалог ошибки запуска показать не удалось.", exc_info=True)


def _run_qt(worker: EngineWorker) -> int:
	"""Создаёт Qt-приложение, показывает окно и крутит цикл событий."""
	from PySide6.QtWidgets import QApplication  # ленивый импорт интерфейса

	from pxcontrol.engine.services.settings import (
		THEME_DARK,
		UI_COMPACT_SPACING,
		UI_CONTROL_HEIGHT,
		UI_FONT_SIZE,
	)
	from pxcontrol.ui import density
	from pxcontrol.ui.main_window import MainWindow
	from pxcontrol.ui.theme import apply_theme

	async def read_appearance() -> tuple[bool, bool, int, int]:
		"""Тема и плотность одним заходом в цикл движка."""
		settings = worker.engine.settings
		return (
			await settings.get(THEME_DARK),
			await settings.get(UI_COMPACT_SPACING),
			await settings.get(UI_CONTROL_HEIGHT),
			await settings.get(UI_FONT_SIZE),
		)

	app = QApplication.instance() or QApplication([])
	# сохранённое оформление — до создания окна (движок уже готов,
	# ожидание — мс): тема красит виджеты на лету, а плотность (отступы,
	# высота полей, шрифт) применима только до их создания. Сбой чтения
	# не валит запуск — откат к умолчаниям ключей.
	try:
		dark, compact, control_height, font_size = worker.submit(read_appearance()).result(
			timeout=_APPEARANCE_READ_TIMEOUT_S
		)
	except Exception:  # noqa: BLE001 — оформление не стоит отказа в запуске
		logger.warning("Не удалось прочитать оформление — использую умолчания.", exc_info=True)
		dark = THEME_DARK.default
		compact = UI_COMPACT_SPACING.default
		control_height = UI_CONTROL_HEIGHT.default
		font_size = UI_FONT_SIZE.default
	apply_theme(dark=dark)
	density.init(compact, control_height, font_size)
	try:
		density.apply_widget_metrics()
	except Exception:  # noqa: BLE001 — оформление не стоит отказа в запуске
		# метод правит внутренности QFluentWidgets: несовместимое обновление
		# библиотеки не должно ронять приложение — остаются штатные размеры
		logger.warning("Не удалось применить плотность интерфейса.", exc_info=True)
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
	try:
		worker.start()
	except Exception:
		logger.critical("Движок не запустился.", exc_info=True)
		raise
	try:
		if seconds:
			time.sleep(seconds)
	finally:
		worker.stop()
