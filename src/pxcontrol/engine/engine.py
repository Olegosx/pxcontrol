"""Ядро движка: оркестрация компонентов и порядок запуска/остановки."""

from __future__ import annotations

import logging

from pxcontrol.config import Settings
from pxcontrol.engine.db.database import Database
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.captions import CaptionsService
from pxcontrol.engine.services.communities import CommunitiesService
from pxcontrol.engine.services.community_stats import CommunityStatsService
from pxcontrol.engine.services.posts import PostsService
from pxcontrol.engine.services.publish_queue import PublishQueue
from pxcontrol.engine.services.settings import (
	FFMPEG_PATH,
	VIDEO_QUEUED_DIR,
	SettingKey,
	SettingsService,
)
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
		# зонды прав попутно актуализируют профиль аккаунта (имя, @имя):
		# связка через крючок — сервис сообществ не зависит от сервиса
		# аккаунтов напрямую
		self.communities = CommunitiesService(
			self.db, self.gateway, self.settings, profile_sync=self.accounts.sync_profile
		)
		self.community_stats = CommunityStatsService(self.db, self.gateway, self.settings)
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

	async def update_video_folders(self, items: list[tuple[SettingKey[str], str]]) -> None:
		"""Сохраняет папки видео, охраняя папку очереди отправки.

		Пока в очереди есть посты (включая ошибки: их файлы тоже живут
		в папке очереди и нужны повтору), менять ``video_queued_dir``
		нельзя — операции жизненного цикла узнают файлы по текущему
		пути, и после смены отмена и отправка молча теряли бы файлы
		в брошенной папке (инвариант зеркала ADR-0016).

		Raises:
			EngineError: Папка очереди меняется при непустой очереди.
		"""
		new_queued = next(
			(value for key, value in items if key.name == VIDEO_QUEUED_DIR.name), None
		)
		if new_queued is not None:
			current = await self.settings.get(VIDEO_QUEUED_DIR)
			live = [i for i in await self.publish_queue.state() if not i.status.left_queue()]
			if new_queued.strip() != (current or "").strip() and live:
				raise EngineError(
					f"Папку очереди отправки нельзя менять: в очереди "
					f"{len(live)} пост(ов), их файлы живут в текущей папке. "
					"Дождитесь отправки или снимите элементы."
				)
		await self.settings.set_many(items)

	async def delete_community(self, community_id: int) -> None:
		"""Удаляет канал вместе с его элементами в очереди отправки.

		Порядок: сначала очередь (ожидающие снимаются с возвратом файлов
		в результаты, активная отправка обрывается), затем строка канала —
		каскад БД подчищает настройки и остатки строк очереди. Связка
		живёт здесь, чтобы ``CommunitiesService`` не зависел от очереди.
		"""
		await self.publish_queue.drop_community(community_id)
		# файл аватара каскад БД не видит — убирается движком до строки
		await self.community_stats.drop(community_id)
		await self.communities.delete_community(community_id)

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
		# после загрузки очереди: файлы живых элементов уже на местах,
		# пустые папки её дерева — остатки отработанных пакетов
		await self.posts.sweep_queue_dirs()
		logger.info("Движок запущен.")

	async def stop(self) -> None:
		"""Останавливает компоненты в обратном порядке.

		Каждый шаг — под собственной защитой: сбой раннего (сеть
		у Telethon, диск у очередей) не должен отменять остальные,
		и прежде всего закрытие БД — его чистота возведена в инвариант
		(ADR-0020). Первая ошибка поднимается после всех шагов.
		"""
		logger.info("Остановка движка…")
		first_error: BaseException | None = None
		steps = (
			self.publish_queue.shutdown,
			self.video_queue.shutdown,
			self.video.shutdown,
			self.gateway.stop,
			self.db.close,
		)
		for step in steps:
			try:
				await step()
			except Exception as exc:  # noqa: BLE001 — остальные шаги важнее
				logger.exception("Шаг остановки %s не удался.", step.__qualname__)
				first_error = first_error or exc
		if first_error is not None:
			raise first_error
		logger.info("Движок остановлен.")
