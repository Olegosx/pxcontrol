"""Страница «Видео»: список файлов с параметрами и очередь подготовки.

Источник — две равнозначные кнопки: «Добавить файл…» (каждый выбор
добавляет карточку в список) и «Добавить папку…» (диалог сканирования
:mod:`video_batch`; отмеченные становятся карточками пакета). У каждого
файла — своя карточка параметров, заполненная из карточки-шаблона
«[Параметры пресета]» (под строкой пресета) в момент добавления;
шаблон правится всегда и служит пресетам («загрузчик»: выбор пресета
заполняет шаблон, сохранение — по явным кнопкам). «Обработать все»
ставит в очередь движка (ADR-0014) весь список, «Обработать» — файлы,
отмеченные чекбоксами в шапках карточек (единственный файл в списке —
и без галочки); каждый — со своими параметрами; карточки очереди видны
на странице. Результат — файл в папке результатов; кнопка
«Опубликовать…» передаёт его странице «Публикация» (контракт — путь
к файлу). Выбор кадра заставки — отдельный диалог (:mod:`frame_picker`).
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	FluentIcon,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.errors import user_message
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.settings import COMMUNITY_DEFAULT_PRESET
from pxcontrol.engine.services.video import (
	BitrateAdvice,
	IntroSourceKind,
	PresetDto,
	PresetFields,
	ProcessedListing,
	SourceAdvice,
	VideoDirs,
	VideoFile,
	batch_subdir_name,
	build_intro_source,
	parse_intro_source,
	video_dialog_filter,
)
from pxcontrol.engine.services.video_queue import (
	ProcessingRequest,
	VideoItemDto,
)
from pxcontrol.engine.video.constants import is_upscale, scaled_size
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	CollapsibleCard,
	DtoComboBox,
	FormDialog,
	QueuePanel,
	bind,
	checked_or_single,
	clear_layout,
	community_combo_label,
	confirm_delete,
	error_reporter,
	exec_dialog,
	file_action_buttons,
	format_local,
	human_size,
	noop,
	open_in_system,
	page_layout,
	pick_dir,
	pick_file,
	row_card,
	show_info,
	show_success,
	show_warning,
)
from pxcontrol.ui.pages.frame_picker import FramePickerDialog
from pxcontrol.ui.pages.video_batch import BatchScanDialog
from pxcontrol.ui.pages.video_form import PresetForm

#: Имя «пресета» в имени файла результата, когда пресет не выбран.
_MANUAL_NAME = "ручные"

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
		# размеры кадра исходника: у пакета приезжают со сканированием,
		# у одиночного файла — с подсказкой движка; None — ещё не знаем
		self.source_frame: tuple[int, int] | None = None
		self.scale_note = ""  # пометка об апскейле (зависит и от ступени)
		trailing = file_action_buttons(
			page,
			path,
			bind(page._remove_entry, self),  # noqa: SLF001 — карточка живёт у страницы
			remove_tip="Убрать из списка (файл на диске не трогается)",
		)
		# чекбокс выбора — слева, перед названием (клик не сворачивает карточку)
		self.check = CheckBox("", page)
		self.check.setToolTip("Отправить файл на обработку («Обработать» берёт отмеченные)")
		title = f"{Path(path).name} — {human_size(size_bytes)}"
		self.card = CollapsibleCard(title, page, trailing=trailing, leading=self.check)
		self.form = PresetForm(page)
		# смена ступени разрешения меняет вердикт об апскейле
		self.form.resolution_changed.connect(bind(page._refresh_scale_note, self))  # noqa: SLF001
		self.card.body.addWidget(self.form)
		self.refresh_summary()

	def refresh_summary(self) -> None:
		"""Сводка шапки: пакет и пометки (видны у свёрнутой карточки)."""
		parts = []
		if self.batch:
			parts.append(f"пакет «{self.batch}»")
		if self.scale_note:
			parts.append(self.scale_note)
		if self.advice_note:
			parts.append(self.advice_note)
		self.card.set_summary(" · ".join(parts))


class VideoPage(ScrollArea):
	"""Панель параметров обработки и подготовка видеофайла."""

	#: Просьба опубликовать готовый файл: путь и id канала со страницы
	#: (0 — канал не выбран). Ловит главное окно → «Публикация».
	publish_requested = Signal(str, int)

	#: Просьба опубликовать несколько готовых файлов пакетом (ADR-0015):
	#: список путей и id канала. Ловит главное окно → пакет на «Публикации».
	publish_files_requested = Signal(list, int)

	#: Просьба опубликовать папку готовых видео пакетом: путь к папке
	#: и id канала. Ловит главное окно → пакет на «Публикации».
	publish_folder_requested = Signal(str, int)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("video")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._session_done = 0  # готовых с последней итоговой плашки
		self._entries: list[_FileEntry] = []  # карточки файлов к обработке
		self._processed_checks: list[tuple[CheckBox, VideoFile]] = []
		self._processed_dir = ""  # папка текущего списка готовых видео
		self._build()
		self._reload_presets()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет каналы и готовые видео при каждом открытии страницы.

		Список перечитывается с диска: файлы могли уехать в опубликованные
		(после отправки поста) или измениться мимо приложения.
		"""
		super().showEvent(event)
		self._reload_communities()
		self._reload_processed()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		layout.addWidget(SubtitleLabel("Подготовка видео", self))
		self._build_source_row(layout)
		self._build_community_row(layout)
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
		queue_box = QVBoxLayout()
		queue_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(queue_box)
		self._queue = QueuePanel(
			self._worker,
			self,
			queue_box,
			service=lambda: self._worker.engine.video_queue,
			subtitle=self._queue_subtitle,
			on_finished=self._on_queue_finished,
			on_refreshed=self._update_queue_summary,
			on_drained=self._notify_drained,
		)

	def _build_processed_block(self, layout: QVBoxLayout) -> None:
		"""Раздел готовых видео: папка результатов текущей подпапки."""
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Готовые видео", self))
		self._processed_hint = CaptionLabel("", self)
		self._processed_hint.setWordWrap(True)
		layout.addWidget(self._processed_hint)
		self._result_box = QVBoxLayout()
		self._result_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._result_box)
		self._build_processed_actions(layout)
		# список идёт за подпапкой: она задаёт папку, куда уйдёт результат
		self._form.subdir_changed.connect(self._reload_processed)

	def _build_processed_actions(self, layout: QVBoxLayout) -> None:
		"""Кнопки массовой публикации под списком готовых видео."""
		row = QHBoxLayout()
		self._publish_all_button = PushButton(FluentIcon.SEND, "Опубликовать все", self)
		self._publish_all_button.setToolTip("Все видео списка — пакетом на «Публикацию»")
		self._publish_all_button.clicked.connect(self._publish_all_processed)
		row.addWidget(self._publish_all_button)
		self._publish_checked_button = PushButton("Опубликовать отмеченные", self)
		self._publish_checked_button.setToolTip(
			"Видео, отмеченные чекбоксами, — пакетом на «Публикацию»"
		)
		self._publish_checked_button.clicked.connect(self._publish_checked_processed)
		row.addWidget(self._publish_checked_button)
		publish_folder = PushButton("Опубликовать папку…", self)
		publish_folder.setToolTip(
			"Выбрать подпапку в обработанных и отправить её пакетом на «Публикацию»"
		)
		publish_folder.clicked.connect(self._publish_processed_folder)
		row.addWidget(publish_folder)
		row.addStretch()
		layout.addLayout(row)

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

	def _build_community_row(self, layout: QVBoxLayout) -> None:
		"""Канал: выбор подставляет его пресет по умолчанию (настройка канала)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Канал:", self))
		self._community_combo: DtoComboBox[CommunityDto] = DtoComboBox(
			self, placeholder="(не выбран)"
		)
		self._community_combo.setToolTip(
			"Выбор канала загружает его пресет по умолчанию "
			"(задаётся на странице «Каналы» → «Пресет…»)"
		)
		self._community_combo.currentIndexChanged.connect(self._on_community_selected)
		row.addWidget(self._community_combo, stretch=1)
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
		process_all = PushButton(FluentIcon.PLAY, "Обработать все", self)
		process_all.setToolTip("Поставить в очередь весь список — галочки не важны")
		process_all.clicked.connect(self._on_process_all)
		run_row.addWidget(process_all)
		self._process_button = PrimaryPushButton(FluentIcon.PLAY, "Обработать", self)
		self._process_button.clicked.connect(self._on_process)
		run_row.addWidget(self._process_button)
		run_row.addStretch()
		layout.addLayout(run_row)

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

	def _reload_communities(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._show_communities,
			self._show_error,
		)

	def _show_communities(self, communities: list[CommunityDto]) -> None:
		"""Наполняет список каналов (выбор сохраняется по id канала)."""
		self._community_combo.set_items(
			communities,
			label=community_combo_label,
			key=lambda community: community.id,
		)

	def _on_community_selected(self, _index: int) -> None:
		"""Выбор канала — загрузка его пресета по умолчанию в панель."""
		community = self._community_combo.selected()
		if community is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(COMMUNITY_DEFAULT_PRESET, community.id),
			self,
			partial(self._apply_community_preset, community),
			self._show_error,
		)

	def _is_stale_community(self, community_id: int) -> bool:
		"""Пришёл ли ответ движка для уже переключённого канала.

		Пока движок занят, ответы задерживаются (та же гонка, что
		``_is_stale`` на «Публикации»): без проверки пресет канала A
		лёг бы в шаблон уже выбранного канала B.
		"""
		return not self._community_combo.is_current_id(community_id)

	def _apply_community_preset(self, community: CommunityDto, preset_id: int | None) -> None:
		"""Подставляет пресет канала; нет пресета — форма не трогается.

		Выбор в списке вызывает ``_on_preset_selected`` — панель заполнится.
		Ссылка на удалённый пресет равнозначна «не задан».
		"""
		if self._is_stale_community(community.id):
			return
		if preset_id is None or not self._preset_combo.select(
			lambda preset: preset.id == preset_id
		):
			show_info(
				self,
				"Пресет не задан",
				f"У канала «{community.title}» нет пресета по умолчанию — "
				"задайте его на странице «Каналы» → «Пресет…».",
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
			partial(self._apply_preset_fields, preset.id),
			self._show_error,
		)

	def _apply_preset_fields(self, preset_id: int, fields: PresetFields) -> None:
		"""Заполняет панель, если пресет всё ещё выбран (защита от гонки)."""
		if self._preset_combo.is_current_id(preset_id):
			self._form.fill(fields)

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
		show_success(self, "Пресет сохранён", preset.name)
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
			f"{video_dialog_filter()};;Все файлы (*)",
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
		# правило имени подпапки пакета держит движок (ADR-0014, п. 4)
		batch = batch_subdir_name(root)
		added = 0
		for video in files:
			if self._add_entry(
				video.path, batch=batch, size_bytes=video.size_bytes, frame=video.frame
			):
				added += 1
		if added:
			show_success(self, "Файлы добавлены", f"В списке новых: {added}")

	def _add_entry(
		self,
		path: str,
		batch: str,
		size_bytes: int | None = None,
		frame: tuple[int, int] | None = None,
	) -> bool:
		"""Добавляет файл карточкой; параметры — снимок шаблона.

		Args:
			path: путь к исходнику.
			batch: подпапка пакета («» — одиночное добавление).
			size_bytes: размер файла, если он уже известен.
			frame: размеры кадра, если исходник уже прощупан (пакет:
				сканирование папки прощупывает каждый файл). Известные
				размеры избавляют от второй пробы ffprobe на файл.

		Returns:
			True — карточка добавлена; False — файл уже в списке
			или не читается.
		"""
		if any(entry.path == path for entry in self._entries):
			show_info(self, "Уже в списке", Path(path).name)
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
		fields = entry.form.fields("")
		# подсказки вспомогательные: сбой не мешает добавлению файла
		if frame is not None:
			# размеры уже есть — спрашиваем движок только о битрейте
			# (для файла в лимите это лишь чтение размера файла)
			entry.source_frame = frame
			self._refresh_scale_note(entry)
			run_in_engine(
				self._worker,
				self._worker.engine.video.bitrate_advice(path, fields.trim_start, fields.trim_end),
				self,
				partial(self._on_entry_bitrate, entry),
				noop,
			)
		else:
			# одиночный файл: размеры кадра и совет по битрейту — одной пробой
			run_in_engine(
				self._worker,
				self._worker.engine.video.source_advice(path, fields.trim_start, fields.trim_end),
				self,
				partial(self._on_entry_source, entry),
				noop,
			)
		return True

	def _on_entry_source(self, entry: _FileEntry, advice: SourceAdvice | None) -> None:
		"""Сведения об исходнике пришли: размеры кадра и совет по битрейту."""
		if advice is None or entry not in self._entries:
			return  # файл не прочитан или карточку уже убрали
		entry.source_frame = (advice.width, advice.height)
		self._refresh_scale_note(entry)
		self._on_entry_bitrate(entry, advice.bitrate)

	def _on_entry_bitrate(self, entry: _FileEntry, advice: BitrateAdvice | None) -> None:
		"""Совет битрейта пришёл — подставляем в параметры карточки файла."""
		if advice is None or entry not in self._entries:
			return  # файл в лимите или карточку уже убрали
		if entry.form.suggest_bitrate(advice.mbps):
			entry.advice_note = (
				f"больше лимита {advice.limit_gb} ГБ — качество {advice.mbps:g} Мбит/с"
			)
			entry.refresh_summary()

	def _refresh_scale_note(self, entry: _FileEntry) -> None:
		"""Пересчитывает предупреждение об апскейле для карточки файла.

		Зовётся и при появлении размеров исходника, и при смене ступени
		разрешения. Пока размеры неизвестны (файл не прощупан), молчим:
		врать про увеличение хуже, чем не сказать ничего.
		"""
		if entry not in self._entries:
			return
		note = ""
		target = entry.form.fields("").target_resolution
		if entry.source_frame is not None and is_upscale(*entry.source_frame, target):
			width, height = entry.source_frame
			out_width, out_height = scaled_size(width, height, target)
			note = f"апскейл: {width}×{height} → {out_width}×{out_height}"
		entry.scale_note = note
		entry.form.set_scale_note(note)
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

	def _on_process_all(self) -> None:
		"""Ставит в очередь весь список — независимо от галочек."""
		if not self._entries:
			self._show_error("Добавьте файл или папку — список пуст.")
			return
		self._process_entries(list(self._entries))

	def _on_process(self) -> None:
		"""Ставит отмеченные файлы в очередь — каждый со своими параметрами."""
		if not self._entries:
			self._show_error("Добавьте файл или папку — список пуст.")
			return
		checked = [entry for entry in self._entries if entry.check.isChecked()]
		selected = checked_or_single(self._entries, checked)
		if selected is None:
			self._show_error("Отметьте чекбоксами файлы, которые обрабатывать.")
			return
		self._process_entries(selected)

	def _process_entries(self, entries: list[_FileEntry]) -> None:
		"""Собирает запросы перечисленных карточек и ставит их в очередь."""
		collected = self._collect_requests(entries)
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
				target_resolution=fields.target_resolution,
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
			show_success(self, "Пакет в очереди", f"Файлов: {len(submitted)}")
		self._queue.poll()  # карточки видны сразу, не по таймеру

	# --- панель очереди обработки -------------------------------------------------

	def queue_busy(self) -> bool:
		"""Есть ли необработанное в очереди (для подтверждения выхода)."""
		return self._queue.busy()

	def _on_queue_finished(self, item: VideoItemDto, done: bool) -> None:
		"""Учитывает завершённый элемент (плашки на каждый файл нет:
		готовый файл — строка «Готовых видео», а не элемент очереди)."""
		del item  # реакции важен только исход
		if done:
			self._session_done += 1
			self._reload_processed()  # готовый файл появляется в списке

	def _notify_drained(self, visible: list[VideoItemDto]) -> None:
		"""Одна итоговая плашка, когда очередь доработала (вместо плашки
		на каждый файл — пакет их наплодил бы десятками).

		Родитель — окно: опрос живёт всегда, и завершение может прийти
		при скрытой странице — плашка на ней погасла бы незамеченной.
		"""
		errors = sum(1 for item in visible if item.status is JobStatus.ERROR)
		if self._session_done:
			text = f"Готово файлов: {self._session_done}"
			if errors:
				text += f" · ошибок: {errors} (см. карточки)"
			show_success(self.window(), "Обработка завершена", text)
		elif errors:
			show_warning(
				self.window(),
				"Обработка завершена",
				f"Ошибок: {errors} — «Повторить» или «Убрать» на карточках.",
			)
		self._session_done = 0

	def _update_queue_summary(self, items: list[VideoItemDto]) -> None:
		"""Итоговая строка над карточками: сколько осталось и ошибки."""
		if not items:
			self._queue_summary.hide()
			return
		left = sum(1 for item in items if not item.status.finished())
		errors = sum(1 for item in items if item.status is JobStatus.ERROR)
		parts = []
		if left:
			parts.append(f"осталось {left}")
		if self._session_done:
			parts.append(f"готово {self._session_done}")
		if errors:
			parts.append(f"ошибок {errors}")
		self._queue_summary.setText("Очередь обработки: " + ", ".join(parts))
		self._queue_summary.show()

	@staticmethod
	def _queue_subtitle(item: VideoItemDto) -> str:
		"""Подпись карточки: пакет, статус и пометки выполнения."""
		status_text = {
			JobStatus.PENDING: "в очереди",
			JobStatus.RUNNING: "кодируется",
			JobStatus.DONE: "готово",
			JobStatus.ERROR: f"ошибка: {item.error}",
			JobStatus.CANCELLED: "отменено",
		}[item.status]
		parts = []
		if item.batch:
			parts.append(f"пакет «{item.batch}»")
		parts.append(status_text)
		if item.note:
			parts.append(item.note)
		return " · ".join(parts)

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
		self._processed_dir = listing.directory  # старт диалога «Опубликовать папку…»
		self._processed_checks = []
		clear_layout(self._result_box)
		has_items = bool(listing.items)
		self._publish_all_button.setEnabled(has_items)
		self._publish_checked_button.setEnabled(has_items)
		if not has_items:
			self._result_box.addWidget(
				CaptionLabel(
					"Готовых видео пока нет — обработайте исходник кнопкой выше.",
					self,
				)
			)
			return
		for item in listing.items:
			self._result_box.addWidget(self._processed_card(item))

	def _processed_card(self, item: VideoFile) -> QWidget:
		"""Карточка готового видео: размер, дата и действия над файлом."""
		open_btn = PushButton(FluentIcon.PLAY, "Открыть", self)
		open_btn.clicked.connect(bind(open_in_system, item.path))
		folder_btn = PushButton(FluentIcon.FOLDER, "Показать в папке", self)
		folder_btn.clicked.connect(bind(open_in_system, str(Path(item.path).parent)))
		publish_btn = PrimaryPushButton(FluentIcon.SEND, "Опубликовать…", self)
		publish_btn.clicked.connect(bind(self._request_publish, item.path))
		buttons = QWidget(self)
		buttons_layout = QHBoxLayout(buttons)
		buttons_layout.setContentsMargins(0, 0, 0, 0)
		for button in (open_btn, folder_btn, publish_btn):
			buttons_layout.addWidget(button)
		# чекбокс — перед корзинкой (row_card добавляет её после trailing)
		check = CheckBox("", buttons)
		check.setToolTip("Отметить для «Опубликовать отмеченные»")
		buttons_layout.addWidget(check)
		self._processed_checks.append((check, item))
		subtitle = f"{human_size(item.size_bytes)} · {format_local(item.modified_at)}"
		card: QWidget = row_card(
			self,
			item.name,
			subtitle,
			trailing=buttons,
			on_delete=bind(self._on_delete_processed, item),
		)
		return card

	# --- массовая публикация готовых видео (ADR-0015) -------------------------------

	def _current_community(self) -> CommunityDto | None:
		"""Выбранный канал или подсказка (имя — как на «Публикации»)."""
		community = self._community_combo.selected()
		if community is None:
			self._show_error("Выберите канал (список над пресетом) — пакет публикуется в него.")
		return community

	def _publish_all_processed(self) -> None:
		"""Все видео списка — пакетом на «Публикацию»."""
		self._emit_publish_files([item for _check, item in self._processed_checks])

	def _publish_checked_processed(self) -> None:
		"""Отмеченные чекбоксами видео — пакетом на «Публикацию»."""
		items = [item for _check, item in self._processed_checks]
		checked = [item for check, item in self._processed_checks if check.isChecked()]
		picked = checked_or_single(items, checked)
		if picked is None:
			self._show_error("Отметьте чекбоксами готовые видео для публикации.")
			return
		self._emit_publish_files(picked)

	def _emit_publish_files(self, items: list[VideoFile]) -> None:
		"""Передаёт файлы пакетом на «Публикацию» (через главное окно)."""
		if not items:
			self._show_error("Готовых видео нет — публиковать нечего.")
			return
		community = self._current_community()
		if community is None:
			return
		if len(items) == 1:
			# один файл — не пакет: обычная форма публикации,
			# как у кнопки «Опубликовать…» на карточке
			self.publish_requested.emit(items[0].path, community.id)
			return
		self.publish_files_requested.emit([item.path for item in items], community.id)

	def _publish_processed_folder(self) -> None:
		"""Выбор подпапки в обработанных — вся она пакетом на «Публикацию»."""
		community = self._current_community()
		if community is None:
			return
		root = pick_dir(self, "Папка готовых видео", start_dir=self._processed_dir)
		if root:
			self.publish_folder_requested.emit(root, community.id)

	def _on_delete_processed(self, item: VideoFile) -> None:
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
		community = self._community_combo.selected()
		self.publish_requested.emit(path, community.id if community else 0)
