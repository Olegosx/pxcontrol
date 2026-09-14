"""Страница «Расписание»: всё, что будет опубликовано, и когда.

Две вкладки — два списка одного вида (ADR-0016, ADR-0010):

- **Отложено** — записи, которые уже принял сервер Telegram: он
  опубликует их сам, даже при выключенном компьютере. Список читается
  с сервера (обход всех включённых сообществ), правится, публикуется
  «сейчас» и удаляется прямо здесь (:mod:`scheduled_panel`).
- **Очередь** — посты, которые ждут отправки **в приложении**: слота
  отложек или своей очереди. Выключите приложение — они не уйдут.
  Полный живой список с фильтрами (:class:`QueueViewTab`); ближайшие
  карточки видны и на «Публикации».

Сюда ведут кнопки «Расписание» и «Очередь» с дашборда сообществ,
«Вся очередь…» с «Публикации» и страницы сообщества. Обход отложенных
дорогой, поэтому список перечитывается при показе не чаще раза
в минуту и никогда — поверх идущего обхода.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PushButton, ScrollArea, SubtitleLabel

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.posts import ScheduledList, ScheduledPostDto
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import TabItem, error_reporter, noop, page_layout, tab_strip
from pxcontrol.ui.pages.list_view import (
	ListPage,
	PagerRow,
	ViewBar,
	paginate,
	step_page,
	summary_text,
)
from pxcontrol.ui.pages.publish_queue_view import QueueFilter, QueueViewTab, post_leading
from pxcontrol.ui.pages.scheduled_panel import (
	SCHEDULED_WORDS,
	ScheduledPanel,
	ScheduledSort,
	apply_scheduled_view,
)

#: Ключи вкладок (маршруты сегментов) в порядке показа.
TAB_SCHEDULED = "scheduled"
TAB_QUEUE = "queue"
_TABS = (TAB_SCHEDULED, TAB_QUEUE)

#: Подсказка под заголовком: в чём разница двух списков — она неочевидна
#: и важна (один список переживает выключенный компьютер, другой — нет).
_HINT = (
	"«Отложено» — записи уже у сервера Telegram: он опубликует их сам, даже "
	"при выключенном компьютере. «Очередь» — посты ждут отправки в приложении "
	"(слота отложек или своей очереди): выключите приложение — они не уйдут."
)


def tab_title(key: str) -> str:
	"""Подпись вкладки страницы «Расписание»."""
	return {TAB_SCHEDULED: "Отложено", TAB_QUEUE: "Очередь"}[key]


def unread_text(unread: tuple[str, ...]) -> str:
	"""Предупреждение над списком: какие сообщества прочитать не удалось.

	Пустая строка — прочитано всё. Честность важнее краткости: без этой
	строки пустой список читался бы как «отложенных нет», хотя часть
	сообществ просто не спросили (ADR-0010).
	"""
	if not unread:
		return ""
	names = ", ".join(f"«{title}»" for title in unread)
	return f"Не удалось прочитать отложенные: {names}."


class ScheduledViewTab(QWidget):
	"""Вкладка «Отложено»: все отложенные записи с фильтрами и страницами."""

	#: Сколько отложенных записей всего (до фильтра) — число на вкладке.
	count_changed = Signal(int)

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._avatars: dict[int, str | None] = {}
		self._page = 1
		self._view: ListPage[ScheduledPostDto] = paginate([], 1)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().row_spacing)
		self._bar = ViewBar(self, ScheduledSort)
		self._bar.changed.connect(self._on_view_changed)
		refresh = PushButton(FluentIcon.SYNC, "Обновить", self)
		refresh.setToolTip("Перечитать отложенные из Telegram (обход всех сообществ)")
		refresh.clicked.connect(self.reload)
		# кнопка — в строке показа, перед растяжкой
		self._bar.layout.insertWidget(self._bar.layout.count() - 1, refresh)
		layout.addLayout(self._bar.layout)
		self._status = CaptionLabel("", self)
		self._status.setWordWrap(True)
		layout.addWidget(self._status)
		box = QVBoxLayout()
		box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(box)
		self._pager = PagerRow(self, self._step)
		layout.addLayout(self._pager.layout)
		self._panel = ScheduledPanel(
			worker,
			self,
			box,
			transform=self._apply_view,
			on_loading=lambda: self._status.setText("Читаю отложенные из Telegram…"),
			on_loaded=self._on_loaded,
			on_refreshed=self._update_summary,
			leading=lambda item, parent: post_leading(
				item.community_id,
				item.community_title,
				item.scheduled_at,
				parent,
				self._avatars.get(item.community_id),
			),
		)
		run_in_engine(
			worker, worker.engine.community_stats.snapshot(), self, self._apply_avatars, noop
		)

	def activate(self) -> None:
		"""Вкладка показана: перечитать, если список не свежий."""
		if not self._panel.fresh():
			self.reload()

	def reload(self) -> None:
		"""Перечитывает отложенные из Telegram (обход не дублируется)."""
		self._panel.reload()

	def show_community(self, community_id: int | None) -> None:
		"""Ставит фильтр по сообществу (переход с дашборда)."""
		self._bar.want_community(community_id)
		self._page = 1
		self._panel.refresh_view()

	def count(self) -> int:
		"""Сколько отложенных записей всего (до фильтра)."""
		return len(self._panel.items)

	def _apply_avatars(self, stats: list[CommunityStatsDto]) -> None:
		self._avatars = {item.community_id: item.avatar_path for item in stats}
		self._panel.refresh_leading()

	def _on_loaded(self, scheduled: ScheduledList) -> None:
		self._status.setText(unread_text(scheduled.unread))
		self.count_changed.emit(len(scheduled.items))

	def _apply_view(self, items: list[ScheduledPostDto]) -> list[ScheduledPostDto]:
		"""Крючок панели: правило показа и нарезка на страницы."""
		self._bar.refresh(items)
		shown = apply_scheduled_view(
			items,
			ScheduledSort(self._bar.sort_option()),
			self._bar.community_id(),
			self._bar.slot_value(),
		)
		self._view = paginate(shown, self._page)
		self._page = self._view.page
		return self._view.items

	def _step(self, delta: int) -> None:
		self._page = step_page(self._page, delta, self._view.pages)
		self._panel.refresh_view()

	def _on_view_changed(self) -> None:
		self._page = 1
		self._panel.refresh_view()

	def _update_summary(self, _shown: list[ScheduledPostDto]) -> None:
		self._pager.update(
			self._view, summary_text(self._view, len(self._panel.items), SCHEDULED_WORDS)
		)


class SchedulePage(ScrollArea):
	"""Страница «Расписание»: вкладки «Отложено» и «Очередь»."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("schedule")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._current_tab = TAB_SCHEDULED
		self._shown = False  # видна ли страница (опрос очереди — только при показе)
		self._build()

	def _build(self) -> None:
		layout = page_layout(self)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Расписание", self))
		header.addStretch()
		layout.addLayout(header)
		hint = CaptionLabel(_HINT, self)
		hint.setWordWrap(True)
		layout.addWidget(hint)
		self._segments = tab_strip(self, layout)
		self._tab_items: dict[str, TabItem] = {}
		for key in _TABS:
			item = TabItem(tab_title(key), self._segments)
			self._tab_items[key] = item
			self._segments.addWidget(key, item)
		self._segments.currentItemChanged.connect(self._show_tab)
		# тела вкладок — оба сразу: очередь опрашивается только при показе,
		# отложенные читаются только при первом показе своей вкладки
		self._scheduled = ScheduledViewTab(self._worker, self)
		self._queue = QueueViewTab(self._worker, self)
		self._queue.count_changed.connect(lambda _count: self._render_tab_titles())
		self._scheduled.count_changed.connect(lambda _count: self._render_tab_titles())
		self._queue.hide()
		self._body = QVBoxLayout()
		self._body.setContentsMargins(0, 0, 0, 0)
		self._body.addWidget(self._scheduled)
		self._body.addWidget(self._queue)
		layout.addLayout(self._body, stretch=1)
		layout.addStretch()
		self._segments.setCurrentItem(TAB_SCHEDULED)

	# --- переходы с других страниц ----------------------------------------------

	def show_scheduled(self, community_id: int | None = None) -> None:
		"""Вкладка «Отложено» с фильтром по сообществу (None — все)."""
		self._segments.setCurrentItem(TAB_SCHEDULED)
		self._show_tab(TAB_SCHEDULED)
		self._scheduled.show_community(community_id)

	def show_queue(
		self, community_id: int | None = None, status: QueueFilter | None = None
	) -> None:
		"""Вкладка «Очередь» с фильтром по сообществу и/или статусу."""
		self._segments.setCurrentItem(TAB_QUEUE)
		self._show_tab(TAB_QUEUE)
		self._queue.show_filter(community_id, status)

	# --- вкладки ------------------------------------------------------------------

	def _show_tab(self, key: str) -> None:
		if key not in _TABS:
			return
		self._current_tab = key
		self._render_tab_titles()
		self._scheduled.setVisible(key == TAB_SCHEDULED)
		self._queue.setVisible(key == TAB_QUEUE)
		self._sync_activity()

	def _sync_activity(self) -> None:
		"""Опрос очереди и чтение отложенных — только у видимой вкладки."""
		queue_visible = self._shown and self._current_tab == TAB_QUEUE
		self._queue.set_polling(queue_visible)
		if self._shown and self._current_tab == TAB_SCHEDULED:
			self._scheduled.activate()

	def _render_tab_titles(self) -> None:
		"""Числа рядом с подписями вкладок (у активной — акцентом)."""
		counts = {TAB_SCHEDULED: self._scheduled.count(), TAB_QUEUE: self._queue_total()}
		for key, item in self._tab_items.items():
			item.set_count(counts.get(key), active=key == self._current_tab)

	def _queue_total(self) -> int:
		return self._queue.total()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — имя Qt
		"""Страница показана: включить видимую вкладку."""
		super().showEvent(event)
		self._shown = True
		self._sync_activity()

	def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 — имя Qt
		"""Страница скрыта: очередь не опрашивается."""
		super().hideEvent(event)
		self._shown = False
		self._sync_activity()
