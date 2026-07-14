"""Главное окно: боковая навигация + разделы (FluentWindow, ADR-0008)."""

from __future__ import annotations

from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import MediaKind
from pxcontrol.ui.pages.accounts import AccountsPage
from pxcontrol.ui.pages.channels import ChannelsPage
from pxcontrol.ui.pages.publish import PublishPage
from pxcontrol.ui.pages.schedule import SchedulePage
from pxcontrol.ui.pages.settings import SettingsPage
from pxcontrol.ui.pages.video import VideoPage


class MainWindow(FluentWindow):
	"""Окно с боковой навигацией. К движку обращается через `EngineWorker`."""

	def __init__(self, worker: EngineWorker) -> None:
		super().__init__()
		self._worker = worker
		self.setWindowTitle("pXcontrol")
		# ширина — под форму параметров видео (самая широкая страница)
		self.resize(1160, 800)
		self.setMinimumSize(1000, 640)
		self._build_navigation()

	def _build_navigation(self) -> None:
		"""Наполняет боковую навигацию разделами приложения."""
		self.addSubInterface(ChannelsPage(self._worker, self), FluentIcon.HOME, "Каналы")
		video_page = VideoPage(self._worker, self)
		self.addSubInterface(video_page, FluentIcon.VIDEO, "Видео")
		self._publish_page = PublishPage(self._worker, self)
		self.addSubInterface(self._publish_page, FluentIcon.SEND, "Публикация")
		video_page.publish_requested.connect(self._open_publish_with_video)
		self.addSubInterface(
			SchedulePage(self._worker, self), FluentIcon.CALENDAR, "Расписание"
		)
		settings_page = SettingsPage(self)
		self.addSubInterface(
			settings_page, FluentIcon.SETTING, "Настройки",
			NavigationItemPosition.BOTTOM,
		)
		# «Аккаунты» — дочерний пункт «Настроек» (дерево навигации).
		self.addSubInterface(
			AccountsPage(self._worker, self), FluentIcon.PEOPLE, "Аккаунты",
			NavigationItemPosition.BOTTOM, parent=settings_page,
		)

	def _open_publish_with_video(self, path: str) -> None:
		"""Переходит на «Публикацию» с предзаполненным видеофайлом."""
		self._publish_page.prefill_media(MediaKind.VIDEO, path)
		self.switchTo(self._publish_page)
