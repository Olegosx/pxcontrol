"""Панель параметров обработки видео (раздел страницы «Видео»).

Самостоятельный виджет без знания о странице: заполняется пресетом
(:meth:`PresetForm.fill`), правится свободно, текущее состояние отдаёт
:meth:`PresetForm.fields`. Контракт со страницей — только ``PresetFields``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	DoubleSpinBox,
	LineEdit,
	PushButton,
	SpinBox,
	StrongBodyLabel,
	SwitchButton,
)

from pxcontrol.engine.services.video import (
	IntroSourceKind,
	PresetFields,
	build_intro_source,
	parse_intro_source,
)
from pxcontrol.ui.pages.common import pick_file

#: Углы вотермарка: подпись → код (коды понимает движок, filtergraph).
_CORNERS = [
	("Правый верхний", "tr"), ("Левый верхний", "tl"),
	("Правый нижний", "br"), ("Левый нижний", "bl"),
]
#: Источники кадра заставки: подпись → вид (протокол — в сервисе видео).
_INTRO_SOURCES = [
	("Случайный кадр из середины", IntroSourceKind.RANDOM_MIDDLE),
	("Случайные кадры на выбор", IntroSourceKind.RANDOM_CHOICE),
	("Момент времени (сек)", IntroSourceKind.TIME),
	("Своя картинка (PNG)", IntroSourceKind.IMAGE),
]


class PresetForm(QWidget):
	"""Панель параметров обработки (бывший диалог пресета, без имени)."""

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(12)
		self._layout.addWidget(self._trim_card())
		self._layout.addWidget(self._fade_card())
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

	def _fade_card(self) -> CardWidget:
		"""Раздел «Затухание»: чекбоксы краёв и длительности эффекта."""
		card, box = self._card("Затухание")
		row = QHBoxLayout()
		self._fade_in_check = CheckBox("В начале, с:", card)
		row.addWidget(self._fade_in_check)
		self._fade_in = self._dspin(
			card, "длительность появления из чёрного", 0.1, 30.0, 2.0, 0.1
		)
		self._fade_in.setEnabled(False)
		self._fade_in_check.toggled.connect(self._fade_in.setEnabled)
		row.addWidget(self._fade_in)
		row.addSpacing(16)
		self._fade_out_check = CheckBox("В конце, с:", card)
		row.addWidget(self._fade_out_check)
		self._fade_out = self._dspin(
			card, "длительность ухода в чёрное", 0.1, 30.0, 2.0, 0.1
		)
		self._fade_out.setEnabled(False)
		self._fade_out_check.toggled.connect(self._fade_out.setEnabled)
		row.addWidget(self._fade_out)
		row.addSpacing(16)
		row.addWidget(CaptionLabel(
			"появление из чёрного / уход в чёрное; видео и звук", card
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
		for label, _kind in _INTRO_SOURCES:
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
		"""Раздел «Вывод»: обложка, звук, качество."""
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
		comment_row = QHBoxLayout()
		comment_row.addWidget(BodyLabel("Комментарий (метаданные):", card))
		self._meta_comment = LineEdit(card)
		self._meta_comment.setPlaceholderText(
			"https://t.me/канал — описание (видно в свойствах файла; пусто — не писать)…"
		)
		comment_row.addWidget(self._meta_comment, stretch=1)
		box.addLayout(comment_row)
		subdir_row = QHBoxLayout()
		subdir_row.addWidget(BodyLabel("Подпапка:", card))
		self._subdir = LineEdit(card)
		self._subdir.setPlaceholderText("внутри папок видео; пусто — их корень…")
		self._subdir.setToolTip(
			"Подпапка внутри базовых папок (Настройки → Папки): исходники, "
			"результаты и опубликованные этого пресета. При создании пресета "
			"заполняется его именем."
		)
		subdir_row.addWidget(self._subdir, stretch=1)
		box.addLayout(subdir_row)
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
		# 0 в пресете — эффект выключен; длительность в поле не сбрасываем
		self._fade_in_check.setChecked(fields.fade_in > 0)
		if fields.fade_in > 0:
			self._fade_in.setValue(fields.fade_in)
		self._fade_out_check.setChecked(fields.fade_out > 0)
		if fields.fade_out > 0:
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
		kind, value = parse_intro_source(fields.intro_source)
		kinds = [item for _label, item in _INTRO_SOURCES]
		self._intro_kind.setCurrentIndex(kinds.index(kind))
		self._intro_value.setText(value)
		self._cover.setChecked(fields.cover)
		self._no_audio.setChecked(fields.no_audio)
		kbps = fields.video_bitrate_kbps
		self._bitrate.setValue(kbps / 1000 if kbps else 0.0)
		self._meta_comment.setText(fields.meta_comment or "")
		self._subdir.setText(fields.subdir)

	def _intro_source(self) -> str:
		"""Собирает строку источника кадра (протокол — в сервисе видео)."""
		kind = _INTRO_SOURCES[int(self._intro_kind.currentIndex())][1]
		return build_intro_source(kind, str(self._intro_value.text()))

	def fields(self, name: str) -> PresetFields:
		"""Текущее состояние панели как поля пресета (имя — от вызывающего)."""
		return PresetFields(
			name=name,
			trim_start=round(float(self._trim_start.value()), 3),
			trim_end=round(float(self._trim_end.value()), 3),
			fade_in=(
				round(float(self._fade_in.value()), 3)
				if self._fade_in_check.isChecked() else 0.0
			),
			fade_out=(
				round(float(self._fade_out.value()), 3)
				if self._fade_out_check.isChecked() else 0.0
			),
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
			subdir=str(self._subdir.text()).strip(),
		)

	def _bitrate_kbps(self) -> int | None:
		"""Битрейт из регулятора: Мбит/с → кбит/с; 0 — «как в оригинале»."""
		mbps = float(self._bitrate.value())
		return int(round(mbps * 1000)) if mbps > 0 else None
