"""Страница «Публикация»: единая точка создания постов всех типов.

Тип контента выбирается сегментами (текст/фото/видео/аудио/файл),
отправка — всегда через userbot (ADR-0011), сразу или отложенно.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CheckBox,
	ComboBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SegmentedWidget,
	SubtitleLabel,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import TemplateDto, title_from_filename
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import (
	MediaKind,
	PostDraft,
	publish_capabilities,
)
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.captions import CaptionDialog, FieldsDialog
from pxcontrol.ui.pages.common import ProgressPanel, WhenRow, noop, show_error

#: Сегменты типов контента: подпись → тип → фильтр диалога выбора файла.
_KINDS: list[tuple[str, MediaKind, str]] = [
	("Текст", MediaKind.NONE, ""),
	("Фото", MediaKind.PHOTO, "Изображения (*.png *.jpg *.jpeg *.webp)"),
	("Видео", MediaKind.VIDEO, "Видео (*.mp4 *.mov *.mkv *.avi *.webm)"),
	("Аудио", MediaKind.AUDIO, "Аудио (*.mp3 *.m4a *.flac *.ogg *.wav)"),
	("Файл", MediaKind.DOCUMENT, "Все файлы (*)"),
]


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
		self._channel_combo.currentIndexChanged.connect(self._on_channel_changed)
		layout.addWidget(self._channel_combo)
		self._caps_hint = CaptionLabel("", self)
		layout.addWidget(self._caps_hint)
		self._text = TextEdit(self)
		self._text.setPlaceholderText("Текст поста…")
		self._text.setMinimumHeight(120)
		layout.addWidget(self._text)
		self._build_caption_tools(layout)
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

	def _build_caption_tools(self, layout: QVBoxLayout) -> None:
		"""Кнопки шаблонизатора подписи."""
		row = QHBoxLayout()
		compose = PushButton("Собрать подпись…", self)
		compose.clicked.connect(self._on_compose_caption)
		row.addWidget(compose)
		setup = PushButton("Поля подписи…", self)
		setup.clicked.connect(self._on_setup_fields)
		row.addWidget(setup)
		row.addStretch()
		layout.addLayout(row)

	def _build_file_row(self, layout: QVBoxLayout) -> None:
		"""Строка выбора файла вложения (скрыта для типа «Текст»)."""
		self._file_box = QWidget(self)
		row = QHBoxLayout(self._file_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._file_edit = LineEdit(self._file_box)
		self._file_edit.setPlaceholderText("Файл вложения…")
		self._file_edit.textChanged.connect(self._clear_rename)
		browse = PushButton("Обзор…", self._file_box)
		browse.clicked.connect(self._pick_file)
		row.addWidget(self._file_edit)
		row.addWidget(browse)
		self._file_box.hide()
		layout.addWidget(self._file_box)
		self._build_rename_row(layout)

	def _build_rename_row(self, layout: QVBoxLayout) -> None:
		"""Строка переименования файла при отправке (появляется из подписи)."""
		self._rename_box = QWidget(self)
		row = QHBoxLayout(self._rename_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._rename_check = CheckBox("Переименовать при отправке:", self._rename_box)
		self._rename_check.setChecked(True)
		row.addWidget(self._rename_check)
		self._rename_edit = LineEdit(self._rename_box)
		row.addWidget(self._rename_edit, stretch=1)
		self._rename_box.hide()
		layout.addWidget(self._rename_box)

	def _clear_rename(self, _text: str = "") -> None:
		"""Сбрасывает переименование (файл сменился — имя устарело)."""
		self._rename_edit.clear()
		self._rename_box.hide()

	def _build_send_row(self, layout: QVBoxLayout) -> None:
		"""Кнопка отправки и индикатор прогресса загрузки."""
		row = QHBoxLayout()
		self._send_button = PrimaryPushButton(FluentIcon.SEND, "Отправить", self)
		self._send_button.clicked.connect(self._on_send)
		row.addWidget(self._send_button)
		row.addStretch()
		layout.addLayout(row)
		self._progress = ProgressPanel(self)
		layout.addWidget(self._progress)

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
			self, self._show_channels, self._show_error,
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
		self._on_channel_changed()

	def _channel_or_none(self) -> ChannelDto | None:
		"""Выбранный канал без показа ошибок (для адаптации формы)."""
		index = int(self._channel_combo.currentIndex())
		if 0 <= index < len(self._channels):
			return self._channels[index]
		return None

	def _on_channel_changed(self, _index: int = 0) -> None:
		"""Адаптирует форму под возможности выбранного канала."""
		channel = self._channel_or_none()
		if channel is None:
			self._caps_hint.setText("")
			self._when_row.set_schedule_allowed(True)
			return
		caps = publish_capabilities(
			channel.bot_id is not None, channel.userbot_admin
		)
		if caps.userbot:
			self._caps_hint.setText(
				"Публикация через userbot: все типы контента, файлы "
				"до 2 ГБ, «сейчас» и отложенные."
			)
			self._when_row.set_schedule_allowed(True)
		elif caps.bot:
			self._caps_hint.setText(
				"Публикация через бота: файлы до 50 МБ, только «сейчас» "
				"(для отложенных нужен userbot-админ)."
			)
			self._when_row.set_schedule_allowed(
				False, "Отложенные требуют userbot-админа в канале"
			)
		else:
			self._caps_hint.setText(
				"⚠ Нет способа публикации — проверьте доступы "
				"на странице «Каналы»."
			)
			self._when_row.set_schedule_allowed(False, "Нет способа публикации")

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

	# --- подпись по шаблону -----------------------------------------------------

	def _current_channel(self) -> ChannelDto | None:
		"""Выбранный канал или None (с показом подсказки)."""
		channel = self._channel_or_none()
		if channel is None:
			self._show_error("Сначала подключите и выберите канал.")
		return channel

	def _on_setup_fields(self) -> None:
		"""Открывает настройку полей и шаблонов подписи канала."""
		channel = self._current_channel()
		if channel is not None:
			FieldsDialog(self._worker, channel.id, channel.title, self.window()).exec()

	def _on_compose_caption(self) -> None:
		"""Загружает шаблоны канала и открывает диалог сборки."""
		channel = self._current_channel()
		if channel is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(channel.id),
			self, self._open_caption_dialog, self._show_error,
		)

	def _open_caption_dialog(self, templates: list[TemplateDto]) -> None:
		"""Собирает подпись по шаблону и вставляет её в поле текста."""
		if not templates or not all(t.fields for t in templates):
			self._show_error(
				"Сначала настройте поля и шаблон — кнопка «Поля подписи…»."
			)
			return
		media = str(self._file_edit.text()).strip()
		title = ""
		if self._kind is not MediaKind.NONE and media:
			title = title_from_filename(media)
		dialog = CaptionDialog(templates, title, self.window())
		if not dialog.exec():
			return
		self._text.setPlainText(dialog.caption())
		run_in_engine(
			self._worker,
			self._worker.engine.captions.record_usage(
				dialog.template_id(), dialog.used_values()
			),
			self, noop, self._show_error,
		)
		self._suggest_rename(templates, dialog, media)

	def _suggest_rename(
		self, templates: list[TemplateDto], dialog: CaptionDialog, media: str
	) -> None:
		"""Предлагает имя файла по шаблону имени (если он задан)."""
		template = next(t for t in templates if t.id == dialog.template_id())
		channel = self._current_channel()
		if not (template.filename_pattern and media and channel):
			return
		if self._kind is MediaKind.NONE:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.render_filename(
				template.id, channel.id, dialog.title(),
				dialog.used_values(), media,
			),
			self, self._show_rename_suggestion, self._show_error,
		)

	def _show_rename_suggestion(self, filename: str) -> None:
		"""Показывает строку переименования с вычисленным именем."""
		self._rename_edit.setText(filename)
		self._rename_check.setChecked(True)
		self._rename_box.show()

	# --- отправка -------------------------------------------------------------------

	def _on_send(self) -> None:
		"""Собирает черновик и отправляет через движок."""
		channel = self._current_channel()
		if channel is None:
			return
		draft = self._draft(channel.id)
		self._send_button.setEnabled(False)
		if draft.media_path is not None:
			self._progress.begin("Загрузка в Telegram", "Отправка…")
		run_in_engine(
			self._worker,
			self._worker.engine.posts.publish(
				draft, on_progress=self._progress.emit_progress
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
			rename_to=self._rename_to(),
		)

	def _rename_to(self) -> str | None:
		"""Новое имя файла, если переименование включено и имя задано."""
		if not self._rename_box.isVisibleTo(self) or not self._rename_check.isChecked():
			return None
		return str(self._rename_edit.text()).strip() or None

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
		"""Показывает ошибку и гасит индикатор прогресса."""
		self._hide_progress()
		show_error(self, message)

	def _hide_progress(self) -> None:
		"""Прячет полосу прогресса и возвращает кнопку."""
		self._progress.finish()
		self._send_button.setEnabled(True)
