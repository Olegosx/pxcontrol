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
	QGridLayout,
	QHBoxLayout,
	QLabel,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
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
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TogglePushButton,
	ToolButton,
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
	pick_file,
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

#: Колонок в плитке выбора кадра заставки.
_FRAME_GRID_COLUMNS = 3


class _PresetForm(QWidget):
	"""Панель параметров обработки (бывший диалог пресета, без имени).

	Живёт прямо на странице: заполняется пресетом (:meth:`fill`),
	правится свободно, текущее состояние отдаёт :meth:`fields`.
	"""

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(12)
		self._layout.addWidget(self._trim_card())
		self._layout.addWidget(self._watermark_card())
		self._layout.addWidget(self._intro_card())
		self._layout.addWidget(self._output_card())

	# --- сборка ----------------------------------------------------------------

	def _card(self, title: str) -> tuple[CardWidget, QVBoxLayout]:
		"""Карточка-раздел с подзаголовком."""
		card = CardWidget(self)
		box = QVBoxLayout(card)
		box.setContentsMargins(16, 12, 16, 12)
		box.setSpacing(10)
		box.addWidget(StrongBodyLabel(title, card))
		return card, box

	@staticmethod
	def _labeled(row: QHBoxLayout, text: str, widget: QWidget) -> None:
		"""Пара «подпись: контрол» в строке (с отступом после)."""
		row.addWidget(BodyLabel(text, widget.parentWidget()))
		row.addWidget(widget)
		row.addSpacing(16)

	def _trim_card(self) -> CardWidget:
		"""Раздел «Обрезка»: отрезаемые края; остальное считается от результата."""
		card, box = self._card("Обрезка")
		row = QHBoxLayout()
		self._trim_start = self._dspin(
			card, "0 — не резать", 0.0, 36000.0, 0.0, 0.1
		)
		self._labeled(row, "Отрезать в начале, с:", self._trim_start)
		self._trim_end = self._dspin(
			card, "0 — не резать", 0.0, 36000.0, 0.0, 0.1
		)
		self._labeled(row, "Отрезать в конце, с:", self._trim_end)
		row.addWidget(CaptionLabel(
			"остальные параметры — от обрезанной версии", card
		))
		row.addStretch()
		box.addLayout(row)
		return card

	def _watermark_card(self) -> CardWidget:
		"""Раздел «Вотермарк»: файл, вид, окно показа, плавность."""
		card, box = self._card("Вотермарк")
		file_row = QHBoxLayout()
		file_row.addWidget(BodyLabel("Файл PNG:", card))
		self._wm_path = LineEdit(card)
		self._wm_path.setPlaceholderText("пусто — без вотермарка…")
		browse = PushButton("Обзор…", card)
		browse.clicked.connect(self._pick_watermark)
		file_row.addWidget(self._wm_path, stretch=1)
		file_row.addWidget(browse)
		box.addLayout(file_row)
		look = QHBoxLayout()
		self._corner = ComboBox(card)
		for label, _code in _CORNERS:
			self._corner.addItem(label)
		self._labeled(look, "Угол:", self._corner)
		self._margin = self._spin(card, "отступ вотермарка от края кадра", 0, 200, 24)
		self._labeled(look, "Отступ, пикс:", self._margin)
		self._opacity = self._dspin(card, "1 — непрозрачен", 0.05, 1.0, 1.0, 0.05)
		self._labeled(look, "Прозрачность:", self._opacity)
		self._scale = self._dspin(card, "доля ширины кадра", 0.05, 0.5, 0.15, 0.01)
		self._labeled(look, "Масштаб:", self._scale)
		look.addStretch()
		box.addLayout(look)
		box.addLayout(self._watermark_window_row(card))
		return card

	def _watermark_window_row(self, card: CardWidget) -> QHBoxLayout:
		"""Строка окна показа: отступы от краёв ролика и плавность."""
		row = QHBoxLayout()
		self._wm_start = self._dspin(card, "0 — виден с самого начала", 0.0, 3600.0, 0.0, 1.0)
		self._labeled(row, "Появление через, с:", self._wm_start)
		self._wm_end = self._dspin(card, "0 — виден до самого конца", 0.0, 3600.0, 0.0, 1.0)
		self._labeled(row, "Скрыть за, с до конца:", self._wm_end)
		self._wm_fade = self._dspin(card, "0 — появляется/исчезает резко", 0.0, 30.0, 0.0, 0.5)
		self._labeled(row, "Плавность, с:", self._wm_fade)
		row.addStretch()
		return row

	def _intro_card(self) -> CardWidget:
		"""Раздел «Кадр для превью»: заставка в начале ролика."""
		card, box = self._card("Кадр для превью (заставка)")
		top = QHBoxLayout()
		self._intro = SwitchButton(card)
		self._labeled(top, "Включена:", self._intro)
		self._hold = self._dspin(card, "сколько секунд держать кадр", 0.2, 5.0, 1.0, 0.1)
		self._labeled(top, "Держать, с:", self._hold)
		self._xfade = self._dspin(card, "длительность растворения в видео", 0.1, 3.0, 0.5, 0.1)
		self._labeled(top, "Растворение, с:", self._xfade)
		top.addStretch()
		box.addLayout(top)
		src_row = QHBoxLayout()
		self._intro_kind = ComboBox(card)
		for label, _code in _INTRO_SOURCES:
			self._intro_kind.addItem(label)
		self._labeled(src_row, "Источник кадра:", self._intro_kind)
		src_row.addWidget(BodyLabel("Значение:", card))
		self._intro_value = LineEdit(card)
		self._intro_value.setPlaceholderText("секунды или путь к картинке")
		src_row.addWidget(self._intro_value, stretch=1)
		box.addLayout(src_row)
		self._intro.checkedChanged.connect(self._toggle_intro_controls)
		self._toggle_intro_controls(False)
		return card

	def _toggle_intro_controls(self, enabled: bool) -> None:
		"""Поля заставки активны только при включённом переключателе."""
		for widget in (self._hold, self._xfade, self._intro_kind, self._intro_value):
			widget.setEnabled(enabled)

	def _output_card(self) -> CardWidget:
		"""Раздел «Вывод»: обложка, звук, качество, затухание на краях."""
		card, box = self._card("Вывод")
		row = QHBoxLayout()
		self._cover = SwitchButton(card)
		self._labeled(row, "Вшить обложку:", self._cover)
		self._no_audio = SwitchButton(card)
		self._labeled(row, "Убрать звук:", self._no_audio)
		self._bitrate = self._dspin(card, "битрейт видео", 0.0, 50.0, 0.0, 0.5)
		self._labeled(row, "Качество, Мбит/с:", self._bitrate)
		row.addWidget(CaptionLabel("0 — как в оригинале", card))
		row.addStretch()
		box.addLayout(row)
		fade_row = QHBoxLayout()
		self._fade_in = self._dspin(card, "0 — без эффекта", 0.0, 30.0, 0.0, 0.1)
		self._labeled(fade_row, "Затухание в начале, с:", self._fade_in)
		self._fade_out = self._dspin(card, "0 — без эффекта", 0.0, 30.0, 0.0, 0.1)
		self._labeled(fade_row, "Затухание в конце, с:", self._fade_out)
		fade_row.addWidget(CaptionLabel(
			"появление из чёрного / уход в чёрное; видео и звук", card
		))
		fade_row.addStretch()
		box.addLayout(fade_row)
		comment_row = QHBoxLayout()
		comment_row.addWidget(BodyLabel("Комментарий (метаданные):", card))
		self._meta_comment = LineEdit(card)
		self._meta_comment.setPlaceholderText(
			"https://t.me/канал — описание (видно в свойствах файла; пусто — не писать)…"
		)
		comment_row.addWidget(self._meta_comment, stretch=1)
		box.addLayout(comment_row)
		return card

	def _spin(self, card: QWidget, tip: str, lo: int, hi: int, val: int) -> SpinBox:
		"""Целочисленный регулятор: диапазон lo..hi, старт val, подсказка tip."""
		box = SpinBox(card)
		box.setRange(lo, hi)
		box.setValue(val)
		box.setToolTip(tip)
		return box

	def _dspin(
		self, card: QWidget, tip: str, lo: float, hi: float, val: float, step: float
	) -> DoubleSpinBox:
		"""Дробный регулятор: диапазон lo..hi, старт val, шаг step, подсказка tip."""
		box = DoubleSpinBox(card)
		box.setRange(lo, hi)
		box.setSingleStep(step)
		box.setValue(val)
		box.setToolTip(tip)
		return box

	def _pick_watermark(self) -> None:
		"""Диалог выбора PNG-файла вотермарка."""
		path = pick_file(self, "Файл вотермарка", "Изображения (*.png)")
		if path:
			self._wm_path.setText(path)

	# --- значения ---------------------------------------------------------------

	def fill(self, fields: PresetFields) -> None:
		"""Заполняет панель полями пресета."""
		self._trim_start.setValue(fields.trim_start)
		self._trim_end.setValue(fields.trim_end)
		self._fade_in.setValue(fields.fade_in)
		self._fade_out.setValue(fields.fade_out)
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
		self._meta_comment.setText(fields.meta_comment or "")

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
			trim_start=round(float(self._trim_start.value()), 3),
			trim_end=round(float(self._trim_end.value()), 3),
			fade_in=round(float(self._fade_in.value()), 3),
			fade_out=round(float(self._fade_out.value()), 3),
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
			meta_comment=str(self._meta_comment.text()).strip() or None,
		)

	def _bitrate_kbps(self) -> int | None:
		"""Битрейт из регулятора: Мбит/с → кбит/с; 0 — «как в оригинале»."""
		mbps = float(self._bitrate.value())
		return int(round(mbps * 1000)) if mbps > 0 else None


