"""Страница «Настройки»: переключатель тёмной/светлой темы."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, SubtitleLabel, SwitchButton

from pxcontrol.ui.theme import apply_theme


class SettingsPage(QWidget):
	"""Настройки приложения. Каркас: пока только тема оформления."""

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("settings")
		self._build()

	def _build(self) -> None:
		"""Собирает блок «Оформление» с переключателем темы."""
		layout = QVBoxLayout(self)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.addWidget(SubtitleLabel("Оформление", self))
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Тёмная тема", self))
		self._theme_switch = SwitchButton(self)
		self._theme_switch.setChecked(True)
		self._theme_switch.checkedChanged.connect(self._on_theme_toggled)
		row.addStretch()
		row.addWidget(self._theme_switch)
		layout.addLayout(row)
		layout.addStretch()

	def _on_theme_toggled(self, dark: bool) -> None:
		"""Переключает тему приложения на лету."""
		apply_theme(dark=dark)
