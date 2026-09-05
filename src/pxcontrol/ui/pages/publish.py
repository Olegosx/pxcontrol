"""Страница «Публикация»: единая точка создания постов всех типов.

Тип контента выбирается сегментами (текст/фото/видео/аудио/файл).
Отправка идёт через очередь движка (ADR-0016): «Отправить» ставит пост
в хвост и сразу освобождает форму под следующий; очередь видна на
странице, каждый элемент можно отменить.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CheckBox,
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
from pxcontrol.engine.services.captions import (
	CaptionLine,
	TemplateDto,
	TitleParseRules,
	title_from_filename,
)
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import (
	BOT_MAX_FILE_BYTES,
	PostDraft,
	publish_capabilities,
)
from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.engine.services.settings import (
	PUBLISH_LAST_CHANNEL_ID,
	PUBLISH_TIMES,
	TITLE_PARSE_RULES,
)
from pxcontrol.engine.services.video import ReadyVideo, VideoDirs, video_dialog_filter
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.captions import CaptionDialog, FieldsDialog
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	QueuePanel,
	WhenRow,
	error_reporter,
	exec_dialog,
	noop,
	page_layout,
	pick_dir,
	pick_file,
	show_warning,
)
from pxcontrol.ui.pages.publish_batch import PublishBatchDialog
from pxcontrol.ui.pages.publish_queue_view import QueueViewDialog, queue_subtitle

logger = logging.getLogger(__name__)

#: Сколько карточек очереди показывать на странице (хвост ждущих —
#: в сводке числом; всё целиком — кнопка «Вся очередь…», ADR-0016).
_QUEUE_MAX_CARDS = 20


@dataclass
class _BatchSetup:
	"""Собираемые данные пакета отправки (ADR-0015).

	Заполняется по шагам цепочки колбэков (папка → сканирование →
	общий шаблон подписи → времена канала → лимит файла), чтобы
	не таскать длинный список аргументов через каждую функцию.
	"""

	channel: ChannelDto
	root: str
	files: list[ReadyVideo] = field(default_factory=list)
	caption_lines: list[CaptionLine] | None = None
	filename_template_id: int | None = None
	used_values: dict[int, list[str]] = field(default_factory=dict)
	times: list[str] = field(default_factory=list)
	busy: list[datetime] = field(default_factory=list)  # отложки канала (UTC)
	title_rules: TitleParseRules = field(default_factory=TitleParseRules)


#: Сегменты типов контента: подпись → тип → фильтр диалога выбора файла.
_KINDS: list[tuple[str, MediaKind, str]] = [
	("Текст", MediaKind.NONE, ""),
	("Фото", MediaKind.PHOTO, "Изображения (*.png *.jpg *.jpeg *.webp)"),
	("Видео", MediaKind.VIDEO, video_dialog_filter()),
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
		# канал прошлой публикации: предвыбор после загрузки списка
		self._restore_channel_id: int | None = None
		self._kind = MediaKind.NONE
		self._build()
		run_in_engine(
			worker,
			worker.engine.settings.get(PUBLISH_LAST_CHANNEL_ID),
			self,
			self._on_last_channel_loaded,
			noop,
		)

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		layout.addWidget(SubtitleLabel("Публикация", self))
		self._build_kind_segments(layout)
		self._channel_combo: DtoComboBox[ChannelDto] = DtoComboBox(self)
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
		"""Кнопки отправки (одиночной и пакетной) и панель очереди под ними."""
		row = QHBoxLayout()
		self._send_button = PrimaryPushButton(FluentIcon.SEND, "Отправить", self)
		self._send_button.clicked.connect(self._on_send)
		row.addWidget(self._send_button)
		batch_button = PushButton(FluentIcon.FOLDER, "Пакет из папки…", self)
		batch_button.setToolTip(
			"Собрать черновики постов из всех видео готовой папки: подписи "
			"по общему шаблону, раскладка времени, правка построчно (ADR-0015)"
		)
		batch_button.clicked.connect(self._on_batch)
		row.addWidget(batch_button)
		view_button = PushButton("Вся очередь…", self)
		view_button.setToolTip(
			"Все элементы очереди отправки с сортировкой и фильтрами "
			f"(на странице — ближайшие {_QUEUE_MAX_CARDS})"
		)
		view_button.clicked.connect(self._on_queue_view)
		row.addWidget(view_button)
		row.addStretch()
		layout.addLayout(row)
		self._queue_summary = CaptionLabel("", self)
		self._queue_summary.hide()
		layout.addWidget(self._queue_summary)
		queue_box = QVBoxLayout()
		queue_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(queue_box)
		self._queue = QueuePanel(
			self._worker,
			self,
			queue_box,
			service=lambda: self._worker.engine.publish_queue,
			subtitle=queue_subtitle,
			on_finished=self._on_queue_finished,
			on_refreshed=self._update_queue_summary,
			# длинный хвост ждущих слота (ADR-0016) не раздувает страницу;
			# всё целиком — в диалоге «Вся очередь…»
			max_cards=_QUEUE_MAX_CARDS,
		)

	def _on_queue_view(self) -> None:
		"""Открывает полный просмотр очереди (сортировка и фильтры)."""
		exec_dialog(QueueViewDialog(self._worker, self.window()))

	# --- поведение -----------------------------------------------------------------

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет список каналов при каждом открытии страницы."""
		super().showEvent(event)
		self._reload_channels()

	def prefill_media(self, kind: MediaKind, path: str, channel_id: int | None = None) -> None:
		"""Подставляет вложение (переход с других страниц, например «Видео»).

		``channel_id`` — предвыбор канала (например, выбранного на «Видео»);
		применяется тем же механизмом, что и канал прошлой публикации.
		"""
		self._segments.setCurrentItem(kind.value)
		self._on_kind_changed(kind.value)
		self._file_edit.setText(path)
		if channel_id is not None:
			self._restore_channel_id = channel_id
			self._apply_channel_restore()

	def _reload_channels(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.channels.list_channels(),
			self,
			self._show_channels,
			self._show_error,
		)

	def _show_channels(self, channels: list[ChannelDto]) -> None:
		"""Обновляет список каналов, сохраняя выбор по id канала.

		Выключенные каналы (настройка ``enabled``) в списке не показываются —
		фильтр презентационный, само правило держит движок (PostsService
		откажет выключенному каналу).
		"""
		self._channel_combo.set_items(
			[channel for channel in channels if channel.enabled],
			label=lambda channel: channel.title,
			key=lambda channel: channel.id,
		)
		# успешное восстановление само запускает обработчик смены (сигнал
		# select); явный вызов нужен только когда восстанавливать нечего.
		# Неудача на свежем списке означает устаревший id (канал выключен
		# или удалён) — забываем его, иначе предвыбор «выстрелил» бы позже,
		# при следующей загрузке списка, внезапной сменой канала
		if not self._apply_channel_restore():
			self._restore_channel_id = None
			self._on_channel_changed()

	def _on_last_channel_loaded(self, channel_id: int | None) -> None:
		"""Пришёл канал прошлой публикации — применяем, если список готов."""
		self._restore_channel_id = channel_id
		self._apply_channel_restore()

	def _apply_channel_restore(self) -> bool:
		"""Предвыбирает канал прошлой публикации (один раз).

		Returns:
			True — выбор применён; обработчик смены уже запущен сигналом
			``select`` (см. контракт DtoComboBox.select), звать его не нужно.
		"""
		wanted = self._restore_channel_id
		if wanted is None:
			return False
		if self._channel_combo.select(lambda channel: channel.id == wanted):
			self._restore_channel_id = None
			return True
		return False

	def _channel_or_none(self) -> ChannelDto | None:
		"""Выбранный канал без показа ошибок (для адаптации формы)."""
		return self._channel_combo.selected()

	def _on_channel_changed(self, _index: int = 0) -> None:
		"""Адаптирует форму под возможности и времена выбранного канала."""
		channel = self._channel_or_none()
		if channel is None:
			self._caps_hint.setText("")
			self._when_row.set_schedule_allowed(True)
			self._when_row.set_times([])
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, channel.id),
			self,
			partial(self._apply_times, channel.id),
			noop,
		)
		caps = publish_capabilities(channel.bot_id is not None, channel.userbot_admin)
		if caps.userbot:
			# лимит зависит от Premium userbot — узнаём у движка
			self._caps_hint.setText(
				"Публикация через userbot: все типы контента, «сейчас» и отложенные."
			)
			run_in_engine(
				self._worker,
				self._worker.engine.posts.userbot_limit_gb(channel.id),
				self,
				partial(self._show_userbot_limit, channel.id),
				noop,
			)
			self._when_row.set_schedule_allowed(True)
		elif caps.bot:
			self._caps_hint.setText(
				f"Публикация через бота: файлы до {BOT_MAX_FILE_BYTES // 2**20} "
				"МБ, только «сейчас» (для отложенных нужен userbot-админ)."
			)
			self._when_row.set_schedule_allowed(False, "Отложенные требуют userbot-админа в канале")
		else:
			self._caps_hint.setText(
				"⚠ Нет способа публикации — проверьте доступы на странице «Каналы»."
			)
			self._when_row.set_schedule_allowed(False, "Нет способа публикации")

	def _is_stale(self, channel_id: int) -> bool:
		"""Пришёл ли ответ движка для уже переключённого канала.

		Пока движок занят (очередь отправки в том же цикле, ADR-0016),
		ответы задерживаются: без проверки подсказка и времена канала A
		перезаписали бы уже показанные данные канала B.
		"""
		current = self._channel_or_none()
		return current is None or current.id != channel_id

	def _apply_times(self, channel_id: int, times: list[str]) -> None:
		"""Подставляет времена канала, если он всё ещё выбран."""
		if not self._is_stale(channel_id):
			self._when_row.set_times(times)

	def _show_userbot_limit(self, channel_id: int, limit_gb: int) -> None:
		"""Дописывает лимит файла в подсказку (2 ГБ; 4 — с Premium)."""
		if self._is_stale(channel_id):
			return
		premium = " (Premium)" if limit_gb >= 4 else ""
		self._caps_hint.setText(
			"Публикация через userbot: все типы контента, файлы "
			f"до {limit_gb} ГБ{premium}, «сейчас» и отложенные."
		)

	def _on_kind_changed(self, kind_key: str) -> None:
		"""Меняет состав формы под выбранный тип контента."""
		self._kind = MediaKind(kind_key)
		is_text = self._kind is MediaKind.NONE
		self._file_box.setVisible(not is_text)
		self._text.setPlaceholderText(
			"Текст поста…" if is_text else "Подпись к файлу (необязательно)…"
		)

	def _pick_file(self) -> None:
		"""Диалог выбора вложения с фильтром по текущему типу контента.

		Для видео диалог открывается в папке результатов обработки
		выбранного канала (подпапка его пресета по умолчанию); для
		остальных типов стартовая папка — на усмотрение Qt (позже).
		"""
		if self._kind is MediaKind.VIDEO:
			channel = self._channel_or_none()
			if channel is not None:
				run_in_engine(
					self._worker,
					self._worker.engine.video.processed_dir_for_channel(channel.id),
					self,
					self._open_file_dialog,
					self._show_error,
				)
			else:
				run_in_engine(
					self._worker,
					self._worker.engine.video.dirs_for(""),
					self,
					self._open_file_dialog,
					self._show_error,
				)
			return
		self._open_file_dialog("")

	def _open_file_dialog(self, start: str | VideoDirs) -> None:
		"""Открывает диалог вложения; ``start`` — папка или VideoDirs."""
		start_dir = start.processed if isinstance(start, VideoDirs) else start
		file_filter = next(f for _l, k, f in _KINDS if k is self._kind)
		path = pick_file(self, "Файл вложения", file_filter, start_dir=start_dir)
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
			exec_dialog(FieldsDialog(self._worker, channel.id, channel.title, self.window()))

	def _on_compose_caption(self) -> None:
		"""Загружает шаблоны канала и открывает диалог сборки."""
		channel = self._current_channel()
		if channel is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(channel.id),
			self,
			self._open_caption_dialog,
			self._show_error,
		)

	def _open_caption_dialog(self, templates: list[TemplateDto]) -> None:
		"""Собирает подпись по шаблону и вставляет её в поле текста."""
		# пустой (например, только что созданный) шаблон не должен
		# блокировать сборку по остальным — в диалог идут пригодные
		usable = [template for template in templates if template.fields]
		if not usable:
			self._show_error("Сначала настройте поля и шаблон — кнопка «Поля подписи…».")
			return
		media = str(self._file_edit.text()).strip()
		title = ""
		if self._kind is not MediaKind.NONE and media:
			title = title_from_filename(media)
		dialog = CaptionDialog(usable, title, self.window())
		if not exec_dialog(dialog):
			return
		self._text.setPlainText(dialog.caption())
		self._record_template_usage(dialog.template_id(), dialog.used_values())
		self._suggest_rename(templates, dialog, media)

	def _record_template_usage(self, template_id: int, used_values: dict[int, list[str]]) -> None:
		"""Запоминает использованные значения шаблона (для предвыбора)."""
		run_in_engine(
			self._worker,
			self._worker.engine.captions.record_usage(template_id, used_values),
			self,
			noop,
			self._show_error,
		)

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
				template.id,
				channel.id,
				dialog.title(),
				dialog.used_values(),
				media,
			),
			self,
			self._show_rename_suggestion,
			self._show_error,
		)

	def _show_rename_suggestion(self, filename: str) -> None:
		"""Показывает строку переименования с вычисленным именем."""
		self._rename_edit.setText(filename)
		self._rename_check.setChecked(True)
		self._rename_box.show()

	# --- пакет из папки (ADR-0015) --------------------------------------------------

	def _on_batch(self) -> None:
		"""Пакетная отправка: канал → папка → сканирование → черновики."""
		channel = self._current_channel()
		if channel is None:
			return
		caps = publish_capabilities(channel.bot_id is not None, channel.userbot_admin)
		if not (caps.userbot or caps.bot):
			self._show_error("Нет способа публикации — проверьте доступы на странице «Каналы».")
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.processed_dir_for_channel(channel.id),
			self,
			partial(self._pick_batch_dir, channel),
			self._show_error,
		)

	def _pick_batch_dir(self, channel: ChannelDto, start_dir: str) -> None:
		"""Выбор готовой папки (по умолчанию — папка результатов канала)."""
		root = pick_dir(self, "Готовая папка с видео", start_dir=start_dir)
		if root:
			self._scan_batch_root(channel, root)

	def _scan_batch_root(self, channel: ChannelDto, root: str) -> None:
		"""Сканирует готовую папку и продолжает цепочку пакета."""
		setup = _BatchSetup(channel, root)
		run_in_engine(
			self._worker,
			self._worker.engine.video.scan_ready(root),
			self,
			partial(self._on_batch_scanned, setup),
			self._show_error,
		)

	def start_batch_with_folder(self, root: str, channel_id: int) -> None:
		"""Пакет из папки, выбранной на другой странице («Видео»).

		Вход с чужой страницы: канал приходит её id (0 — не выбран)
		и предвыбирается в списке каналов этой страницы.
		"""
		channel = self._batch_channel(channel_id)
		if channel is not None:
			self._scan_batch_root(channel, root)

	def start_batch_with_files(self, paths: list[str], channel_id: int) -> None:
		"""Пакет из готового списка файлов (выбор на странице «Видео»).

		Сборку списка (размеры, пропуск исчезнувших, порядок) делает
		движок — источник пакета держит он (ADR-0015), страница только
		показывает результат.
		"""
		channel = self._batch_channel(channel_id)
		if channel is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.ready_from_paths(paths),
			self,
			partial(self._on_batch_files_ready, channel),
			self._show_error,
		)

	def _on_batch_files_ready(self, channel: ChannelDto, files: list[ReadyVideo]) -> None:
		"""Список собран движком — дальше обычная цепочка пакета."""
		if not files:
			self._show_error("Файлы не найдены на диске — публиковать нечего.")
			return
		root = str(Path(files[0].path).parent)
		self._on_batch_scanned(_BatchSetup(channel, root), files)

	def _batch_channel(self, channel_id: int) -> ChannelDto | None:
		"""Канал пакета по id с другой страницы (с предвыбором в списке).

		Страница «Видео» показывает все каналы, а этот список — только
		включённые: неудачный предвыбор означает, что канал недоступен
		для публикации, и пакет отменяется. Иначе выбор молча остался бы
		на прежнем канале и пакет ушёл бы не туда.
		"""
		if channel_id and not self._channel_combo.select(lambda channel: channel.id == channel_id):
			self._show_error(
				"Канал недоступен для публикации (выключен или список каналов "
				"ещё загружается) — проверьте настройки канала и повторите."
			)
			return None
		channel = self._channel_or_none()
		if channel is None:
			self._show_error(
				"Канал не выбран (или список каналов ещё загружается) — выберите канал и повторите."
			)
		return channel

	def _on_batch_scanned(self, setup: _BatchSetup, files: list[ReadyVideo]) -> None:
		"""Файлы найдены — общий шаблон подписи (если шаблоны настроены)."""
		if not files:
			InfoBar.info(
				"Видео не найдено",
				f"В папке нет видеофайлов (включая вложенные): {setup.root}",
				parent=self,
			)
			return
		setup.files = files
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(setup.channel.id),
			self,
			partial(self._batch_caption_pass, setup),
			self._show_error,
		)

	def _batch_caption_pass(self, setup: _BatchSetup, templates: list[TemplateDto]) -> None:
		"""Один проход диалога подписи: шаблон и общие значения на весь пакет.

		Название у каждой строки будет своё (из имени файла), поэтому поле
		названия в диалоге пустое. Отмена диалога — пакет без подписей,
		а не отмена пакета: подписи правятся построчно дальше.
		"""
		usable = [template for template in templates if template.fields]
		if usable:
			dialog = CaptionDialog(usable, "", self.window())
			if exec_dialog(dialog):
				setup.caption_lines = dialog.lines()
				setup.used_values = dialog.used_values()
				template = next(t for t in usable if t.id == dialog.template_id())
				if template.filename_pattern:
					setup.filename_template_id = template.id
				self._record_template_usage(template.id, setup.used_values)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, setup.channel.id),
			self,
			partial(self._batch_times_loaded, setup),
			self._show_error,
		)

	def _batch_times_loaded(self, setup: _BatchSetup, times: list[str]) -> None:
		"""Времена канала получены — читаем существующие отложки.

		Раскладка пропускает занятые слоты, поэтому диалогу нужны
		времена уже созданных в Telegram отложенных записей канала.
		"""
		setup.times = times
		run_in_engine(
			self._worker,
			self._worker.engine.posts.scheduled_times(setup.channel.id),
			self,
			partial(self._batch_scheduled_loaded, setup),
			partial(self._batch_scheduled_failed, setup),
		)

	def _batch_scheduled_failed(self, setup: _BatchSetup, message: str) -> None:
		"""Отложки не прочитались — пакет продолжается без их учёта.

		Проверка занятых слотов вспомогательная: отказ userbot не должен
		блокировать пакет, но о слепой раскладке честно предупреждаем.
		"""
		show_warning(
			self,
			"Отложки не прочитаны",
			f"Раскладка не учтёт существующие отложки: {message}",
		)
		self._batch_scheduled_loaded(setup, [])

	def _batch_scheduled_loaded(self, setup: _BatchSetup, scheduled: list[datetime]) -> None:
		"""Отложки получены — заготовка правил разбора имени файла."""
		setup.busy = scheduled
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(TITLE_PARSE_RULES, setup.channel.id),
			self,
			partial(self._batch_rules_loaded, setup),
			self._show_error,
		)

	def _batch_rules_loaded(self, setup: _BatchSetup, tokens: list[str]) -> None:
		"""Правила разбора получены — осталась граница размера файла."""
		setup.title_rules = TitleParseRules.from_tokens(tokens)
		caps = publish_capabilities(setup.channel.bot_id is not None, setup.channel.userbot_admin)
		if caps.userbot:
			run_in_engine(
				self._worker,
				self._worker.engine.posts.userbot_limit_bytes(setup.channel.id),
				self,
				partial(self._open_batch_dialog, setup, True),
				self._show_error,
			)
		else:
			# запасной бот-путь: лимит 50 МБ и только «сейчас» (ADR-0011)
			self._open_batch_dialog(setup, False, BOT_MAX_FILE_BYTES)

	def _open_batch_dialog(
		self, setup: _BatchSetup, schedule_allowed: bool, limit_bytes: int
	) -> None:
		"""Показывает черновики пакета; принятые ставит в очередь отправки."""
		dialog = PublishBatchDialog(
			self._worker,
			setup.channel,
			setup.root,
			setup.files,
			self.window(),
			caption_lines=setup.caption_lines,
			filename_template_id=setup.filename_template_id,
			used_values=setup.used_values,
			channel_times=setup.times,
			limit_bytes=limit_bytes,
			schedule_allowed=schedule_allowed,
			title_rules=setup.title_rules,
			# отложки приходят из Telegram в UTC, раскладка живёт
			# в местном наивном времени — как ввод пользователя
			busy=[moment.astimezone().replace(tzinfo=None) for moment in setup.busy],
		)
		if not exec_dialog(dialog):
			return
		try:
			drafts = dialog.drafts(setup.channel.id)
		except ValueError as exc:  # страховка: validate диалога это уже проверил
			self._show_error(str(exc))
			return
		if not drafts:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.enqueue_many(drafts),
			self,
			partial(self._on_batch_enqueued, len(drafts)),
			self._show_error,
		)

	def _on_batch_enqueued(self, count: int, _ids: list[int]) -> None:
		"""Пакет принят в очередь — карточки видны сразу, не по таймеру."""
		InfoBar.success("Пакет в очереди", f"Постов: {count}", parent=self)
		self._queue.poll()

	# --- отправка через очередь ---------------------------------------------------

	def _on_send(self) -> None:
		"""Ставит черновик в очередь отправки; форма сразу свободна."""
		channel = self._current_channel()
		if channel is None:
			return
		try:
			draft = self._draft(channel.id)
		except ValueError as exc:  # поля формы не согласованы (файл, время)
			self._show_error(str(exc))
			return
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.enqueue(draft),
			self,
			self._on_enqueued,
			self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(PUBLISH_LAST_CHANNEL_ID, channel.id),
			self,
			noop,
			noop,
		)

	def _draft(self, channel_id: int) -> PostDraft:
		"""Собирает черновик публикации из полей формы.

		Raises:
			ValueError: Поля формы не согласованы: выбран тип с вложением,
				а файл не указан, либо время публикации не «ЧЧ:ММ».
		"""
		media = str(self._file_edit.text()).strip() or None
		is_text = self._kind is MediaKind.NONE
		if not is_text and media is None:
			label = next(name for name, kind, _f in _KINDS if kind is self._kind)
			raise ValueError(
				f"Выбран тип «{label}», а файл не указан — "
				"выберите файл или переключитесь на «Текст»."
			)
		return PostDraft(
			channel_id=channel_id,
			text=str(self._text.toPlainText()).strip(),
			media_path=None if is_text else media,
			media_kind=MediaKind.NONE if is_text else self._kind,
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
		self._queue.poll()  # панель очереди обновляется сразу, не по таймеру

	# --- панель очереди -------------------------------------------------------------

	def upload_active(self) -> bool:
		"""Идёт ли отправка прямо сейчас (для подтверждения выхода).

		Ждущие и готовые к отправке не в счёт: очередь персистентна
		(ADR-0016), при выходе они сохранятся и продолжатся при
		следующем запуске.
		"""
		return self._queue.active()

	def _on_queue_finished(self, item: QueueItemDto, done: bool) -> None:
		"""Итоговая плашка завершённого элемента.

		Родитель — окно: опрос живёт всегда, и завершение может прийти
		при скрытой странице — плашка на ней погасла бы незамеченной.
		"""
		if done:
			InfoBar.success(
				"Отложенная запись создана" if item.scheduled else "Опубликовано",
				item.title,
				parent=self.window(),
			)
		else:
			InfoBar.info("Отправка отменена", item.title, parent=self.window())

	def _update_queue_summary(self, items: list[QueueItemDto]) -> None:
		"""Сводка над карточками: отправка, очередь, ждущие слота, ошибки."""
		if not items:
			self._queue_summary.hide()
			return
		waiting = sum(1 for item in items if item.status is QueueItemStatus.WAITING)
		pending = sum(
			1 for item in items if item.status in (QueueItemStatus.PENDING, QueueItemStatus.SENDING)
		)
		errors = sum(1 for item in items if item.status is QueueItemStatus.ERROR)
		parts = []
		if pending:
			parts.append(f"к отправке {pending}")
		if waiting:
			parts.append(f"ждут слота отложек {waiting}")
		if errors:
			parts.append(f"ошибок {errors}")
		text = "Очередь отправки: " + ", ".join(parts)
		if len(items) > _QUEUE_MAX_CARDS:
			text += f" · показаны ближайшие {_QUEUE_MAX_CARDS}"
		self._queue_summary.setText(text)
		self._queue_summary.show()
