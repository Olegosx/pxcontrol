"""Панель параметров обработки видео (раздел страницы «Видео»).

Самостоятельный виджет без знания о странице: заполняется пресетом
(:meth:`PresetForm.fill`), правится свободно, текущее состояние отдаёт
:meth:`PresetForm.fields`. Контракт со страницей — только ``PresetFields``.
Форма тяжёлая (130 виджетов, около 9 МБ памяти в компоновке — замер
19.09.2026), поэтому страница держит две: шаблон и общий редактор
карточек файлов; у самих карточек — только снимок ``PresetFields``.
Сворачиваемая карточка ``CollapsibleCard`` жила здесь до третьего
пользователя — теперь она в ``common`` (аудит 05.09).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Signal, SignalInstance
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
	SwitchButton,
)

from pxcontrol.engine.services.video import (
	IntroSourceKind,
	PresetFields,
	build_intro_source,
	parse_intro_source,
)
from pxcontrol.engine.video.constants import (
	CONSTANT_QUALITY_CRF,
	RESCALE_BITRATE_EXPONENT,
	RESCALE_BITRATE_MARGIN,
	RESOLUTION_STEPS,
	RescaleBitrateMode,
)
from pxcontrol.engine.video.filtergraph import CORNER_POSITIONS
from pxcontrol.ui import density
from pxcontrol.ui.pages.common import (
	INPUT_DEBOUNCE_MS,
	CollapsibleCard,
	WarningLabel,
	debounced,
	pick_file,
)

#: Значения по умолчанию параметров обработки — единственная точка истины
#: движка (``PresetFields``): «чистая» форма совпадает с «чистым» пресетом,
#: смена дефолта в движке подхватывается формой сама.
_DEFAULTS = PresetFields(name="")

logger = logging.getLogger(__name__)

#: Имена углов вотермарка. Сам перечень кодов — за движком
#: (``CORNER_POSITIONS``): куда можно ставить вотермарк, решает граф
#: фильтров, здесь только слова для человека. Тот же приём, что
#: у ступеней разрешения ниже: угол без имени покажется кодом,
#: а не пропадёт из списка.
_CORNER_NAMES = {
	"tr": "Правый верхний",
	"tl": "Левый верхний",
	"tc": "Сверху по центру",
	"br": "Правый нижний",
	"bl": "Левый нижний",
	"bc": "Снизу по центру",
	"lc": "Вдоль левого края",
	"rc": "Вдоль правого края",
}

#: Пункты списка «Угол»: подпись → код. Порядок — как у имён выше,
#: незнакомые движку коды сюда не попадают, а незнакомые нам —
#: показываются кодом в конце списка.
_CORNERS: list[tuple[str, str]] = [
	*((name, code) for code, name in _CORNER_NAMES.items() if code in CORNER_POSITIONS),
	*((code, code) for code in CORNER_POSITIONS if code not in _CORNER_NAMES),
]
#: Имена ступеней разрешения. Сам перечень ступеней — за движком
#: (``RESOLUTION_STEPS``): числа это логика обработки, слова — показ.
#: Ступень без имени покажется одним числом, а не пропадёт из списка.
_RESOLUTION_NAMES = {720: "HD", 1080: "FullHD", 1440: "QHD", 2160: "4K UHD"}

#: Пункты списка «Разрешение»: подпись → ступень (None — без масштабирования).
_RESOLUTIONS: list[tuple[str, int | None]] = [
	*((f"{_RESOLUTION_NAMES.get(step, '')} ({step})".strip(), step) for step in RESOLUTION_STEPS),
	("Как в оригинале", None),
]

#: Пункт по умолчанию: он же — запасной для пресета с незнакомой
#: ступенью (запись из будущей версии). Ступень берётся из «чистого»
#: пресета движка, как и остальные умолчания панели.
_RESOLUTION_FALLBACK = next(
	index for index, (_, step) in enumerate(_RESOLUTIONS) if step == _DEFAULTS.target_resolution
)

#: Режимы битрейта при смене размера кадра (ADR-0044): подпись в списке,
#: режим движка, фраза для сводки карточки «Вывод».
_RESCALE_MODES: list[tuple[str, RescaleBitrateMode, str]] = [
	(
		f"Постоянное качество (CRF {CONSTANT_QUALITY_CRF})",
		RescaleBitrateMode.CONSTANT_QUALITY,
		f"при смене размера — CRF {CONSTANT_QUALITY_CRF}",
	),
	(
		f"Пересчитать от исходника (+{round((RESCALE_BITRATE_MARGIN - 1) * 100)} %)",
		RescaleBitrateMode.SCALED_SOURCE,
		"при смене размера — пересчёт",
	),
]

#: Пункт по умолчанию и запасной для незнакомого режима (запись из
#: будущей версии) — как у ступени разрешения.
_RESCALE_FALLBACK = next(
	index
	for index, (_, mode, _) in enumerate(_RESCALE_MODES)
	if mode == _DEFAULTS.rescale_bitrate_mode
)

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


def apply_bitrate_advice(current_kbps: int | None, suggested: bool, mbps: float) -> int | None:
	"""Правило автоподстановки рекомендованного битрейта (без Qt).

	Поле свободно, если оно пустое (None — «как в оригинале») или его
	значение подставила прежняя рекомендация (рекомендация нового файла
	обновляет рекомендацию старого). Заполненное рукой или пресетом
	значение не трогается; ручной ноль снова освобождает поле. Правило
	одно на форму и на снимок параметров файла (страница «Видео» держит
	параметры карточек снимками и правит их в общем редакторе).

	Returns:
		Новое значение в кбит/с — или None, если поле занято.
	"""
	if current_kbps is not None and not suggested:
		return None
	return int(round(mbps * 1000))


class PresetForm(QWidget):
	"""Панель параметров обработки (бывший диалог пресета, без имени)."""

	#: Подпапка сменилась — правкой поля или загрузкой пресета. Страница
	#: перечитывает по ней список готовых видео: он всегда показывает
	#: ту папку, в которую уйдёт следующий результат.
	subdir_changed = Signal(str)

	#: Ступень разрешения сменилась — списком или загрузкой пресета.
	#: Страница пересчитывает по ней предупреждение об апскейле: оно
	#: зависит и от размеров исходника, и от выбранной ступени.
	resolution_changed = Signal()

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
		self._labeled(look, "Положение:", self._corner)
		self._margin = self._spin(
			card, "отступ вотермарка от края кадра", 0, 200, _DEFAULTS.wm_margin
		)
		self._labeled(look, "Отступ, пикс:", self._margin)
		self._opacity = self._dspin(card, "1 — непрозрачен", 0.05, 1.0, _DEFAULTS.wm_opacity, 0.05)
		self._labeled(look, "Прозрачность:", self._opacity)
		self._scale = self._dspin(card, "доля ширины кадра", 0.05, 0.75, _DEFAULTS.wm_scale, 0.01)
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
		"""Раздел «Вывод»: разрешение, обложка, звук, качество."""
		card, box = self._card("Вывод")
		res_row = QHBoxLayout()
		self._resolution = ComboBox(card)
		self._resolution.addItems([title for title, _ in _RESOLUTIONS])
		self._resolution.setCurrentIndex(_RESOLUTION_FALLBACK)
		self._resolution.setToolTip(
			"Число ступени получает короткая сторона кадра (у альбомного "
			"кадра это высота, у книжного — ширина), вторая сторона "
			"считается из пропорций исходника — они не меняются."
		)
		self._labeled(res_row, "Разрешение:", self._resolution)
		self._scale_note = WarningLabel(card)
		res_row.addWidget(self._scale_note)
		res_row.addStretch()
		box.addLayout(res_row)
		self._resolution.currentIndexChanged.connect(lambda *_: self.resolution_changed.emit())
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
		box.addLayout(self._rescale_row(card))
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
			self._resolution.currentIndexChanged,
			self._bitrate.valueChanged,
			self._rescale.currentIndexChanged,
			self._cover.checkedChanged,
			self._no_audio.checkedChanged,
			self._subdir.textChanged,
		)
		return card

	def _rescale_row(self, card: QWidget) -> QHBoxLayout:
		"""Строка «Битрейт при смене разрешения» (ADR-0044).

		Список активен, только когда режим может подействовать: битрейт
		не задан («0 — как в оригинале») и выбрана ступень разрешения.
		"""
		row = QHBoxLayout()
		self._rescale = ComboBox(card)
		self._rescale.addItems([title for title, _, _ in _RESCALE_MODES])
		self._rescale.setCurrentIndex(_RESCALE_FALLBACK)
		exponent = _fmt_num(RESCALE_BITRATE_EXPONENT)
		margin = _fmt_num(RESCALE_BITRATE_MARGIN)
		self._rescale.setToolTip(
			"Как выбрать битрейт, когда кадр меняет размер, а «Качество» — 0.\n"
			"Постоянное качество: кодек сам тратит столько бит, сколько нужно "
			"картинке нового размера; размер файла заранее неизвестен.\n"
			"Пересчёт: битрейт исходника × (площадь итога / площадь исходника)"
			f"^{exponent} × {margin} — размер файла предсказуем."
		)
		self._labeled(row, "Битрейт при смене разрешения:", self._rescale)
		row.addStretch()
		self._resolution.currentIndexChanged.connect(self._sync_rescale_enabled)
		self._bitrate.valueChanged.connect(self._sync_rescale_enabled)
		self._sync_rescale_enabled()
		return row

	def _sync_rescale_enabled(self, *_args: object) -> None:
		"""Режим битрейта при смене размера действует только без явного битрейта."""
		self._rescale.setEnabled(
			float(self._bitrate.value()) == 0 and self._target_resolution() is not None
		)

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
		"""«Вотермарк»: имя файла, положение и особенности показа — или «выкл»."""
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
		"""«Вывод»: разрешение, битрейт и особенности (всегда непустая)."""
		parts = [self._resolution_summary(), self._bitrate_summary()]
		if self._cover.isChecked():
			parts.append("обложка")
		if self._no_audio.isChecked():
			parts.append("без звука")
		subdir = str(self._subdir.text()).strip()
		if subdir:
			parts.append(f"подпапка «{subdir}»")
		return ", ".join(parts)

	def _bitrate_summary(self) -> str:
		"""Битрейт для сводки: явный, исходника или исходника с режимом смены размера."""
		mbps = float(self._bitrate.value())
		if mbps > 0:
			return f"{_fmt_num(mbps)} Мбит/с"
		if self._target_resolution() is None:
			return "битрейт исходника"
		return f"битрейт исходника, {_RESCALE_MODES[int(self._rescale.currentIndex())][2]}"

	def _resolution_summary(self) -> str:
		"""Ступень для сводки: «1080p» или «разрешение исходника»."""
		step = self._target_resolution()
		return f"{step}p" if step is not None else "разрешение исходника"

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
		# незнакомый код из пресета (запись будущей версии) не должен
		# ронять форму: показываем первый угол, как и у ступени
		# разрешения делает _RESOLUTION_FALLBACK
		if fields.wm_corner in codes:
			self._corner.setCurrentIndex(codes.index(fields.wm_corner))
		else:
			logger.warning("Пресет: неизвестный угол вотермарка %r.", fields.wm_corner)
			self._corner.setCurrentIndex(0)
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
		self._resolution.setCurrentIndex(
			next(
				(
					index
					for index, (_, step) in enumerate(_RESOLUTIONS)
					if step == fields.target_resolution
				),
				_RESOLUTION_FALLBACK,
			)
		)
		kbps = fields.video_bitrate_kbps
		self._bitrate.setValue(kbps / 1000 if kbps else 0.0)
		self._rescale.setCurrentIndex(
			next(
				(
					index
					for index, (_, mode, _) in enumerate(_RESCALE_MODES)
					if mode == fields.rescale_bitrate_mode
				),
				_RESCALE_FALLBACK,
			)
		)
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
			rescale_bitrate_mode=_RESCALE_MODES[int(self._rescale.currentIndex())][1].value,
			target_resolution=self._target_resolution(),
			meta_comment=str(self._meta_comment.text()).strip() or None,
			subdir=str(self._subdir.text()).strip(),
		)

	def _target_resolution(self) -> int | None:
		"""Выбранная ступень разрешения; None — «как в оригинале»."""
		return _RESOLUTIONS[int(self._resolution.currentIndex())][1]

	def set_scale_note(self, note: str) -> None:
		"""Показывает предупреждение рядом с выбором разрешения.

		Текст готовит страница: только она знает размеры исходника
		этой карточки. Пустая строка убирает предупреждение.
		"""
		self._scale_note.set_note(note)

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
		kbps = apply_bitrate_advice(self._bitrate_kbps(), self._bitrate_suggested, mbps)
		if kbps is None:
			return False
		self._set_bitrate_programmatically(kbps / 1000)
		self._bitrate_suggested = True
		return True

	def bitrate_suggested(self) -> bool:
		"""Подставлен ли битрейт в поле автоматически (а не рукой или пресетом)."""
		return self._bitrate_suggested

	def set_bitrate_suggested(self, suggested: bool) -> None:
		"""Восстанавливает признак автоподстановки после :meth:`fill`.

		Заполнение пишет в поле битрейта и снимает признак (как любая
		правка не через подстановку), а у снимка параметров файла признак
		свой — общий редактор страницы «Видео» возвращает его сюда.
		"""
		self._bitrate_suggested = suggested

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
