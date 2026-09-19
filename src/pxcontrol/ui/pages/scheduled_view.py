"""Вид списка отложенных записей: фильтры, страницы, чтение из Telegram.

Тело экрана «Отложено» раздела «Публикация» (ADR-0032). Парный модуль
к :mod:`publish_queue_view` (вид списка очереди отправки): у обоих
списков одно устройство — панель показа, правило показа поверх общих
правил (:mod:`list_view`: сортировка, фильтры по сообществу и слоту,
страницы) и строка итога с перелистыванием.

Разница у списков предметная, а не оформительская: очередь живёт
в приложении и опрашивается по таймеру, а отложенные записи живут
на сервере Telegram (ADR-0010) — их список читается обходом сообществ
при показе, не чаще раза в минуту и никогда поверх идущего обхода
(ADR-0024: флуд-лимит на обходе заморозил бы дорожку аккаунта вместе
с публикацией).
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PushButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.posts import (
	ScheduledList,
	ScheduledPostDto,
	UnreadCommunity,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import noop
from pxcontrol.ui.pages.list_view import (
	ListPage,
	PagerRow,
	ViewBar,
	paginate,
	step_page,
	summary_text,
)
from pxcontrol.ui.pages.publish_queue_view import post_leading, post_leading_signature
from pxcontrol.ui.pages.scheduled_panel import (
	SCHEDULED_WORDS,
	ScheduledPanel,
	ScheduledSort,
	apply_scheduled_view,
)


def unread_text(unread: tuple[UnreadCommunity, ...]) -> str:
	"""Предупреждение над списком: какие сообщества прочитать не удалось.

	Пустая строка — прочитано всё. Честность важнее краткости: без этой
	строки пустой список читался бы как «отложенных нет», хотя часть
	сообществ просто не спросили (ADR-0010).
	"""
	if not unread:
		return ""
	names = ", ".join(f"«{entry.title}»" for entry in unread)
	return f"Не удалось прочитать отложенные: {names}."


class ScheduledView(QWidget):
	"""Все отложенные записи с фильтрами и страницами.

	Показ ставится извне (:meth:`show_community` — переход с дашборда
	сообществ), чтение начинается при показе экрана (:meth:`activate`)
	и по кнопке «Обновить».
	"""

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
		# растяжка снизу: без неё лишнюю высоту экрана забирают подписи
		# с переносом слов, и список расползается пустотами
		layout.addStretch()

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
			leading_signature=lambda item: post_leading_signature(
				item.community_id, item.scheduled_at, self._avatars.get(item.community_id)
			),
		)
		run_in_engine(
			worker, worker.engine.community_stats.snapshot(), self, self._apply_avatars, noop
		)

	def activate(self) -> None:
		"""Экран показан: перечитать, если список не свежий."""
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

	def _apply_avatars(self, stats: list[CommunityStatsDto]) -> None:
		"""Раскладывает аватары сообществ; шапки пересоберутся по отпечатку."""
		self._avatars = {item.community_id: item.avatar_path for item in stats}
		self._panel.refresh_view()

	def _on_loaded(self, scheduled: ScheduledList) -> None:
		self._status.setText(unread_text(scheduled.unread))

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
