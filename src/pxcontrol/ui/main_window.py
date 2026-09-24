"""Главное окно: боковая навигация + разделы (FluentWindow, ADR-0008, ADR-0041).

Дерево навигации неизменно за всё время работы: разделы приложения
и по паре групп у дашбордов сообществ и исполнителей. Сущности
(каналы, группы, пользователи, боты) в панели не живут — они
открываются со своих дашбордов на **одной** странице сообщества
и **одной** странице аккаунта, которые лежат в стопке окна без пункта
навигации (ADR-0041).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QCloseEvent
from qfluentwidgets import (
	DotInfoBadge,
	FluentIcon,
	FluentWindow,
	InfoBadge,
	InfoBadgePosition,
	MessageBox,
	NavigationDisplayMode,
	NavigationItemPosition,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import TgAccountDto
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.publish_queue import QueueItemDto
from pxcontrol.engine.services.settings import UI_NAV_COMPACT, WINDOW_GEOMETRY
from pxcontrol.engine.telegram.types import CommunityKind, ExecutorRef, MediaKind, OwnerKind
from pxcontrol.ui.async_bridge import ask_engine, run_in_engine
from pxcontrol.ui.pages.common import (
	error_reporter,
	exec_dialog,
	noop,
	queue_counts,
	show_info,
	show_success,
)
from pxcontrol.ui.pages.communities import CommunitiesPage, CommunityScope
from pxcontrol.ui.pages.community_page import CommunityPage
from pxcontrol.ui.pages.publish_section import PublishSection
from pxcontrol.ui.pages.publish_stages import (
	SECTION_ICON,
	SECTION_ROUTE_KEY,
	SECTION_TITLE,
	PublishStage,
	stage_icon,
	stage_title,
)
from pxcontrol.ui.pages.settings import SettingsPage
from pxcontrol.ui.pages.user_page import Subject, UserPage
from pxcontrol.ui.pages.users import UserScope, UsersPage
from pxcontrol.ui.pages.video import VideoPage
from pxcontrol.ui.queue_watcher import QueueView, QueueWatchers

logger = logging.getLogger(__name__)

#: Минимальная ширина окна: развёрнутая панель навигации (322) плюс
#: самая широкая страница (форма параметров «Видео» и таблица дашборда).
#: Она же порог разворота панели — на любой допустимой ширине окна
#: панель развёрнута (ADR-0041, п. 7).
WINDOW_MIN_WIDTH = 1160
WINDOW_MIN_HEIGHT = 640

#: Маршрутные ключи групп навигации. Своей страницы у групп нет: клик
#: показывает дашборд, суженный до раздела (ADR-0041, п. 3).
NAV_USERS_PEOPLE = "users_people"
NAV_USERS_BOTS = "users_bots"
NAV_COMMUNITIES_CHANNELS = "communities_channels"
NAV_COMMUNITIES_GROUPS = "communities_groups"

#: Раздел дашборда сообществ по виду сообщества: канал открылся —
#: подсвечена группа «Каналы».
_COMMUNITY_SCOPES: dict[CommunityKind, tuple[CommunityScope, str]] = {
	CommunityKind.CHANNEL: (CommunityScope.CHANNELS, NAV_COMMUNITIES_CHANNELS),
	CommunityKind.GROUP: (CommunityScope.GROUPS, NAV_COMMUNITIES_GROUPS),
}

#: То же у исполнителей: пользователь — «Пользователи», бот — «Боты».
_USER_SCOPES: dict[OwnerKind, tuple[UserScope, str]] = {
	OwnerKind.USER: (UserScope.PEOPLE, NAV_USERS_PEOPLE),
	OwnerKind.BOT: (UserScope.BOTS, NAV_USERS_BOTS),
}


class MainWindow(FluentWindow):
	"""Окно с боковой навигацией. К движку обращается через `EngineWorker`."""

	def __init__(self, worker: EngineWorker) -> None:
		super().__init__()
		self._worker = worker
		self._show_error = error_reporter(self)
		self.setWindowTitle("pXcontrol")
		self.resize(WINDOW_MIN_WIDTH, 800)
		self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
		# порог разворота панели равен минимуму окна: уже окно не бывает,
		# поэтому сворачивает панель только человек кнопкой-«бутербродом»
		self.navigationInterface.setMinimumExpandWidth(WINDOW_MIN_WIDTH)
		self._restore_geometry()
		# страницы сущностей: по одной на приложение, рождаются при первом
		# открытии и живут до конца сеанса (ADR-0041, п. 4)
		self._community_page: CommunityPage | None = None
		self._user_page: UserPage | None = None
		# метки тревоги навигации: пока ошибок нет, их вовсе нет
		self._queue_errors = 0
		self._queue_badge: InfoBadge | None = None
		self._publish_dot: DotInfoBadge | None = None
		# наблюдатели очередей — по одному на очередь, при окне (ADR-0034);
		# страницы получают их зрителями, поэтому заводятся до навигации
		self._watchers = QueueWatchers(worker, self)
		self._build_navigation()
		self._watch_publish_queue()
		self._restore_nav_mode()

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

	# --- навигация ------------------------------------------------------------

	def _build_navigation(self) -> None:
		"""Наполняет боковую навигацию разделами приложения (ADR-0041, п. 1).

		Дерево собирается один раз и дальше не меняется: ни один пункт
		не добавляется и не снимается на лету.
		"""
		# пользователи и боты — над сообществами (ADR-0029): без них
		# подключать сообщества нечем, порядок разделов — порядок настройки
		self._users_page = UsersPage(self._worker, self)
		users_item = self.addSubInterface(
			self._users_page, FluentIcon.PEOPLE, "Пользователи и боты"
		)
		users_item.clicked.connect(partial(self._show_users, UserScope.ALL, self._users_key()))
		self._users_page.open_user.connect(self._open_user)
		self._add_group(
			NAV_USERS_PEOPLE,
			FluentIcon.PEOPLE,
			"Пользователи",
			self._users_key(),
			partial(self._show_users, UserScope.PEOPLE, NAV_USERS_PEOPLE),
		)
		self._add_group(
			NAV_USERS_BOTS,
			FluentIcon.ROBOT,
			"Боты",
			self._users_key(),
			partial(self._show_users, UserScope.BOTS, NAV_USERS_BOTS),
		)
		self._communities_page = CommunitiesPage(self._worker, self._watchers, self)
		communities_item = self.addSubInterface(
			self._communities_page, FluentIcon.HOME, "Каналы и группы"
		)
		communities_item.clicked.connect(
			partial(self._show_communities, CommunityScope.ALL, self._communities_key())
		)
		self._communities_page.open_community.connect(self._open_community)
		self._add_group(
			NAV_COMMUNITIES_CHANNELS,
			FluentIcon.CHAT,
			"Каналы",
			self._communities_key(),
			partial(self._show_communities, CommunityScope.CHANNELS, NAV_COMMUNITIES_CHANNELS),
		)
		self._add_group(
			NAV_COMMUNITIES_GROUPS,
			FluentIcon.PEOPLE,
			"Группы",
			self._communities_key(),
			partial(self._show_communities, CommunityScope.GROUPS, NAV_COMMUNITIES_GROUPS),
		)
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

	def _users_key(self) -> str:
		"""Маршрутный ключ дашборда исполнителей."""
		return str(self._users_page.objectName())

	def _communities_key(self) -> str:
		"""Маршрутный ключ дашборда сообществ."""
		return str(self._communities_page.objectName())

	def _add_group(
		self,
		route_key: str,
		icon: FluentIcon,
		text: str,
		parent_key: str,
		on_click: Callable[..., None],
	) -> None:
		"""Пункт-группа под дашбордом: тот же дашборд, суженный до раздела.

		Числа у групп («Каналы 58») не показываются: счёт живёт
		в заголовке раздела дашборда, а навигация — не сводка.
		"""
		self.navigationInterface.addItem(
			routeKey=route_key,
			icon=icon,
			text=text,
			onClick=on_click,
			tooltip=text,
			parentRouteKey=parent_key,
		)

	def _show_communities(self, scope: CommunityScope, route_key: str, *_args: object) -> None:
		"""Показывает дашборд сообществ с выбранным разделом (ADR-0041, п. 3).

		Порядок важен: ``switchTo`` подсвечивает пункт страницы
		(родителя), поэтому выбор возвращается пункту группы после него.
		"""
		self._communities_page.show_scope(scope)
		self.switchTo(self._communities_page)
		self.navigationInterface.setCurrentItem(route_key)

	def _show_users(self, scope: UserScope, route_key: str, *_args: object) -> None:
		"""Показывает дашборд исполнителей с выбранным разделом."""
		self._users_page.show_scope(scope)
		self.switchTo(self._users_page)
		self.navigationInterface.setCurrentItem(route_key)

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
		# входы в раздел с других экранов: дашборд, страница сообщества
		# (подключается при её создании) и «Видео»
		self._communities_page.publish_requested.connect(self._publish.show_new_post)
		self._communities_page.schedule_requested.connect(self._publish.show_scheduled)
		self._communities_page.queue_requested.connect(self._publish.show_queue)
		self._communities_page.queue_errors_requested.connect(self._publish.show_queue_errors)
		self._video_page.publish_requested.connect(self._open_publish_with_video)
		self._video_page.publish_files_requested.connect(self._publish.show_batch_files)
		self._video_page.publish_folder_requested.connect(self._publish.show_batch_folder)

	def _refresh_alert_badges(self) -> None:
		"""Метки тревоги навигации: ошибки очереди отправки (ADR-0041, п. 6).

		Число — на пункте «Очередь», точка — на корне «Публикации»:
		в свёрнутой панели детей не видно, и без точки об ошибках
		никто бы не узнал. Счётчиков «сколько всего» в навигации нет.

		«Ошибок нет» — это **снятая** метка, а не спрятанная: видимость
		метки ведёт её менеджер (``InfoBadgeManager``), и спрятанную он
		показывает заново, едва её пункт снова виден, — при разворачивании
		панели или раскрытии ветки.
		"""
		if self._queue_errors > 0:
			self._show_alert_badges()
		else:
			self._drop_alert_badges()

	def _show_alert_badges(self) -> None:
		"""Ставит метки или обновляет число на уже стоящей."""
		panel = self.navigationInterface
		badge = self._queue_badge
		if badge is None:
			self._queue_badge = InfoBadge.error(
				self._queue_errors,
				panel,
				panel.widget(PublishStage.QUEUE.value),
				InfoBadgePosition.NAVIGATION_ITEM,
			)
			self._publish_dot = DotInfoBadge.error(
				panel, panel.widget(SECTION_ROUTE_KEY), InfoBadgePosition.NAVIGATION_ITEM
			)
			return
		badge.setText(str(self._queue_errors))
		badge.adjustSize()
		badge.move(badge.manager.position())

	def _drop_alert_badges(self) -> None:
		"""Снимает метки: ошибок в очереди не осталось."""
		for badge in (self._queue_badge, self._publish_dot):
			if badge is None:
				continue
			# фильтр менеджера живёт на пункте навигации: снять до удаления,
			# иначе он двигал бы уже удалённую метку
			badge.manager.target.removeEventFilter(badge.manager)
			badge.setParent(None)
			badge.deleteLater()
		self._queue_badge = None
		self._publish_dot = None

	def _restore_nav_mode(self) -> None:
		"""Разворачивает панель навигации, если человек не сворачивал её.

		Библиотека сама разворачивает панель по ширине окна только при
		скрытой кнопке-«бутерброде» (``NavigationPanel.eventFilter``),
		а она у нас видна — значит, состояние применяем сами. Панель
		рождается свёрнутой, поэтому делать нужно только разворот.
		"""
		self._nav_compact = ask_engine(
			self._worker,
			self._worker.engine.settings.get(UI_NAV_COMPACT),
			False,
			what="прочитать состояние панели навигации",
		)
		if not self._nav_compact:
			self.navigationInterface.panel.expand(useAni=False)
		self.navigationInterface.displayModeChanged.connect(self._on_nav_mode_changed)

	def _on_nav_mode_changed(self, mode: NavigationDisplayMode) -> None:
		"""Запоминает, свернул ли человек панель (ADR-0041, п. 7)."""
		compact = mode is not NavigationDisplayMode.EXPAND
		if compact == self._nav_compact:
			return
		self._nav_compact = compact
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(UI_NAV_COMPACT, compact),
			self,
			noop,
			# состояние панели — удобство: не записалось, скажем в журнал
			lambda message: logger.warning("Состояние панели не сохранено: %s", message),
		)

	# --- очередь отправки -----------------------------------------------------

	def _watch_publish_queue(self) -> None:
		"""Плашка об исходе поста и метки тревоги — зритель очереди отправки.

		Исход показывается всегда, какой бы экран ни был открыт
		(ADR-0032): экраны стадий отсоединяются от наблюдателя, когда
		их не видно, а окно — нет. Само снятие завершённых ведёт
		наблюдатель (ADR-0034), окну остаётся сказать человеку.
		"""
		self._watchers.publish.attach(
			self,
			QueueView(on_state=self._on_queue_state, on_finished=self._on_post_finished),
		)

	def _on_queue_state(self, items: list[QueueItemDto]) -> None:
		"""Снимок очереди: метка тревоги меняется только при смене числа."""
		errors = sum(counts.errors for counts in queue_counts(items).values())
		if errors == self._queue_errors:
			return
		self._queue_errors = errors
		self._refresh_alert_badges()

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

	# --- страницы сущностей ---------------------------------------------------

	def _open_community(self, community_id: int) -> None:
		"""Клик по карточке дашборда или строке страницы аккаунта — на сообщество.

		Снимок читается у движка по id: он один для обоих входов
		и всегда свежий, а страница сообщества принимает готовый снимок.
		"""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.get_community(community_id),
			self,
			self._show_community,
			self._show_error,
		)

	def _show_community(self, community: CommunityDto) -> None:
		"""Показывает сообщество на единственной странице сообщества."""
		page = self._community_page
		if page is None:
			page = CommunityPage(self._worker, self._watchers, community, self)
			page.changed.connect(self._communities_page.reload)
			page.publish_requested.connect(self._publish.show_new_post)
			page.queue_requested.connect(self._publish.show_queue)
			page.dashboard_requested.connect(self._open_communities_dashboard)
			self.stackedWidget.addWidget(page)
			self._community_page = page
		else:
			page.show_community(community)
		self.switchTo(page)
		# у страницы сущности своего пункта нет: подсвечена её группа
		_scope, route_key = _COMMUNITY_SCOPES[community.kind]
		self.navigationInterface.setCurrentItem(route_key)

	def _open_communities_dashboard(self, kind: object) -> None:
		"""Клик по строке пути страницы сообщества — назад на дашборд."""
		scope, route_key = (
			_COMMUNITY_SCOPES[kind]
			if isinstance(kind, CommunityKind)
			else (CommunityScope.ALL, self._communities_key())
		)
		self._show_communities(scope, route_key)

	def _open_user(self, owner: ExecutorRef) -> None:
		"""Клик по карточке дашборда — страница пользователя или бота."""
		accounts = self._worker.engine.accounts
		if owner.kind is OwnerKind.USER:
			run_in_engine(
				self._worker,
				accounts.get_tg_account(owner.id),
				self,
				self._show_subject,
				self._show_error,
			)
		else:
			run_in_engine(
				self._worker, accounts.get_bot(owner.id), self, self._show_subject, self._show_error
			)

	def _show_subject(self, subject: Subject) -> None:
		"""Показывает исполнителя на единственной странице аккаунта."""
		page = self._user_page
		if page is None:
			page = UserPage(self._worker, subject, self)
			page.changed.connect(self._users_page.reload)
			page.open_community.connect(self._open_community)
			page.dashboard_requested.connect(self._open_users_dashboard)
			self.stackedWidget.addWidget(page)
			self._user_page = page
		else:
			page.show_subject(subject)
		self.switchTo(page)
		kind = OwnerKind.USER if isinstance(subject, TgAccountDto) else OwnerKind.BOT
		_scope, route_key = _USER_SCOPES[kind]
		self.navigationInterface.setCurrentItem(route_key)

	def _open_users_dashboard(self, kind: object) -> None:
		"""Клик по строке пути страницы аккаунта — назад на дашборд."""
		scope, route_key = (
			_USER_SCOPES[kind]
			if isinstance(kind, OwnerKind)
			else (UserScope.ALL, self._users_key())
		)
		self._show_users(scope, route_key)

	def _open_publish_with_video(self, path: str, community_id: int) -> None:
		"""«Опубликовать…» на «Видео» — форма поста с этим файлом и каналом."""
		self._publish.show_with_media(MediaKind.VIDEO, path, community_id)

	# --- закрытие -------------------------------------------------------------

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


__all__ = ["MainWindow"]
