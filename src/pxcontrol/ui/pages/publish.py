"""Страница «Публикация»: единая точка создания постов всех типов.

Тип контента выбирается сегментами (текст/фото/видео/аудио/файл).
Отправка идёт через очередь движка (ADR-0012): «Отправить» ставит пост
в хвост и сразу освобождает форму под следующий; очередь видна на
странице, каждый элемент можно отменить.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CardWidget,
	CheckBox,
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
from pxcontrol.engine.services.captions import TemplateDto, title_from_filename
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import (
	BOT_MAX_FILE_BYTES,
	USERBOT_MAX_FILE_BYTES,
	PostDraft,
	publish_capabilities,
)
from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.engine.services.settings import PUBLISH_LAST_CHANNEL_ID
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.captions import CaptionDialog, FieldsDialog
from pxcontrol.ui.pages.common import (
	WhenRow,
	bind,
	clear_layout,
	error_reporter,
	noop,
	page_layout,
	pick_file,
	row_card,
)

#: Период опроса состояния очереди отправки (мс).
_QUEUE_POLL_MS = 500

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
		self._show_error = error_reporter(self)
		self._channels: list[ChannelDto] = []
		# канал прошлой публикации: предвыбор после загрузки списка
		self._restore_channel_id: int | None = None
		self._kind = MediaKind.NONE
		self._queue_signature: tuple[tuple[int, QueueItemStatus, str | None], ...] = ()
		self._queue_bars: dict[int, ProgressBar] = {}
		self._handled_ids: set[int] = set()  # завершённые, уже показанные плашкой
		self._queue_busy = False
		self._build()
		run_in_engine(
			worker, worker.engine.settings.get(PUBLISH_LAST_CHANNEL_ID),
			self, self._on_last_channel_loaded, noop,
		)
		# опрос очереди живёт всегда (не только при видимой странице):
		# завершения снимаются с показа, а кэш занятости нужен при выходе
		self._queue_timer = QTimer(self)
		self._queue_timer.setInterval(_QUEUE_POLL_MS)
		self._queue_timer.timeout.connect(self._poll_queue)
		self._queue_timer.start()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
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
		"""Кнопка отправки и панель очереди отправки под ней."""
		row = QHBoxLayout()
		self._send_button = PrimaryPushButton(FluentIcon.SEND, "Отправить", self)
		self._send_button.clicked.connect(self._on_send)
		row.addWidget(self._send_button)
		row.addStretch()
		layout.addLayout(row)
		self._queue_box = QVBoxLayout()
		self._queue_box.setSpacing(8)
		layout.addLayout(self._queue_box)

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
		"""Обновляет список каналов, сохраняя текущий выбор.

		Выключенные каналы (настройка ``enabled``) в списке не показываются —
		публиковать в них нельзя, пока их не включат на странице «Каналы».
		"""
		selected = int(self._channel_combo.currentIndex())
		channels = [channel for channel in channels if channel.enabled]
		self._channels = channels
		self._channel_combo.clear()
		for channel in channels:
			self._channel_combo.addItem(channel.title)
		if 0 <= selected < len(channels):
			self._channel_combo.setCurrentIndex(selected)
		self._apply_channel_restore()
		self._on_channel_changed()

	def _on_last_channel_loaded(self, channel_id: object) -> None:
		"""Пришёл канал прошлой публикации — применяем, если список готов."""
		self._restore_channel_id = channel_id if isinstance(channel_id, int) else None
		self._apply_channel_restore()

	def _apply_channel_restore(self) -> None:
		"""Предвыбирает канал прошлой публикации (один раз)."""
		if self._restore_channel_id is None:
			return
		ids = [channel.id for channel in self._channels]
		if self._restore_channel_id in ids:
			self._channel_combo.setCurrentIndex(ids.index(self._restore_channel_id))
			self._restore_channel_id = None
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
				f"до {USERBOT_MAX_FILE_BYTES // 2**30} ГБ, "
				"«сейчас» и отложенные."
			)
			self._when_row.set_schedule_allowed(True)
		elif caps.bot:
			self._caps_hint.setText(
				f"Публикация через бота: файлы до {BOT_MAX_FILE_BYTES // 2**20} "
				"МБ, только «сейчас» (для отложенных нужен userbot-админ)."
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
		"""Диалог выбора вложения с фильтром по текущему типу контента."""
		file_filter = next(f for _l, k, f in _KINDS if k is self._kind)
		path = pick_file(self, "Файл вложения", file_filter)
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

	# --- отправка через очередь ---------------------------------------------------

	def _on_send(self) -> None:
		"""Ставит черновик в очередь отправки; форма сразу свободна."""
		channel = self._current_channel()
		if channel is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.enqueue(self._draft(channel.id)),
			self, self._on_enqueued, self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(PUBLISH_LAST_CHANNEL_ID, channel.id),
			self, noop, noop,
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

	def _on_enqueued(self, _item_id: object = None) -> None:
		"""Черновик принят в очередь — чистим форму под следующий пост."""
		self._text.clear()
		self._file_edit.clear()
		self._poll_queue()  # панель очереди обновляется сразу, не по таймеру

	# --- панель очереди -------------------------------------------------------------

	def queue_busy(self) -> bool:
		"""Есть ли неотправленное в очереди (для подтверждения выхода)."""
		return self._queue_busy

	def _poll_queue(self) -> None:
		"""Запрашивает состояние очереди (по таймеру и после постановки)."""
		# ошибки опроса не показываем плашками: мост пишет их в лог,
		# а раз в полсекунды спамить пользователя нечем и незачем
		run_in_engine(
			self._worker, self._worker.engine.publish_queue.state(),
			self, self._show_queue, noop,
		)

	def _show_queue(self, items: list[QueueItemDto]) -> None:
		"""Обновляет панель очереди; завершённые — плашка и снятие с показа."""
		visible: list[QueueItemDto] = []
		for item in items:
			if item.status is QueueItemStatus.DONE:
				self._finish_item(item, notify=True)
			elif item.status is QueueItemStatus.CANCELLED:
				self._finish_item(item, notify=False)
			else:
				visible.append(item)
		self._queue_busy = any(
			item.status in (QueueItemStatus.PENDING, QueueItemStatus.SENDING)
			for item in visible
		)
		signature = tuple((i.id, i.status, i.error) for i in visible)
		if signature != self._queue_signature:
			self._queue_signature = signature
			self._rebuild_queue(visible)
		for item in visible:  # прогресс — без пересборки карточек
			bar = self._queue_bars.get(item.id)
			if bar is not None:
				bar.setValue(int(item.progress * 100))

	def _finish_item(self, item: QueueItemDto, notify: bool) -> None:
		"""Показывает итог завершённого элемента и снимает его с показа."""
		if item.id in self._handled_ids:
			return  # уже показали; ждём, пока движок уберёт из состояния
		self._handled_ids.add(item.id)
		if notify:
			InfoBar.success(
				"Отложенная запись создана" if item.scheduled else "Опубликовано",
				item.title, parent=self.window(),
			)
		else:
			InfoBar.info("Отправка отменена", item.title, parent=self.window())
		self._dismiss(item.id)

	def _rebuild_queue(self, items: list[QueueItemDto]) -> None:
		"""Перестраивает карточки очереди (только при смене состава/статусов)."""
		clear_layout(self._queue_box)
		self._queue_bars = {}
		for item in items:
			self._queue_box.addWidget(self._queue_row(item))

	def _queue_row(self, item: QueueItemDto) -> CardWidget:
		"""Карточка элемента очереди: статус, прогресс, отмена/убрать."""
		trailing = QWidget(self)
		row = QHBoxLayout(trailing)
		row.setContentsMargins(0, 0, 0, 0)
		if item.status is QueueItemStatus.SENDING:
			bar = ProgressBar(trailing)
			bar.setRange(0, 100)
			bar.setValue(int(item.progress * 100))
			bar.setFixedWidth(160)
			row.addWidget(bar)
			self._queue_bars[item.id] = bar
		if item.status is QueueItemStatus.ERROR:
			action = PushButton("Убрать", trailing)
			action.clicked.connect(bind(self._dismiss, item.id))
		else:
			action = PushButton("Отмена", trailing)
			action.clicked.connect(bind(self._cancel_item, item.id))
		row.addWidget(action)
		return row_card(
			self, item.title, self._queue_subtitle(item), trailing=trailing
		)

	@staticmethod
	def _queue_subtitle(item: QueueItemDto) -> str:
		"""Подпись карточки: канал и человекочитаемый статус."""
		if item.status is QueueItemStatus.SENDING:
			status = "отправляется"
		elif item.status is QueueItemStatus.ERROR:
			status = f"ошибка: {item.error}"
		else:
			status = "в очереди"
			if item.scheduled:
				status += " · отложенный"
		return f"{item.channel_title} · {status}"

	def _cancel_item(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди."""
		run_in_engine(
			self._worker, self._worker.engine.publish_queue.cancel(item_id),
			self, noop, self._show_error,
		)

	def _dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из состояния очереди."""
		run_in_engine(
			self._worker, self._worker.engine.publish_queue.dismiss(item_id),
			self, noop, noop,
		)
