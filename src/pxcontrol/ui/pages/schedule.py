"""Страница «Расписание»: отложенные записи каналов из Telegram.

Источник истины — сам канал (ADR-0010): список отложенных читается
из Telegram, править и удалять их можно из любого клиента Telegram.
Создание постов — на странице «Публикация». Фильтр по каналам —
презентационный: скрывает карточки, не меняя загруженный список.
"""

from __future__ import annotations

from functools import partial

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	CheckBox,
	FluentIcon,
	PushButton,
	ScrollArea,
	StrongBodyLabel,
	SubtitleLabel,
	themeColor,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import ScheduledPostDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import clear_layout, error_reporter, page_layout


class SchedulePage(ScrollArea):
	"""Отложенные записи каналов (читаются из Telegram)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("schedule")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._items: list[ScheduledPostDto] = []
		# снятые галки фильтра (id каналов): выбор переживает «Обновить»
		self._unchecked: set[int] = set()
		self._build()
		self._reload()

	def _build(self) -> None:
		"""Шапка с кнопками, фильтр по каналам и область списка."""
		layout = page_layout(self)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Отложенные записи", self))
		header.addStretch()
		refresh = PushButton(FluentIcon.SYNC, "Обновить", self)
		refresh.clicked.connect(self._reload)
		header.addWidget(refresh)
		layout.addLayout(header)
		hint = CaptionLabel(
			"Список читается из Telegram. Создание постов — на странице "
			"«Публикация»; править и удалять отложенные можно из любого "
			"клиента Telegram.", self,
		)
		layout.addWidget(hint)
		self._filter_box = QHBoxLayout()
		self._filter_box.setSpacing(12)
		layout.addLayout(self._filter_box)
		self._list = QVBoxLayout()
		self._list.setSpacing(8)
		layout.addLayout(self._list)
		layout.addStretch()

	# --- список отложенных (из Telegram) ---------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.posts.list_scheduled(),
			self, self._show_scheduled, self._show_error,
		)

	def _show_scheduled(self, items: list[ScheduledPostDto]) -> None:
		"""Принимает свежий список: перестраивает фильтр и карточки."""
		self._items = items
		self._rebuild_filter()
		self._render()

	# --- фильтр по каналам -------------------------------------------------------

	def _rebuild_filter(self) -> None:
		"""Строка чекбоксов: по одному на канал с отложенными записями.

		Состояние галок хранится по id канала и переживает обновление
		списка; канал, исчезнувший из списка, пропадает и из фильтра.
		"""
		clear_layout(self._filter_box)
		channels: dict[int, str] = {}
		for item in self._items:
			channels.setdefault(item.channel_id, item.channel_title)
		if not channels:
			return
		self._filter_box.addWidget(CaptionLabel("Показывать:", self))
		for channel_id, title in channels.items():
			box = CheckBox(title, self)
			box.setChecked(channel_id not in self._unchecked)
			box.toggled.connect(partial(self._on_filter_toggled, channel_id))
			self._filter_box.addWidget(box)
		self._filter_box.addStretch()

	def _on_filter_toggled(self, channel_id: int, checked: bool) -> None:
		"""Галка канала: показывает/скрывает его карточки (без перезагрузки)."""
		if checked:
			self._unchecked.discard(channel_id)
		else:
			self._unchecked.add(channel_id)
		self._render()

	# --- карточки ----------------------------------------------------------------

	def _render(self) -> None:
		"""Перерисовывает карточки с учётом фильтра."""
		clear_layout(self._list)
		if not self._items:
			self._list.addWidget(CaptionLabel(
				"Отложенных записей нет. Создайте пост на странице «Публикация».",
				self,
			))
			return
		visible = [
			item for item in self._items
			if item.channel_id not in self._unchecked
		]
		if not visible:
			self._list.addWidget(CaptionLabel(
				"Все каналы скрыты фильтром — включите хотя бы один.", self,
			))
			return
		for item in visible:
			self._list.addWidget(self._item_row(item))

	def _item_row(self, item: ScheduledPostDto) -> CardWidget:
		"""Карточка записи: момент публикации — первой строкой, акцентом.

		Время показывается местное (хранится UTC, как отдаёт Telegram).
		Цвет — акцентный цвет темы (``setTextColor`` перекрашивает и при
		смене темы, в отличие от жёсткого стиля).
		"""
		card = CardWidget(self)
		box = QVBoxLayout(card)
		box.setContentsMargins(16, 10, 16, 10)
		box.setSpacing(2)
		when = StrongBodyLabel(
			f"{item.scheduled_at.astimezone():%d.%m.%Y %H:%M}", card
		)
		when.setTextColor(themeColor(), themeColor())
		box.addWidget(when)
		text = BodyLabel(item.text_preview, card)
		text.setWordWrap(True)
		box.addWidget(text)
		box.addWidget(CaptionLabel(item.channel_title, card))
		return card
