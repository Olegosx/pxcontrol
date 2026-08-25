"""Главное окно: боковая навигация + разделы (FluentWindow, ADR-0008)."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QCloseEvent
from qfluentwidgets import FluentIcon, FluentWindow, MessageBox, NavigationItemPosition

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.settings import WINDOW_GEOMETRY
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.pages.channels import ChannelsPage
from pxcontrol.ui.pages.common import exec_dialog
from pxcontrol.ui.pages.publish import PublishPage
from pxcontrol.ui.pages.schedule import SchedulePage
from pxcontrol.ui.pages.settings import SettingsPage
from pxcontrol.ui.pages.video import VideoPage

logger = logging.getLogger(__name__)


class MainWindow(FluentWindow):
	"""Окно с боковой навигацией. К движку обращается через `EngineWorker`."""

	def __init__(self, worker: EngineWorker) -> None:
		super().__init__()
		self._worker = worker
		self.setWindowTitle("pXcontrol")
		# ширина — под форму параметров видео (самая широкая страница)
		self.resize(1160, 800)
		self.setMinimumSize(1000, 640)
		self._restore_geometry()
		self._build_navigation()

	def _restore_geometry(self) -> None:
		"""Восстанавливает сохранённое состояние окна (движок уже готов).

		Симметрично ``_save_geometry``: сбой чтения (зависший движок,
		таймаут) не должен валить запуск — окно откроется с умолчаниями.
		"""
		try:
			saved = self._worker.submit(self._worker.engine.settings.get(WINDOW_GEOMETRY)).result(
				timeout=5
			)
			# применение — тоже под защитой: битое значение из БД (не-ASCII,
			# мусор вместо base64) не должно валить создание окна
			if saved:
				self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))
		except Exception:  # noqa: BLE001 — геометрия не стоит отказа в запуске
			logger.warning("Не удалось прочитать состояние окна.", exc_info=True)

	def _build_navigation(self) -> None:
		"""Наполняет боковую навигацию разделами приложения."""
		self.addSubInterface(ChannelsPage(self._worker, self), FluentIcon.HOME, "Каналы")
		self._video_page = VideoPage(self._worker, self)
		self.addSubInterface(self._video_page, FluentIcon.VIDEO, "Видео")
		self._publish_page = PublishPage(self._worker, self)
		self.addSubInterface(self._publish_page, FluentIcon.SEND, "Публикация")
		self._video_page.publish_requested.connect(self._open_publish_with_video)
		self._video_page.publish_files_requested.connect(self._open_publish_batch_files)
		self._video_page.publish_folder_requested.connect(self._open_publish_batch_folder)
		self.addSubInterface(SchedulePage(self._worker, self), FluentIcon.CALENDAR, "Расписание")
		# категории настроек (Общие, Аккаунты) — внутри самой страницы
		self.addSubInterface(
			SettingsPage(self._worker, self),
			FluentIcon.SETTING,
			"Настройки",
			NavigationItemPosition.BOTTOM,
		)

	def _open_publish_with_video(self, path: str, channel_id: int) -> None:
		"""Переходит на «Публикацию» с видеофайлом и каналом со страницы «Видео»."""
		self._publish_page.prefill_media(MediaKind.VIDEO, path, channel_id=channel_id or None)
		self.switchTo(self._publish_page)

	def _open_publish_batch_files(self, paths: list[str], channel_id: int) -> None:
		"""Пакет из готовых видео, выбранных на «Видео» (ADR-0015)."""
		self.switchTo(self._publish_page)
		self._publish_page.start_batch_with_files(list(paths), channel_id)

	def _open_publish_batch_folder(self, root: str, channel_id: int) -> None:
		"""Пакет из папки готовых видео, выбранной на «Видео» (ADR-0015)."""
		self.switchTo(self._publish_page)
		self._publish_page.start_batch_with_folder(root, channel_id)

	def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — API Qt
		"""Подтверждает выход, если очереди отправки или обработки не пусты.

		Обе очереди живут в памяти (ADR-0012, ADR-0014): при выходе
		неотправленные посты и необработанные видео пропадут из очередей —
		без вопроса терять их нельзя (готовые файлы при этом остаются
		на диске: результат обработки пишется атомарно).
		"""
		reasons = []
		if self._publish_page.queue_busy():
			reasons.append("неотправленные посты")
		if self._video_page.queue_busy():
			reasons.append("необработанные видео")
		if reasons:
			box = MessageBox(
				"Работа не завершена",
				f"В очередях остались {' и '.join(reasons)} — при выходе они пропадут. Выйти?",
				self,
			)
			box.yesButton.setText("Выйти")
			box.cancelButton.setText("Остаться")
			if not exec_dialog(box):
				event.ignore()
				return
		self._save_geometry()
		super().closeEvent(event)

	def _save_geometry(self) -> None:
		"""Сохраняет состояние окна (движок ещё жив: он гасится после Qt)."""
		data = bytes(self.saveGeometry().toBase64()).decode("ascii")
		try:
			self._worker.submit(self._worker.engine.settings.set(WINDOW_GEOMETRY, data)).result(
				timeout=5
			)
		except Exception:  # noqa: BLE001 — потеря геометрии не мешает выходу
			logger.warning("Не удалось сохранить состояние окна.", exc_info=True)
