"""Экран «Настройки › В Telegram»: настройки сообщества глазами публикатора (ADR-0043).

Живёт во вкладке «Настройки» страницы сообщества, как экран пресета
подписи: строка пути, шапка, форма разделами, одна кнопка «Сохранить».
Форму экран строит по каталогу движка: для каждой показываемой
настройки — подпись, редактор её вида (:mod:`setting_editors`) и под
ним пояснение или причина, по которой править нельзя. Своих знаний
о настройках у экрана нет — кроме одной заметки о боте (см.
:data:`BOT_NOTES`), которая относится к транспорту, а не к настройке.

Сохранение — одной командой движку: он применяет изменения по одному
и отвечает итогом по каждому. Отказ по части настроек человек видит
списком, экран показывает свежий снимок, а уход с экрана при частичном
отказе не происходит — непринятое надо увидеть.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	BreadcrumbBar,
	CaptionLabel,
	PushButton,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.community_settings.catalog import SettingSpec
from pxcontrol.engine.community_settings.model import (
	SECTION_TITLES,
	LinkedChat,
	SettingValue,
)
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_settings import SettingsSaved, SettingsView
from pxcontrol.engine.telegram.types import OwnerKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ErrorLabel,
	clear_layout,
	error_reporter,
	leave_with_question,
	section_header,
	show_success,
	show_warning,
)
from pxcontrol.ui.pages.setting_editors import EditorContext, SettingEditor, build_editor

#: Вопрос при уходе с несохранёнными правками.
SAVE_ON_LEAVE_HINT = (
	"В настройках сообщества в Telegram остались несохранённые изменения. "
	"Сохранить их перед уходом?"
)

#: Заметки о том, как настройку меняет бот (особенность Bot API, не настройки).
BOT_NOTES: dict[str, str] = {
	"permissions": "Бот меняет стикеры, GIF, игры и встроенных ботов только вместе.",
}

#: Ключ элемента «Настройки» строки пути.
_PATH_SETTINGS = "settings"

#: Ширина колонки подписей формы (как у форм «Задач»).
_LABEL_WIDTH = 220


def executor_caption(view: SettingsView) -> str:
	"""Подстрочник шапки: кто меняет настройки и что это значит."""
	if view.executor_kind is OwnerKind.BOT:
		return (
			f"Меняет бот {view.executor_label}. Боту доступны название, описание, фото "
			"и разрешения; остальное — через userbot-публикатора."
		)
	return f"Меняет публикатор по умолчанию {view.executor_label}."


def save_summary(saved: SettingsSaved, labels: dict[str, str]) -> tuple[bool, str]:
	"""Итог сохранения для человека: всё ли принято и что сказать.

	Returns:
		Пара «всё принято», текст — перечень непринятого по подписям
		настроек, либо сколько изменений применено.
	"""
	failed = saved.failed
	if not failed:
		count = len(saved.results)
		text = "Изменений нет." if count == 0 else f"Применено изменений: {count}."
		return True, text
	lines = [f"«{labels.get(item.key, item.key)}»: {item.error}" for item in failed]
	if saved.reread_error:
		lines.append(f"Настройки не перечитаны: {saved.reread_error}")
	return False, "\n".join(lines)


class TelegramSettingsScreen(QWidget):
	"""Экран настроек сообщества в Telegram.

	Сигналы: ``back_requested`` — клик по «Настройки» в строке пути;
	``members_requested`` — «Участники…» в объяснении, почему править
	некому; ``community_changed`` — сохранение изменило сообщество
	(название, @имя, темы) — страница перечитает шапку и путь.
	"""

	back_requested = Signal()
	members_requested = Signal()
	community_changed = Signal()

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self.community = community
		self._show_error = error_reporter(self)
		self._view: SettingsView | None = None
		self._editors: dict[str, SettingEditor] = {}
		self._dirty = False
		self._building_path = False
		self._build()

	# --- каркас -------------------------------------------------------------------

	def _build(self) -> None:
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().block_spacing)
		self._path = BreadcrumbBar(self)
		self._path.currentItemChanged.connect(self._on_path)
		layout.addWidget(self._path, alignment=Qt.AlignmentFlag.AlignLeft)
		head = QVBoxLayout()
		head.setSpacing(2)
		head.addWidget(SubtitleLabel("Настройки в Telegram", self))
		self._caption = CaptionLabel(self)
		self._caption.setWordWrap(True)
		head.addWidget(self._caption)
		layout.addLayout(head)
		self._status = QVBoxLayout()
		layout.addLayout(self._status)
		self._form = QVBoxLayout()
		self._form.setSpacing(density.spacing().block_spacing)
		layout.addLayout(self._form)
		self._error = ErrorLabel(self)
		layout.addWidget(self._error)
		layout.addLayout(self._build_save_row())
		layout.addStretch()
		self.render_path()

	def _build_save_row(self) -> QHBoxLayout:
		row = QHBoxLayout()
		row.addStretch()
		self._revert = PushButton("Отменить изменения", self)
		self._revert.clicked.connect(self.discard)
		row.addWidget(self._revert)
		self._save = PushButton("Сохранить", self)
		self._save.clicked.connect(lambda: self.save())
		row.addWidget(self._save)
		self._set_dirty(False)
		return row

	def render_path(self) -> None:
		"""Строка пути: «Настройки» › «В Telegram»."""
		self._building_path = True
		try:
			self._path.clear()
			self._path.addItem(_PATH_SETTINGS, "Настройки")
			self._path.addItem("telegram", "В Telegram")
		finally:
			self._building_path = False

	def _on_path(self, route_key: str) -> None:
		if not self._building_path and route_key == _PATH_SETTINGS:
			self.back_requested.emit()

	# --- загрузка и показ ------------------------------------------------------------

	def open(self) -> None:
		"""Читает настройки из Telegram (экран показывает «читаю…»)."""
		self.render_path()
		self._show_status("Читаю настройки из Telegram…")
		clear_layout(self._form)
		self._editors = {}
		self._set_dirty(False)
		run_in_engine(
			self._worker,
			self._worker.engine.community_settings.open(self.community.id),
			self,
			self.show_view,
			self._on_open_failed,
		)

	def _on_open_failed(self, message: str) -> None:
		retry = PushButton("Повторить", self)
		retry.clicked.connect(self.open)
		self._show_status(f"Настройки не прочитаны: {message}", retry)

	def _show_status(self, text: str, action: QWidget | None = None) -> None:
		clear_layout(self._status)
		label = BodyLabel(text, self)
		label.setWordWrap(True)
		self._status.addWidget(label)
		if action is not None:
			self._status.addWidget(action, alignment=Qt.AlignmentFlag.AlignLeft)

	def show_view(self, view: SettingsView) -> None:
		"""Показывает экран: форму — или объяснение, почему править некому."""
		self._view = view
		clear_layout(self._status)
		clear_layout(self._form)
		self._editors = {}
		self._error.succeed()
		if view.reason is not None or view.settings is None:
			members = PushButton("Участники…", self)
			members.clicked.connect(self.members_requested.emit)
			self._caption.setText("")
			self._show_status(view.reason or "Править некому.", members)
			self._set_dirty(False)
			return
		self._caption.setText(executor_caption(view))
		for section, title in SECTION_TITLES.items():
			specs = [spec for spec in view.specs if spec.section is section]
			if specs:
				self._form.addWidget(self._section(title, specs, view))
		self._set_dirty(False)

	def _section(self, title: str, specs: list[SettingSpec], view: SettingsView) -> QWidget:
		"""Раздел формы: заголовок и строки «подпись — редактор — пояснение»."""
		assert view.settings is not None
		box = QWidget(self)
		column = QVBoxLayout(box)
		column.setContentsMargins(0, 0, 0, 0)
		column.addWidget(section_header(box, title))
		grid = QGridLayout()
		grid.setHorizontalSpacing(16)
		grid.setColumnMinimumWidth(0, _LABEL_WIDTH)
		grid.setColumnStretch(1, 1)
		context = EditorContext(view.settings.context, self._load_discussion)
		for row, spec in enumerate(specs):
			self._add_row(grid, row, spec, view, context)
		column.addLayout(grid)
		return box

	def _add_row(
		self,
		grid: QGridLayout,
		row: int,
		spec: SettingSpec,
		view: SettingsView,
		context: EditorContext,
	) -> None:
		assert view.settings is not None and view.access is not None
		label = BodyLabel(spec.label, self)
		label.setWordWrap(True)
		grid.addWidget(label, 2 * row, 0, Qt.AlignmentFlag.AlignTop)
		editor = build_editor(spec, view.settings.value(spec.key), context, self)
		access = view.access[spec.key]
		editor.setEnabled(access.editable)
		editor.changed.connect(self._on_changed)
		grid.addWidget(editor, 2 * row, 1)
		self._editors[spec.key] = editor
		note = self._note(spec, view, access.reason)
		if note:
			caption = CaptionLabel(note, self)
			caption.setWordWrap(True)
			grid.addWidget(caption, 2 * row + 1, 1)

	@staticmethod
	def _note(spec: SettingSpec, view: SettingsView, reason: str | None) -> str:
		"""Пояснение под редактором: причина недоступности важнее подсказки."""
		if reason:
			return reason
		if view.executor_kind is OwnerKind.BOT and spec.key in BOT_NOTES:
			return BOT_NOTES[spec.key]
		return spec.hint

	def _load_discussion(self, done: Callable[[list[LinkedChat]], None]) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.community_settings.discussion_candidates(self.community.id),
			self,
			done,
			self._show_error,
		)

	# --- правки и сохранение --------------------------------------------------------------

	def _values(self) -> dict[str, SettingValue]:
		return {key: editor.value() for key, editor in self._editors.items()}

	def _on_changed(self) -> None:
		view = self._view
		if view is None or view.settings is None:
			return
		settings = view.settings
		self._set_dirty(any(value != settings.value(key) for key, value in self._values().items()))

	def _set_dirty(self, dirty: bool) -> None:
		self._dirty = dirty
		self._save.setEnabled(dirty)
		self._revert.setEnabled(dirty)

	@property
	def dirty(self) -> bool:
		"""Есть ли несохранённые правки."""
		return self._dirty

	def save(
		self,
		then: Callable[[], None] | None = None,
		failed: Callable[[], None] | None = None,
	) -> None:
		"""Отправляет правку движку; ``then`` — только если приняли всё."""
		view = self._view
		if view is None or view.settings is None:
			if failed is not None:
				failed()
			return
		self._save.setEnabled(False)

		def on_error(message: str) -> None:
			self._error.fail(message)
			self._set_dirty(True)
			if failed is not None:
				failed()

		run_in_engine(
			self._worker,
			self._worker.engine.community_settings.save(
				self.community.id, view.settings, self._values()
			),
			self,
			lambda saved: self._on_saved(saved, then, failed),
			on_error,
		)

	def _on_saved(
		self,
		saved: SettingsSaved,
		then: Callable[[], None] | None,
		failed: Callable[[], None] | None,
	) -> None:
		labels = {spec.key: spec.label for spec in (self._view.specs if self._view else ())}
		complete, text = save_summary(saved, labels)
		if saved.view is not None:
			self.show_view(saved.view)
		else:
			self.open()
		if any(result.applied for result in saved.results):
			self.community_changed.emit()
		if complete:
			show_success(self, "Готово", text)
			if then is not None:
				then()
			return
		show_warning(self, "Не всё принято Telegram", text)
		if failed is not None:
			failed()

	def discard(self) -> None:
		"""Отбрасывает правки: форма возвращается к прочитанному."""
		if self._view is not None:
			self.show_view(self._view)

	def leave(self, then: Callable[[], None], *, stay: Callable[[], None] | None = None) -> None:
		"""Уход с экрана: сразу — без правок, иначе по ответу человека."""
		leave_with_question(
			self,
			SAVE_ON_LEAVE_HINT,
			dirty=self._dirty,
			save=lambda done, failed: self.save(then=done, failed=failed),
			discard=self.discard,
			then=then,
			stay=stay,
		)
