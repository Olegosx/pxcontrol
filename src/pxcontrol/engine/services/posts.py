"""Сервис работы с постами и публикацией (каркас)."""

from __future__ import annotations

import logging

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.scheduler.scheduler import Scheduler
from pxcontrol.engine.telegram.gateway import TelegramGateway

logger = logging.getLogger(__name__)


class PostsService:
	"""Создание постов, постановка в расписание и публикация."""

	def __init__(self, db: Database, gateway: TelegramGateway, scheduler: Scheduler) -> None:
		self._db = db
		self._gateway = gateway
		self._scheduler = scheduler

	async def publish_due(self, catch_up: bool = False) -> int:
		"""Публикует посты, чьё время публикации наступило.

		Args:
			catch_up: Если ``True``, догоняет публикации, пропущенные за время
				простоя приложения (вызывается при запуске движка).

		Returns:
			Количество опубликованных постов.
		"""
		if catch_up:
			logger.info("Догон пропущенных публикаций — заглушка каркаса")
		return 0
