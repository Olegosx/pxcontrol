"""Базовая страница-заглушка с пустым состоянием по центру."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, SubtitleLabel


class PlaceholderPage(QWidget):
	"""Страница с заголовком и пояснением по центру — до появления функции."""

	def __init__(
		self, name: str, title: str, description: str, parent: QWidget | None = None
	) -> None:
		super().__init__(parent)
		self.setObjectName(name)
		self._build(title, description)

	def _build(self, title: str, description: str) -> None:
		"""Собирает центрированное пустое состояние."""
		layout = QVBoxLayout(self)
		layout.addStretch()
		title_label = SubtitleLabel(title, self)
		title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
		desc_label = BodyLabel(description, self)
		desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title_label)
		layout.addWidget(desc_label)
		layout.addStretch()
