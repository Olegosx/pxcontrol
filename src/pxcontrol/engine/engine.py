"""Ядро движка: оркестрация компонентов и порядок запуска/остановки."""

from __future__ import annotations

import logging

from pxcontrol.config import Settings
from pxcontrol.engine.db.database import Database
from pxcontrol.engine.scheduler.scheduler import Scheduler
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.channels import ChannelsService
from pxcontrol.engine.services.posts import PostsService
from pxcontrol.engine.telegram.gateway import TelegramGateway
from pxcontrol.engine.video.processor import VideoProcessor

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
		self.gateway = TelegramGateway(settings)
		self.scheduler = Scheduler(timezone=settings.tz)
		self.video = VideoProcessor(settings.ffmpeg_path)
		self.accounts = AccountsService(self.db, self.gateway)
		self.channels = ChannelsService(self.db, self.gateway)
		self.posts = PostsService(self.db, self.gateway)

	async def start(self) -> None:
		"""Запускает компоненты в правильном порядке."""
		logger.info("Запуск движка…")
		await self.db.init()
		await self._activate_userbot_if_logged_in()
		await self.gateway.start()
		self.scheduler.start()
		logger.info("Движок запущен.")

	async def _activate_userbot_if_logged_in(self) -> None:
		"""Подключает userbot, если в БД есть аккаунт с сессией.

		Отложенные посты публикует сервер Telegram (ADR-0010), но для их
		создания и чтения нужен подключённый userbot.
		"""
		from sqlalchemy import select

		from pxcontrol.engine.db.models import TgAccount

		async with self.db.session_factory() as session:
			account = (
				(await session.execute(
					select(TgAccount)
					.where(TgAccount.session.is_not(None))
					.order_by(TgAccount.id)
				)).scalars().first()
			)
		if account is None or account.session is None:
			return
		self.gateway.mtproto.configure(
			account.api_id, account.api_hash, account.session
		)
		logger.info("Userbot «%s» будет подключён при старте шлюза.", account.label)

	async def stop(self) -> None:
		"""Останавливает компоненты в обратном порядке."""
		logger.info("Остановка движка…")
		self.scheduler.shutdown()
		await self.gateway.stop()
		await self.db.close()
		logger.info("Движок остановлен.")