class _FrameTileButton(TogglePushButton):
	"""Кнопка-плитка кадра: двойной клик подтверждает выбор."""

	doubleClicked = Signal()  # noqa: N815 — соглашение имён сигналов Qt

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
		self,
		worker: EngineWorker,
		source_path: str,
		parent: QWidget,
		trim_start: float = 0.0,
		trim_end: float = 0.0,
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._source = source_path
		# кандидаты — из обрезанного диапазона, время — от обрезанной версии
		self._trim_start = trim_start
		self._trim_end = trim_end
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
		"""Строка управления партией: число кадров и кнопка «Обновить»."""
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
				self._source, int(self._count.value()),
				trim_start=self._trim_start, trim_end=self._trim_end,
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
			row, column = divmod(index, _FRAME_GRID_COLUMNS)
			self._grid.addWidget(self._frame_tile(frame), row, column)
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
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Параметры обработки", self))
		layout.addWidget(CaptionLabel(
			"К видео применяется то, что на экране; пресет — только "
			"загрузка и сохранение набора.", self,
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
		"""Диалог выбора исходного видеофайла."""
		path = pick_file(
			self, "Исходное видео",
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
		dialog = _FramePickerDialog(
			self._worker, source, self.window(),
			trim_start=fields.trim_start, trim_end=fields.trim_end,
		)
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
		"""Показывает итог обработки и карточку результата с действиями."""
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
