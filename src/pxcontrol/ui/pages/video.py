"""Страница «Видео»: панель параметров обработки и подготовка файла.

Параметры живут прямо на странице: «Обработать» применяет то, что на
экране, ничего не сохраняя. Пресет — «загрузчик»: выбор в списке
заполняет панель, сохранение — только по явным кнопкам. Результат —
файл в ``media/processed``; кнопка «Опубликовать…» передаёт его
странице «Публикация» (контракт — путь к файлу).
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
	QButtonGroup,
	QFileDialog,
	QGridLayout,
	QHBoxLayout,
	QLabel,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	ComboBox,
	DoubleSpinBox,
	FluentIcon,
	IndeterminateProgressRing,
	InfoBar,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SpinBox,
	SubtitleLabel,
	SwitchButton,
	TogglePushButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.video import FrameCandidate, PresetDto, PresetFields
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	FormDialog,
	ProgressPanel,
	bind,
	clear_layout,
	confirm_delete,
	row_card,
	show_error,
)

#: Углы вотермарка: подпись → код.
_CORNERS = [
	("Правый верхний", "tr"), ("Левый верхний", "tl"),
	("Правый нижний", "br"), ("Левый нижний", "bl"),
]
#: Источники кадра заставки: подпись → код.
_INTRO_SOURCES = [
	("Случайный кадр из середины", "random-middle"),
	("Случайные кадры на выбор", "random-choice"),
	("Момент времени (сек)", "time"),
	("Своя картинка (PNG)", "image"),
]

#: Режимы источника кадра без значения (поле «секунды/путь» не нужно).
_SOURCES_WITHOUT_VALUE = {"random-middle", "random-choice"}

#: Имя «пресета» в имени файла результата, когда пресет не выбран.
_MANUAL_NAME = "ручные"


class _PresetForm(QWidget):
	"""Панель параметров обработки (бывший диалог пресета, без имени).

	Живёт прямо на странице: заполняется пресетом (:meth:`fill`),
	правится свободно, текущее состояние отдаёт :meth:`fields`.
	"""

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(10)
		self._build_watermark_block()
		self._build_intro_block()
		self._build_flags_row()

	# --- сборка ----------------------------------------------------------------

	def _build_watermark_block(self) -> None:
		"""Вотермарк: файл, угол, отступ, прозрачность, масштаб, окно, плавность."""
		self._layout.addWidget(BodyLabel("Вотермарк (PNG, пусто — без него):", self))
		file_row = QHBoxLayout()
		self._wm_path = LineEdit(self)
		self._wm_path.setPlaceholderText("Файл вотермарка…")
		browse = PushButton("Обзор…", self)
		browse.clicked.connect(self._pick_watermark)
		file_row.addWidget(self._wm_path)
		file_row.addWidget(browse)
		self._layout.addLayout(file_row)
		row = QHBoxLayout()
		self._corner = ComboBox(self)
		for label, _code in _CORNERS:
			self._corner.addItem(label)
		self._margin = self._spin(row, "отступ от края, пикселей", 0, 200, 24)
		self._opacity = self._dspin(row, "прозрачность", 0.05, 1.0, 1.0, 0.05)
		self._scale = self._dspin(row, "масштаб (доля ширины кадра)", 0.05, 0.5, 0.15, 0.01)
		row.insertWidget(0, self._corner)
		row.addStretch()
		self._layout.addLayout(row)
		self._build_watermark_window_row()

	def _build_watermark_window_row(self) -> None:
		"""Окно показа вотермарка: отступы от краёв и плавность переходов."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Показ вотермарка:", self))
		self._wm_start = self._dspin(
			row, "появление через N секунд (0 — с начала)", 0.0, 3600.0, 0.0, 1.0
		)
		row.addWidget(CaptionLabel("с после начала", self))
		self._wm_end = self._dspin(
			row, "скрыть за N секунд до конца (0 — до конца)", 0.0, 3600.0, 0.0, 1.0
		)
		row.addWidget(CaptionLabel("с до конца", self))
		self._wm_fade = self._dspin(
			row, "плавность появления/исчезания, с (0 — резко)", 0.0, 30.0, 0.0, 0.5
		)
		row.addWidget(CaptionLabel("с плавность", self))
		row.addStretch()
		self._layout.addLayout(row)

	def _build_intro_block(self) -> None:
		"""Заставка: включение, источник кадра, тайминги."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Заставка для превью:", self))
		self._intro = SwitchButton(self)
		row.addWidget(self._intro)
		row.addStretch()
		self._hold = self._dspin(row, "держать, с", 0.2, 5.0, 1.0, 0.1)
		self._xfade = self._dspin(row, "растворение, с", 0.1, 3.0, 0.5, 0.1)
		self._layout.addLayout(row)
		src_row = QHBoxLayout()
		self._intro_kind = ComboBox(self)
		for label, _code in _INTRO_SOURCES:
			self._intro_kind.addItem(label)
		self._intro_value = LineEdit(self)
		self._intro_value.setPlaceholderText("секунды или путь к картинке")
		src_row.addWidget(self._intro_kind)
		src_row.addWidget(self._intro_value)
		self._layout.addLayout(src_row)
		self._intro.checkedChanged.connect(self._toggle_intro_controls)
		self._toggle_intro_controls(False)

	def _toggle_intro_controls(self, enabled: bool) -> None:
		"""Поля заставки активны только при включённом переключателе."""
		for widget in (self._hold, self._xfade, self._intro_kind, self._intro_value):
			widget.setEnabled(enabled)

	def _build_flags_row(self) -> None:
		"""Обложка, звук и качество."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Вшить обложку:", self))
		self._cover = SwitchButton(self)
		row.addWidget(self._cover)
		row.addSpacing(24)
		row.addWidget(BodyLabel("Убрать звук:", self))
		self._no_audio = SwitchButton(self)
		row.addWidget(self._no_audio)
		row.addSpacing(24)
		row.addWidget(BodyLabel("Качество, Мбит/с:", self))
		self._bitrate = self._dspin(row, "битрейт видео", 0.0, 50.0, 0.0, 0.5)
		row.addWidget(CaptionLabel("0 — как в оригинале", self))
		row.addStretch()
		self._layout.addLayout(row)

	def _spin(self, row: QHBoxLayout, tip: str, lo: int, hi: int, val: int) -> SpinBox:
		box = SpinBox(self)
		box.setRange(lo, hi)
		box.setValue(val)
		box.setToolTip(tip)
		row.addWidget(box)
		return box

	def _dspin(
		self, row: QHBoxLayout, tip: str, lo: float, hi: float, val: float, step: float
	) -> DoubleSpinBox:
		box = DoubleSpinBox(self)
		box.setRange(lo, hi)
		box.setSingleStep(step)
		box.setValue(val)
		box.setToolTip(tip)
		row.addWidget(box)
		return box

	def _pick_watermark(self) -> None:
		path, _ = QFileDialog.getOpenFileName(
			self, "Файл вотермарка", "", "Изображения (*.png)"
		)
		if path:
			self._wm_path.setText(path)

	# --- значения ---------------------------------------------------------------

	def fill(self, fields: PresetFields) -> None:
		"""Заполняет панель полями пресета."""
		self._wm_path.setText(fields.watermark_path or "")
		codes = [code for _label, code in _CORNERS]
		self._corner.setCurrentIndex(codes.index(fields.wm_corner))
		self._margin.setValue(fields.wm_margin)
		self._opacity.setValue(fields.wm_opacity)
		self._scale.setValue(fields.wm_scale)
		self._wm_start.setValue(fields.wm_start_offset or 0.0)
		self._wm_end.setValue(fields.wm_end_offset or 0.0)
		self._wm_fade.setValue(fields.wm_fade)
		self._intro.setChecked(fields.intro)
		self._hold.setValue(fields.intro_hold)
		self._xfade.setValue(fields.xfade)
		kind, _sep, value = fields.intro_source.partition(":")
		kinds = [code for _label, code in _INTRO_SOURCES]
		self._intro_kind.setCurrentIndex(kinds.index(kind) if kind in kinds else 0)
		self._intro_value.setText(value)
		self._cover.setChecked(fields.cover)
		self._no_audio.setChecked(fields.no_audio)
		kbps = fields.video_bitrate_kbps
		self._bitrate.setValue(kbps / 1000 if kbps else 0.0)

	def _intro_source(self) -> str:
		"""Собирает строку источника кадра ('random-middle'/'time:…'/'image:…')."""
		kind = _INTRO_SOURCES[int(self._intro_kind.currentIndex())][1]
		if kind in _SOURCES_WITHOUT_VALUE:
			return kind
		return f"{kind}:{str(self._intro_value.text()).strip()}"

	def fields(self, name: str) -> PresetFields:
		"""Текущее состояние панели как поля пресета (имя — от вызывающего)."""
		return PresetFields(
			name=name,
			watermark_path=str(self._wm_path.text()).strip() or None,
			wm_corner=_CORNERS[int(self._corner.currentIndex())][1],
			wm_margin=int(self._margin.value()),
			wm_opacity=round(float(self._opacity.value()), 3),
			wm_scale=round(float(self._scale.value()), 3),
			wm_start_offset=float(self._wm_start.value()) or None,
			wm_end_offset=float(self._wm_end.value()) or None,
			wm_fade=round(float(self._wm_fade.value()), 2),
			intro=self._intro.isChecked(),
			intro_source=self._intro_source(),
			intro_hold=round(float(self._hold.value()), 2),
			xfade=round(float(self._xfade.value()), 2),
			cover=self._cover.isChecked(),
			no_audio=self._no_audio.isChecked(),
			video_bitrate_kbps=self._bitrate_kbps(),
		)

	def _bitrate_kbps(self) -> int | None:
		"""Битрейт из регулятора: Мбит/с → кбит/с; 0 — «как в оригинале»."""
		mbps = float(self._bitrate.value())
		return int(round(mbps * 1000)) if mbps > 0 else None


