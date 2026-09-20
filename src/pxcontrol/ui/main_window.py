"""Главное окно: боковая навигация + разделы (FluentWindow, ADR-0008)."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QEvent, QObject, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
	FluentIcon,
	FluentWindow,
	MessageBox,
	NavigationItemPosition,
	NavigationTreeWidget,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.publish_queue import QueueItemDto
from pxcontrol.engine.services.settings import WINDOW_GEOMETRY
from pxcontrol.engine.telegram.types import CommunityKind, ExecutorRef, MediaKind, OwnerKind
from pxcontrol.ui.async_bridge import ask_engine
from pxcontrol.ui.pages.common import exec_dialog, show_info, show_success
from pxcontrol.ui.pages.communities import CommunitiesPage
from pxcontrol.ui.pages.community_page import CommunityPage
from pxcontrol.ui.pages.publish_section import PublishSection
from pxcontrol.ui.pages.publish_stages import (
	SECTION_ICON,
	SECTION_ROUTE_KEY,
	SECTION_TITLE,
	stage_icon,
	stage_title,
)
from pxcontrol.ui.pages.settings import SettingsPage
from pxcontrol.ui.pages.user_page import UserPage, subject_owner
from pxcontrol.ui.pages.users import UsersPage
from pxcontrol.ui.pages.video import VideoPage
from pxcontrol.ui.queue_watcher import QueueView, QueueWatchers

logger = logging.getLogger(__name__)


class MainWindow(FluentWindow):
	"""Окно с боковой навигацией. К движку обращается через `EngineWorker`."""

	#: Панель навигации: её ресайз чинит высоту веток подменю. Поле
	#: объявлено на классе, потому что фильтр событий срабатывает уже
	#: в конструкторе базового окна — до сборки навигации.
	_nav_panel: QWidget | None = None

	def __init__(self, worker: EngineWorker) -> None:
		super().__init__()
		self._worker = worker
		self.setWindowTitle("pXcontrol")
		# ширина — под форму параметров видео (самая широкая страница)
		self.resize(1160, 800)
		self.setMinimumSize(1000, 640)
		self._restore_geometry()
		# наблюдатели очередей — по одному на очередь, при окне (ADR-0034);
		# страницы получают их зрителями, поэтому заводятся до навигации
		self._watchers = QueueWatchers(worker, self)
		self._build_navigation()
		self._watch_publish_queue()

	def _restore_geometry(self) -> None:
		"""Восстанавливает сохранённое состояние окна (движок уже готов).

		Симметрично ``_save_geometry``: сбой чтения (зависший движок,
		таймаут) не должен валить запуск — окно откроется с умолчаниями.
		"""
		saved = ask_engine(
			self._worker,
			self._worker.engine.settings.get(WINDOW_GEOMETRY),
			None,
			what="прочитать состояние окна",
		)
		try:
			# применение — под своей защитой: битое значение из БД
			# (не-ASCII, мусор вместо base64) не должно валить создание окна
			if saved:
				self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))
		except Exception:  # noqa: BLE001 — геометрия не стоит отказа в запуске
			logger.warning("Состояние окна не восстановлено: запись повреждена.", exc_info=True)

	def _build_navigation(self) -> None:
		"""Наполняет боковую навигацию разделами приложения.

		Здесь же чинится высота веток подменю при смене ширины панели —
		см. :meth:`_fix_branch_sizes`.
		"""
		# ширину панели меняют и кнопкой-«бутербродом», и размером окна;
		# сигнала об этом библиотека шлёт не всегда (только при сворачивании),
		# поэтому ловим сам ресайз панели — см. _fix_branch_sizes
		self._nav_panel = self.navigationInterface.panel
		self._nav_panel.installEventFilter(self)
		# пользователи и боты — над сообществами (ADR-0029): без них
		# подключать сообщества нечем, порядок разделов — порядок настройки
		self._users_page = UsersPage(self._worker, self)
		self.addSubInterface(self._users_page, FluentIcon.PEOPLE, "Пользователи и боты")
		# подменю аккаунтов — живое, как у сообществ (ADR-0030)
		self._user_pages: dict[ExecutorRef, UserPage] = {}
		self._users_page.users_changed.connect(self._sync_user_nav)
		self._users_page.open_user.connect(self._open_user)
		self._communities_page = CommunitiesPage(self._worker, self._watchers, self)
		self.addSubInterface(self._communities_page, FluentIcon.HOME, "Каналы и группы")
		# подменю сообществ — живое: дашборд после каждой загрузки шлёт
		# свежий список, окно приводит пункты и страницы в соответствие
		self._community_pages: dict[int, CommunityPage] = {}
		# переход на сообщество, чьей страницы ещё нет (дашборд не показывали):
		# дашборд загрузится, подменю соберётся — и переход довершится
		self._pending_community: int | None = None
		self._communities_page.communities_changed.connect(self._sync_community_nav)
		self._communities_page.open_community.connect(self._open_community)
		self._video_page = VideoPage(self._worker, self._watchers.video, self)
		self.addSubInterface(self._video_page, FluentIcon.VIDEO, "Видео")
		self._build_publish_section()
		# категории настроек (Общие, Аккаунты) — внутри самой страницы
		self.addSubInterface(
			SettingsPage(self._worker, self),
			FluentIcon.SETTING,
			"Настройки",
			NavigationItemPosition.BOTTOM,
		)

	def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 — API Qt
		"""Следит за шириной панели навигации, чтобы чинить ветки подменю.

		Панель меняет размер при разворачивании, сворачивании и смене
		ширины окна; каждый такой переход сбрасывает высоту веток
		(см. :meth:`_fix_branch_sizes`). Событие не перехватывается —
		панель обрабатывает его как обычно.
		"""
		if watched is self._nav_panel and event.type() == QEvent.Type.Resize:
			# отложенно: библиотека доводит новый режим до пунктов уже
			# после ресайза, и чинить размер раньше неё бессмысленно
			QTimer.singleShot(0, self._fix_branch_sizes)
		return bool(super().eventFilter(watched, event))

	def _fix_branch_sizes(self) -> None:
		"""Возвращает раскрытым веткам подменю высоту по их содержимому.

		Обход дефекта QFluentWidgets 1.11.3: при смене ширины панели она
		назначает каждому пункту ``setFixedSize(ширина, 36)``
		(``NavigationWidget.setCompacted``) — высота в одну строку. Для
		обычного пункта это верно, а ветка подменю на этом теряет место
		под своих детей: они остаются в её компоновке и налезают друг
		на друга и на заголовок ветки.

		Лечится пересчётом по подсказке компоновки — той самой, которой
		библиотека пользуется сама при раскрытии ветки. Свёрнутую ветку
		и ветку без детей трогать незачем: у них высота и есть одна
		строка.
		"""
		for branch in self._branches():
			if branch is not None and branch.isExpanded and branch.treeChildren:
				branch.setFixedSize(branch.sizeHint())

	def _branches(self) -> list[NavigationTreeWidget | None]:
		"""Ветки подменю навигации (могут ещё не существовать при сборке)."""
		keys = (
			self._users_page.objectName(),
			self._communities_page.objectName(),
			SECTION_ROUTE_KEY,
		)
		return [self.navigationInterface.widget(key) for key in keys]

	def _build_publish_section(self) -> None:
		"""Раздел «Публикация»: ветка подменю по стадиям жизни поста (ADR-0032).

		Корень ветки — заголовок без своей страницы: стадии равноправны,
		и прятать первую из них в корень значило бы соврать о пути поста.
		Ветка сразу раскрыта — она и есть карта этого пути.
		"""
		self._publish = PublishSection(self._worker, self._watchers, self, self.switchTo)
		self.navigationInterface.addItem(
			routeKey=SECTION_ROUTE_KEY,
			icon=SECTION_ICON,
			text=SECTION_TITLE,
			selectable=False,
			tooltip=SECTION_TITLE,
		)
		for stage, page in self._publish.pages():
			self.addSubInterface(
				page, stage_icon(stage), stage_title(stage), parent=SECTION_ROUTE_KEY
			)
		self.navigationInterface.widget(SECTION_ROUTE_KEY).setExpanded(True)
		# входы в раздел с других экранов: дашборд, страницы сообществ
		# (подключаются в _sync_community_nav) и «Видео»
		self._communities_page.publish_requested.connect(self._publish.show_new_post)
		self._communities_page.schedule_requested.connect(self._publish.show_scheduled)
		self._communities_page.queue_requested.connect(self._publish.show_queue)
		self._communities_page.queue_errors_requested.connect(self._publish.show_queue_errors)
		self._video_page.publish_requested.connect(self._open_publish_with_video)
		self._video_page.publish_files_requested.connect(self._publish.show_batch_files)
		self._video_page.publish_folder_requested.connect(self._publish.show_batch_folder)

	def _watch_publish_queue(self) -> None:
		"""Плашка об исходе поста — постоянный зритель очереди отправки.

		Исход показывается всегда, какой бы экран ни был открыт
		(ADR-0032): экраны стадий отсоединяются от наблюдателя, когда
		их не видно, а окно — нет. Само снятие завершённых ведёт
		наблюдатель (ADR-0034), окну остаётся сказать человеку.
		"""
		self._watchers.publish.attach(self, QueueView(on_finished=self._on_post_finished))

	def _on_post_finished(self, item: QueueItemDto, done: bool) -> None:
		"""Итоговая плашка поста, покинувшего очередь отправки."""
		if done:
			show_success(
				self,
				"Отложенная запись создана" if item.scheduled else "Опубликовано",
				item.title,
			)
		else:
			show_info(self, "Отправка отменена", item.title)

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
				page = CommunityPage(self._worker, self._watchers, community, self)
				page.changed.connect(self._communities_page.reload)
				page.publish_requested.connect(self._publish.show_new_post)
				page.queue_requested.connect(self._publish.show_queue)
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
		pending = self._pending_community
		if pending is not None and pending in self._community_pages:
			self._pending_community = None
			self.switchTo(self._community_pages[pending])

	def _sync_user_nav(self, accounts: list[TgAccountDto], bots: list[BotDto]) -> None:
		"""Приводит подменю пользователей и ботов к свежим спискам дашборда.

		Удалённые снимаются вместе со страницами (активная — с возвратом
		на дашборд), новые добавляются, у существующих обновляется снимок
		и подпись пункта (имя могло смениться пометкой или из Telegram).
		"""
		subjects: list[TgAccountDto | BotDto] = [*accounts, *bots]
		fresh = {subject_owner(subject): subject for subject in subjects}
		for owner in list(self._user_pages):
			if owner in fresh:
				continue
			page = self._user_pages.pop(owner)
			if self.stackedWidget.currentWidget() is page:
				self.switchTo(self._users_page)
			self.navigationInterface.removeWidget(page.objectName())
			self.stackedWidget.removeWidget(page)
			page.deleteLater()
		for owner, subject in fresh.items():
			title = subject.display if isinstance(subject, TgAccountDto) else subject.label
			existing = self._user_pages.get(owner)
			if existing is None:
				page = UserPage(self._worker, subject, self)
				page.changed.connect(self._users_page.reload)
				page.open_community.connect(self._open_community)
				self._user_pages[owner] = page
				icon = FluentIcon.PEOPLE if owner.kind is OwnerKind.USER else FluentIcon.ROBOT
				self.addSubInterface(page, icon, title, parent=self._users_page)
			else:
				existing.update_subject(subject)
				item = self.navigationInterface.widget(existing.objectName())
				if item is not None:
					item.setText(title)

	def _open_user(self, owner: ExecutorRef) -> None:
		"""Клик по карточке дашборда — переход на страницу аккаунта."""
		page = self._user_pages.get(owner)
		if page is not None:
			self.switchTo(page)

	def _open_community(self, community_id: int) -> None:
		"""Клик по карточке дашборда или строке на странице аккаунта — на сообщество.

		Страницы сообществ рождаются загрузкой дашборда; если её ещё
		не было (со страницы аккаунта пришли раньше), показывается дашборд,
		а переход довершается после сборки подменю.
		"""
		page = self._community_pages.get(community_id)
		if page is not None:
			self.switchTo(page)
			return
		self._pending_community = community_id
		self.switchTo(self._communities_page)

	def _open_publish_with_video(self, path: str, community_id: int) -> None:
		"""«Опубликовать…» на «Видео» — форма поста с этим файлом и каналом."""
		self._publish.show_with_media(MediaKind.VIDEO, path, community_id)

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
		if self._watchers.publish.active():
			reasons.append(
				"идёт отправка поста — загрузка оборвётся (пост уйдёт при следующем запуске)"
			)
		if self._watchers.video.busy():
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
		ask_engine(
			self._worker,
			self._worker.engine.settings.set(WINDOW_GEOMETRY, data),
			None,
			what="сохранить состояние окна",
		)
