"""Страница «Настройки»: категории слева, правка выбранной — справа.

Категории: «Общие» (оформление: тема, компактные отступы, высота
полей, шрифт; путь к ffmpeg), «Папки»
(базовые папки видео: исходники / результаты / опубликованные)
и «Аккаунты» (боты, userbot, ключи ИИ — встроенная
:class:`AccountsPage`).

Значения общих настроек живут в ``app_settings`` (ADR-0013)
и переживают перезапуск; тема применяется на лету, плотность
(отступы, высота полей, шрифт) — при следующем запуске: вёрстка
строится один раз при старте (см. ui/density.py).
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	InfoBar,
	LineEdit,
	ListWidget,
	PushButton,
	SpinBox,
	SubtitleLabel,
	SwitchButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.settings import (
	FFMPEG_PATH,
	THEME_DARK,
	UI_COMPACT_SPACING,
	UI_CONTROL_HEIGHT,
	UI_FONT_SIZE,
	VIDEO_PROCESSED_DIR,
	VIDEO_PUBLISHED_DIR,
	VIDEO_SOURCE_DIR,
	SettingKey,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.accounts import AccountsPage
from pxcontrol.ui.pages.common import (
	INPUT_DEBOUNCE_MS,
	bind,
	debounced,
	error_reporter,
	noop,
	pick_dir,
)
from pxcontrol.ui.theme import apply_theme

#: Ширина списка категорий слева.
_CATEGORIES_WIDTH = 200


class SettingsPage(QWidget):
	"""Контейнер настроек: список категорий и стек панелей правки."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("settings")
		layout = QHBoxLayout(self)
		margins = density.spacing().page_margins
		layout.setContentsMargins(margins[0], margins[1], 0, 0)
		layout.setSpacing(density.spacing().block_spacing)
		self._categories = ListWidget(self)
		self._categories.setFixedWidth(_CATEGORIES_WIDTH)
		self._stack = QStackedWidget(self)
		# категория = пункт списка + панель в стеке (порядок общий)
		self._add_category("Общие", _GeneralSettings(worker, self))
		self._add_category("Папки", _FoldersSettings(worker, self))
		self._add_category("Аккаунты", AccountsPage(worker, self))
		self._categories.currentRowChanged.connect(self._stack.setCurrentIndex)
		self._categories.setCurrentRow(0)
		layout.addWidget(self._categories)
		layout.addWidget(self._stack, stretch=1)

	def _add_category(self, title: str, panel: QWidget) -> None:
		"""Добавляет категорию: пункт в список и панель в стек."""
		self._categories.addItem(title)
		self._stack.addWidget(panel)