class _FrameTileButton(TogglePushButton):
	"""Кнопка-плитка кадра: двойной клик подтверждает выбор."""

	doubleClicked = Signal()

	def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		super().mouseDoubleClickEvent(event)
		self.doubleClicked.emit()


class _FramePickerDialog(MessageBoxBase):
	"""Выбор кадра заставки из случайных кандидатов (плиткой).

	Кандидаты извлечены движком в финальном качестве; выбранный файл
	уходит в обработку как есть (`image:<путь>`) — без повторного
	извлечения и риска соседнего кадра.
	"""

	def __init__(
		self, worker: EngineWorker, source_path: str, parent: QWidget
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._source = source_path
		self._chosen: str | None = None
		self._group = QButtonGroup(self)
		self._group.setExclusive(True)
		self.viewLayout.addWidget(SubtitleLabel("Выберите кадр заставки", self))
		self._build_controls_row()
		self._grid_box = QWidget(self)
		self._grid = QGridLayout(self._grid_box)
		self.viewLayout.addWidget(self._grid_box)
		self._ring = IndeterminateProgressRing(self)
		self._ring.setFixedSize(48, 48)
		self.viewLayout.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignHCenter)
		self.yesButton.setText("Использовать кадр")
		self.yesButton.setEnabled(False)
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(760)
		self._reload()

	def _build_controls_row(self) -> None:
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Кадров:", self))
		self._count = SpinBox(self)
		self._count.setRange(2, 12)
		self._count.setValue(6)
		row.addWidget(self._count)
		self._refresh = PushButton(FluentIcon.SYNC, "Обновить", self)
		self._refresh.clicked.connect(self._reload)
		row.addWidget(self._refresh)
		row.addStretch()
		self.viewLayout.addLayout(row)

	def chosen_path(self) -> str | None:
		"""Путь к выбранному кадру (None — не выбран)."""
		return self._chosen

	def _reload(self) -> None:
		"""Запрашивает новую партию: чистит плитку и крутит колёсико."""
		self.yesButton.setEnabled(False)
		self._refresh.setEnabled(False)
		self._chosen = None
		self._clear_grid()
		self._ring.show()
		run_in_engine(
			self._worker,
			self._worker.engine.video.extract_random_frames(
				self._source, int(self._count.value())
			),
			self, self._show_frames, self._show_error,
		)

	def _clear_grid(self) -> None:
		"""Убирает плитку кандидатов."""
		while self._grid.count():
			item = self._grid.takeAt(0)
			widget = item.widget() if item else None
			if widget is not None:
				widget.deleteLater()
		for button in self._group.buttons():
			self._group.removeButton(button)

	def _show_frames(self, frames: list[FrameCandidate]) -> None:
		"""Перерисовывает плитку кандидатов."""
		self._ring.hide()
		self._refresh.setEnabled(True)
		for index, frame in enumerate(frames):
			self._grid.addWidget(self._frame_tile(frame), index // 3, index % 3)
		self.widget.adjustSize()

	def _frame_tile(self, frame: FrameCandidate) -> QWidget:
		"""Плитка кандидата: миниатюра по центру, время подписью снизу."""
		tile = QWidget(self._grid_box)
		column = QVBoxLayout(tile)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(2)
		button = _FrameTileButton(tile)
		button.setFixedSize(QSize(232, 133))
		# картинка — QLabel внутри кнопки: родная отрисовка иконки
		# смещала её от центра и обрезала; подпись прозрачна для мыши
		inner = QVBoxLayout(button)
		inner.setContentsMargins(8, 8, 8, 8)
		image = QLabel(button)
		image.setPixmap(QPixmap(frame.path).scaled(
			216, 117, Qt.AspectRatioMode.KeepAspectRatio,
			Qt.TransformationMode.SmoothTransformation,
		))
		image.setAlignment(Qt.AlignmentFlag.AlignCenter)
		image.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
		inner.addWidget(image)
		button.toggled.connect(partial(self._on_toggled, frame.path))
		button.doubleClicked.connect(partial(self._on_double_clicked, frame.path))
		self._group.addButton(button)
		column.addWidget(button)
		minutes, seconds = divmod(int(frame.timestamp), 60)
		caption = CaptionLabel(f"{minutes}:{seconds:02d}", tile)
		caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
		column.addWidget(caption)
		return tile

	def _on_toggled(self, path: str, checked: bool) -> None:
		if checked:
			self._chosen = path
			self.yesButton.setEnabled(True)

	def _on_double_clicked(self, path: str) -> None:
		"""Двойной клик по плитке = выбрать кадр и «Использовать кадр»."""
		self._chosen = path
		self.yesButton.setEnabled(True)
		self.yesButton.click()

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку и останавливает колёсико."""
		self._ring.hide()
		self._refresh.setEnabled(True)
		show_error(self, message)


class VideoPage(ScrollArea):
	"""Панель параметров обработки и подготовка видеофайла."""

	#: Просьба опубликовать готовый файл (ловит главное окно → «Публикация»).
	publish_requested = Signal(str)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("video")
		self._worker = worker
		self._presets: list[PresetDto] = []
		self._build()
		self._reload_presets()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		container = QWidget(self)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.setSpacing(16)
		layout.addWidget(SubtitleLabel("Подготовка видео", self))
		self._build_source_row(layout)
		self._build_preset_row(layout)
		layout.addWidget(CaptionLabel(
			"Параметры: к видео применяется то, что на экране; "
			"пресет — только загрузка и сохранение набора.", self,
		))
		self._form = _PresetForm(self)
		layout.addWidget(self._form)
		self._build_process_row(layout)
		self._progress = ProgressPanel(self)
		layout.addWidget(self._progress)
		self._result_box = QVBoxLayout()
		layout.addLayout(self._result_box)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()

	def _build_source_row(self, layout: QVBoxLayout) -> None:
		src_row = QHBoxLayout()
		self._source = LineEdit(self)
		self._source.setPlaceholderText("Исходный видеофайл…")
		browse = PushButton("Обзор…", self)
		browse.clicked.connect(self._pick_source)
		src_row.addWidget(self._source)
		src_row.addWidget(browse)
		layout.addLayout(src_row)

	def _build_preset_row(self, layout: QVBoxLayout) -> None:
		"""Пресет: выбор-загрузка и кнопки сохранения/удаления."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Пресет:", self))
		self._preset_combo = ComboBox(self)
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
		"""Наполняет список пресетов; первый пункт — «свои настройки»."""
		self._presets = presets
		self._preset_combo.blockSignals(True)
		self._preset_combo.clear()
		self._preset_combo.addItem("(свои настройки)")
		for preset in presets:
			self._preset_combo.addItem(preset.name)
		index = 0
		if select_name is not None:
			names = [p.name for p in presets]
			index = names.index(select_name) + 1 if select_name in names else 0
		self._preset_combo.setCurrentIndex(index)
		self._preset_combo.blockSignals(False)
		self._update_preset_buttons()

	def _selected_preset(self) -> PresetDto | None:
		"""Выбранный пресет (None — «свои настройки»)."""
		index = int(self._preset_combo.currentIndex()) - 1
		if 0 <= index < len(self._presets):
			return self._presets[index]
		return None

	def _update_preset_buttons(self) -> None:
		"""«Сохранить»/«Удалить» доступны только при выбранном пресете."""
		has_preset = self._selected_preset() is not None
		self._save_button.setEnabled(has_preset)
		self._delete_button.setEnabled(has_preset)

	def _on_preset_selected(self, _index: int) -> None:
		"""Выбор пресета — загрузка его значений в панель."""
		self._update_preset_buttons()
		preset = self._selected_preset()
		if preset is None:
			return
		run_in_engine(
			self._worker, self._worker.engine.video.get_preset_fields(preset.id),
			self, self._form.fill, self._show_error,
		)

	def _on_save_preset(self) -> None:
		"""Перезаписывает выбранный пресет текущим состоянием панели."""
		preset = self._selected_preset()
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
		if not dialog.exec():
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
		preset = self._selected_preset()
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
		path, _ = QFileDialog.getOpenFileName(
			self, "Исходное видео", "",
			"Видео (*.mp4 *.mov *.mkv *.avi *.webm);;Все файлы (*)",
		)
		if path:
			self._source.setText(path)

	def _on_process(self) -> None:
		"""Собирает параметры с экрана и запускает обработку."""
		source = str(self._source.text()).strip()
		if not source:
			self._show_error("Выберите исходный видеофайл.")
			return
		preset = self._selected_preset()
		fields = self._form.fields(preset.name if preset else _MANUAL_NAME)
		needs_choice = fields.intro_source == "random-choice" and (
			fields.intro or fields.cover
		)
		if not needs_choice:
			self._start_prepare(source, fields, None)
			return
		dialog = _FramePickerDialog(self._worker, source, self.window())
		if not dialog.exec() or dialog.chosen_path() is None:
			return
		self._start_prepare(source, fields, f"image:{dialog.chosen_path()}")

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

	def _on_processed(self, output_path: object) -> None:
		self._hide_progress()
		path = Path(str(output_path))
		# всплывашка не переносит строки — длинное имя укорачиваем
		short = path.name if len(path.name) <= 60 else f"{path.name[:57]}…"
		InfoBar.success("Готово", short, parent=self)
		clear_layout(self._result_box)
		open_btn = PushButton(FluentIcon.PLAY, "Открыть", self)
		open_btn.clicked.connect(bind(self._open_path, str(path)))
		folder_btn = PushButton(FluentIcon.FOLDER, "Показать в папке", self)
		folder_btn.clicked.connect(bind(self._open_path, str(path.parent)))
		publish_btn = PrimaryPushButton(FluentIcon.SEND, "Опубликовать…", self)
		publish_btn.clicked.connect(bind(self.publish_requested.emit, str(path)))
		buttons = QWidget(self)
		buttons_layout = QHBoxLayout(buttons)
		buttons_layout.setContentsMargins(0, 0, 0, 0)
		buttons_layout.addWidget(open_btn)
		buttons_layout.addWidget(folder_btn)
		buttons_layout.addWidget(publish_btn)
		self._result_box.addWidget(row_card(
			self, path.name, f"Результат: {path.parent}", trailing=buttons,
		))

	@staticmethod
	def _open_path(path: str) -> None:
		"""Открывает файл или папку системным приложением."""
		QDesktopServices.openUrl(QUrl.fromLocalFile(path))
