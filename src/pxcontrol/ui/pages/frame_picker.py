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
	QHBoxLayout,
	QLabel,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	FlowLayout,
	FluentIcon,
	IndeterminateProgressRing,
	PushButton,
	SpinBox,
	TogglePushButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.video import FrameCandidate
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	WorkDialog,
	format_duration,
	list_area,
	show_error,
)

#: Размер миниатюры кадра (пиксели) и поля вокруг неё внутри плитки.
#: Размер самой плитки не задаётся: его считает её компоновка.
_FRAME_IMAGE_SIZE = QSize(216, 117)
_TILE_PADDING = 8

#: Границы и умолчание числа кадров-кандидатов за раз: больше дюжины —
#: плитка нечитаема, меньше пары — не из чего выбирать.
_FRAMES_MIN = 2
_FRAMES_MAX = 12
_FRAMES_DEFAULT = 6


class _FrameTileButton(TogglePushButton):
	"""Кнопка-плитка кадра: двойной клик подтверждает выбор."""

	doubleClicked = Signal()  # noqa: N815 — соглашение имён сигналов Qt

	def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		super().mouseDoubleClickEvent(event)
		self.doubleClicked.emit()

	def sizeHint(self) -> QSize:  # noqa: N802 — API Qt
		"""Размер — от компоновки: кнопка сама её не спрашивает.

		Внутри кнопки лежит миниатюра, и её размер вместе с полями
		и есть размер плитки (то же, что у пилюли словаря). Своего
		``sizeHint`` у кнопки — по тексту, а текста здесь нет.
		"""
		layout = self.layout()
		return QSize(layout.sizeHint()) if layout is not None else super().sizeHint()


class FramePickerDialog(WorkDialog):
	"""Выбор кадра заставки из случайных кандидатов."""

	def __init__(
		self,
		worker: EngineWorker,
		source_path: str,
		parent: QWidget,
		trim_start: float = 0.0,
		trim_end: float = 0.0,
		*,
		target_resolution: int | None,
		file_label: str | None = None,
	) -> None:
		"""``target_resolution`` — ступень разрешения обработки: кандидаты
		извлекаются точно в размере итогового кадра и уходят в обработку
		как есть, поэтому ступень обязана совпадать с выбранной в параметрах.

		``file_label`` — подпись с именем файла под заголовком: пакетная
		обработка показывает диалог по разу на файл, и без подписи не видно,
		для какого видео сейчас выбирается кадр."""
		super().__init__("Выберите кадр заставки", parent, size=(820, 700))
		self._worker = worker
		self._source = source_path
		# кандидаты — из обрезанного диапазона, время — от обрезанной версии
		self._trim_start = trim_start
		self._trim_end = trim_end
		self._target_resolution = target_resolution
		self._chosen: str | None = None
		self._group = QButtonGroup(self)
		self._group.setExclusive(True)
		if file_label:
			name_label = CaptionLabel(file_label, self)
			name_label.setWordWrap(True)
			self.content.addWidget(name_label)
		self._build_controls_row()
		# плитка кандидатов — в прокручиваемой области: при двенадцати
		# кадрах она выше окна, и полосу область показывает сама.
		# Раскладка поточная: сколько плиток войдёт в ширину окна,
		# столько и встанет в ряд — при растягивании окна перестроится
		area, box = list_area(self, spacing=density.spacing().list_spacing)
		self._grid_box = QWidget(self)
		self._grid = FlowLayout(self._grid_box, needAni=False)
		box.addWidget(self._grid_box)
		box.addStretch()
		self.content.addWidget(area, stretch=1)
		self._ring = IndeterminateProgressRing(self)
		self._ring.setFixedSize(48, 48)
		self.content.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignHCenter)
		self.add_accept_buttons("Использовать кадр")
		self.accept_button.setEnabled(False)
		self._reload()

	def _build_controls_row(self) -> None:
		"""Строка управления партией: число кадров и кнопка «Обновить»."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Кадров:", self))
		self._count = SpinBox(self)
		self._count.setRange(_FRAMES_MIN, _FRAMES_MAX)
		self._count.setValue(_FRAMES_DEFAULT)
		row.addWidget(self._count)
		self._refresh = PushButton(FluentIcon.SYNC, "Обновить", self)
		self._refresh.clicked.connect(self._reload)
		row.addWidget(self._refresh)
		row.addStretch()
		self.content.addLayout(row)

	def chosen_path(self) -> str | None:
		"""Путь к выбранному кадру (None — не выбран)."""
		return self._chosen

	def _reload(self) -> None:
		"""Запрашивает новую партию: чистит плитку и крутит колёсико."""
		self.accept_button.setEnabled(False)
		self._refresh.setEnabled(False)
		self._chosen = None
		self._clear_grid()
		self._ring.show()
		run_in_engine(
			self._worker,
			self._worker.engine.video.extract_random_frames(
				self._source,
				int(self._count.value()),
				trim_start=self._trim_start,
				trim_end=self._trim_end,
				target_resolution=self._target_resolution,
			),
			self,
			self._show_frames,
			self._show_error,
		)

	def _clear_grid(self) -> None:
		"""Убирает плитку кандидатов (виджеты + снятие кнопок из группы)."""
		self._grid.takeAllWidgets()
		for button in self._group.buttons():
			self._group.removeButton(button)

	def _show_frames(self, frames: list[FrameCandidate]) -> None:
		"""Перерисовывает плитку кандидатов."""
		self._ring.hide()
		self._refresh.setEnabled(True)
		for frame in frames:
			self._grid.addWidget(self._frame_tile(frame))

	def _frame_tile(self, frame: FrameCandidate) -> QWidget:
		"""Плитка кандидата: миниатюра по центру, время подписью снизу."""
		tile = QWidget(self._grid_box)
		column = QVBoxLayout(tile)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(2)
		button = _FrameTileButton(tile)
		# картинка — QLabel внутри кнопки: родная отрисовка иконки
		# смещала её от центра и обрезала; подпись прозрачна для мыши.
		# Размер плитки задаёт эта компоновка: миниатюра плюс поля
		inner = QVBoxLayout(button)
		inner.setContentsMargins(_TILE_PADDING, _TILE_PADDING, _TILE_PADDING, _TILE_PADDING)
		image = QLabel(button)
		# фикс-размер надписи держит плитки одинаковыми: кадры уже,
		# чем рамка, вписываются в неё и центруются
		image.setFixedSize(_FRAME_IMAGE_SIZE)
		image.setPixmap(
			QPixmap(frame.path).scaled(
				_FRAME_IMAGE_SIZE,
				Qt.AspectRatioMode.KeepAspectRatio,
				Qt.TransformationMode.SmoothTransformation,
			)
		)
		image.setAlignment(Qt.AlignmentFlag.AlignCenter)
		image.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
		inner.addWidget(image)
		button.toggled.connect(partial(self._on_toggled, frame.path))
		button.doubleClicked.connect(partial(self._on_double_clicked, frame.path))
		self._group.addButton(button)
		column.addWidget(button)
		caption = CaptionLabel(format_duration(frame.timestamp), tile)
		caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
		column.addWidget(caption)
		return tile

	def _on_toggled(self, path: str, checked: bool) -> None:
		if checked:
			self._chosen = path
			self.accept_button.setEnabled(True)

	def _on_double_clicked(self, path: str) -> None:
		"""Двойной клик по плитке = выбрать кадр и «Использовать кадр»."""
		self._chosen = path
		self.accept_button.setEnabled(True)
		self.accept_button.click()

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку и останавливает колёсико."""
		self._ring.hide()
		self._refresh.setEnabled(True)
		show_error(self, message)
