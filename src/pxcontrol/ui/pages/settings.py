"""Страница «Настройки»: тема оформления и путь к ffmpeg.

Значения живут в настройках приложения (таблица ``app_settings``,
ADR-0013) и переживают перезапуск; тема применяется на лету.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	InfoBar,
	LineEdit,
	PushButton,
	SubtitleLabel,
	SwitchButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.settings import FFMPEG_PATH, THEME_DARK
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import error_reporter, noop
from pxcontrol.ui.theme import apply_theme


class SettingsPage(QWidget):
	"""Настройки приложения: оформление и обработка видео."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("settings")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._build()
		self._load()

	def _build(self) -> None:
		"""Собирает блоки «Оформление» и «Обработка видео»."""
		layout = QVBoxLayout(self)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.setSpacing(12)
		layout.addWidget(SubtitleLabel("Оформление", self))
		theme_row = QHBoxLayout()
		theme_row.addWidget(BodyLabel("Тёмная тема", self))
		self._theme_switch = SwitchButton(self)
		self._theme_switch.setChecked(True)
		self._theme_switch.checkedChanged.connect(self._on_theme_toggled)
		theme_row.addStretch()
		theme_row.addWidget(self._theme_switch)
		layout.addLayout(theme_row)
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
		layout.addWidget(CaptionLabel(
			"ffprobe ищется рядом с указанным ffmpeg; смена пути "
			"применяется сразу, без перезапуска.", self,
		))
		layout.addStretch()

	def _load(self) -> None:
		"""Подтягивает сохранённые значения из движка."""
		run_in_engine(
			self._worker, self._worker.engine.settings.get(THEME_DARK),
			self, self._show_theme, noop,
		)
		run_in_engine(
			self._worker, self._worker.engine.settings.get(FFMPEG_PATH),
			self, self._ffmpeg_edit.setText, noop,
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
			self._worker, self._worker.engine.settings.set(THEME_DARK, dark),
			self, noop, self._show_error,
		)

	def _on_save_ffmpeg(self) -> None:
		"""Сохраняет путь к ffmpeg (пусто — вернуться к .env/PATH)."""
		path = str(self._ffmpeg_edit.text()).strip()
		run_in_engine(
			self._worker, self._worker.engine.settings.set(FFMPEG_PATH, path),
			self, self._on_ffmpeg_saved, self._show_error,
		)

	def _on_ffmpeg_saved(self, _result: object = None) -> None:
		"""Подтверждает сохранение пути."""
		InfoBar.success(
			"Сохранено",
			"Путь к ffmpeg применён (пусто — из .env или PATH).",
			parent=self,
		)
