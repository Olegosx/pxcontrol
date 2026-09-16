"""Раздел «Публикация»: экраны по стадиям жизни поста (ADR-0032).

Путь поста длиннее одной формы: его создают, он ждёт отправки
в приложении, потом ждёт своего часа на сервере Telegram и наконец
выходит в ленту. Раньше эти стадии были раскиданы по разделам
(«Публикация» и «Расписание»), а вышедшего поста приложение не видело
вовсе. ADR-0032 собрал их в один раздел с живым подменю, где пункты
идут в порядке самого пути.

Словарь самих стадий (порядок, подписи, подсказки, значки) живёт
рядом — в :mod:`publish_stages`, общая рамка экрана — в :mod:`stage_page`.
Здесь — тонкие экраны над готовыми панелями показа и **раздел**
(:class:`PublishSection`): владелец экранов и точка переходов. Дашборд
сообществ, страница сообщества, «Видео» и форма поста просят показать
стадию с нужным фильтром, а устройство раздела не знают.

Стадий пять: форма поста, пакет, наша очередь, отложки на сервере
Telegram и лента вышедших постов.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QWidget

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.pages.publish import PublishPage
from pxcontrol.ui.pages.publish_batch_page import BatchStagePage
from pxcontrol.ui.pages.publish_queue_view import QueueFilter, QueueView
from pxcontrol.ui.pages.publish_stages import PublishStage
from pxcontrol.ui.pages.published_view import PublishedView
from pxcontrol.ui.pages.scheduled_view import ScheduledView
from pxcontrol.ui.pages.stage_page import StagePage


class QueueStagePage(StagePage):
	"""Экран «Очередь»: вся очередь отправки приложения (ADR-0016)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(PublishStage.QUEUE, parent)
		self._view = QueueView(worker, self)
		self.mount(self._view)

	def set_active(self, active: bool) -> None:
		"""Опрос очереди идёт, только пока экран виден."""
		self._view.set_polling(active)

	def show_filter(self, community_id: int | None, status: QueueFilter | None = None) -> None:
		"""Ставит правило показа извне: сообщество и/или статус."""
		self._view.show_filter(community_id, status)


class ScheduledStagePage(StagePage):
	"""Экран «Отложено»: записи, которые уже принял сервер Telegram."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(PublishStage.SCHEDULED, parent)
		self._view = ScheduledView(worker, self)
		self.mount(self._view)

	def set_active(self, active: bool) -> None:
		"""Показ экрана перечитывает список, если он не свежий."""
		if active:
			self._view.activate()

	def show_community(self, community_id: int | None) -> None:
		"""Ставит фильтр по сообществу (переход с дашборда)."""
		self._view.show_community(community_id)


class PublishedStagePage(StagePage):
	"""Экран «Опубликовано»: лента вышедших постов сообщества (ADR-0032)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(PublishStage.PUBLISHED, parent)
		self._view = PublishedView(worker, self)
		self.mount(self._view)

	def set_active(self, active: bool) -> None:
		"""Показ экрана обновляет список сообществ и несвежую ленту."""
		if active:
			self._view.activate()


class PublishSection:
	"""Раздел «Публикация»: страницы стадий и переходы между ними.

	Владеет страницами и знает их порядок — порядок пути поста. Главное
	окно регистрирует страницы в навигации и переводит на раздел сигналы
	других экранов; сам переход делает раздел: ставит фильтр и просит
	окно показать нужную страницу. Так устройство раздела (сколько
	в нём стадий и какая за что отвечает) не растекается по окну.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		parent: QWidget,
		switch: Callable[[QWidget], None],
	) -> None:
		"""Args:
		worker: мост к движку.
		parent: главное окно — родитель страниц раздела.
		switch: показать страницу (``FluentWindow.switchTo``).
		"""
		self._switch = switch
		self.new_post = PublishPage(worker, parent)
		self.batch = BatchStagePage(worker, parent)
		self.queue = QueueStagePage(worker, parent)
		self.scheduled = ScheduledStagePage(worker, parent)
		self.published = PublishedStagePage(worker, parent)
		self._pages: dict[PublishStage, QWidget] = {
			PublishStage.NEW_POST: self.new_post,
			PublishStage.BATCH: self.batch,
			PublishStage.QUEUE: self.queue,
			PublishStage.SCHEDULED: self.scheduled,
			PublishStage.PUBLISHED: self.published,
		}
		# «Вся очередь…» на форме поста — соседний пункт раздела
		self.new_post.queue_requested.connect(lambda: self.show_queue(None))

	def pages(self) -> list[tuple[PublishStage, QWidget]]:
		"""Страницы стадий в порядке пути поста — для сборки подменю."""
		return [(stage, self._pages[stage]) for stage in PublishStage]

	# --- переходы ------------------------------------------------------------

	def show_new_post(self, community_id: int | None = None) -> None:
		"""Форма нового поста; ``community_id`` — предвыбор сообщества."""
		self._switch(self.new_post)
		if community_id is not None:
			self.new_post.select_community(community_id)

	def show_queue(
		self, community_id: int | None = None, status: QueueFilter | None = None
	) -> None:
		"""Очередь отправки с фильтром по сообществу и/или статусу."""
		self._switch(self.queue)
		self.queue.show_filter(community_id, status)

	def show_queue_errors(self) -> None:
		"""Очередь с фильтром «ошибки» (плашка сводки дашборда)."""
		self.show_queue(None, QueueFilter.ERRORS)

	def show_scheduled(self, community_id: int | None = None) -> None:
		"""Отложенные записи с фильтром по сообществу."""
		self._switch(self.scheduled)
		self.scheduled.show_community(community_id)

	# --- входы с других страниц ------------------------------------------------

	def show_with_media(self, kind: MediaKind, path: str, community_id: int) -> None:
		"""Форма с готовым вложением (переход со страницы «Видео»)."""
		self.new_post.prefill_media(kind, path, community_id=community_id or None)
		self._switch(self.new_post)

	def show_batch_files(self, paths: list[str], community_id: int) -> None:
		"""Пакет из выбранных на «Видео» файлов (ADR-0015)."""
		self._switch(self.batch)
		self.batch.start_with_files(list(paths), community_id)

	def show_batch_folder(self, root: str, community_id: int) -> None:
		"""Пакет из папки готовых видео, выбранной на «Видео» (ADR-0015)."""
		self._switch(self.batch)
		self.batch.start_with_folder(root, community_id)
