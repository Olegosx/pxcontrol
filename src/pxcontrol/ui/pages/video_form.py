"""Панель параметров обработки видео и сворачиваемая карточка.

Два экспорта: :class:`PresetForm` — самостоятельный виджет параметров
без знания о странице (заполняется пресетом через :meth:`PresetForm.fill`,
состояние отдаёт :meth:`PresetForm.fields`; контракт со страницей —
только ``PresetFields``) и :class:`CollapsibleCard` — универсальная
сворачиваемая карточка, в которой живут и панель, и карточки файлов
страницы «Видео» (перенос в ``common`` — при третьем пользователе).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, SignalInstance
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	DoubleSpinBox,
	FluentIcon,
	LineEdit,
	PushButton,
	SpinBox,
	StrongBodyLabel,
	SwitchButton,
	TransparentToolButton,
)

from pxcontrol.engine.services.video import (
	IntroSourceKind,
	PresetFields,
	build_intro_source,
	parse_intro_source,
)
from pxcontrol.ui import density
from pxcontrol.ui.pages.common import INPUT_DEBOUNCE_MS, debounced, pick_file

#: Углы вотермарка: подпись → код (коды понимает движок, filtergraph).
#: Значения по умолчанию параметров обработки — единственная точка истины
#: движка (``PresetFields``): «чистая» форма совпадает с «чистым» пресетом,
#: смена дефолта в движке подхватывается формой сама.
_DEFAULTS = PresetFields(name="")

#: Приглушённый цвет сводки в шапке карточки: светлая/тёмная тема.
#: Подпись, а не ошибка — единая точка цветов ошибок (``ErrorLabel``
#: в ``common``) тут не подходит.
_SUMMARY_COLORS = ("#5f5f5f", "#9c9c9c")

_CORNERS = [
	("Правый верхний", "tr"),
	("Левый верхний", "tl"),
	("Правый нижний", "br"),
	("Левый нижний", "bl"),
]
#: Источники кадра заставки: подпись → вид (протокол — в сервисе видео).
_INTRO_SOURCES = [
	("Случайный кадр из середины", IntroSourceKind.RANDOM_MIDDLE),
	("Случайные кадры на выбор", IntroSourceKind.RANDOM_CHOICE),
	("Момент времени (сек)", IntroSourceKind.TIME),
	("Своя картинка (PNG)", IntroSourceKind.IMAGE),
]


def _fmt_num(value: float) -> str:
	"""Число для сводки: без хвостовых нулей, запятая по-русски."""
	return f"{value:g}".replace(".", ",")


class _CardHeader(QWidget):
	"""Шапка сворачиваемой карточки: ловит клик по всей своей площади."""

	clicked = Signal()

	def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		"""Левый клик в любом месте шапки — сигнал о сворачивании."""
		if event.button() == Qt.MouseButton.LeftButton:
			self.clicked.emit()
		super().mouseReleaseEvent(event)


class CollapsibleCard(CardWidget):
	"""Карточка-раздел, сворачиваемая кликом по шапке.

	Разделы параметров свёрнуты по умолчанию: форма занимает несколько
	строк вместо целого экрана (параметры обычно приходят из пресета
	и правятся редко). Свёрнутость — только про показ: виджеты скрытого
	тела сохраняют значения, и :meth:`PresetForm.fields` читает их
	как обычно.
	"""

	def __init__(
		self,
		title: str,
		parent: QWidget,
		trailing: QWidget | None = None,
		leading: QWidget | None = None,
	) -> None:
		"""``trailing`` — виджет с кнопками в правом краю шапки (например,
		просмотр и удаление у карточки файла); ``leading`` — виджет перед
		названием (например, чекбокс выбора). Клики по обоим остаются
		их виджетам и карточку не сворачивают (Qt не передаёт их шапке)."""
		super().__init__(parent)
		outer = QVBoxLayout(self)
		outer.setContentsMargins(0, 0, 0, 0)
		outer.setSpacing(0)
		self._chevron = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
		self._chevron.setFixedSize(24, 24)
		self._chevron.setIconSize(QSize(12, 12))
		self._chevron.clicked.connect(self.toggle)
		header = _CardHeader(self)
		header.setCursor(Qt.CursorShape.PointingHandCursor)
		header.clicked.connect(self.toggle)
		head_row = QHBoxLayout(header)
		head_row.setContentsMargins(12, 8, 16, 8)
		head_row.setSpacing(8)
		head_row.addWidget(self._chevron)
		if leading is not None:
			leading.setParent(header)
			head_row.addWidget(leading)
		head_row.addWidget(StrongBodyLabel(title, header))
		self._summary_text = ""
		self._summary = CaptionLabel("", header)
		self._summary.setTextColor(*_SUMMARY_COLORS)
		# сводка занимает остаток шапки и обрезается, а не распирает форму
		self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		head_row.addWidget(self._summary, stretch=1)
		head_row.addStretch()
		if trailing is not None:
			trailing.setParent(header)
			head_row.addWidget(trailing)
		outer.addWidget(header)
		self._body = QWidget(self)
		#: Компоновка тела — раздел добавляет сюда своё содержимое.
		self.body = QVBoxLayout(self._body)
		self.body.setContentsMargins(*density.spacing().card_body_margins)
		self.body.setSpacing(density.spacing().card_body_spacing)
		outer.addWidget(self._body)
		self._body.hide()

	def toggle(self) -> None:
		"""Разворачивает свёрнутое и наоборот (клик по шапке или стрелке)."""
		self.set_expanded(not self._body.isVisible())

	def set_expanded(self, expanded: bool) -> None:
		"""Показывает или прячет тело; стрелка отражает состояние."""
		self._body.setVisible(expanded)
		icon = FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED
		self._chevron.setIcon(icon)
		self._refresh_summary()

	def set_summary(self, text: str) -> None:
		"""Сводка значений для шапки; видна только у свёрнутой карточки.

		У развёрнутой сводка дублировала бы поля прямо под шапкой —
		поэтому прячется.
		"""
		self._summary_text = text
		self._refresh_summary()

	def _refresh_summary(self) -> None:
		self._summary.setText(self._summary_text)
		self._summary.setVisible(bool(self._summary_text) and not self._body.isVisible())


class PresetForm(QWidget):
	"""Панель параметров обработки (бывший диалог пресета, без имени)."""

	#: Подпапка сменилась — правкой поля или загрузкой пресета. Страница
	#: перечитывает по ней список готовых видео: он всегда показывает
	#: ту папку, в которую уйдёт следующий результат.
	subdir_changed = Signal(str)

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		# текущее значение «Качество, Мбит/с» — автоподстановка рекомендации?
		# Автоподстановку можно обновлять и сбрасывать; введённое руками
		# или пресетом — неприкосновенно (ноль снова освобождает поле)
		self._bitrate_suggested = False
		self._suggest_guard = False  # различает программную запись и ручную правку
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(density.spacing().row_spacing)
		self._layout.addWidget(self._trim_card())
		self._layout.addWidget(self._fade_card())
		self._layout.addWidget(self._watermark_card())
		self._layout.addWidget(self._intro_card())
		self._layout.addWidget(self._output_card())
		self._bitrate.valueChanged.connect(self._on_bitrate_edited)

	# --- сборка ----------------------------------------------------------------

	def _card(self, title: str) -> tuple[CollapsibleCard, QVBoxLayout]:
		"""Сворачиваемая карточка-раздел; тело — компоновка содержимого."""
		card = CollapsibleCard(title, self)
		return card, card.body

	def _bind_summary(
		self, card: CollapsibleCard, make: Callable[[], str], *signals: SignalInstance
	) -> None:
		"""Сводка карточки: пересчёт по сигналам полей и сразу при сборке.

		Загрузка пресета (``fill``) отдельного пересчёта не требует:
		она пишет в виджеты, а те излучают перечисленные сигналы сами.
		"""

		def refresh(*_args: object) -> None:
			card.set_summary(make())

		for signal in signals:
			signal.connect(refresh)
		refresh()

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
		self._trim_start = self._dspin(card, "0 — не резать", 0.0, 36000.0, 0.0, 0.1)
		self._labeled(row, "Отрезать в начале, с:", self._trim_start)
		self._trim_end = self._dspin(card, "0 — не резать", 0.0, 36000.0, 0.0, 0.1)
		self._labeled(row, "Отрезать в конце, с:", self._trim_end)
		row.addWidget(CaptionLabel("остальные параметры — от обрезанной версии", card))
		row.addStretch()
		box.addLayout(row)
		self._bind_summary(
			card, self._trim_summary, self._trim_start.valueChanged, self._trim_end.valueChanged
		)
		return card

	def _fade_card(self) -> CardWidget:
		"""Раздел «Затухание»: чекбоксы краёв и длительности эффекта."""
		card, box = self._card("Затухание")
		row = QHBoxLayout()
		self._fade_in_check = CheckBox("В начале, с:", card)
		row.addWidget(self._fade_in_check)
		self._fade_in = self._dspin(card, "длительность появления из чёрного", 0.1, 30.0, 2.0, 0.1)
		self._fade_in.setEnabled(False)
		self._fade_in_check.toggled.connect(self._fade_in.setEnabled)
		row.addWidget(self._fade_in)
		row.addSpacing(16)
		self._fade_out_check = CheckBox("В конце, с:", card)
		row.addWidget(self._fade_out_check)
		self._fade_out = self._dspin(card, "длительность ухода в чёрное", 0.1, 30.0, 2.0, 0.1)
		self._fade_out.setEnabled(False)
		self._fade_out_check.toggled.connect(self._fade_out.setEnabled)
		row.addWidget(self._fade_out)
		row.addSpacing(16)
		row.addWidget(CaptionLabel("появление из чёрного / уход в чёрное; видео и звук", card))
		row.addStretch()
		box.addLayout(row)
		self._bind_summary(
			card,
			self._fade_summary,
			self._fade_in_check.toggled,
			self._fade_out_check.toggled,
			self._fade_in.valueChanged,
			self._fade_out.valueChanged,
		)
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
		self._margin = self._spin(
			card, "отступ вотермарка от края кадра", 0, 200, _DEFAULTS.wm_margin
		)
		self._labeled(look, "Отступ, пикс:", self._margin)
		self._opacity = self._dspin(card, "1 — непрозрачен", 0.05, 1.0, _DEFAULTS.wm_opacity, 0.05)
		self._labeled(look, "Прозрачность:", self._opacity)
		self._scale = self._dspin(card, "доля ширины кадра", 0.05, 0.5, _DEFAULTS.wm_scale, 0.01)
		self._labeled(look, "Масштаб:", self._scale)
		look.addStretch()
		box.addLayout(look)
		box.addLayout(self._watermark_window_row(card))
		self._bind_summary(
			card,
			self._watermark_summary,
			self._wm_path.textChanged,
			self._corner.currentIndexChanged,
			self._wm_start.valueChanged,
			self._wm_end.valueChanged,
			self._wm_fade.valueChanged,
		)
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
		self._hold = self._dspin(
			card, "сколько секунд держать кадр", 0.2, 5.0, _DEFAULTS.intro_hold, 0.1
		)
		self._labeled(top, "Держать, с:", self._hold)
		self._xfade = self._dspin(
			card, "длительность растворения в видео", 0.1, 3.0, _DEFAULTS.xfade, 0.1
		)
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
		self._bind_summary(
			card,
			self._intro_summary,
			self._intro.checkedChanged,
			self._intro_kind.currentIndexChanged,
			self._hold.valueChanged,
		)
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
		# после паузы ввода, не на каждый символ: подписчик сканирует диск
		self._subdir.textChanged.connect(
			debounced(
				self,
				INPUT_DEBOUNCE_MS,
				lambda: self.subdir_changed.emit(str(self._subdir.text())),
			)
		)
		subdir_row.addWidget(self._subdir, stretch=1)
		box.addLayout(subdir_row)
		self._bind_summary(
			card,
			self._output_summary,
			self._bitrate.valueChanged,
			self._cover.checkedChanged,
			self._no_audio.checkedChanged,
			self._subdir.textChanged,
		)
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

	# --- сводки для шапок карточек ----------------------------------------------

	def _trim_summary(self) -> str:
		"""«Обрезка»: отрезаемые края или «выкл»."""
		parts: list[str] = []
		if float(self._trim_start.value()) > 0:
			parts.append(f"в начале {_fmt_num(float(self._trim_start.value()))} с")
		if float(self._trim_end.value()) > 0:
			parts.append(f"в конце {_fmt_num(float(self._trim_end.value()))} с")
		return ", ".join(parts) or "выкл"

	def _fade_summary(self) -> str:
		"""«Затухание»: включённые края с длительностью или «выкл»."""
		parts: list[str] = []
		if self._fade_in_check.isChecked():
			parts.append(f"в начале {_fmt_num(float(self._fade_in.value()))} с")
		if self._fade_out_check.isChecked():
			parts.append(f"в конце {_fmt_num(float(self._fade_out.value()))} с")
		return ", ".join(parts) or "выкл"

	def _watermark_summary(self) -> str:
		"""«Вотермарк»: имя файла, угол и особенности показа — или «выкл»."""
		path = str(self._wm_path.text()).strip()
		if not path:
			return "выкл"
		corner = _CORNERS[int(self._corner.currentIndex())][0].lower()
		parts = [Path(path).name, corner]
		if float(self._wm_start.value()) > 0 or float(self._wm_end.value()) > 0:
			parts.append("окно показа")
		if float(self._wm_fade.value()) > 0:
			parts.append("плавно")
		return ", ".join(parts)

	def _intro_summary(self) -> str:
		"""«Кадр для превью»: источник кадра и длительность — или «выкл»."""
		if not self._intro.isChecked():
			return "выкл"
		source = _INTRO_SOURCES[int(self._intro_kind.currentIndex())][0].lower()
		return f"{source}, держать {_fmt_num(float(self._hold.value()))} с"

	def _output_summary(self) -> str:
		"""«Вывод»: битрейт и включённые особенности (всегда непустая)."""
		mbps = float(self._bitrate.value())
		parts = [f"{_fmt_num(mbps)} Мбит/с" if mbps > 0 else "битрейт исходника"]
		if self._cover.isChecked():
			parts.append("обложка")
		if self._no_audio.isChecked():
			parts.append("без звука")
		subdir = str(self._subdir.text()).strip()
		if subdir:
			parts.append(f"подпапка «{subdir}»")
		return ", ".join(parts)

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
				round(float(self._fade_in.value()), 3) if self._fade_in_check.isChecked() else 0.0
			),
			fade_out=(
				round(float(self._fade_out.value()), 3) if self._fade_out_check.isChecked() else 0.0
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

	def suggest_bitrate(self, mbps: float) -> bool:
		"""Подставляет рекомендованный битрейт, если поле не занято.

		Свободное поле — «0 — как в оригинале» или прежняя автоподстановка
		(рекомендация нового файла обновляет рекомендацию старого).
		Заполненное вручную или пресетом значение не трогается; ручной
		«0» снова освобождает поле.

		Returns:
			True, если значение подставлено.
		"""
		if float(self._bitrate.value()) > 0 and not self._bitrate_suggested:
			return False
		self._set_bitrate_programmatically(mbps)
		self._bitrate_suggested = True
		return True

	def _set_bitrate_programmatically(self, mbps: float) -> None:
		"""Пишет значение в поле, не снимая пометку «автоподстановка»."""
		self._suggest_guard = True
		try:
			self._bitrate.setValue(mbps)
		finally:
			self._suggest_guard = False

	def _on_bitrate_edited(self, _value: float) -> None:
		"""Любая правка поля не через автоподстановку снимает её пометку.

		Сюда попадают и ручной ввод, и загрузка пресета (``fill``) — оба
		случая означают осознанное значение, которое подстановка рекомендаций
		трогать не должна.
		"""
		if not self._suggest_guard:
			self._bitrate_suggested = False
