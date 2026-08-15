"""Страница «Видео»: панель параметров обработки и очередь подготовки.

Параметры живут прямо на странице: «Обработать» применяет то, что на
экране, ничего не сохраняя. Пресет — «загрузчик»: выбор в списке
заполняет панель (:mod:`video_form`), сохранение — только по явным
кнопкам. Обработка — и одиночная, и пакетная («Обработать папку…»,
диалог :mod:`video_batch`) — идёт через очередь движка (ADR-0014):
кнопки ставят элементы в хвост, карточки очереди видны на странице.
Результат — файл в папке результатов; кнопка «Опубликовать…» передаёт
его странице «Публикация» (контракт — путь к файлу). Выбор кадра
заставки — отдельный диалог (:mod:`frame_picker`).
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
	FluentIcon,
	InfoBar,
	LineEdit,
	PrimaryPushButton,
	ProgressBar,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	ToolButton,
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
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	INPUT_DEBOUNCE_MS,
	DtoComboBox,
	FormDialog,
	bind,
	clear_layout,
	confirm_delete,
	debounced,
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
from pxcontrol.ui.pages.video_form import PresetForm

#: Имя «пресета» в имени файла результата, когда пресет не выбран.
_MANUAL_NAME = "ручные"

#: Период опроса состояния очереди обработки (мс) — как у очереди отправки.
_QUEUE_POLL_MS = 500

#: Сколько ждать копирования выбранного кадра в папку очереди (сек).
_STASH_TIMEOUT_S = 10.0


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
		self._done_seen: set[int] = set()  # готовые, уже обновившие список
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
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Параметры обработки", self))
		layout.addWidget(
			CaptionLabel(
				"К видео применяется то, что на экране; пресет — только "
				"загрузка и сохранение набора.",
				self,
			)
		)
		self._form = PresetForm(self)
		layout.addWidget(self._form)
		self._build_process_row(layout)
		self._build_queue_block(layout)
		self._build_processed_block(layout)
		layout.addStretch()

	def _build_queue_block(self, layout: QVBoxLayout) -> None:
		"""Панель очереди обработки: итоговая строка и карточки элементов."""
		self._queue_summary = CaptionLabel("", self)
		self._queue_summary.hide()
		layout.addWidget(self._queue_summary)
		self._queue_box = QVBoxLayout()
		self._queue_box.setSpacing(8)
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
		"""Строка исходника: файл ИЛИ папка — выбор источника в одном месте.

		«Обзор…» и просмотр — про одиночный файл; «Обработать папку…» —
		альтернативный источник (пакет): рядом, потому что это выбор
		«что обрабатываем», а не действие запуска.
		"""
		src_row = QHBoxLayout()
		self._source = LineEdit(self)
		self._source.setPlaceholderText("Исходный видеофайл…")
		# после паузы ввода: промежуточная строка, совпавшая с файлом,
		# запускала бы ffprobe (рекомендация битрейта) на каждый символ
		self._source.textChanged.connect(
			debounced(self, INPUT_DEBOUNCE_MS, self._on_source_changed)
		)
		browse = PushButton("Обзор…", self)
		browse.clicked.connect(self._pick_source)
		self._play_button = ToolButton(FluentIcon.PLAY, self)
		self._play_button.setToolTip("Посмотреть выбранный файл (системный плеер)")
		self._play_button.setEnabled(False)  # активируется выбором файла
		self._play_button.clicked.connect(self._play_source)
		batch_button = PushButton(FluentIcon.FOLDER, "Обработать папку…", self)
		batch_button.setToolTip(
			"Вместо одного файла: рекурсивно найти видео в папке и обработать "
			"выбранные текущими параметрами (пакет — в свою подпапку результатов)"
		)
		batch_button.clicked.connect(self._on_batch)
		src_row.addWidget(self._source)
		src_row.addWidget(browse)
		src_row.addWidget(self._play_button)
		src_row.addWidget(batch_button)
		layout.addLayout(src_row)

	def _on_source_changed(self) -> None:
		"""Реакция на выбор исходника: просмотр и рекомендация битрейта.

		Просмотр доступен, только когда путь указывает на существующий
		файл. Для файла больше лимита Telegram движок считает
		рекомендуемый битрейт (размер итога — лимит минус 1 %).
		Вызывается после паузы ввода (см. подключение сигнала).
		"""
		path = str(self._source.text()).strip()
		is_file = Path(path).is_file()
		self._play_button.setEnabled(is_file)
		if not is_file:
			return
		fields = self._form.fields("")
		run_in_engine(
			self._worker,
			self._worker.engine.video.bitrate_advice(path, fields.trim_start, fields.trim_end),
			self,
			self._on_bitrate_advice,
			self._show_error,
		)

	def _on_bitrate_advice(self, advice: BitrateAdvice | None) -> None:
		"""Подставляет рекомендованный битрейт или снимает ненужный.

		None — файл в лимите (или не читается): прежняя автоподстановка
		сбрасывается в «0», иначе рекомендация от предыдущего файла
		молча ушла бы в кодирование нового.
		"""
		if advice is None:
			self._form.clear_suggested_bitrate()
			return
		if self._form.suggest_bitrate(advice.mbps):
			InfoBar.info(
				"Исходник больше лимита Telegram",
				f"Лимит {advice.limit_gb} ГБ — в «Качество» подставлено "
				f"{advice.mbps:g} Мбит/с (итог: лимит минус 1 %).",
				parent=self,  # сообщение относится к странице, как остальные её плашки
			)

	def _play_source(self) -> None:
		"""Открывает исходник системным плеером (встроенного пока нет)."""
		self._open_path(str(self._source.text()).strip())

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

	# --- подготовка -----------------------------------------------------------------

	def _pick_source(self) -> None:
		"""Диалог выбора исходника — в папке исходников подпапки пресета."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker,
			self._worker.engine.video.dirs_for(subdir),
			self,
			self._open_source_dialog,
			self._show_error,
		)

	def _open_source_dialog(self, dirs: VideoDirs) -> None:
		"""Открывает диалог исходника в действующей папке исходников."""
		path = pick_file(
			self,
			"Исходное видео",
			"Видео (*.mp4 *.mov *.mkv *.avi *.webm);;Все файлы (*)",
			start_dir=dirs.source,
		)
		if path:
			self._source.setText(path)

	def _fields(self) -> PresetFields:
		"""Параметры с экрана; имя — от выбранного пресета или «ручные»."""
		preset = self._preset_combo.selected()
		return self._form.fields(preset.name if preset else _MANUAL_NAME)

	def _on_process(self) -> None:
		"""Ставит выбранный исходник в очередь обработки."""
		source = str(self._source.text()).strip()
		if not source:
			self._show_error("Выберите исходный видеофайл.")
			return
		fields = self._fields()
		requests = self._requests_with_frames([source], fields)
		if requests:
			self._enqueue(requests, fields, "")

	def _on_batch(self) -> None:
		"""Пакетная обработка: выбор папки → сканирование → очередь."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker,
			self._worker.engine.video.dirs_for(subdir),
			self,
			self._pick_batch_root,
			self._show_error,
		)

	def _pick_batch_root(self, dirs: VideoDirs) -> None:
		"""Открывает выбор папки пакета (по умолчанию — папка исходников)."""
		root = pick_dir(self, "Папка с исходниками", start_dir=dirs.source)
		if not root:
			return
		dialog = BatchScanDialog(self._worker, root, self.window())
		if not exec_dialog(dialog):
			return
		files = dialog.selected()
		if not files:
			return
		fields = self._fields()
		requests = self._requests_with_frames([video.path for video in files], fields)
		if not requests:
			return
		# подпапка пакета: штамп запуска + имя папки-источника (узнаваемость);
		# внутри подпапки пресета, спецсимволы вычистит движок
		batch_subdir = f"{datetime.now():%Y%m%d-%H%M%S}_{Path(root).name}"
		self._enqueue(requests, fields, batch_subdir)

	def _requests_with_frames(
		self, paths: list[str], fields: PresetFields
	) -> list[ProcessingRequest] | None:
		"""Собирает заявки; для «случайных кадров на выбор» — проход по файлам.

		Интерактивный источник кадра несовместим с фоновой очередью,
		поэтому кадры выбираются заранее: диалог по разу на файл, выбранный
		кадр копируется в папку очереди (следующая партия кандидатов стёрла
		бы его). Отмена выбора: для одиночного файла — отмена запуска, для
		пакета — предложение исключить файл (остальные не теряются).

		Returns:
			Заявки на обработку; None или пустой список — запуск отменён.
		"""
		kind, _value = parse_intro_source(fields.intro_source)
		if not (kind is IntroSourceKind.RANDOM_CHOICE and (fields.intro or fields.cover)):
			return [ProcessingRequest(path) for path in paths]
		single = len(paths) == 1
		requests: list[ProcessingRequest] = []
		for path in paths:
			while True:
				dialog = FramePickerDialog(
					self._worker,
					path,
					self.window(),
					trim_start=fields.trim_start,
					trim_end=fields.trim_end,
					file_label=None if single else Path(path).name,
				)
				accepted = exec_dialog(dialog)
				chosen = dialog.chosen_path()
				if accepted and chosen is not None:
					stashed = self._stash_frame(chosen)
					if stashed is None:
						return None  # ошибка уже показана
					requests.append(
						ProcessingRequest(path, build_intro_source(IntroSourceKind.IMAGE, stashed))
					)
					break
				if single:
					return None
				if confirm_delete(
					self,
					f"Кадр для «{Path(path).name}» не выбран. Исключить файл из пакета?",
					accept_text="Исключить",
				):
					break  # файл пропущен, заявка не создаётся
				# «Отмена» в подтверждении — вернуться к выбору кадра
		return requests

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

	def _enqueue(
		self, requests: list[ProcessingRequest], fields: PresetFields, batch_subdir: str
	) -> None:
		"""Ставит заявки в очередь обработки движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.enqueue_many(requests, fields, batch_subdir),
			self,
			partial(self._on_enqueued, len(requests)),
			self._show_error,
		)

	def _on_enqueued(self, count: int, _ids: list[int]) -> None:
		"""Заявки приняты — панель очереди обновляется сразу, не по таймеру."""
		if count > 1:
			InfoBar.success("Пакет в очереди", f"Файлов: {count}", parent=self)
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
		"""Обновляет панель очереди; новые готовые обновляют список видео."""
		self._queue_busy = any(not item.status.finished() for item in items)
		done_ids = {item.id for item in items if item.status is VideoItemStatus.DONE}
		if done_ids - self._done_seen:
			self._reload_processed()
		self._done_seen = done_ids
		signature = tuple((i.id, i.status, i.error, i.note) for i in items)
		if signature != self._queue_signature:
			self._queue_signature = signature
			self._rebuild_queue(items)
		for item in items:  # прогресс — без пересборки карточек
			bar = self._queue_bars.get(item.id)
			if bar is not None:
				bar.setValue(int(item.progress * 100))
		self._update_queue_summary(items)

	def _update_queue_summary(self, items: list[VideoItemDto]) -> None:
		"""Итоговая строка над карточками: «готово M из N, ошибок K»."""
		if not items:
			self._queue_summary.hide()
			return
		done = sum(1 for item in items if item.status is VideoItemStatus.DONE)
		errors = sum(1 for item in items if item.status is VideoItemStatus.ERROR)
		text = f"Очередь обработки: готово {done} из {len(items)}"
		if errors:
			text += f", ошибок {errors}"
		self._queue_summary.setText(text)
		self._queue_summary.show()

	def _rebuild_queue(self, items: list[VideoItemDto]) -> None:
		"""Перестраивает карточки очереди (только при смене состава/статусов)."""
		clear_layout(self._queue_box)
		self._queue_bars = {}
		for item in items:
			self._queue_box.addWidget(self._queue_row(item))

	def _queue_row(self, item: VideoItemDto) -> CardWidget:
		"""Карточка элемента очереди: статус, прогресс и действия."""
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
		if item.status is VideoItemStatus.DONE and item.output_path:
			publish = PrimaryPushButton(FluentIcon.SEND, "Опубликовать…", trailing)
			publish.clicked.connect(bind(self._request_publish, item.output_path))
			row.addWidget(publish)
		if item.status.finished():
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
