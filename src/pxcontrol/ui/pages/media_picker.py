"""Файлы поста: один, несколько (альбом) или ни одного (ADR-0033, C4).

Пост — это текст и список файлов, и форма показывает именно список:
выбрал один — обычное вложение, выбрал несколько — альбом одной
записью. Компонент один на обе формы поста (новый пост и правка
элемента очереди): правила у них общие, и расходиться им незачем.

Что компонент обещает форме:

- список файлов (:meth:`files`) в том порядке, в каком человек их
  выбрал: в альбоме порядок виден читателю, а подпись достаётся
  первому файлу;
- переименование — **только у одиночного файла**. У альбома его нет
  осознанно: десять полей имени в форме поста превратили бы её
  в таблицу, а смысла в переименовании пачки ровно столько же,
  сколько в одном имени на всех;
- честная строка о том, что альбом отнимает: кнопки и превью ссылки
  ему недоступны (правила Telegram, ADR-0031 и ADR-0033).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton

from pxcontrol.engine.services.posts import MAX_ALBUM_FILES, MediaFile
from pxcontrol.engine.telegram.types import BOT_MAX_FILE_BYTES, MediaKind
from pxcontrol.ui.pages.common import (
	DIM_TEXT,
	bind,
	clear_layout,
	kind_file_filter,
	kind_label,
	plural,
	rename_row,
	tinted,
)


def over_bot_limit(files: Sequence[MediaFile]) -> bool:
	"""Хоть один файл не по силам боту (от этого зависят маршрут и кнопки).

	Правило щадящее, в отличие от одноимённой проверки движка: файл,
	размер которого не прочитался (сетевой диск, права), считается
	маленьким. Форма только подсказывает, а судьбу такого файла решит
	отправка — она о недоступном скажет прямо, и пугать человека
	заранее незачем.
	"""
	for file in files:
		try:
			if Path(file.path).stat().st_size > BOT_MAX_FILE_BYTES:
				return True
		except OSError:
			continue
	return False


def album_note(count: int) -> str:
	"""Строка под списком файлов: что выбрано и чего это стоит.

	Чистая функция — её проверяет тест, а не глаз.
	"""
	if count == 0:
		return ""
	if count == 1:
		return "Один файл — обычное вложение."
	return (
		f"Альбом: {count} {plural(count, 'файл', 'файла', 'файлов')} одной записью. "
		"У альбома не бывает кнопок под постом, а подпись достаётся первому файлу."
	)


class MediaPicker(QWidget):
	"""Список файлов поста: выбор, порядок, переименование одиночного."""

	#: Состав файлов изменился (форма пересобирает подсказки и правила).
	changed = Signal()

	def __init__(self, parent: QWidget, on_pick: Callable[[], None] | None = None) -> None:
		"""Args:
		parent: виджет-владелец.
		on_pick: что делать по нажатию «Выбрать файлы…». Форма поста
			сперва спрашивает у движка папку результатов сообщества
			и только потом открывает диалог (:meth:`open_dialog`);
			None — открыть сразу, на усмотрение Qt.
		"""
		super().__init__(parent)
		self._on_pick = on_pick
		self._kind = MediaKind.NONE
		self._paths: list[str] = []
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(4)
		row = QHBoxLayout()
		self._pick = PushButton("Выбрать файлы…", self)
		self._pick.setToolTip(
			f"Один файл — обычное вложение, несколько — альбом одной записью (до {MAX_ALBUM_FILES})"
		)
		self._pick.clicked.connect(self._pick_clicked)
		row.addWidget(self._pick)
		self._summary = BodyLabel("", self)
		self._summary.setWordWrap(True)
		row.addWidget(self._summary, stretch=1)
		layout.addLayout(row)
		self._files_box = QVBoxLayout()
		self._files_box.setSpacing(2)
		layout.addLayout(self._files_box)
		self._note = tinted(CaptionLabel("", self), DIM_TEXT)
		self._note.setWordWrap(True)
		layout.addWidget(self._note)
		self._build_rename(layout)
		self._render()

	def _build_rename(self, layout: QVBoxLayout) -> None:
		"""Строка переименования — только у одиночного файла."""
		row = rename_row(self, layout)
		self._rename_box, self._rename_check, self._rename_edit = row.box, row.check, row.edit
		self._rename_box.hide()

	# --- состав -----------------------------------------------------------------

	def set_kind(self, kind: MediaKind) -> None:
		"""Тип содержимого сменился: список очищается, если он стал лишним."""
		self._kind = kind
		if kind is MediaKind.NONE:
			self._paths = []
		self.setVisible(kind is not MediaKind.NONE)
		self._render()

	def files(self) -> tuple[MediaFile, ...]:
		"""Файлы поста в порядке выбора (пусто — текстовый пост)."""
		rename = self.rename_to()
		return tuple(
			MediaFile(path, self._kind, rename if len(self._paths) == 1 else None)
			for path in self._paths
		)

	def set_files(self, files: tuple[MediaFile, ...]) -> None:
		"""Показывает готовый список (правка элемента очереди)."""
		self._paths = [file.path for file in files]
		if files:
			self._kind = files[0].kind
		single_rename = files[0].rename_to if len(files) == 1 else None
		self._rename_edit.setText(single_rename or "")
		self._rename_check.setChecked(bool(single_rename))
		self._render()

	def rename_to(self) -> str | None:
		"""Новое имя одиночного файла (None — переименования нет)."""
		if len(self._paths) != 1 or not self._rename_check.isChecked():
			return None
		return str(self._rename_edit.text()).strip() or None

	def suggest_rename(self, filename: str) -> None:
		"""Показывает предложенное имя (шаблон имени файла, ADR-0015)."""
		if len(self._paths) != 1:
			return
		self._rename_edit.setText(filename)
		self._rename_check.setChecked(True)
		self._render()

	def clear(self) -> None:
		"""Забывает выбранные файлы (форма освободилась под следующий пост)."""
		self._paths = []
		self._rename_edit.clear()
		self._render()

	# --- показ --------------------------------------------------------------------

	def _pick_clicked(self) -> None:
		"""Нажали «Выбрать файлы…»: спросить папку или открыть сразу."""
		if self._on_pick is not None:
			self._on_pick()
			return
		self.open_dialog("")

	def open_dialog(self, start_dir: str = "") -> None:
		"""Диалог выбора: несколько файлов сразу — это и есть альбом."""
		paths, _ = QFileDialog.getOpenFileNames(
			self,
			f"Файлы поста — {kind_label(self._kind).lower()}",
			start_dir,
			kind_file_filter(self._kind),
		)
		if not paths:
			return
		self._paths = list(paths)
		self._rename_edit.clear()
		self._rename_check.setChecked(False)
		self._render()

	def _drop(self, path: str) -> None:
		"""Убирает файл из списка (сам файл на диске не трогается)."""
		self._paths = [item for item in self._paths if item != path]
		self._render()

	def _render(self) -> None:
		"""Перерисовывает список, подсказку и строку переименования."""
		clear_layout(self._files_box)
		for path in self._paths:
			row = QHBoxLayout()
			name = BodyLabel(Path(path).name, self)
			name.setToolTip(path)
			row.addWidget(name, stretch=1)
			drop = PushButton("Убрать", self)
			drop.setToolTip("Убрать файл из поста (на диске он останется)")
			drop.clicked.connect(bind(self._drop, path))
			row.addWidget(drop)
			self._files_box.addLayout(row)
		count = len(self._paths)
		self._summary.setText(
			"Файл не выбран" if not count else f"{count} {plural(count, 'файл', 'файла', 'файлов')}"
		)
		self._note.setText(album_note(count))
		self._rename_box.setVisible(count == 1)
		self.changed.emit()
