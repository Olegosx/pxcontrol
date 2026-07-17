"""Страница «Видео»: панель параметров обработки и подготовка файла.

Параметры живут прямо на странице: «Обработать» применяет то, что на
экране, ничего не сохраняя. Пресет — «загрузчик»: выбор в списке
заполняет панель (:mod:`video_form`), сохранение — только по явным
кнопкам. Результат — файл в папке результатов; кнопка «Опубликовать…»
передаёт его странице «Публикация» (контракт — путь к файлу). Выбор
кадра заставки — отдельный диалог (:mod:`frame_picker`).
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	FluentIcon,
	InfoBar,
	LineEdit,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	ToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import text_preview
from pxcontrol.engine.services.settings import CHANNEL_DEFAULT_PRESET
from pxcontrol.engine.services.video import (
	IntroSourceKind,
	PresetDto,
	PresetFields,
	VideoDirs,
	build_intro_source,
	parse_intro_source,
)
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	FormDialog,
	ProgressPanel,
	bind,
	clear_layout,
	confirm_delete,
	exec_dialog,
	page_layout,
	pick_file,
	row_card,
	show_error,
)
from pxcontrol.ui.pages.frame_picker import FramePickerDialog
from pxcontrol.ui.pages.video_form import PresetForm

#: Имя «пресета» в имени файла результата, когда пресет не выбран.
_MANUAL_NAME = "ручные"

#: Предел длины имени файла во всплывающей плашке (не переносит строки).
_TOAST_NAME_CHARS = 60


class VideoPage(ScrollArea):
	"""Панель параметров обработки и подготовка видеофайла."""

	#: Просьба опубликовать готовый файл: путь и id канала со страницы
	#: (0 — канал не выбран). Ловит главное окно → «Публикация».
	publish_requested = Signal(str, int)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("video")
		self._worker = worker
		self._build()
		self._reload_presets()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет список каналов при каждом открытии страницы."""
		super().showEvent(event)
		self._reload_channels()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		layout.addWidget(SubtitleLabel("Подготовка видео", self))
		self._build_source_row(layout)
		self._build_channel_row(layout)
		self._build_preset_row(layout)
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Параметры обработки", self))
		layout.addWidget(CaptionLabel(
			"К видео применяется то, что на экране; пресет — только "
			"загрузка и сохранение набора.", self,
		))
		self._form = PresetForm(self)
		layout.addWidget(self._form)
		self._build_process_row(layout)
		self._progress = ProgressPanel(self)
		layout.addWidget(self._progress)
		self._result_box = QVBoxLayout()
		layout.addLayout(self._result_box)
		layout.addStretch()

	def _build_source_row(self, layout: QVBoxLayout) -> None:
		"""Строка исходника: путь, «Обзор…» и просмотр системным плеером."""
		src_row = QHBoxLayout()
		self._source = LineEdit(self)
		self._source.setPlaceholderText("Исходный видеофайл…")
		self._source.textChanged.connect(self._on_source_changed)
		browse = PushButton("Обзор…", self)
		browse.clicked.connect(self._pick_source)
		self._play_button = ToolButton(FluentIcon.PLAY, self)
		self._play_button.setToolTip("Посмотреть выбранный файл (системный плеер)")
		self._play_button.setEnabled(False)  # активируется выбором файла
		self._play_button.clicked.connect(self._play_source)
		src_row.addWidget(self._source)
		src_row.addWidget(browse)
		src_row.addWidget(self._play_button)
		layout.addLayout(src_row)

	def _on_source_changed(self, text: str) -> None:
		"""Просмотр доступен, только когда путь указывает на существующий файл."""
		self._play_button.setEnabled(Path(text.strip()).is_file())

	def _play_source(self) -> None:
		"""Открывает исходник системным плеером (встроенного пока нет)."""
		self._open_path(str(self._source.text()).strip())

	def _build_channel_row(self, layout: QVBoxLayout) -> None:
		"""Канал: выбор подставляет его пресет по умолчанию (настройка канала)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Канал:", self))
		self._channel_combo: DtoComboBox[ChannelDto] = DtoComboBox(
			self, placeholder="(не выбран)"
		)
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
		"""Показывает ошибку и гасит индикатор прогресса."""
		self._hide_progress()
		show_error(self, message)

	def _hide_progress(self) -> None:
		"""Прячет полосу прогресса и возвращает кнопку."""
		self._progress.finish()
		self._process_button.setEnabled(True)

	# --- пресеты -------------------------------------------------------------------

	def _reload_presets(self, select_name: str | None = None) -> None:
		run_in_engine(
			self._worker, self._worker.engine.video.list_presets(),
			self, partial(self._show_presets, select_name), self._show_error,
		)

	def _show_presets(
		self, select_name: str | None, presets: list[PresetDto]
	) -> None:
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
			self._worker, self._worker.engine.channels.list_channels(),
			self, self._show_channels, self._show_error,
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
			self, partial(self._apply_channel_preset, channel), self._show_error,
		)

	def _apply_channel_preset(
		self, channel: ChannelDto, preset_id: int | None
	) -> None:
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
			self._worker, self._worker.engine.video.get_preset_fields(preset.id),
			self, self._form.fill, self._show_error,
		)

	def _on_save_preset(self) -> None:
		"""Перезаписывает выбранный пресет текущим состоянием панели."""
		preset = self._preset_combo.selected()
		if preset is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.save_preset(
				self._form.fields(preset.name), preset.id
			),
			self, self._on_preset_saved, self._show_error,
		)

	def _on_save_preset_as(self) -> None:
		"""Сохраняет состояние панели новым пресетом (спрашивает имя)."""
		dialog = FormDialog(
			"Сохранить пресет", [("name", "Имя пресета…")], self.window(),
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
			self, self._on_preset_saved, self._show_error,
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
			self._worker, self._worker.engine.video.delete_preset(preset.id),
			self, lambda *_a: self._reload_presets(), self._show_error,
		)

	# --- подготовка -----------------------------------------------------------------

	def _pick_source(self) -> None:
		"""Диалог выбора исходника — в папке исходников подпапки пресета."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker, self._worker.engine.video.dirs_for(subdir),
			self, self._open_source_dialog, self._show_error,
		)

	def _open_source_dialog(self, dirs: VideoDirs) -> None:
		"""Открывает диалог исходника в действующей папке исходников."""
		path = pick_file(
			self, "Исходное видео",
			"Видео (*.mp4 *.mov *.mkv *.avi *.webm);;Все файлы (*)",
			start_dir=dirs.source,
		)
		if path:
			self._source.setText(path)

	def _on_process(self) -> None:
		"""Собирает параметры с экрана и запускает обработку."""
		source = str(self._source.text()).strip()
		if not source:
			self._show_error("Выберите исходный видеофайл.")
			return
		preset = self._preset_combo.selected()
		fields = self._form.fields(preset.name if preset else _MANUAL_NAME)
		kind, _value = parse_intro_source(fields.intro_source)
		needs_choice = kind is IntroSourceKind.RANDOM_CHOICE and (
			fields.intro or fields.cover
		)
		if not needs_choice:
			self._start_prepare(source, fields, None)
			return
		dialog = FramePickerDialog(
			self._worker, source, self.window(),
			trim_start=fields.trim_start, trim_end=fields.trim_end,
		)
		accepted = exec_dialog(dialog)
		chosen = dialog.chosen_path()
		if not accepted or chosen is None:
			return
		self._start_prepare(
			source, fields, build_intro_source(IntroSourceKind.IMAGE, chosen)
		)

	def _start_prepare(
		self, source: str, fields: PresetFields, intro_source: str | None
	) -> None:
		"""Запускает обработку (с подменой источника кадра или без)."""
		self._process_button.setEnabled(False)
		self._progress.begin("Кодирование", "Анализ файла и подготовка кадра заставки…")
		run_in_engine(
			self._worker,
			self._worker.engine.video.prepare(
				source, fields, intro_source=intro_source,
				on_progress=self._progress.emit_progress,
			),
			self, self._on_processed, self._show_error,
		)

	def _on_processed(self, output_path: str) -> None:
		"""Показывает итог обработки и карточку результата с действиями."""
		self._hide_progress()
		path = Path(output_path)
		# всплывашка не переносит строки — длинное имя укорачиваем
		InfoBar.success("Готово", text_preview(path.name, _TOAST_NAME_CHARS), parent=self)
		clear_layout(self._result_box)
		open_btn = PushButton(FluentIcon.PLAY, "Открыть", self)
		open_btn.clicked.connect(bind(self._open_path, str(path)))
		folder_btn = PushButton(FluentIcon.FOLDER, "Показать в папке", self)
		folder_btn.clicked.connect(bind(self._open_path, str(path.parent)))
		publish_btn = PrimaryPushButton(FluentIcon.SEND, "Опубликовать…", self)
		publish_btn.clicked.connect(bind(self._request_publish, str(path)))
		buttons = QWidget(self)
		buttons_layout = QHBoxLayout(buttons)
		buttons_layout.setContentsMargins(0, 0, 0, 0)
		buttons_layout.addWidget(open_btn)
		buttons_layout.addWidget(folder_btn)
		buttons_layout.addWidget(publish_btn)
		self._result_box.addWidget(row_card(
			self, path.name, f"Результат: {path.parent}", trailing=buttons,
		))

	def _request_publish(self, path: str) -> None:
		"""Передаёт файл на «Публикацию» вместе с выбранным каналом."""
		channel = self._channel_combo.selected()
		self.publish_requested.emit(path, channel.id if channel else 0)

	@staticmethod
	def _open_path(path: str) -> None:
		"""Открывает файл или папку системным приложением."""
		QDesktopServices.openUrl(QUrl.fromLocalFile(path))
