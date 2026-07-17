"""Ядро движка: оркестрация компонентов и порядок запуска/остановки."""

from __future__ import annotations

import logging

from pxcontrol.config import Settings
from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.captions import CaptionsService
from pxcontrol.engine.services.channels import ChannelsService
from pxcontrol.engine.services.posts import PostsService
from pxcontrol.engine.services.publish_queue import PublishQueue
from pxcontrol.engine.services.settings import FFMPEG_PATH, SettingsService
from pxcontrol.engine.services.video import VideoService
from pxcontrol.engine.telegram.gateway import TelegramGateway

logger = logging.getLogger(__name__)


class Engine:
	"""Собирает компоненты движка и управляет их жизненным циклом.

	Движок не зависит от интерфейса и может работать без него (например,
	в тестах). Асинхронные методы выполняются в цикле событий, который
	заводит :class:`EngineWorker`.
	"""

	def __init__(self, settings: Settings) -> None:
		self._settings = settings
		self.db = Database(settings.database_url)
		self.settings = SettingsService(self.db)
		self.gateway = TelegramGateway()
		self.accounts = AccountsService(self.db, self.gateway)
		self.channels = ChannelsService(self.db, self.gateway, self.settings)
		# путь к ffmpeg — провайдером: настройка из БД (правится в UI),
		# пусто — бутстрап из .env; смена подхватывается без перезапуска
		self.posts = PostsService(
			self.db, self.gateway, self._ffmpeg_path, self.settings
		)
		self.publish_queue = PublishQueue(self.posts)
		self.video = VideoService(self.db, self._ffmpeg_path, self.settings)
		self.captions = CaptionsService(self.db, self._ffmpeg_path)

	def _ffmpeg_path(self) -> str:
		"""Действующий путь к ffmpeg: настройка из БД или бутстрап .env."""
		return self.settings.cached(FFMPEG_PATH) or self._settings.ffmpeg_path

	async def start(self) -> None:
		"""Запускает компоненты в правильном порядке.

		Userbot активируется по сохранённой сессии: отложенные посты
		публикует сервер Telegram (ADR-0010), но для их создания и чтения
		нужен подключённый userbot. Неудача подключения не мешает запуску.
		"""
		logger.info("Запуск движка…")
		await self.db.init()
		await self.settings.prime()
		await self.accounts.activate_stored_userbot()
		await self.gateway.start()
		logger.info("Движок запущен.")

	async def stop(self) -> None:
		"""Останавливает компоненты в обратном порядке."""
		logger.info("Остановка движка…")
		await self.publish_queue.shutdown()
		await self.video.shutdown()
		await self.gateway.stop()
		await self.db.close()
		logger.info("Движок остановлен.")