class _GeneralSettings(QWidget):
	"""Категория «Общие»: оформление и обработка видео."""

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._show_error = error_reporter(self)
		self._build()
		self._load()

	def _build(self) -> None:
		"""Собирает блоки «Оформление» и «Обработка видео»."""
		layout = QVBoxLayout(self)
		margins = density.spacing().page_margins
		layout.setContentsMargins(0, 0, margins[2], margins[3])
		layout.setSpacing(density.spacing().row_spacing)
		layout.addWidget(SubtitleLabel("Оформление", self))
		theme_row = QHBoxLayout()
		theme_row.addWidget(BodyLabel("Тёмная тема", self))
		self._theme_switch = SwitchButton(self)
		self._theme_switch.setChecked(True)
		self._theme_switch.checkedChanged.connect(self._on_theme_toggled)
		theme_row.addSpacing(8)
		theme_row.addWidget(self._theme_switch)
		theme_row.addStretch()
		layout.addLayout(theme_row)
		compact_row = QHBoxLayout()
		compact_row.addWidget(BodyLabel("Компактные отступы", self))
		self._compact_switch = SwitchButton(self)
		self._compact_switch.checkedChanged.connect(self._on_compact_toggled)
		compact_row.addSpacing(8)
		compact_row.addWidget(self._compact_switch)
		compact_row.addStretch()
		layout.addLayout(compact_row)
		size_row = QHBoxLayout()
		size_row.addWidget(BodyLabel("Высота полей ввода, пикс:", self))
		self._height_spin = SpinBox(self)
		self._height_spin.setRange(*density.CONTROL_HEIGHT_RANGE)
		self._height_spin.setValue(density.STOCK_CONTROL_HEIGHT)
		# пауза после правки: стрелки регулятора кликают сериями
		self._height_spin.valueChanged.connect(
			debounced(self, INPUT_DEBOUNCE_MS, self._save_height)
		)
		size_row.addWidget(self._height_spin)
		size_row.addSpacing(16)
		size_row.addWidget(BodyLabel("Размер шрифта, пикс:", self))
		self._font_spin = SpinBox(self)
		self._font_spin.setRange(*density.FONT_SIZE_RANGE)
		self._font_spin.setValue(density.STOCK_FONT_SIZE)
		self._font_spin.valueChanged.connect(debounced(self, INPUT_DEBOUNCE_MS, self._save_font))
		size_row.addWidget(self._font_spin)
		size_row.addStretch()
		layout.addLayout(size_row)
		layout.addWidget(
			CaptionLabel(
				"Отступы, высота полей и шрифт применяются после перезапуска "
				"приложения; тема — сразу.",
				self,
			)
		)
		layout.addSpacing(12)
		layout.addWidget(SubtitleLabel("Обработка видео", self))
		ffmpeg_row = QHBoxLayout()
		ffmpeg_row.addWidget(BodyLabel("Путь к ffmpeg:", self))
		self._ffmpeg_edit = LineEdit(self)
		self._ffmpeg_edit.setPlaceholderText("пусто — из .env или поиск в PATH…")
		ffmpeg_row.addWidget(self._ffmpeg_edit, stretch=1)
		save = PushButton("Сохранить", self)
		save.clicked.connect(self._on_save_ffmpeg)
		ffmpeg_row.addWidget(save)
		layout.addLayout(ffmpeg_row)
		layout.addWidget(
			CaptionLabel(
				"ffprobe ищется рядом с указанным ffmpeg; смена пути "
				"применяется сразу, без перезапуска.",
				self,
			)
		)
		layout.addStretch()

	def _load(self) -> None:
		"""Подтягивает сохранённые значения из движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get(THEME_DARK),
			self,
			self._show_theme,
			noop,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get(FFMPEG_PATH),
			self,
			self._ffmpeg_edit.setText,
			noop,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get(UI_COMPACT_SPACING),
			self,
			self._show_compact,
			noop,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get(UI_CONTROL_HEIGHT),
			self,
			self._show_height,
			noop,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get(UI_FONT_SIZE),
			self,
			self._show_font,
			noop,
		)

	def _show_theme(self, dark: bool) -> None:
		"""Ставит переключатель без срабатывания сохранения."""
		self._theme_switch.blockSignals(True)
		self._theme_switch.setChecked(dark)
		self._theme_switch.blockSignals(False)

	def _on_theme_toggled(self, dark: bool) -> None:
		"""Переключает тему на лету и сохраняет выбор."""
		apply_theme(dark=dark)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(THEME_DARK, dark),
			self,
			noop,
			self._show_error,
		)

	def _show_compact(self, compact: bool) -> None:
		"""Ставит переключатель отступов без срабатывания сохранения."""
		self._compact_switch.blockSignals(True)
		self._compact_switch.setChecked(compact)
		self._compact_switch.blockSignals(False)

	def _show_height(self, value: int) -> None:
		"""Показывает сохранённую высоту полей без срабатывания сохранения."""
		self._show_spin(self._height_spin, value)

	def _show_font(self, value: int) -> None:
		"""Показывает сохранённый размер шрифта без срабатывания сохранения."""
		self._show_spin(self._font_spin, value)

	@staticmethod
	def _show_spin(spin: SpinBox, value: int) -> None:
		"""Ставит значение регулятора без срабатывания сохранения."""
		spin.blockSignals(True)
		spin.setValue(value)
		spin.blockSignals(False)

	def _on_compact_toggled(self, compact: bool) -> None:
		"""Сохраняет выбор отступов (подействует после перезапуска)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(UI_COMPACT_SPACING, compact),
			self,
			noop,
			self._show_error,
		)

	def _save_height(self) -> None:
		"""Сохраняет высоту полей ввода (подействует после перезапуска)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(UI_CONTROL_HEIGHT, int(self._height_spin.value())),
			self,
			noop,
			self._show_error,
		)

	def _save_font(self) -> None:
		"""Сохраняет размер шрифта (подействует после перезапуска)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(UI_FONT_SIZE, int(self._font_spin.value())),
			self,
			noop,
			self._show_error,
		)

	def _on_save_ffmpeg(self) -> None:
		"""Сохраняет путь к ffmpeg (пусто — вернуться к .env/PATH)."""
		path = str(self._ffmpeg_edit.text()).strip()
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(FFMPEG_PATH, path),
			self,
			self._on_ffmpeg_saved,
			self._show_error,
		)

	def _on_ffmpeg_saved(self, _result: object = None) -> None:
		"""Подтверждает сохранение пути."""
		InfoBar.success(
			"Сохранено",
			"Путь к ffmpeg применён (пусто — из .env или PATH).",
			parent=self,
		)


