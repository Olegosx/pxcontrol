"""Страница «Публикация»: единая точка создания постов всех типов.

Тип контента выбирается сегментами (текст/фото/видео/аудио/файл),
отправка — всегда через userbot (ADR-0011), сразу или отложенно.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	ComboBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	PrimaryPushButton,
	ProgressBar,
	PushButton,
	ScrollArea,
	SegmentedWidget,
	SubtitleLabel,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import MediaKind, PostDraft
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import WhenRow, show_error

#: Сегменты типов контента: подпись → тип → фильтр диалога выбора файла.
_KINDS: list[tuple[str, MediaKind, str]] = [
	("Текст", MediaKind.NONE, ""),
	("Фото", MediaKind.PHOTO, "Изображения (*.png *.jpg *.jpeg *.webp)"),
	("Видео", MediaKind.VIDEO, "Видео (*.mp4 *.mov *.mkv *.avi *.webm)"),
	("Аудио", MediaKind.AUDIO, "Аудио (*.mp3 *.m4a *.flac *.ogg *.wav)"),
	("Файл", MediaKind.DOCUMENT, "Все файлы (*)"),
]


class _ProgressRelay(QObject):
	"""Мост прогресса: колбэк из потока движка → сигнал в поток интерфейса."""

	progressed = Signal(float)


class PublishPage(ScrollArea):
	"""Создание публикации: тип контента, канал, текст, время, отправка."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("publish")
		self._worker = worker
		self._channels: list[ChannelDto] = []
		self._kind = MediaKind.NONE
		self._build()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		container = QWidget(self)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.setSpacing(16)
		layout.addWidget(SubtitleLabel("Публикация", self))
		self._build_kind_segments(layout)
		self._channel_combo = ComboBox(self)
		layout.addWidget(self._channel_combo)
		self._text = TextEdit(self)
		self._text.setPlaceholderText("Текст поста…")
		self._text.setMinimumHeight(120)
		layout.addWidget(self._text)
		self._build_file_row(layout)
		self._when_row = WhenRow(self, layout)
		self._build_send_row(layout)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()
		# после сборки всех полей — сегмент по умолчанию (сигнал трогает форму)
		self._segments.setCurrentItem(MediaKind.NONE.value)

	def _build_kind_segments(self, layout: QVBoxLayout) -> None:
		"""Сегментный переключатель типа контента."""
		self._segments = SegmentedWidget(self)
		for label, kind, _file_filter in _KINDS:
			self._segments.addItem(routeKey=kind.value, text=label)
		self._segments.currentItemChanged.connect(self._on_kind_changed)
		layout.addWidget(self._segments)

	def _build_file_row(self, layout: QVBoxLayout) -> None:
		"""Строка выбора файла вложения (скрыта для типа «Текст»)."""
		self._file_box = QWidget(self)
		row = QHBoxLayout(self._file_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._file_edit = LineEdit(self._file_box)
		self._file_edit.setPlaceholderText("Файл вложения…")
		browse = PushButton("Обзор…", self._file_box)
		browse.clicked.connect(self._pick_file)
		row.addWidget(self._file_edit)
		row.addWidget(browse)
		self._file_box.hide()
		layout.addWidget(self._file_box)

	def _build_send_row(self, layout: QVBoxLayout) -> None:
		"""Кнопка отправки и индикатор прогресса загрузки."""
		row = QHBoxLayout()
		self._send_button = PrimaryPushButton(FluentIcon.SEND, "Отправить", self)
		self._send_button.clicked.connect(self._on_send)
		row.addWidget(self._send_button)
		row.addStretch()
		layout.addLayout(row)
		self._progress = ProgressBar(self)
		self._progress.setRange(0, 100)
		self._progress.hide()
		layout.addWidget(self._progress)
		self._progress_label = CaptionLabel("", self)
		self._progress_label.hide()
		layout.addWidget(self._progress_label)
		self._relay = _ProgressRelay(self)
		self._relay.progressed.connect(self._on_progress)

	# --- поведение -----------------------------------------------------------------

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет список каналов при каждом открытии страницы."""
		super().showEvent(event)
		self._reload_channels()

	def prefill_media(self, kind: MediaKind, path: str) -> None:
		"""Подставляет вложение (переход с других страниц, например «Видео»)."""
		self._segments.setCurrentItem(kind.value)
		self._on_kind_changed(kind.value)
		self._file_edit.setText(path)

	def _reload_channels(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.channels.list_channels(),
			self, self._show_channels, partial(show_error, self),
		)

	def _show_channels(self, channels: list[ChannelDto]) -> None:
		"""Обновляет список каналов, сохраняя текущий выбор."""
		selected = int(self._channel_combo.currentIndex())
		self._channels = channels
		self._channel_combo.clear()
		for channel in channels:
			self._channel_combo.addItem(channel.title)
		if 0 <= selected < len(channels):
			self._channel_combo.setCurrentIndex(selected)

	def _on_kind_changed(self, kind_key: str) -> None:
		"""Меняет состав формы под выбранный тип контента."""
		self._kind = MediaKind(kind_key)
		is_text = self._kind is MediaKind.NONE
		self._file_box.setVisible(not is_text)
		self._text.setPlaceholderText(
			"Текст поста…" if is_text else "Подпись к файлу (необязательно)…"
		)

	def _pick_file(self) -> None:
		file_filter = next(f for _l, k, f in _KINDS if k is self._kind)
		path, _ = QFileDialog.getOpenFileName(self, "Файл вложения", "", file_filter)
		if path:
			self._file_edit.setText(path)

	def _on_progress(self, fraction: float) -> None:
		percent = int(fraction * 100)
		self._progress.setValue(percent)
		self._progress_label.setText(f"Загрузка в Telegram: {percent}%")

	# --- отправка -------------------------------------------------------------------

	def _on_send(self) -> None:
		"""Собирает черновик и отправляет через движок."""
		index = int(self._channel_combo.currentIndex())
		if index < 0 or index >= len(self._channels):
			show_error(self, "Сначала подключите канал на странице «Каналы».")
			return
		draft = self._draft(self._channels[index].id)
		self._send_button.setEnabled(False)
		if draft.media_path is not None:
			self._progress.setValue(0)
			self._progress.show()
			self._progress_label.setText("Отправка…")
			self._progress_label.show()
		run_in_engine(
			self._worker,
			self._worker.engine.posts.publish(
				draft, on_progress=self._relay.progressed.emit
			),
			self, partial(self._on_sent, draft.when), self._show_error,
		)

	def _draft(self, channel_id: int) -> PostDraft:
		"""Собирает черновик публикации из полей формы."""
		media = str(self._file_edit.text()).strip() or None
		is_text = self._kind is MediaKind.NONE
		return PostDraft(
			channel_id=channel_id,
			text=str(self._text.toPlainText()).strip(),
			media_path=None if is_text else media,
			media_kind=MediaKind.NONE if is_text or media is None else self._kind,
			when=self._when_row.when(),
		)

	def _on_sent(self, when: datetime | None, _result: object = None) -> None:
		"""Показывает итог, чистит форму и гасит прогресс."""
		self._hide_progress()
		self._text.clear()
		self._file_edit.clear()
		if when is None:
			InfoBar.success("Опубликовано", "Пост отправлен в канал.", parent=self)
		else:
			InfoBar.success(
				"Отложенная запись создана",
				"Публикацию выполнит сервер Telegram.", parent=self,
			)

	def _show_error(self, message: str) -> None:
		self._hide_progress()
		show_error(self, message)

	def _hide_progress(self) -> None:
		self._progress.hide()
		self._progress_label.hide()
		self._send_button.setEnabled(True)
