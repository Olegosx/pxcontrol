"""Диалог выбора кадра заставки из случайных кандидатов (плиткой).

Кандидаты извлекает движок в финальном качестве; выбранный файл уходит
в обработку как есть (``image:<путь>``) — без повторного извлечения
и риска соседнего кадра.
"""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
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
	FluentIcon,
	IndeterminateProgressRing,
	MessageBoxBase,
	PushButton,
	SpinBox,
	SubtitleLabel,
	TogglePushButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.video import FrameCandidate
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import show_error

#: Колонок в плитке выбора кадра заставки.
_FRAME_GRID_COLUMNS = 3

#: Размеры плитки кадра: кнопка и картинка внутри (кнопка минус поля 2×8).
_TILE_SIZE = QSize(232, 133)
_TILE_PADDING = 8


class _FrameTileButton(TogglePushButton):
	"""Кнопка-плитка кадра: двойной клик подтверждает выбор."""

	doubleClicked = Signal()  # noqa: N815 — соглашение имён сигналов Qt

	def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		super().mouseDoubleClickEvent(event)
		self.doubleClicked.emit()


class FramePickerDialog(MessageBoxBase):
	"""Выбор кадра заставки из случайных кандидатов."""

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
		button.setFixedSize(_TILE_SIZE)
		# картинка — QLabel внутри кнопки: родная отрисовка иконки
		# смещала её от центра и обрезала; подпись прозрачна для мыши
		inner = QVBoxLayout(button)
		inner.setContentsMargins(
			_TILE_PADDING, _TILE_PADDING, _TILE_PADDING, _TILE_PADDING
		)
		image = QLabel(button)
		image.setPixmap(QPixmap(frame.path).scaled(
			_TILE_SIZE.width() - 2 * _TILE_PADDING,
			_TILE_SIZE.height() - 2 * _TILE_PADDING,
			Qt.AspectRatioMode.KeepAspectRatio,
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
