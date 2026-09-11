"""Правка элемента очереди отправки: рабочее окно (ADR-0016, ADR-0023).

Правится всё, что ещё не ушло в Telegram: текст, вложение (замена,
удаление, добавление), переименование при отправке, тема форума и время
публикации. Канал-получатель не меняется — у другого канала свои темы,
лимит файла и права на отложенную публикацию; такой пост создают заново.

Окно открывается уже с данными: :func:`open_queue_item_editor` читает
черновик, сообщество и темы форума, и только потом строит форму — так
поля не мигают пустыми, пока движок отвечает.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	LineEdit,
	PrimaryPushButton,
	PushButton,
	SegmentedWidget,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.posts import PostDraft, TextLimits, publish_capabilities
from pxcontrol.engine.services.video import VideoDirs
from pxcontrol.engine.telegram.types import ForumTopicInfo, MediaKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	CONTENT_KINDS,
	CharCounter,
	DtoComboBox,
	ErrorLabel,
	WhenRow,
	WorkDialog,
	community_combo_label,
	error_reporter,
	exec_dialog,
	kind_file_filter,
	kind_label,
	pick_file,
	topic_label,
	visible_topics,
)

logger = logging.getLogger(__name__)

#: Подсказка под временем, когда канал публикует только ботом: у бота
#: нет отложенных (ADR-0010/0011), остаётся «сейчас».
_BOT_ONLY_HINT = "Отложенная публикация требует userbot-админа канала — через бота только «сейчас»."


class QueueItemEditDialog(WorkDialog):
	"""Окно правки одного элемента очереди отправки.

	Состав формы повторяет «Публикацию», но канал в ней не выбирают:
	он задан элементом и показан в шапке. Сохранение возвращает пост
	в работу — «сейчас» уходит в отправку, отложенный ждёт слота.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		parent: QWidget,
		item_id: int,
		draft: PostDraft,
		community: CommunityDto,
		limits: TextLimits,
		topics: list[ForumTopicInfo],
		topics_error: str = "",
	) -> None:
		"""Args:
		worker: мост к движку.
		parent: окно-родитель.
		item_id: элемент очереди, который правим.
		draft: его текущий черновик (из ``PublishQueue.get_draft``).
		community: канал-получатель (для тем, вида и возможностей).
		limits: пределы длины текста канала (счётчик под полем).
		topics: темы форума; пустой список — не форум или не прочитались.
		topics_error: почему темы не прочитались (пусто — прочитались).
		"""
		super().__init__("Правка поста в очереди", parent, size=(720, 640))
		self._worker = worker
		self._item_id = item_id
		self._draft = draft
		self._community = community
		self._limits = limits
		self._caps = publish_capabilities(community.bot_id is not None, community.userbot_assigned)
		self._kind = draft.media_kind
		# тема, которую пост сохранит, если ряд выбора скрыт: правка
		# не должна молча переселять пост из темы в общую ленту
		self._fallback_topic_id = draft.topic_id if community.forum else None
		self._build(topics, topics_error)

	# --- сборка ----------------------------------------------------------------

	def _build(self, topics: list[ForumTopicInfo], topics_error: str) -> None:
		"""Собирает форму и заполняет её текущим черновиком."""
		layout = self.content
		layout.addWidget(BodyLabel(f"Канал: {community_combo_label(self._community)}", self))
		self._build_topic_row(layout, topics, topics_error)
		self._build_kind_segments(layout)
		self._build_file_row(layout)
		self._text = TextEdit(self)
		self._text.setPlainText(self._draft.text)
		layout.addWidget(self._text, stretch=1)
		self._counter = CharCounter(self, layout, self._text)
		self._when_row = WhenRow(self, layout)
		self._when_row.set_schedule_allowed(self._caps.userbot, _BOT_ONLY_HINT)
		self._when_row.set_when(self._draft.when)
		self._error = ErrorLabel(self)
		layout.addWidget(self._error)
		self._apply_kind()
		self._build_buttons()

	def _build_topic_row(
		self, layout: QVBoxLayout, topics: list[ForumTopicInfo], topics_error: str
	) -> None:
		"""Ряд выбора темы форума с предвыбором текущей (ADR-0021/0022)."""
		self._topic_box = QWidget(self)
		row = QHBoxLayout(self._topic_box)
		row.setContentsMargins(0, 0, 0, 0)
		row.addWidget(BodyLabel("Тема форума:", self._topic_box))
		self._topic_combo: DtoComboBox[ForumTopicInfo] = DtoComboBox(
			self._topic_box, placeholder="Общая лента"
		)
		row.addWidget(self._topic_combo, stretch=1)
		self._topic_hint = CaptionLabel("", self._topic_box)
		row.addWidget(self._topic_hint)
		layout.addWidget(self._topic_box)
		if not self._community.forum or not self._caps.userbot:
			self._topic_box.setVisible(False)
			return
		if topics_error:
			# ряд скрыт, но тема поста сохранится прежней (_fallback_topic_id)
			self._topic_box.setVisible(False)
			self._topic_hint.setText(topics_error)
			return
		shown, closed = visible_topics(topics, self._community.default_role)
		self._topic_combo.set_items(shown, label=topic_label, key=lambda topic: topic.id)
		if closed:
			self._topic_hint.setText(f"Закрытых тем скрыто: {closed} — в них пишет только админ.")
		if self._draft.topic_id is not None and not self._topic_combo.select(
			lambda topic: topic.id == self._draft.topic_id
		):
			# тему удалили или она закрылась для участника: молчать нельзя —
			# сохранение переставит пост в общую ленту
			self._topic_hint.setText("Прежняя тема недоступна — пост уйдёт в общую ленту.")

	def _build_kind_segments(self, layout: QVBoxLayout) -> None:
		"""Сегментный переключатель типа контента (как на «Публикации»)."""
		self._segments = SegmentedWidget(self)
		for label, kind, _file_filter in CONTENT_KINDS:
			self._segments.addItem(routeKey=kind.value, text=label)
		self._segments.setCurrentItem(self._kind.value)
		self._segments.currentItemChanged.connect(self._on_kind_changed)
		layout.addWidget(self._segments)

	def _build_file_row(self, layout: QVBoxLayout) -> None:
		"""Строка вложения: путь, «Обзор…», «Убрать» и переименование."""
		self._file_box = QWidget(self)
		row = QHBoxLayout(self._file_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._file_edit = LineEdit(self._file_box)
		self._file_edit.setPlaceholderText("Файл вложения…")
		self._file_edit.setText(self._draft.media_path or "")
		row.addWidget(self._file_edit, stretch=1)
		browse = PushButton("Обзор…", self._file_box)
		browse.clicked.connect(self._pick_file)
		row.addWidget(browse)
		drop = PushButton("Убрать", self._file_box)
		drop.setToolTip("Убрать вложение — пост станет текстовым.")
		drop.clicked.connect(self._drop_file)
		row.addWidget(drop)
		layout.addWidget(self._file_box)
		self._build_rename_row(layout)

	def _build_rename_row(self, layout: QVBoxLayout) -> None:
		"""Строка переименования файла при отправке."""
		self._rename_box = QWidget(self)
		row = QHBoxLayout(self._rename_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._rename_check = CheckBox("Переименовать при отправке:", self._rename_box)
		self._rename_check.setChecked(bool(self._draft.rename_to))
		row.addWidget(self._rename_check)
		self._rename_edit = LineEdit(self._rename_box)
		self._rename_edit.setText(self._draft.rename_to or "")
		self._rename_edit.setPlaceholderText("Новое имя файла с расширением…")
		row.addWidget(self._rename_edit, stretch=1)
		layout.addWidget(self._rename_box)

	def _build_buttons(self) -> None:
		"""Кнопки окна: сохранение возвращает пост в работу."""
		cancel = PushButton("Отмена", self)
		cancel.clicked.connect(self.reject)
		self.buttons.addWidget(cancel)
		self._save_button = PrimaryPushButton("Сохранить и отправить", self)
		self._save_button.setToolTip(
			"Пост вернётся в очередь: «сейчас» — в отправку, отложенный — ждать слота."
		)
		self._save_button.clicked.connect(self._on_save)
		self.buttons.addWidget(self._save_button)

	# --- поведение формы -------------------------------------------------------

	def _on_kind_changed(self, kind_key: str) -> None:
		"""Меняет состав формы под выбранный тип контента."""
		self._kind = MediaKind(kind_key)
		self._apply_kind()

	def _apply_kind(self) -> None:
		"""Показывает ряды вложения по типу и правит подсказку текста."""
		is_text = self._kind is MediaKind.NONE
		self._file_box.setVisible(not is_text)
		self._rename_box.setVisible(not is_text)
		self._text.setPlaceholderText(
			"Текст поста…" if is_text else "Подпись к файлу (необязательно)…"
		)
		# подпись к файлу вчетверо короче поста без вложения
		self._counter.set_limit(self._limits.text if is_text else self._limits.caption)

	def _drop_file(self) -> None:
		"""Убирает вложение: пост становится текстовым."""
		self._file_edit.clear()
		self._rename_edit.clear()
		self._rename_check.setChecked(False)
		self._segments.setCurrentItem(MediaKind.NONE.value)
		self._kind = MediaKind.NONE
		self._apply_kind()

	def _pick_file(self) -> None:
		"""Диалог выбора вложения: видео — из папки результатов канала."""
		if self._kind is MediaKind.VIDEO:
			run_in_engine(
				self._worker,
				self._worker.engine.video.processed_dir_for_community(self._community.id),
				self,
				self._open_file_dialog,
				# папку не узнали — не повод не дать выбрать файл
				lambda _message: self._open_file_dialog(""),
			)
			return
		current = self._file_edit.text().strip()
		self._open_file_dialog(str(Path(current).parent) if current else "")

	def _open_file_dialog(self, start: str | VideoDirs) -> None:
		"""Открывает диалог вложения; ``start`` — папка или VideoDirs."""
		start_dir = start.processed if isinstance(start, VideoDirs) else start
		path = pick_file(self, "Файл вложения", kind_file_filter(self._kind), start_dir=start_dir)
		if not path:
			return
		self._file_edit.setText(path)
		# имя от прежнего файла к новому не относится
		self._rename_edit.clear()
		self._rename_check.setChecked(False)

	# --- сохранение ------------------------------------------------------------

	def _on_save(self) -> None:
		"""Отдаёт правку движку; он же скажет, если черновик не годится."""
		try:
			draft = self._collect()
		except ValueError as exc:  # поля формы не согласованы (файл, время)
			self._error.fail(str(exc))
			return
		self._save_button.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.edit(self._item_id, draft),
			self,
			self.accept,
			self._on_save_failed,
		)

	def _on_save_failed(self, message: str) -> None:
		"""Движок отклонил правку — показываем причину, форму не теряем."""
		self._save_button.setEnabled(True)
		self._error.fail(message)

	def _collect(self) -> PostDraft:
		"""Собирает черновик из полей формы.

		Raises:
			ValueError: Выбран тип с вложением, а файл не указан, либо
				время публикации не «ЧЧ:ММ».
		"""
		media = self._file_edit.text().strip() or None
		is_text = self._kind is MediaKind.NONE
		if not is_text and media is None:
			raise ValueError(
				f"Выбран тип «{kind_label(self._kind)}», а файл не указан — "
				"выберите файл или переключитесь на «Текст»."
			)
		return PostDraft(
			community_id=self._draft.community_id,
			text=self._text.toPlainText().strip(),
			media_path=None if is_text else media,
			media_kind=MediaKind.NONE if is_text else self._kind,
			when=self._when_row.when(),
			rename_to=self._rename_to(),
			topic_id=self._selected_topic_id(),
		)

	def _rename_to(self) -> str | None:
		"""Новое имя файла, если переименование включено и имя задано."""
		if self._kind is MediaKind.NONE or not self._rename_check.isChecked():
			return None
		return self._rename_edit.text().strip() or None

	def _selected_topic_id(self) -> int | None:
		"""Тема из видимого ряда; ряд скрыт — прежняя тема поста."""
		if not self._topic_box.isVisibleTo(self):
			return self._fallback_topic_id
		topic = self._topic_combo.selected()
		return topic.id if topic is not None else None


def open_queue_item_editor(
	worker: EngineWorker, parent: QWidget, item_id: int, on_saved: object = None
) -> None:
	"""Читает данные элемента и открывает окно правки.

	Цепочка чтений: черновик → сообщество → пределы длины текста → темы
	форума (последние — только у форума с userbot-публикатором). Темы
	не прочитались — окно всё равно откроется, но ряд выбора темы будет
	скрыт, а пост сохранит свою прежнюю тему. Общая точка входа для панели очереди на «Публикации»
	и для окна полного просмотра.

	Args:
		worker: мост к движку.
		parent: окно-родитель для диалога и владелец колбэков.
		item_id: элемент очереди.
		on_saved: вызывается после сохранения (обновить панель).
	"""
	show_error = error_reporter(parent)

	def show(
		draft: PostDraft,
		community: CommunityDto,
		limits: TextLimits,
		topics: list[ForumTopicInfo],
		topics_error: str = "",
	) -> None:
		dialog = QueueItemEditDialog(
			worker, parent, item_id, draft, community, limits, topics, topics_error
		)
		if exec_dialog(dialog) and callable(on_saved):
			on_saved()

	def with_limits(draft: PostDraft, community: CommunityDto, limits: TextLimits) -> None:
		caps = publish_capabilities(community.bot_id is not None, community.userbot_assigned)
		if not community.forum or not caps.userbot:
			show(draft, community, limits, [])
			return
		run_in_engine(
			worker,
			worker.engine.posts.list_topics(community.id),
			parent,
			lambda topics: show(draft, community, limits, topics),
			lambda message: show(
				draft,
				community,
				limits,
				[],
				f"Темы форума не загрузились ({message}) — пост останется в своей теме.",
			),
		)

	def with_community(draft: PostDraft, community: CommunityDto) -> None:
		run_in_engine(
			worker,
			worker.engine.posts.text_limits(community.id),
			parent,
			lambda limits: with_limits(draft, community, limits),
			show_error,
		)

	def with_draft(draft: PostDraft) -> None:
		run_in_engine(
			worker,
			worker.engine.communities.get_community(draft.community_id),
			parent,
			lambda community: with_community(draft, community),
			show_error,
		)

	run_in_engine(
		worker, worker.engine.publish_queue.get_draft(item_id), parent, with_draft, show_error
	)
