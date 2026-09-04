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
from pxcontrol.engine.services.video_queue import ProcessingQueue
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
		self.posts = PostsService(self.db, self.gateway, self._ffmpeg_path, self.settings)
		self.publish_queue = PublishQueue(self.posts, self.db, self.settings)
		self.video = VideoService(
			self.db,
			self._ffmpeg_path,
			self.settings,
			# эвристика без контекста канала (очередь обработки канала
			# не знает): Premium хоть одного подключённого аккаунта; строгий
			# пер-канальный лимит остаётся за публикацией (ADR-0019)
			userbot_premium=self.gateway.any_userbot_premium,
		)
		self.video_queue = ProcessingQueue(self.video)
		self.captions = CaptionsService(self.db, self._ffmpeg_path)

	async def delete_channel(self, channel_id: int) -> None:
		"""Удаляет канал вместе с его элементами в очереди отправки.

		Порядок: сначала очередь (ожидающие снимаются с возвратом файлов
		в результаты, активная отправка обрывается), затем строка канала —
		каскад БД подчищает настройки и остатки строк очереди. Связка
		живёт здесь, чтобы ``ChannelsService`` не зависел от очереди.
		"""
		await self.publish_queue.drop_channel(channel_id)
		await self.channels.delete_channel(channel_id)

	def _ffmpeg_path(self) -> str:
		"""Действующий путь к ffmpeg: настройка из БД или бутстрап .env."""
		return self.settings.cached(FFMPEG_PATH) or self._settings.ffmpeg_path

	async def start(self) -> None:
		"""Запускает компоненты в правильном порядке.

		Userbot-аккаунты активируются по сохранённым сессиям (все,
		у кого они есть, — ADR-0019): отложенные посты публикует сервер
		Telegram (ADR-0010), но для их создания и чтения нужен
		подключённый userbot канала. Неудача подключения не мешает
		запуску: активация сама ловит недоступность каждого аккаунта
		(нет сети, сессия отозвана), а повторного подключения через шлюз
		здесь нет — иначе то же исключение улетело бы наружу и уронило
		приложение.
		"""
		logger.info("Запуск движка…")
		await self.db.init()
		await self.settings.prime()
		await self.accounts.activate_stored_userbots()
		# после userbot: восстановленной очереди (ADR-0016) сразу нужна
		# проверка слотов, а она читает отложки канала через userbot
		await self.publish_queue.load()
		logger.info("Движок запущен.")

	async def stop(self) -> None:
		"""Останавливает компоненты в обратном порядке."""
		logger.info("Остановка движка…")
		await self.publish_queue.shutdown()
		await self.video_queue.shutdown()
		await self.video.shutdown()
		await self.gateway.stop()
		await self.db.close()
		logger.info("Движок остановлен.")
