"""Страница «Видео»: список файлов с параметрами и очередь подготовки.

Источник — две равнозначные кнопки: «Добавить файл…» (каждый выбор
добавляет карточку в список) и «Добавить папку…» (диалог сканирования
:mod:`video_batch`; отмеченные становятся карточками пакета). У каждого
файла — своя карточка параметров, заполненная из карточки-шаблона
«[Параметры пресета]» (под строкой пресета) в момент добавления;
шаблон правится всегда и служит пресетам («загрузчик»: выбор пресета
заполняет шаблон, сохранение — по явным кнопкам). «Обработать» ставит
в очередь движка (ADR-0014) файлы, отмеченные чекбоксами в шапках
карточек, — каждый со своими параметрами; карточки очереди видны
на странице. Результат — файл в папке результатов; кнопка
«Опубликовать…» передаёт его странице «Публикация» (контракт — путь
к файлу). Выбор кадра заставки — отдельный диалог (:mod:`frame_picker`).
"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	CheckBox,
	FluentIcon,
	InfoBar,
	PrimaryPushButton,
	ProgressBar,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.errors import user_message
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.settings import CHANNEL_DEFAULT_PRESET
from pxcontrol.engine.services.video import (
	BitrateAdvice,
	IntroSourceKind,
	PresetDto,
	PresetFields,
	ProcessedListing,
	ProcessedVideo,
	VideoDirs,
	build_intro_source,
	parse_intro_source,
)
from pxcontrol.engine.services.video_queue import (
	ProcessingRequest,
	VideoItemDto,
	VideoItemStatus,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	FormDialog,
	bind,
	clear_layout,
	confirm_delete,
	exec_dialog,
	format_local,
	human_size,
	noop,
	page_layout,
	pick_dir,
	pick_file,
	row_card,
	show_error,
)
from pxcontrol.ui.pages.frame_picker import FramePickerDialog
from pxcontrol.ui.pages.video_batch import BatchScanDialog
from pxcontrol.ui.pages.video_form import CollapsibleCard, PresetForm

#: Имя «пресета» в имени файла результата, когда пресет не выбран.
_MANUAL_NAME = "ручные"

#: Период опроса состояния очереди обработки (мс) — как у очереди отправки.
_QUEUE_POLL_MS = 500

#: Сколько ждать копирования выбранного кадра в папку очереди (сек).
_STASH_TIMEOUT_S = 10.0


class _AbortRun(Exception):  # noqa: N818 — служебный сигнал, не ошибка
	"""Служебный сигнал прохода выбора кадров: отменить постановку целиком."""


class _FileEntry:
	"""Карточка файла в списке подготовки: шапка + свои параметры.

	Шапка — имя и размер файла, пометки (пакет, авто-битрейт), чекбокс
	«обрабатывать» (по умолчанию выключен: на обработку уходят только
	отмеченные) и кнопки «посмотреть» / «убрать из списка»; тело —
	собственная панель параметров (:class:`PresetForm`), заполненная
	из шаблона в момент добавления и правимая независимо.
	"""

	def __init__(
		self,
		page: VideoPage,
		path: str,
		size_bytes: int,
		batch: str,
		preset_name: str,
	) -> None:
		self.path = path
		self.batch = batch  # подпапка пакета («» — одиночное добавление)
		self.preset_name = preset_name  # имя параметров на момент добавления
		self.advice_note = ""  # пометка авто-битрейта (после совета движка)
		trailing = QWidget()
		buttons = QHBoxLayout(trailing)
		buttons.setContentsMargins(0, 0, 0, 0)
		buttons.setSpacing(4)
		self.check = CheckBox("", trailing)
		self.check.setToolTip("Отправить файл на обработку («Обработать» берёт отмеченные)")
		buttons.addWidget(self.check)
		play = TransparentToolButton(FluentIcon.PLAY, trailing)
		play.setToolTip("Посмотреть файл (системный плеер)")
		play.clicked.connect(bind(page._open_path, path))  # noqa: SLF001 — внутренний класс страницы
		buttons.addWidget(play)
		remove = TransparentToolButton(FluentIcon.DELETE, trailing)
		remove.setToolTip("Убрать из списка (файл на диске не трогается)")
		remove.clicked.connect(bind(page._remove_entry, self))  # noqa: SLF001
		buttons.addWidget(remove)
		title = f"{Path(path).name} — {human_size(size_bytes)}"
		self.card = CollapsibleCard(title, page, trailing=trailing)
		self.form = PresetForm(page)
		self.card.body.addWidget(self.form)
		self.refresh_summary()

	def refresh_summary(self) -> None:
		"""Сводка шапки: пакет и пометка авто-битрейта (видна у свёрнутой)."""
		parts = []
		if self.batch:
			parts.append(f"пакет «{self.batch}»")
		if self.advice_note:
			parts.append(self.advice_note)
		self.card.set_summary(" · ".join(parts))


class VideoPage(ScrollArea):
	"""Панель параметров обработки и подготовка видеофайла."""

	#: Просьба опубликовать готовый файл: путь и id канала со страницы
	#: (0 — канал не выбран). Ловит главное окно → «Публикация».
	publish_requested = Signal(str, int)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("video")
		self._worker = worker
		self._queue_signature: tuple[tuple[int, VideoItemStatus, str | None, str | None], ...] = ()
		self._queue_bars: dict[int, ProgressBar] = {}
		self._queue_busy = False
		self._handled_ids: set[int] = set()  # завершённые, уже учтённые
		self._session_done = 0  # готовых с последней итоговой плашки
		self._entries: list[_FileEntry] = []  # карточки файлов к обработке
		self._build()
		self._reload_presets()
		# опрос очереди живёт всегда (не только при видимой странице):
		# кэш занятости нужен подтверждению выхода из приложения
		self._queue_timer = QTimer(self)
		self._queue_timer.setInterval(_QUEUE_POLL_MS)
		self._queue_timer.timeout.connect(self._poll_queue)
		self._queue_timer.start()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет каналы и готовые видео при каждом открытии страницы.

		Список перечитывается с диска: файлы могли уехать в опубликованные
		(после отправки поста) или измениться мимо приложения.
		"""
		super().showEvent(event)
		self._reload_channels()
		self._reload_processed()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		layout.addWidget(SubtitleLabel("Подготовка видео", self))
		self._build_source_row(layout)
		self._build_channel_row(layout)
		self._build_preset_row(layout)
		self._build_template_card(layout)
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Файлы и параметры обработки", self))
		self._empty_hint = CaptionLabel("Файлы не выбраны", self)
		layout.addWidget(self._empty_hint)
		self._files_box = QVBoxLayout()
		self._files_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._files_box)
		self._build_process_row(layout)
		self._build_queue_block(layout)
		self._build_processed_block(layout)
		layout.addStretch()

	def _build_template_card(self, layout: QVBoxLayout) -> None:
		"""Карточка-шаблон «[Параметры пресета]» — сразу под строкой пресета.

		Параметры без файла: правятся и сохраняются в пресеты всегда,
		а добавляемые файлы получают их снимок в свои карточки.
		"""
		self._template_card = CollapsibleCard("[Параметры пресета]", self)
		self._template_card.set_summary("шаблон: эти параметры получат добавляемые файлы")
		self._form = PresetForm(self)
		self._template_card.body.addWidget(self._form)
		layout.addWidget(self._template_card)

	def _update_empty_hint(self) -> None:
		"""Заглушка «Файлы не выбраны» видна только при пустом списке."""
		self._empty_hint.setVisible(not self._entries)

	def _build_queue_block(self, layout: QVBoxLayout) -> None:
		"""Панель очереди обработки: итоговая строка и карточки элементов."""
		self._queue_summary = CaptionLabel("", self)
		self._queue_summary.hide()
		layout.addWidget(self._queue_summary)
		self._queue_box = QVBoxLayout()
		self._queue_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._queue_box)

	def _build_processed_block(self, layout: QVBoxLayout) -> None:
		"""Раздел готовых видео: папка результатов текущей подпапки."""
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Готовые видео", self))
		self._processed_hint = CaptionLabel("", self)
		self._processed_hint.setWordWrap(True)
		layout.addWidget(self._processed_hint)
		self._result_box = QVBoxLayout()
		layout.addLayout(self._result_box)
		# список идёт за подпапкой: она задаёт папку, куда уйдёт результат
		self._form.subdir_changed.connect(self._reload_processed)

	def _build_source_row(self, layout: QVBoxLayout) -> None:
		"""Источник: две равнозначные кнопки — файл или папка.

		Каждый выбранный файл добавляется карточкой в список (не заменяет
		прежний); папка добавляет пачку через диалог сканирования.
		"""
		src_row = QHBoxLayout()
		add_file = PushButton(FluentIcon.VIDEO, "Добавить файл…", self)
		add_file.setToolTip("Выбрать видеофайл — он добавится карточкой в список ниже")
		add_file.clicked.connect(self._add_file)
		src_row.addWidget(add_file)
		add_folder = PushButton(FluentIcon.FOLDER, "Добавить папку…", self)
		add_folder.setToolTip(
			"Рекурсивно найти видео в папке и добавить выбранные в список "
			"(результаты пакета — в его подпапке)"
		)
		add_folder.clicked.connect(self._add_folder)
		src_row.addWidget(add_folder)
		src_row.addStretch()
		layout.addLayout(src_row)

	def _build_channel_row(self, layout: QVBoxLayout) -> None:
		"""Канал: выбор подставляет его пресет по умолчанию (настройка канала)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Канал:", self))
		self._channel_combo: DtoComboBox[ChannelDto] = DtoComboBox(self, placeholder="(не выбран)")
		self._channel_combo.setToolTip(
			"Выбор канала загружает его пресет по умолчанию "
			"(задаётся на странице «Каналы» → «Пресет…»)"
		)
		self._channel_combo.currentIndexChanged.connect(self._on_channel_selected)
		row.addWidget(self._channel_combo, stretch=1)
		layout.addLayout(row)

	def _build_preset_row(self, layout: QVBoxLayout) -> None:
		"""Пресет: выбор-загрузка и кнопки сохранения/удаления."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Пресет:", self))
		self._preset_combo: DtoComboBox[PresetDto] = DtoComboBox(
			self, placeholder="(свои настройки)"
		)
		self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
		row.addWidget(self._preset_combo, stretch=1)
		self._save_button = PushButton(FluentIcon.SAVE, "Сохранить", self)
		self._save_button.clicked.connect(self._on_save_preset)
		row.addWidget(self._save_button)
		save_as = PushButton("Сохранить как…", self)
		save_as.clicked.connect(self._on_save_preset_as)
		row.addWidget(save_as)
		self._delete_button = PushButton(FluentIcon.DELETE, "Удалить", self)
		self._delete_button.clicked.connect(self._on_delete_preset)
		row.addWidget(self._delete_button)
		layout.addLayout(row)

	def _build_process_row(self, layout: QVBoxLayout) -> None:
		run_row = QHBoxLayout()
		self._process_button = PrimaryPushButton(FluentIcon.PLAY, "Обработать", self)
		self._process_button.clicked.connect(self._on_process)
		run_row.addWidget(self._process_button)
		run_row.addStretch()
		layout.addLayout(run_row)

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку всплывающей плашкой."""
		show_error(self, message)

	# --- пресеты -------------------------------------------------------------------

	def _reload_presets(self, select_name: str | None = None) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_presets(),
			self,
			partial(self._show_presets, select_name),
			self._show_error,
		)

	def _show_presets(self, select_name: str | None, presets: list[PresetDto]) -> None:
		"""Наполняет список пресетов (выбор сохраняется по id пресета)."""
		self._preset_combo.set_items(
			presets,
			label=lambda preset: preset.name,
			key=lambda preset: preset.id,
		)
		if select_name is not None:
			self._preset_combo.select(lambda preset: preset.name == select_name)
		self._update_preset_buttons()

	# --- канал и его пресет по умолчанию -------------------------------------------

	def _reload_channels(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.channels.list_channels(),
			self,
			self._show_channels,
			self._show_error,
		)

	def _show_channels(self, channels: list[ChannelDto]) -> None:
		"""Наполняет список каналов (выбор сохраняется по id канала)."""
		self._channel_combo.set_items(
			channels,
			label=lambda channel: channel.title,
			key=lambda channel: channel.id,
		)

	def _on_channel_selected(self, _index: int) -> None:
		"""Выбор канала — загрузка его пресета по умолчанию в панель."""
		channel = self._channel_combo.selected()
		if channel is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(CHANNEL_DEFAULT_PRESET, channel.id),
			self,
			partial(self._apply_channel_preset, channel),
			self._show_error,
		)

	def _apply_channel_preset(self, channel: ChannelDto, preset_id: int | None) -> None:
		"""Подставляет пресет канала; нет пресета — форма не трогается.

		Выбор в списке вызывает ``_on_preset_selected`` — панель заполнится.
		Ссылка на удалённый пресет равнозначна «не задан».
		"""
		if preset_id is None or not self._preset_combo.select(
			lambda preset: preset.id == preset_id
		):
			InfoBar.info(
				"Пресет не задан",
				f"У канала «{channel.title}» нет пресета по умолчанию — "
				"задайте его на странице «Каналы» → «Пресет…».",
				parent=self,
			)

	def _update_preset_buttons(self) -> None:
		"""«Сохранить»/«Удалить» доступны только при выбранном пресете."""
		has_preset = self._preset_combo.selected() is not None
		self._save_button.setEnabled(has_preset)
		self._delete_button.setEnabled(has_preset)

	def _on_preset_selected(self, _index: int) -> None:
		"""Выбор пресета — загрузка его значений в панель."""
		self._update_preset_buttons()
		preset = self._preset_combo.selected()
		if preset is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.get_preset_fields(preset.id),
			self,
			self._form.fill,
			self._show_error,
		)

	def _on_save_preset(self) -> None:
		"""Перезаписывает выбранный пресет текущим состоянием панели."""
		preset = self._preset_combo.selected()
		if preset is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.save_preset(self._form.fields(preset.name), preset.id),
			self,
			self._on_preset_saved,
			self._show_error,
		)

	def _on_save_preset_as(self) -> None:
		"""Сохраняет состояние панели новым пресетом (спрашивает имя)."""
		dialog = FormDialog(
			"Сохранить пресет",
			[("name", "Имя пресета…")],
			self.window(),
			accept_text="Сохранить",
		)
		if not exec_dialog(dialog):
			return
		name = dialog.value("name")
		if not name:
			self._show_error("У пресета должно быть имя.")
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.save_preset(self._form.fields(name)),
			self,
			self._on_preset_saved,
			self._show_error,
		)

	def _on_preset_saved(self, preset: PresetDto) -> None:
		InfoBar.success("Пресет сохранён", preset.name, parent=self)
		self._reload_presets(select_name=preset.name)

	def _on_delete_preset(self) -> None:
		preset = self._preset_combo.selected()
		if preset is None:
			return
		if not confirm_delete(self, f"Удалить пресет «{preset.name}»?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.delete_preset(preset.id),
			self,
			lambda *_a: self._reload_presets(),
			self._show_error,
		)

	# --- список файлов и постановка в очередь ---------------------------------------

	def _template_fields(self) -> PresetFields:
		"""Параметры шаблона; имя — от выбранного пресета или «ручные»."""
		preset = self._preset_combo.selected()
		return self._form.fields(preset.name if preset else _MANUAL_NAME)

	def _add_file(self) -> None:
		"""«Добавить файл…»: диалог — в папке исходников подпапки шаблона."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker,
			self._worker.engine.video.dirs_for(subdir),
			self,
			self._pick_file_source,
			self._show_error,
		)

	def _pick_file_source(self, dirs: VideoDirs) -> None:
		"""Выбор одиночного файла — он добавляется карточкой в список."""
		path = pick_file(
			self,
			"Исходное видео",
			"Видео (*.mp4 *.mov *.mkv *.avi *.webm);;Все файлы (*)",
			start_dir=dirs.source,
		)
		if path:
			self._add_entry(path, batch="")

	def _add_folder(self) -> None:
		"""«Добавить папку…»: сканирование и выбор — как пакет ADR-0014."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker,
			self._worker.engine.video.dirs_for(subdir),
			self,
			self._pick_folder_source,
			self._show_error,
		)

	def _pick_folder_source(self, dirs: VideoDirs) -> None:
		"""Выбор папки; отмеченные в диалоге файлы добавляются карточками."""
		root = pick_dir(self, "Папка с исходниками", start_dir=dirs.source)
		if not root:
			return
		dialog = BatchScanDialog(self._worker, root, self.window())
		if not exec_dialog(dialog):
			return
		files = dialog.selected()
		if not files:
			return
		# подпапка пакета: штамп + имя папки-источника (узнаваемость);
		# внутри подпапки пресета, спецсимволы вычистит движок
		batch = f"{datetime.now():%Y%m%d-%H%M%S}_{Path(root).name}"
		added = 0
		for video in files:
			if self._add_entry(video.path, batch=batch, size_bytes=video.size_bytes):
				added += 1
		if added:
			InfoBar.success("Файлы добавлены", f"В списке новых: {added}", parent=self)

	def _add_entry(self, path: str, batch: str, size_bytes: int | None = None) -> bool:
		"""Добавляет файл карточкой; параметры — снимок шаблона.

		Returns:
			True — карточка добавлена; False — файл уже в списке
			или не читается.
		"""
		if any(entry.path == path for entry in self._entries):
			InfoBar.info("Уже в списке", Path(path).name, parent=self)
			return False
		if size_bytes is None:
			try:
				size_bytes = Path(path).stat().st_size
			except OSError:
				self._show_error(f"Файл не читается: {path}")
				return False
		preset = self._preset_combo.selected()
		entry = _FileEntry(
			self,
			path,
			size_bytes,
			batch,
			preset.name if preset else _MANUAL_NAME,
		)
		entry.form.fill(self._template_fields())
		self._entries.append(entry)
		self._files_box.addWidget(entry.card)
		self._update_empty_hint()
		# рекомендация битрейта — в параметры именно этой карточки
		fields = entry.form.fields("")
		run_in_engine(
			self._worker,
			self._worker.engine.video.bitrate_advice(path, fields.trim_start, fields.trim_end),
			self,
			partial(self._on_entry_advice, entry),
			noop,  # совет вспомогательный: сбой не мешает добавлению
		)
		return True

	def _on_entry_advice(self, entry: _FileEntry, advice: BitrateAdvice | None) -> None:
		"""Совет битрейта пришёл — подставляем в параметры карточки файла."""
		if advice is None or entry not in self._entries:
			return  # файл в лимите или карточку уже убрали
		if entry.form.suggest_bitrate(advice.mbps):
			entry.advice_note = (
				f"больше лимита {advice.limit_gb} ГБ — качество {advice.mbps:g} Мбит/с"
			)
			entry.refresh_summary()

	def _remove_entry(self, entry: _FileEntry) -> None:
		"""Убирает карточку файла из списка (сам файл не трогается)."""
		self._remove_entries([entry])

	def _remove_entries(self, entries: list[_FileEntry]) -> None:
		"""Убирает перечисленные карточки (после постановки или корзинкой)."""
		for entry in entries:
			if entry in self._entries:
				self._entries.remove(entry)
				entry.card.deleteLater()
		self._update_empty_hint()

	def _on_process(self) -> None:
		"""Ставит отмеченные файлы в очередь — каждый со своими параметрами."""
		if not self._entries:
			self._show_error("Добавьте файл или папку — список пуст.")
			return
		selected = [entry for entry in self._entries if entry.check.isChecked()]
		if not selected:
			self._show_error("Отметьте чекбоксами файлы, которые обрабатывать.")
			return
		collected = self._collect_requests(selected)
		if collected is None:
			return
		requests, submitted = collected
		if not requests:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.enqueue_many(requests),
			self,
			partial(self._on_enqueued, submitted),
			self._show_error,
		)

	def _collect_requests(
		self, entries: list[_FileEntry]
	) -> tuple[list[ProcessingRequest], list[_FileEntry]] | None:
		"""Заявки по отмеченным карточкам; «кадры на выбор» — проход по файлам.

		Интерактивный источник кадра несовместим с фоновой очередью,
		поэтому кадры выбираются заранее: диалог по разу на файл, выбранный
		кадр копируется в папку очереди (следующая партия кандидатов стёрла
		бы его). Отмена выбора: для единственного файла — отмена запуска,
		иначе — предложение исключить файл (остальные не теряются).

		Returns:
			Пара (заявки, их карточки) — карточки убираются после успешной
			постановки; None — запуск отменён целиком.
		"""
		single = len(entries) == 1
		requests: list[ProcessingRequest] = []
		submitted: list[_FileEntry] = []
		for entry in entries:
			fields = entry.form.fields(entry.preset_name)
			kind, _value = parse_intro_source(fields.intro_source)
			if not (kind is IntroSourceKind.RANDOM_CHOICE and (fields.intro or fields.cover)):
				requests.append(ProcessingRequest(entry.path, fields, batch_subdir=entry.batch))
				submitted.append(entry)
				continue
			try:
				intro = self._pick_frame_for(entry, fields, single)
			except _AbortRun:
				return None
			if intro is None:
				continue  # файл исключён из постановки — карточка остаётся
			requests.append(
				ProcessingRequest(entry.path, fields, intro_source=intro, batch_subdir=entry.batch)
			)
			submitted.append(entry)
		return requests, submitted

	def _pick_frame_for(self, entry: _FileEntry, fields: PresetFields, single: bool) -> str | None:
		"""Выбор кадра заставки для одного файла (до постановки).

		Returns:
			Строка «image:путь» с копией кадра; None — файл исключён.

		Raises:
			_AbortRun: Пользователь отменил постановку целиком.
		"""
		while True:
			dialog = FramePickerDialog(
				self._worker,
				entry.path,
				self.window(),
				trim_start=fields.trim_start,
				trim_end=fields.trim_end,
				file_label=None if single else Path(entry.path).name,
			)
			accepted = exec_dialog(dialog)
			chosen = dialog.chosen_path()
			if accepted and chosen is not None:
				stashed = self._stash_frame(chosen)
				if stashed is None:
					raise _AbortRun  # ошибка уже показана
				return build_intro_source(IntroSourceKind.IMAGE, stashed)
			if single:
				raise _AbortRun
			if confirm_delete(
				self,
				f"Кадр для «{Path(entry.path).name}» не выбран. Исключить файл из постановки?",
				accept_text="Исключить",
			):
				return None
			# «Отмена» в подтверждении — вернуться к выбору кадра

	def _stash_frame(self, chosen: str) -> str | None:
		"""Копирует выбранный кадр в папку очереди (синхронно, с таймаутом).

		Синхронный вызов оправдан: копирование PNG — мгновенное, а проход
		выбора кадров — последовательность модальных диалогов, где колбэки
		моста только запутали бы поток управления.
		"""
		try:
			future = self._worker.submit(self._worker.engine.video_queue.stash_frame(chosen))
			stashed: str = future.result(timeout=_STASH_TIMEOUT_S)
			return stashed
		except Exception as exc:  # noqa: BLE001 — показываем и прерываем постановку
			self._show_error(user_message(exc))
			return None

	def _on_enqueued(self, submitted: list[_FileEntry], _ids: list[int]) -> None:
		"""Заявки приняты — поставленные карточки уходят из списка.

		Неотмеченные (и исключённые при выборе кадров) остаются —
		их можно доправить и поставить следующим заходом.
		"""
		self._remove_entries(submitted)
		if len(submitted) > 1:
			InfoBar.success("Пакет в очереди", f"Файлов: {len(submitted)}", parent=self)
		self._poll_queue()

	# --- панель очереди обработки -------------------------------------------------

	def queue_busy(self) -> bool:
		"""Есть ли необработанное в очереди (для подтверждения выхода)."""
		return self._queue_busy

	def _poll_queue(self) -> None:
		"""Запрашивает состояние очереди (по таймеру и после постановки)."""
		# ошибки опроса не показываем плашками: мост пишет их в лог,
		# а раз в полсекунды спамить пользователя нечем и незачем
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.state(),
			self,
			self._show_queue,
			noop,
		)

	def _show_queue(self, items: list[VideoItemDto]) -> None:
		"""Обновляет панель очереди; завершённые снимаются с показа сами.

		Готовый файл — не элемент очереди, а строка «Готовых видео»
		(двух списков одного и того же быть не должно): готовые
		и отменённые убираются автоматически, в очереди остаются только
		ждущие, кодирующиеся и ошибки (им нужны «Повторить»/«Убрать»).
		"""
		visible: list[VideoItemDto] = []
		for item in items:
			if item.status is VideoItemStatus.DONE:
				self._finish_item(item, done=True)
			elif item.status is VideoItemStatus.CANCELLED:
				self._finish_item(item, done=False)
			else:
				visible.append(item)
		# id, исчезнувшие из состояния движка (после dismiss), больше
		# не встретятся — набор «уже учтённых» не растёт бесконечно
		self._handled_ids &= {item.id for item in items}
		busy = any(not item.status.finished() for item in visible)
		if self._queue_busy and not busy:
			self._notify_drained(visible)
		self._queue_busy = busy
		signature = tuple((i.id, i.status, i.error, i.note) for i in visible)
		if signature != self._queue_signature:
			self._queue_signature = signature
			self._rebuild_queue(visible)
		for item in visible:  # прогресс — без пересборки карточек
			bar = self._queue_bars.get(item.id)
			if bar is not None:
				bar.setValue(int(item.progress * 100))
		self._update_queue_summary(visible)

	def _finish_item(self, item: VideoItemDto, done: bool) -> None:
		"""Учитывает завершённый элемент и снимает его с показа.

		Снятие асинхронное, до него элемент успевает попасть в опрос
		ещё раз-другой — набор «уже учтённых» защищает от двойного счёта.
		"""
		if item.id in self._handled_ids:
			return
		self._handled_ids.add(item.id)
		if done:
			self._session_done += 1
			self._reload_processed()  # готовый файл появляется в списке
		self._dismiss_item(item.id)

	def _notify_drained(self, visible: list[VideoItemDto]) -> None:
		"""Одна итоговая плашка, когда очередь доработала (вместо плашки
		на каждый файл — пакет их наплодил бы десятками)."""
		errors = sum(1 for item in visible if item.status is VideoItemStatus.ERROR)
		if self._session_done:
			text = f"Готово файлов: {self._session_done}"
			if errors:
				text += f" · ошибок: {errors} (см. карточки)"
			InfoBar.success("Обработка завершена", text, parent=self)
		elif errors:
			InfoBar.warning(
				"Обработка завершена",
				f"Ошибок: {errors} — «Повторить» или «Убрать» на карточках.",
				parent=self,
			)
		self._session_done = 0

	def _update_queue_summary(self, items: list[VideoItemDto]) -> None:
		"""Итоговая строка над карточками: сколько осталось и ошибки."""
		if not items:
			self._queue_summary.hide()
			return
		left = sum(1 for item in items if not item.status.finished())
		errors = sum(1 for item in items if item.status is VideoItemStatus.ERROR)
		parts = []
		if left:
			parts.append(f"осталось {left}")
		if self._session_done:
			parts.append(f"готово {self._session_done}")
		if errors:
			parts.append(f"ошибок {errors}")
		self._queue_summary.setText("Очередь обработки: " + ", ".join(parts))
		self._queue_summary.show()

	def _rebuild_queue(self, items: list[VideoItemDto]) -> None:
		"""Перестраивает карточки очереди (только при смене состава/статусов)."""
		clear_layout(self._queue_box)
		self._queue_bars = {}
		for item in items:
			self._queue_box.addWidget(self._queue_row(item))

	def _queue_row(self, item: VideoItemDto) -> CardWidget:
		"""Карточка элемента очереди: статус, прогресс и действия.

		Показываются только живые элементы и ошибки: готовые и отменённые
		сняты с показа раньше (см. ``_show_queue``).
		"""
		trailing = QWidget(self)
		row = QHBoxLayout(trailing)
		row.setContentsMargins(0, 0, 0, 0)
		if item.status is VideoItemStatus.PROCESSING:
			bar = ProgressBar(trailing)
			bar.setRange(0, 100)
			bar.setValue(int(item.progress * 100))
			bar.setFixedWidth(160)
			row.addWidget(bar)
			self._queue_bars[item.id] = bar
		if item.status in (VideoItemStatus.PENDING, VideoItemStatus.PROCESSING):
			cancel = PushButton("Отмена", trailing)
			cancel.clicked.connect(bind(self._cancel_item, item.id))
			row.addWidget(cancel)
		if item.status is VideoItemStatus.ERROR:
			retry = PushButton("Повторить", trailing)
			retry.clicked.connect(bind(self._retry_item, item.id))
			row.addWidget(retry)
			dismiss = PushButton("Убрать", trailing)
			dismiss.clicked.connect(bind(self._dismiss_item, item.id))
			row.addWidget(dismiss)
		return row_card(self, item.title, self._queue_subtitle(item), trailing=trailing)

	@staticmethod
	def _queue_subtitle(item: VideoItemDto) -> str:
		"""Подпись карточки: пакет, статус и пометки выполнения."""
		status_text = {
			VideoItemStatus.PENDING: "в очереди",
			VideoItemStatus.PROCESSING: "кодируется",
			VideoItemStatus.DONE: "готово",
			VideoItemStatus.ERROR: f"ошибка: {item.error}",
			VideoItemStatus.CANCELLED: "отменено",
		}[item.status]
		parts = []
		if item.batch:
			parts.append(f"пакет «{item.batch}»")
		parts.append(status_text)
		if item.note:
			parts.append(item.note)
		return " · ".join(parts)

	def _cancel_item(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди обработки."""
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.cancel(item_id),
			self,
			noop,
			self._show_error,
		)

	def _retry_item(self, item_id: int) -> None:
		"""Возвращает элемент с ошибкой в очередь на новую попытку."""
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.retry(item_id),
			self,
			lambda *_a: self._poll_queue(),
			self._show_error,
		)

	def _dismiss_item(self, item_id: int) -> None:
		"""Убирает завершённый элемент с показа."""
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.dismiss(item_id),
			self,
			lambda *_a: self._poll_queue(),
			noop,
		)

	# --- готовые видео -----------------------------------------------------------

	def _reload_processed(self, *_args: object) -> None:
		"""Перечитывает список готовых видео текущей подпапки."""
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_processed(self._form.fields("").subdir),
			self,
			self._show_processed,
			self._show_error,
		)

	def _show_processed(self, listing: ProcessedListing) -> None:
		"""Показывает готовые видео карточками (новые — сверху)."""
		self._processed_hint.setText(f"Папка: {listing.directory}")
		clear_layout(self._result_box)
		if not listing.items:
			self._result_box.addWidget(
				CaptionLabel(
					"Готовых видео пока нет — обработайте исходник кнопкой выше.",
					self,
				)
			)
			return
		for item in listing.items:
			self._result_box.addWidget(self._processed_card(item))

	def _processed_card(self, item: ProcessedVideo) -> QWidget:
		"""Карточка готового видео: размер, дата и действия над файлом."""
		open_btn = PushButton(FluentIcon.PLAY, "Открыть", self)
		open_btn.clicked.connect(bind(self._open_path, item.path))
		folder_btn = PushButton(FluentIcon.FOLDER, "Показать в папке", self)
		folder_btn.clicked.connect(bind(self._open_path, str(Path(item.path).parent)))
		publish_btn = PrimaryPushButton(FluentIcon.SEND, "Опубликовать…", self)
		publish_btn.clicked.connect(bind(self._request_publish, item.path))
		buttons = QWidget(self)
		buttons_layout = QHBoxLayout(buttons)
		buttons_layout.setContentsMargins(0, 0, 0, 0)
		for button in (open_btn, folder_btn, publish_btn):
			buttons_layout.addWidget(button)
		subtitle = f"{human_size(item.size_bytes)} · {format_local(item.modified_at)}"
		card: QWidget = row_card(
			self,
			item.name,
			subtitle,
			trailing=buttons,
			on_delete=bind(self._on_delete_processed, item),
		)
		return card

	def _on_delete_processed(self, item: ProcessedVideo) -> None:
		"""Удаляет готовое видео с диска (вместе с кадром-превью)."""
		if not confirm_delete(
			self,
			f"Удалить файл «{item.name}» с диска? Вместе с ним удалится "
			"кадр-превью. Отменить удаление будет нельзя.",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.delete_processed(item.path),
			self,
			lambda *_a: self._reload_processed(),
			self._show_error,
		)

	def _request_publish(self, path: str) -> None:
		"""Передаёт файл на «Публикацию» вместе с выбранным каналом."""
		channel = self._channel_combo.selected()
		self.publish_requested.emit(path, channel.id if channel else 0)

	@staticmethod
	def _open_path(path: str) -> None:
		"""Открывает файл или папку системным приложением."""
		QDesktopServices.openUrl(QUrl.fromLocalFile(path))