#: Папки видео: подпись, ключ настройки, стандартное имя в папке приложения.
_VIDEO_FOLDERS: list[tuple[str, SettingKey[str], str]] = [
	("Исходники видео:", VIDEO_SOURCE_DIR, "media/source"),
	("Результаты обработки:", VIDEO_PROCESSED_DIR, "media/processed"),
	("Опубликованные:", VIDEO_PUBLISHED_DIR, "media/published"),
]


class _FoldersSettings(QWidget):
	"""Категория «Папки»: базовые папки видео (ADR-0013, ключи video_*_dir).

	Внутри каждой папки живёт подпапка пресета (поле «Подпапка» на странице
	«Видео»). После публикации видео переезжает из результатов
	в опубликованные с сохранением подпапки.
	"""

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._show_error = error_reporter(self)
		self._edits: dict[str, LineEdit] = {}
		self._build()
		self._load()

	def _build(self) -> None:
		"""Три строки «подпись + путь + Обзор…» и одна кнопка сохранения."""
		layout = QVBoxLayout(self)
		margins = density.spacing().page_margins
		layout.setContentsMargins(0, 0, margins[2], margins[3])
		layout.setSpacing(density.spacing().row_spacing)
		layout.addWidget(SubtitleLabel("Папки видео", self))
		for label, key, default_hint in _VIDEO_FOLDERS:
			row = QHBoxLayout()
			row.addWidget(BodyLabel(label, self))
			edit = LineEdit(self)
			edit.setPlaceholderText(f"пусто — {default_hint} в папке приложения…")
			edit.setClearButtonEnabled(True)
			row.addWidget(edit, stretch=1)
			browse = PushButton("Обзор…", self)
			browse.clicked.connect(bind(self._pick_folder, key.name))
			row.addWidget(browse)
			layout.addLayout(row)
			self._edits[key.name] = edit
		save = PushButton("Сохранить", self)
		save.clicked.connect(self._on_save)
		save_row = QHBoxLayout()
		save_row.addWidget(save)
		save_row.addStretch()
		layout.addLayout(save_row)
		layout.addWidget(
			CaptionLabel(
				"Внутри каждой папки — подпапка пресета (поле «Подпапка» "
				"на «Видео»). После публикации видео переезжает из результатов "
				"в опубликованные.",
				self,
			)
		)
		layout.addStretch()

	def _load(self) -> None:
		"""Подтягивает сохранённые пути из движка."""
		for _label, key, _hint in _VIDEO_FOLDERS:
			run_in_engine(
				self._worker,
				self._worker.engine.settings.get(key),
				self,
				self._edits[key.name].setText,
				noop,
			)

	def _pick_folder(self, key_name: str) -> None:
		"""Диалог выбора папки для строки настройки."""
		edit = self._edits[key_name]
		path = pick_dir(self, "Выбор папки", str(edit.text()).strip())
		if path:
			edit.setText(path)

	def _on_save(self) -> None:
		"""Сохраняет все три пути (пусто — вернуться к стандартной папке).

		Одна операция — одна транзакция (set_many): успех сообщается
		по факту записи, а не до неё (образец честной плашки — channels).
		"""
		items = [
			(key, str(self._edits[key.name].text()).strip())
			for _label, key, _hint in _VIDEO_FOLDERS
		]
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_many(items),
			self,
			self._on_saved,
			self._show_error,
		)

	def _on_saved(self, _result: object = None) -> None:
		"""Подтверждает сохранение папок (вызывается по факту записи)."""
		InfoBar.success(
			"Сохранено",
			"Папки видео применены.",
			parent=self,
		)
