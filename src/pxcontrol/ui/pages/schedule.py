"""Страница «Расписание»: отложенные записи каналов из Telegram.

Источник истины — сам канал (ADR-0010): список отложенных читается
из Telegram, править и удалять их можно из любого клиента Telegram.
Создание постов — на странице «Публикация».
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CardWidget,
	FluentIcon,
	PushButton,
	ScrollArea,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import ScheduledPostDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import clear_layout, error_reporter, row_card


class SchedulePage(ScrollArea):
	"""Отложенные записи каналов (читаются из Telegram)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("schedule")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._build()
		self._reload()

	def _build(self) -> None:
		"""Шапка с кнопками и область списка."""
		container = QWidget(self)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.setSpacing(16)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Отложенные записи", container))
		header.addStretch()
		refresh = PushButton(FluentIcon.SYNC, "Обновить", container)
		refresh.clicked.connect(self._reload)
		header.addWidget(refresh)
		layout.addLayout(header)
		hint = CaptionLabel(
			"Список читается из Telegram. Создание постов — на странице "
			"«Публикация»; править и удалять отложенные можно из любого "
			"клиента Telegram.", container,
		)
		layout.addWidget(hint)
		self._list = QVBoxLayout()
		self._list.setSpacing(8)
		layout.addLayout(self._list)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()

	# --- список отложенных (из Telegram) ---------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.posts.list_scheduled(),
			self, self._show_scheduled, self._show_error,
		)

	def _show_scheduled(self, items: list[ScheduledPostDto]) -> None:
		clear_layout(self._list)
		if not items:
			self._list.addWidget(CaptionLabel(
				"Отложенных записей нет. Создайте пост на странице «Публикация».",
				self,
			))
			return
		for item in items:
			self._list.addWidget(self._item_row(item))

	def _item_row(self, item: ScheduledPostDto) -> CardWidget:
		"""Карточка отложенной записи: текст, канал и локальное время."""
		when_local = item.scheduled_at.astimezone()
		subtitle = f"{item.channel_title} · {when_local:%d.%m.%Y %H:%M}"
		return row_card(self, item.text_preview, subtitle)
