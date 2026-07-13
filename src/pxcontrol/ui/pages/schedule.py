"""Страница «Расписание»: новый пост и отложенные записи из Telegram.

Источник истины — сам канал (ADR-0010): список отложенных читается
из Telegram, править и удалять их можно из любого клиента Telegram.
"""

from __future__ import annotations

from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CardWidget,
	ComboBox,
	FluentIcon,
	InfoBar,
	MessageBoxBase,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import ScheduledPostDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import WhenRow, clear_layout, row_card, show_error


class _NewPostDialog(MessageBoxBase):
	"""Диалог нового поста: канал, текст, «сейчас» или дата+время."""

	def __init__(self, channels: list[ChannelDto], parent: QWidget) -> None:
		super().__init__(parent)
		self._channels = channels
		self.viewLayout.addWidget(SubtitleLabel("Новый пост", self))
		self._combo = ComboBox(self)
		for channel in channels:
			self._combo.addItem(channel.title)
		self.viewLayout.addWidget(self._combo)
		self._text = TextEdit(self)
		self._text.setPlaceholderText("Текст поста…")
		self._text.setMinimumHeight(120)
		self.viewLayout.addWidget(self._text)
		self._when_row = WhenRow(self, self.viewLayout)
		self.yesButton.setText("Отправить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(480)

	def channel_id(self) -> int:
		"""Идентификатор выбранного канала."""
		return self._channels[int(self._combo.currentIndex())].id

	def text(self) -> str:
		"""Текст поста без крайних пробелов."""
		return str(self._text.toPlainText()).strip()

	def when(self) -> datetime | None:
		"""None — «сейчас», иначе выбранный момент (в UTC)."""
		return self._when_row.when()


class SchedulePage(ScrollArea):
	"""Отложенные записи каналов (из Telegram) и создание постов."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("schedule")
		self._worker = worker
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
		new_post = PrimaryPushButton(FluentIcon.ADD, "Новый пост", container)
		new_post.clicked.connect(self._on_new_post)
		header.addWidget(new_post)
		layout.addLayout(header)
		hint = CaptionLabel(
			"Список читается из Telegram. Править и удалять отложенные "
			"можно из любого клиента Telegram.", container,
		)
		layout.addWidget(hint)
		self._list = QVBoxLayout()
		self._list.setSpacing(8)
		layout.addLayout(self._list)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку всплывающей плашкой."""
		show_error(self, message)

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
				"Отложенных записей нет. Создайте пост кнопкой «Новый пост».",
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

	# --- создание поста ----------------------------------------------------------

	def _on_new_post(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.channels.list_channels(),
			self, self._open_dialog, self._show_error,
		)

	def _open_dialog(self, channels: list[ChannelDto]) -> None:
		if not channels:
			self._show_error("Сначала подключите канал на странице «Каналы».")
			return
		dialog = _NewPostDialog(channels, self.window())
		if not dialog.exec():
			return
		if not dialog.text():
			self._show_error("Текст поста пуст.")
			return
		when = dialog.when()
		coro: Coroutine[Any, Any, object]
		if when is None:
			coro = self._worker.engine.posts.send_now(
				dialog.channel_id(), dialog.text()
			)
		else:
			coro = self._worker.engine.posts.schedule(
				dialog.channel_id(), dialog.text(), when
			)
		run_in_engine(self._worker, coro, self, self._on_sent, self._show_error)

	def _on_sent(self, result: object) -> None:
		if result is None:
			InfoBar.success(
				"Отложенный пост создан",
				"Публикацию выполнит сервер Telegram.", parent=self,
			)
		else:
			InfoBar.success("Опубликовано", f"ID сообщения: {result}", parent=self)
		self._reload()
