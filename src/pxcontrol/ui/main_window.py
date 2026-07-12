"""Главное окно: боковая навигация + разделы (FluentWindow, ADR-0008)."""

from __future__ import annotations

from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition

from pxcontrol.engine import EngineWorker
from pxcontrol.ui.pages.accounts import AccountsPage
from pxcontrol.ui.pages.base import PlaceholderPage
from pxcontrol.ui.pages.channels import ChannelsPage
from pxcontrol.ui.pages.schedule import SchedulePage
from pxcontrol.ui.pages.settings import SettingsPage


class MainWindow(FluentWindow):
	"""Окно с боковой навигацией. К движку обращается через `EngineWorker`."""

	def __init__(self, worker: EngineWorker) -> None:
		super().__init__()
		self._worker = worker
		self.setWindowTitle("pXcontrol")
		self.resize(1000, 640)
		self._build_navigation()

	def _build_navigation(self) -> None:
		"""Наполняет боковую навигацию разделами приложения."""
		self.addSubInterface(ChannelsPage(self._worker, self), FluentIcon.HOME, "Каналы")
		self.addSubInterface(
			SchedulePage(self._worker, self), FluentIcon.CALENDAR, "Расписание"
		)
		self.addSubInterface(
			PlaceholderPage(
				"queue", "Очередь модерации",
				"Здесь будут заготовки контента, ожидающие одобрения.", self,
			),
			FluentIcon.CHECKBOX, "Очередь",
		)
		self.addSubInterface(
			PlaceholderPage(
				"sources", "Источники",
				"Здесь подключаются каналы, сайты и RSS-ленты.", self,
			),
			FluentIcon.GLOBE, "Источники",
		)
		self.addSubInterface(
			PlaceholderPage(
				"generation", "Генерация",
				"Здесь нейросеть будет предлагать тексты для постов.", self,
			),
			FluentIcon.ROBOT, "Генерация",
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
