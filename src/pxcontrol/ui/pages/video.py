"""Страница «Видео»: пресеты обработки и подготовка файла.

Подготовка отделена от публикации: результат — файл в ``media/processed``;
публикация видео — отдельная функция (следующий шаг). Контракт между
ними — путь к файлу.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	ComboBox,
	DoubleSpinBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	ProgressBar,
	PushButton,
	ScrollArea,
	SpinBox,
	SubtitleLabel,
	SwitchButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.video import PresetDto, PresetFields
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import bind, clear_layout, confirm_delete, row_card, show_error

#: Углы вотермарка: подпись → код.
_CORNERS = [
	("Правый верхний", "tr"), ("Левый верхний", "tl"),
	("Правый нижний", "br"), ("Левый нижний", "bl"),
]
#: Источники кадра заставки: подпись → код.
_INTRO_SOURCES = [
	("Случайный кадр из середины", "random-middle"),
	("Момент времени (сек)", "time"),
	("Своя картинка (PNG)", "image"),
]


class _ProgressRelay(QObject):
	"""Мост прогресса: колбэк из потока движка → сигнал в поток интерфейса."""

	progressed = Signal(float)


class _PresetDialog(MessageBoxBase):
	"""Диалог создания/правки пресета обработки."""

	def __init__(self, parent: QWidget, fields: PresetFields | None = None) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel(
			"Пресет обработки" if fields is None else "Правка пресета", self,
		))
		self._build_controls()
		if fields is not None:
			self._fill(fields)
		self.yesButton.setText("Сохранить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(520)

	# --- сборка ----------------------------------------------------------------

	def _build_controls(self) -> None:
		"""Собирает все поля диалога."""
		self._name = LineEdit(self)
		self._name.setPlaceholderText("Название пресета")
		self.viewLayout.addWidget(self._name)
		self._build_watermark_block()
		self._build_intro_block()
		self._build_flags_row()
		self._build_quality_row()

	def _build_watermark_block(self) -> None:
		"""Вотермарк: файл, угол, отступ, прозрачность, масштаб."""
		self.viewLayout.addWidget(BodyLabel("Вотермарк (PNG, пусто — без него):", self))
		file_row = QHBoxLayout()
		self._wm_path = LineEdit(self)
		self._wm_path.setPlaceholderText("Файл вотермарка…")
		browse = PushButton("Обзор…", self)
		browse.clicked.connect(self._pick_watermark)
		file_row.addWidget(self._wm_path)
		file_row.addWidget(browse)
		self.viewLayout.addLayout(file_row)
		row = QHBoxLayout()
		self._corner = ComboBox(self)
		for label, _code in _CORNERS:
			self._corner.addItem(label)
		self._margin = self._spin(row, "отступ", 0, 200, 24)
		self._opacity = self._dspin(row, "прозрачность", 0.05, 1.0, 1.0, 0.05)
		self._scale = self._dspin(row, "масштаб", 0.05, 0.5, 0.15, 0.01)
		row.insertWidget(0, self._corner)
		self.viewLayout.addLayout(row)

	def _build_intro_block(self) -> None:
		"""Заставка: включение, источник кадра, тайминги."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Заставка для превью:", self))
		self._intro = SwitchButton(self)
		row.addWidget(self._intro)
		row.addStretch()
		self._hold = self._dspin(row, "держать, с", 0.2, 5.0, 1.0, 0.1)
		self._xfade = self._dspin(row, "растворение, с", 0.1, 3.0, 0.5, 0.1)
		self.viewLayout.addLayout(row)
		src_row = QHBoxLayout()
		self._intro_kind = ComboBox(self)
		for label, _code in _INTRO_SOURCES:
			self._intro_kind.addItem(label)
		self._intro_value = LineEdit(self)
		self._intro_value.setPlaceholderText("секунды или путь к картинке")
		src_row.addWidget(self._intro_kind)
		src_row.addWidget(self._intro_value)
		self.viewLayout.addLayout(src_row)

	def _build_flags_row(self) -> None:
		"""Обложка и звук."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Вшить обложку:", self))
		self._cover = SwitchButton(self)
		row.addWidget(self._cover)
		row.addSpacing(24)
		row.addWidget(BodyLabel("Убрать звук:", self))
		self._no_audio = SwitchButton(self)
		row.addWidget(self._no_audio)
		row.addStretch()
		self.viewLayout.addLayout(row)

	def _build_quality_row(self) -> None:
		"""Качество видео: целевой битрейт (0 — как в оригинале)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Качество видео, Мбит/с:", self))
		self._bitrate = self._dspin(row, "битрейт видео", 0.0, 50.0, 0.0, 0.5)
		row.addWidget(CaptionLabel("0 — как в оригинале", self))
		row.addStretch()
		self.viewLayout.addLayout(row)

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

	def _fill(self, fields: PresetFields) -> None:
		"""Заполняет диалог полями существующего пресета."""
		self._name.setText(fields.name)
		self._wm_path.setText(fields.watermark_path or "")
		codes = [code for _label, code in _CORNERS]
		self._corner.setCurrentIndex(codes.index(fields.wm_corner))
		self._margin.setValue(fields.wm_margin)
		self._opacity.setValue(fields.wm_opacity)
		self._scale.setValue(fields.wm_scale)
		self._intro.setChecked(fields.intro)
		self._hold.setValue(fields.intro_hold)
		self._xfade.setValue(fields.xfade)
		kind, _sep, value = fields.intro_source.partition(":")
		kinds = [code for _label, code in _INTRO_SOURCES]
		self._intro_kind.setCurrentIndex(
			kinds.index(kind) if kind in kinds else 0
		)
		self._intro_value.setText(value)
		self._cover.setChecked(fields.cover)
		self._no_audio.setChecked(fields.no_audio)
		kbps = fields.video_bitrate_kbps
		self._bitrate.setValue(kbps / 1000 if kbps else 0.0)

	def _intro_source(self) -> str:
		"""Собирает строку источника кадра ('random-middle'/'time:…'/'image:…')."""
		kind = _INTRO_SOURCES[int(self._intro_kind.currentIndex())][1]
		if kind == "random-middle":
			return kind
		return f"{kind}:{str(self._intro_value.text()).strip()}"

	def fields(self) -> PresetFields:
		"""Возвращает заполненные поля пресета."""
		return PresetFields(
			name=str(self._name.text()).strip(),
			watermark_path=str(self._wm_path.text()).strip() or None,
			wm_corner=_CORNERS[int(self._corner.currentIndex())][1],
			wm_margin=int(self._margin.value()),
			wm_opacity=round(float(self._opacity.value()), 3),
			wm_scale=round(float(self._scale.value()), 3),
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


class VideoPage(ScrollArea):
	"""Пресеты обработки и подготовка видеофайла."""

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
		self._build_presets_header(layout)
		self._presets_list = QVBoxLayout()
		self._presets_list.setSpacing(8)
		layout.addLayout(self._presets_list)
		layout.addSpacing(16)
		self._build_process_block(layout)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()

	def _build_presets_header(self, layout: QVBoxLayout) -> None:
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Пресеты обработки", self))
		header.addStretch()
		add = PushButton(FluentIcon.ADD, "Новый пресет", self)
		add.clicked.connect(self._on_add_preset)
		header.addWidget(add)
		layout.addLayout(header)

	def _build_process_block(self, layout: QVBoxLayout) -> None:
		layout.addWidget(SubtitleLabel("Подготовка видео", self))
		src_row = QHBoxLayout()
		self._source = LineEdit(self)
		self._source.setPlaceholderText("Исходный видеофайл…")
		browse = PushButton("Обзор…", self)
		browse.clicked.connect(self._pick_source)
		src_row.addWidget(self._source)
		src_row.addWidget(browse)
		layout.addLayout(src_row)
		run_row = QHBoxLayout()
		self._preset_combo = ComboBox(self)
		self._process_button = PrimaryPushButton(FluentIcon.PLAY, "Обработать", self)
		self._process_button.clicked.connect(self._on_process)
		run_row.addWidget(self._preset_combo)
		run_row.addWidget(self._process_button)
		run_row.addStretch()
		layout.addLayout(run_row)
		self._progress = ProgressBar(self)
		self._progress.setRange(0, 100)
		self._progress.hide()
		layout.addWidget(self._progress)
		self._progress_label = CaptionLabel("", self)
		self._progress_label.hide()
		layout.addWidget(self._progress_label)
		self._relay = _ProgressRelay(self)
		self._relay.progressed.connect(self._on_progress)
		self._result_box = QVBoxLayout()
		layout.addLayout(self._result_box)

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку и гасит индикатор прогресса."""
		self._hide_progress()
		show_error(self, message)

	def _hide_progress(self) -> None:
		"""Прячет полосу прогресса и возвращает кнопку."""
		self._progress.hide()
		self._progress_label.hide()
		self._process_button.setEnabled(True)

	def _on_progress(self, fraction: float) -> None:
		"""Обновляет полосу и подпись хода кодирования (сигнал из движка)."""
		percent = int(fraction * 100)
		self._progress.setValue(percent)
		self._progress_label.setText(f"Кодирование: {percent}%")

	# --- пресеты -------------------------------------------------------------------

	def _reload_presets(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.video.list_presets(),
			self, self._show_presets, self._show_error,
		)

	def _show_presets(self, presets: list[PresetDto]) -> None:
		self._presets = presets
		clear_layout(self._presets_list)
		self._preset_combo.clear()
		if not presets:
			self._presets_list.addWidget(CaptionLabel(
				"Пока нет пресетов — создайте первый: вотермарк, заставка, обложка.",
				self,
			))
			return
		for preset in presets:
			edit = PushButton("Изменить", self)
			edit.clicked.connect(bind(self._on_edit_preset, preset))
			self._presets_list.addWidget(row_card(
				self, preset.name, preset.summary,
				trailing=edit, on_delete=bind(self._delete_preset, preset),
			))
			self._preset_combo.addItem(preset.name)

	def _on_add_preset(self) -> None:
		dialog = _PresetDialog(self.window())
		if dialog.exec():
			self._save_preset(dialog.fields(), None)

	def _on_edit_preset(self, preset: PresetDto) -> None:
		run_in_engine(
			self._worker, self._worker.engine.video.get_preset_fields(preset.id),
			self, partial(self._edit_with_fields, preset.id), self._show_error,
		)

	def _edit_with_fields(self, preset_id: int, fields: PresetFields) -> None:
		"""Открывает диалог правки с предзаполненными полями."""
		dialog = _PresetDialog(self.window(), fields)
		if dialog.exec():
			self._save_preset(dialog.fields(), preset_id)

	def _save_preset(self, fields: PresetFields, preset_id: int | None) -> None:
		if not fields.name:
			self._show_error("У пресета должно быть название.")
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.save_preset(fields, preset_id),
			self, self._on_preset_saved, self._show_error,
		)

	def _on_preset_saved(self, preset: PresetDto) -> None:
		InfoBar.success("Пресет сохранён", preset.name, parent=self)
		self._reload_presets()

	def _delete_preset(self, preset: PresetDto) -> None:
		if not confirm_delete(self, f"Удалить пресет «{preset.name}»?"):
			return
		run_in_engine(
			self._worker, self._worker.engine.video.delete_preset(preset.id),
			self, self._reload_presets, self._show_error,
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
		source = str(self._source.text()).strip()
		if not source:
			self._show_error("Выберите исходный видеофайл.")
			return
		index = int(self._preset_combo.currentIndex())
		if index < 0 or index >= len(self._presets):
			self._show_error("Создайте и выберите пресет обработки.")
			return
		self._process_button.setEnabled(False)
		self._progress.setValue(0)
		self._progress.show()
		self._progress_label.setText("Анализ файла и подготовка кадра заставки…")
		self._progress_label.show()
		run_in_engine(
			self._worker,
			self._worker.engine.video.prepare(
				source, self._presets[index].id,
				on_progress=self._relay.progressed.emit,
			),
			self, self._on_processed, self._show_error,
		)

	def _on_processed(self, output_path: object) -> None:
		self._hide_progress()
		path = Path(str(output_path))
		InfoBar.success("Готово", path.name, parent=self)
		clear_layout(self._result_box)
		open_btn = PushButton(FluentIcon.PLAY, "Открыть", self)
		open_btn.clicked.connect(bind(self._open_path, str(path)))
		folder_btn = PushButton(FluentIcon.FOLDER, "Показать в папке", self)
		folder_btn.clicked.connect(bind(self._open_path, str(path.parent)))
		buttons = QWidget(self)
		buttons_layout = QHBoxLayout(buttons)
		buttons_layout.setContentsMargins(0, 0, 0, 0)
		buttons_layout.addWidget(open_btn)
		buttons_layout.addWidget(folder_btn)
		self._result_box.addWidget(row_card(
			self, path.name, f"Результат: {path.parent}", trailing=buttons,
		))

	@staticmethod
	def _open_path(path: str) -> None:
		"""Открывает файл или папку системным приложением."""
		QDesktopServices.openUrl(QUrl.fromLocalFile(path))
