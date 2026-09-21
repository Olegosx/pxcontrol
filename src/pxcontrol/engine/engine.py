"""Ядро движка: оркестрация компонентов и порядок запуска/остановки."""

from __future__ import annotations

import logging
from datetime import datetime

from pxcontrol.config import Settings
from pxcontrol.engine.db.database import Database
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.activity import ActivityService
from pxcontrol.engine.services.captions import CaptionsService
from pxcontrol.engine.services.communities import CommunitiesService
from pxcontrol.engine.services.community_stats import CommunityStatsService
from pxcontrol.engine.services.maintenance import MaintenanceService
from pxcontrol.engine.services.markups import MarkupsService
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
		# учёт активности (ADR-0030): забирает записи операций из буфера
		# шлюза и пишет в БД пачкой; живое состояние читает из дорожек
		self.activity = ActivityService(self.db, self.gateway)
		self.accounts = AccountsService(self.db, self.gateway)
		# зонды прав попутно актуализируют профиль аккаунта (имя, @имя):
		# связка через крючок — сервис сообществ не зависит от сервиса
		# аккаунтов напрямую
		self.communities = CommunitiesService(
			self.db, self.gateway, self.settings, profile_sync=self.accounts.sync_profile
		)
		self.community_stats = CommunityStatsService(self.db, self.gateway, self.settings)
		# обслуживание сообществ (ADR-0026): чистка служебных записей;
		# итог прохода по удалённым аккаунтам уходит в кэш статистики
		# крючком — очередь обслуживания о кэше не знает (ADR-0027)
		self.maintenance = MaintenanceService(
			self.gateway,
			self.communities,
			on_members_report=self.community_stats.record_members_report,
		)
		# обещанные клавиатуры (ADR-0031): хранилище кнопок, которые ещё
		# нельзя применить, и дозор, который ставит их после выхода поста
		self.markups = MarkupsService(self.db, self.gateway)
		# путь к ffmpeg — провайдером: настройка из БД (правится в UI),
		# пусто — бутстрап из .env; смена подхватывается без перезапуска
		# крючки ведут обещанные кнопки за судьбой отложенной записи
		# (ADR-0031): правка уводит обещание за собой, удаление снимает
		self.posts = PostsService(
			self.db,
			self.gateway,
			self._ffmpeg_path,
			self.settings,
			markup_moved=self._markup_moved,
			markup_gone=self.markups.drop_scheduled_quiet,
			promised_markups=self.markups.promised_ids,
			# экран «Опубликовано» показывает, дождался ли вышедший пост
			# своих кнопок: состояние обещания знает только приложение
			post_promises=self.markups.post_promises,
			# человек поставил, снял кнопки или удалил пост — обещание
			# дозору больше не нужно (ADR-0032, подача A4)
			markup_settled=self.markups.drop_post,
			# снимок прав бота стареет: право «изменять сообщения» человек
			# выдаёт в Telegram, и отказывать по памяти нечестно — перед
			# отказом посты просят перепроверить доступы живым зондом
			refresh_rights=self._refresh_rights,
		)
		# очереди нужно хранилище обещаний: пост может выйти, а кнопки
		# не поставиться — тогда обещание ждёт повтора (ADR-0031)
		self.publish_queue = PublishQueue(self.posts, self.db, self.settings, self.markups)
		self.video = VideoService(
			self.db,
			self._ffmpeg_path,
			self.settings,
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

	async def _refresh_rights(self, community_id: int) -> None:
		"""Перепроверяет доступы сообщества (крючок сервиса постов).

		Связка живёт здесь, а не внутри постов: сервис постов не должен
		знать про сервис сообществ — движок и так собирает такие связи
		(как у обещанных кнопок).
		"""
		await self.communities.recheck_community(community_id)

	async def delete_community(self, community_id: int) -> None:
		"""Удаляет сообщество вместе с его работой в очередях.

		Порядок: сначала очереди (ожидающая отправка снимается с возвратом
		файлов в результаты, активная обрывается; задания обслуживания
		снимаются — иначе уборка продолжала бы удалять записи и исключать
		участников в Telegram для сущности, которой в приложении уже нет),
		затем строка сообщества — каскад БД подчищает настройки и остатки
		строк очереди. Связка живёт здесь, чтобы ``CommunitiesService``
		не зависел от очередей.
		"""
		await self.publish_queue.drop_community(community_id)
		await self.maintenance.drop_community(community_id)
		# файл аватара каскад БД не видит — убирается движком до строки
		await self.community_stats.drop(community_id)
		await self.communities.delete_community(community_id)

	async def _markup_moved(
		self, community_id: int, message_id: int, when: datetime, text: str | None
	) -> None:
		"""Ведёт обещанные кнопки за правкой отложенной записи (ADR-0031)."""
		await self.markups.retarget(community_id, message_id, when=when, match_text=text)

	def _ffmpeg_path(self) -> str:
		"""Действующий путь к ffmpeg: настройка из БД или бутстрап .env."""
		return self.settings.cached(FFMPEG_PATH) or self._settings.ffmpeg_path

	async def start(self) -> None:
		"""Запускает компоненты в правильном порядке.

		Userbot-аккаунты активируются по сохранённым сессиям (все,
		у кого они есть, — ADR-0019): отложенные посты публикует сервер
		Telegram (ADR-0010), но для их создания и чтения нужен
		подключённый userbot сообщества. Неудача подключения не мешает
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
		# проверка слотов, а она читает отложки сообщества через userbot
		await self.publish_queue.load()
		# после загрузки очереди: файлы живых элементов уже на местах,
		# пустые папки её дерева — остатки отработанных пакетов
		await self.posts.sweep_queue_dirs()
		# периодический опрос статистики (ADR-0027) — последним: его
		# первый проход пойдёт по дорожкам, где уже стоит очередь отправки
		self.community_stats.start_polling()
		# сброс активности — периодическая задача; буфер шлюза уже полнится
		self.activity.start()
		# дозор кнопок (ADR-0031): ставит обещанное отложенным постам
		# после их выхода; идёт по дорожкам с фоновым приоритетом
		self.markups.start_polling()
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
			self.community_stats.shutdown,
			self.markups.shutdown,
			self.maintenance.shutdown,
			self.publish_queue.shutdown,
			self.video_queue.shutdown,
			self.video.shutdown,
			# после очередей (их последние операции уже в буфере) и до шлюза
			# и БД: последний сброс активности пишет в ещё открытую базу
			self.activity.shutdown,
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
