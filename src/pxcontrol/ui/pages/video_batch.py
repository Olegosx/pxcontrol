"""Диалог пакетной обработки: сканирование папки и выбор файлов (ADR-0014).

Папку пользователь выбирает до открытия диалога; здесь движок рекурсивно
ищет в ней видео (с длительностью через ffprobe), а пользователь галочками
отмечает, что отправить в очередь обработки. Ход сканирования виден:
для большой папки пробы занимают заметное время.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
	CaptionLabel,
	CheckBox,
	IndeterminateProgressRing,
	MessageBoxBase,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.video import FoundVideo
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	SelectionRow,
	fixed_list_area,
	format_duration,
	human_size,
	show_error,
)

#: Высота списка найденных файлов (прокрутка внутри, а не рост диалога).
_LIST_HEIGHT = 320


class BatchScanDialog(MessageBoxBase):
	"""Выбор видео для пакета из рекурсивно просканированной папки."""

	#: Ход сканирования (прочитано, всего) — колбэк движка вызывается
	#: из рабочего потока, сигнал доставляет его в поток интерфейса.
	_scan_progressed = Signal(int, int)

	def __init__(self, worker: EngineWorker, root: str, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._rows: list[tuple[CheckBox, FoundVideo]] = []
		self.viewLayout.addWidget(SubtitleLabel("Пакетная обработка", self))
		folder_label = CaptionLabel(f"Папка: {root}", self)
		folder_label.setWordWrap(True)
		self.viewLayout.addWidget(folder_label)
		self._status = CaptionLabel("Чтение файлов…", self)
		self.viewLayout.addWidget(self._status)
		self._ring = IndeterminateProgressRing(self)
		self._ring.setFixedSize(48, 48)
		self.viewLayout.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignHCenter)
		self._build_list_area()
		self._build_selection_row()
		self.yesButton.setText("Обработать")
		self.yesButton.setEnabled(False)
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(720)
		self._scan_progressed.connect(self._on_scan_progress)
		run_in_engine(
			worker,
			worker.engine.video.scan_sources(root, on_progress=self._scan_progressed.emit),
			self,
			self._show_found,
			self._on_scan_error,
		)

	def selected(self) -> list[FoundVideo]:
		"""Отмеченные галочками файлы (в порядке списка)."""
		return [video for check, video in self._rows if check.isChecked()]

	# --- сборка ----------------------------------------------------------------

	def _build_list_area(self) -> None:
		"""Прокручиваемый список найденных файлов (заполняется после скана)."""
		area, self._list_box = fixed_list_area(self, _LIST_HEIGHT, spacing=4)
		self._list_box.addStretch()
		self.viewLayout.addWidget(area)

	def _build_selection_row(self) -> None:
		"""Кнопки «Выбрать все»/«Снять все» и итог по отмеченному."""
		self._selection = SelectionRow(self, self._set_all)
		self._selection.set_enabled(False)  # до окончания сканирования
		self.viewLayout.addLayout(self._selection.layout)

	# --- сканирование ----------------------------------------------------------

	def _on_scan_progress(self, read: int, total: int) -> None:
		self._status.setText(f"Чтение файлов… {read} из {total}")

	def _show_found(self, found: list[FoundVideo]) -> None:
		"""Наполняет список; читаемые файлы отмечены по умолчанию."""
		self._ring.hide()
		if not found:
			self._status.setText("Видео в папке не найдено (включая вложенные).")
			return
		self._status.setText(f"Найдено видео: {len(found)}")
		for video in found:
			check = CheckBox(self._row_text(video), self)
			if video.duration_s is None:
				# нечитаемый файл в обработке всё равно упал бы — галочку
				# можно поставить осознанно, но по умолчанию она снята
				check.setChecked(False)
			else:
				check.setChecked(True)
			check.stateChanged.connect(self._update_summary)
			# перед распоркой в конце списка
			self._list_box.insertWidget(self._list_box.count() - 1, check)
			self._rows.append((check, video))
		self._selection.set_enabled(True)
		self._update_summary()

	@staticmethod
	def _row_text(video: FoundVideo) -> str:
		"""Строка файла: относительный путь, размер, длительность."""
		duration = (
			"не читается ffprobe" if video.duration_s is None else format_duration(video.duration_s)
		)
		return f"{video.name} — {human_size(video.size_bytes)} · {duration}"

	def _on_scan_error(self, message: str) -> None:
		self._ring.hide()
		self._status.setText("Сканирование не удалось.")
		show_error(self, message)

	# --- выбор -----------------------------------------------------------------

	def _set_all(self, checked: bool) -> None:
		for check, _video in self._rows:
			check.setChecked(checked)

	def _update_summary(self, *_args: object) -> None:
		"""Итог по отмеченному; кнопка «Обработать» — только при выборе."""
		picked = self.selected()
		total_bytes = sum(video.size_bytes for video in picked)
		self._selection.set_summary(len(picked), len(self._rows), total_bytes)
		self.yesButton.setEnabled(bool(picked))
