"""Страница «Расписание»: новый пост и отложенные записи из Telegram.

Источник истины — сам канал (ADR-0010): список отложенных читается
из Telegram, править и удалять их можно из любого клиента Telegram.
"""

from __future__ import annotations

from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QDate, QTime
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	ComboBox,
	FluentIcon,
	InfoBar,
	MessageBoxBase,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TextEdit,
	TimePicker,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import ScheduledPostDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import clear_layout


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
		self._build_when_row()
		self.yesButton.setText("Отправить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(480)

	def _build_when_row(self) -> None:
		"""Переключатель «сейчас» и скрываемые дата+время."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Опубликовать сейчас", self))
		self._now_switch = SwitchButton(self)
		self._now_switch.setChecked(True)
		self._now_switch.checkedChanged.connect(self._on_now_toggled)
		row.addWidget(self._now_switch)
		row.addStretch()
		self._date = CalendarPicker(self)
		self._date.setDate(QDate.currentDate())
		self._time = TimePicker(self)
		self._time.setTime(QTime.currentTime().addSecs(3600))
		self._date.hide()
		self._time.hide()
		row.addWidget(self._date)
		row.addWidget(self._time)
		self.viewLayout.addLayout(row)

	def _on_now_toggled(self, now: bool) -> None:
		self._date.setVisible(not now)
		self._time.setVisible(not now)

	def channel_id(self) -> int:
		"""Идентификатор выбранного канала."""
		return self._channels[int(self._combo.currentIndex())].id

	def text(self) -> str:
		"""Текст поста без крайних пробелов."""
		return str(self._text.toPlainText()).strip()

	def when(self) -> datetime | None:
		"""None — «сейчас», иначе выбранный момент (в UTC)."""
		if self._now_switch.isChecked():
			return None
		date, time = self._date.getDate(), self._time.getTime()
		local = datetime(
			date.year(), date.month(), date.day(), time.hour(), time.minute()
		)
		return local.astimezone(UTC)


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
		InfoBar.error("Ошибка", message, parent=self, duration=6000)

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
		card = CardWidget(self)
		layout = QHBoxLayout(card)
		layout.setContentsMargins(16, 10, 16, 10)
		column = QVBoxLayout()
		column.setSpacing(2)
		column.addWidget(StrongBodyLabel(item.text_preview, card))
		when_local = item.scheduled_at.astimezone()
		column.addWidget(CaptionLabel(
			f"{item.channel_title} · {when_local:%d.%m.%Y %H:%M}", card,
		))
		layout.addLayout(column)
		layout.addStretch()
		return card

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
