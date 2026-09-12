"""Главное окно: боковая навигация + разделы (FluentWindow, ADR-0008)."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QCloseEvent
from qfluentwidgets import FluentIcon, FluentWindow, MessageBox, NavigationItemPosition

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.settings import WINDOW_GEOMETRY
from pxcontrol.engine.telegram.types import CommunityKind, MediaKind
from pxcontrol.ui.pages.common import exec_dialog
from pxcontrol.ui.pages.communities import CommunitiesPage
from pxcontrol.ui.pages.community_page import CommunityPage
from pxcontrol.ui.pages.publish import PublishPage
from pxcontrol.ui.pages.schedule import SchedulePage
from pxcontrol.ui.pages.settings import SettingsPage
from pxcontrol.ui.pages.video import VideoPage

logger = logging.getLogger(__name__)

#: Предел синхронного чтения/записи настроек окна из цикла движка:
#: геометрия нужна до показа и при выходе, штатно это миллисекунды —
#: предел лишь страхует от зависшего цикла.
_SETTINGS_SYNC_TIMEOUT_S = 5


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
				timeout=_SETTINGS_SYNC_TIMEOUT_S
			)
			# применение — тоже под защитой: битое значение из БД (не-ASCII,
			# мусор вместо base64) не должно валить создание окна
			if saved:
				self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))
		except Exception:  # noqa: BLE001 — геометрия не стоит отказа в запуске
			logger.warning("Не удалось прочитать состояние окна.", exc_info=True)

	def _build_navigation(self) -> None:
		"""Наполняет боковую навигацию разделами приложения."""
		self._communities_page = CommunitiesPage(self._worker, self)
		self.addSubInterface(self._communities_page, FluentIcon.HOME, "Каналы и группы")
		# подменю сообществ — живое: дашборд после каждой загрузки шлёт
		# свежий список, окно приводит пункты и страницы в соответствие
		self._community_pages: dict[int, CommunityPage] = {}
		self._communities_page.communities_changed.connect(self._sync_community_nav)
		self._communities_page.open_community.connect(self._open_community)
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

	def _sync_community_nav(self, communities: list[CommunityDto]) -> None:
		"""Приводит подменю сообществ к свежему списку из дашборда.

		Удалённые сообщества снимаются вместе со страницами (активная —
		с возвратом на дашборд), новые добавляются пунктами подменю,
		у существующих обновляются снимок страницы и подпись пункта
		(название могло смениться перепроверкой доступов).
		"""
		fresh = {community.id: community for community in communities}
		for community_id in list(self._community_pages):
			if community_id in fresh:
				continue
			page = self._community_pages.pop(community_id)
			if self.stackedWidget.currentWidget() is page:
				self.switchTo(self._communities_page)
			self.navigationInterface.removeWidget(page.objectName())
			self.stackedWidget.removeWidget(page)
			page.deleteLater()
		for community in communities:
			existing = self._community_pages.get(community.id)
			if existing is None:
				page = CommunityPage(self._worker, community, self)
				page.changed.connect(self._communities_page.reload)
				self._community_pages[community.id] = page
				icon = (
					FluentIcon.CHAT
					if community.kind is CommunityKind.CHANNEL
					else FluentIcon.PEOPLE
				)
				self.addSubInterface(page, icon, community.title, parent=self._communities_page)
			else:
				existing.update_community(community)
				item = self.navigationInterface.widget(existing.objectName())
				if item is not None:
					item.setText(community.title)

	def _open_community(self, community_id: int) -> None:
		"""Клик по карточке дашборда — переход на страницу сообщества."""
		page = self._community_pages.get(community_id)
		if page is not None:
			self.switchTo(page)

	def _open_publish_with_video(self, path: str, community_id: int) -> None:
		"""Переходит на «Публикацию» с видеофайлом и каналом со страницы «Видео»."""
		self._publish_page.prefill_media(MediaKind.VIDEO, path, community_id=community_id or None)
		self.switchTo(self._publish_page)

	def _open_publish_batch_files(self, paths: list[str], community_id: int) -> None:
		"""Пакет из готовых видео, выбранных на «Видео» (ADR-0015)."""
		self.switchTo(self._publish_page)
		self._publish_page.start_batch_with_files(list(paths), community_id)

	def _open_publish_batch_folder(self, root: str, community_id: int) -> None:
		"""Пакет из папки готовых видео, выбранной на «Видео» (ADR-0015)."""
		self.switchTo(self._publish_page)
		self._publish_page.start_batch_with_folder(root, community_id)

	def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — API Qt
		"""Подтверждает выход при активной отправке или непустой обработке.

		Очередь отправки персистентная (ADR-0016): ожидающие посты выхода
		не задерживают — они сохранятся и продолжатся при следующем
		запуске. Вопрос заслуживают обрыв идущей загрузки (пост уйдёт
		заново при следующем запуске) и очередь обработки видео — она
		живёт в памяти (ADR-0014) и при выходе пропадает (готовые файлы
		остаются на диске: результат пишется атомарно).
		"""
		reasons = []
		if self._publish_page.upload_active():
			reasons.append(
				"идёт отправка поста — загрузка оборвётся (пост уйдёт при следующем запуске)"
			)
		if self._video_page.queue_busy():
			reasons.append("в очереди обработки остались видео — при выходе они из неё пропадут")
		if reasons:
			text = "\n".join(f"— {reason};" for reason in reasons)
			box = MessageBox("Работа не завершена", f"{text}\n\nВыйти?", self)
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
				timeout=_SETTINGS_SYNC_TIMEOUT_S
			)
		except Exception:  # noqa: BLE001 — потеря геометрии не мешает выходу
			logger.warning("Не удалось сохранить состояние окна.", exc_info=True)
