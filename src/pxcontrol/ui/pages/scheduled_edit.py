"""Правка отложенной записи прямо в её карточке — текст и время.

Форма живёт внутри карточки списка отложенных и раскрывается кликом,
как у элемента очереди (ADR-0016, п. 7). Правится то, что разрешает
Telegram у записи в очереди отложенных: текст (у записи с вложением —
подпись) и момент публикации. Вложение и тема показываются, но
не меняются: у ``messages.editMessage`` нет адресата темы, а замена
файла — загрузка с прогрессом, то есть задание очереди, а не правка;
такую запись удаляют и создают заново с «Публикации». «Сейчас» —
не вариант времени, а отдельное действие карточки.

Запись читается с сервера при раскрытии (:func:`mount_scheduled_editor`),
а не из снимка списка: её могли изменить из другого клиента Telegram.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, PrimaryPushButton, PushButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import ScheduledDraft, ScheduledPostDto
from pxcontrol.engine.telegram.rich_text import trimmed
from pxcontrol.engine.telegram.types import ForumTopicInfo, MediaKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DIM_TEXT,
	CharCounter,
	ErrorLabel,
	WhenRow,
	caption_placeholder,
	clear_layout,
	kind_label,
	tinted,
)
from pxcontrol.ui.pages.rich_edit import RichPostEdit

#: Высота поля текста в карточке: форма не должна занимать весь список.
_TEXT_HEIGHT = 120

#: Интервал между рядами формы (макет карточки очереди).
_FORM_SPACING = 10

#: Подсказка у выключенного «сейчас»: у отложки это действие карточки.
_NOW_HINT = "Опубликовать сразу — кнопка «Сейчас» у записи."

#: Сноска под формой: что здесь не правится и почему.
_LIMITS_NOTE = (
	"Сообщество, вложение и тема форума не меняются: такую запись удаляют "
	"и создают заново на «Публикации»."
)


def attachment_note(draft: ScheduledDraft, topic_title: str | None) -> str:
	"""Строка о вложении и теме записи (пустая — текст в общей ленте).

	Чистая функция: интерфейс подписывает то, что не правится, а не
	прячет — иначе человек искал бы, куда делось видео из его поста.
	"""
	parts: list[str] = []
	if draft.media_kind is MediaKind.POLL:
		parts.append("опрос: вопрос и варианты не меняются — правится только время")
	elif draft.media_kind is MediaKind.OTHER:
		parts.append("вложение без подписи (геопозиция, контакт…) — правится только время")
	elif draft.media_kind is not MediaKind.NONE:
		parts.append(f"вложение: {kind_label(draft.media_kind).lower()}")
	if draft.topic_id is not None:
		parts.append(f"тема форума: {topic_title or f'#{draft.topic_id}'}")
	return " · ".join(parts)


class ScheduledEditor(QWidget):
	"""Форма правки одной отложенной записи — тело её карточки."""

	def __init__(
		self,
		worker: EngineWorker,
		parent: QWidget,
		draft: ScheduledDraft,
		topic_title: str | None,
		on_saved: Callable[[], None],
		on_close: Callable[[], None],
	) -> None:
		"""Args:
		worker: мост к движку.
		parent: владелец (карточка списка).
		draft: запись, как её отдал сервер (``PostsService.scheduled_draft``).
		topic_title: название темы форума (None — не прочиталось или нет темы).
		on_saved: вызвать после успешного сохранения (перечитать сообщество).
		on_close: закрыть форму (свернуть карточку).
		"""
		super().__init__(parent)
		self._worker = worker
		self._draft = draft
		self._on_saved = on_saved
		self._on_close = on_close
		self._build(topic_title)

	def _build(self, topic_title: str | None) -> None:
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(_FORM_SPACING)
		note = attachment_note(self._draft, topic_title)
		if note:
			layout.addWidget(tinted(CaptionLabel(note, self), DIM_TEXT))
		with_media = self._draft.media_kind is not MediaKind.NONE
		# поле с оформлением (ADR-0033): разметка записи показана стилями
		# и уезжает обратно сущностями — правка её больше не стирает
		self._post_text = RichPostEdit(self, height=_TEXT_HEIGHT)
		self._text = self._post_text.edit
		self._post_text.set_rich(self._draft.rich)
		self._text.setPlaceholderText(caption_placeholder(not with_media))
		# у вложений не наших видов подписи нет — поле только показывает
		self._post_text.setEnabled(self._draft.text_editable)
		layout.addWidget(self._post_text)
		self._counter = CharCounter(self, layout, self._text, self._draft.text_limit)
		self._when_row = WhenRow(self, layout, compact=True, trailing=self._build_buttons())
		self._when_row.set_when(self._draft.when)
		self._when_row.set_now_allowed(False, _NOW_HINT)
		self._error = ErrorLabel(self)
		layout.addWidget(self._error)
		layout.addWidget(tinted(CaptionLabel(_LIMITS_NOTE, self), DIM_TEXT))

	def _build_buttons(self) -> list[QWidget]:
		cancel = PushButton("Отмена", self)
		cancel.setToolTip("Закрыть форму, ничего не меняя")
		cancel.clicked.connect(self._on_close)
		self._save_button = PrimaryPushButton("Сохранить", self)
		self._save_button.setToolTip("Изменить запись на сервере Telegram")
		self._save_button.clicked.connect(self._on_save)
		return [cancel, self._save_button]

	def _on_save(self) -> None:
		"""Отдаёт правку движку; он же скажет, если текст или время не годятся."""
		try:
			when = self._when_row.when()
		except ValueError as exc:  # время не «ЧЧ:ММ»
			self._error.fail(str(exc))
			return
		if when is None:
			self._error.fail(_NOW_HINT)
			return
		self._save_button.setEnabled(False)
		rich = trimmed(self._post_text.rich())
		run_in_engine(
			self._worker,
			self._worker.engine.posts.edit_scheduled(self._draft, rich.text, when, rich.entities),
			self,
			self._on_save_done,
			self._on_save_failed,
		)

	def _on_save_done(self) -> None:
		"""Правка принята сервером: перечитать сообщество и закрыть форму."""
		self._on_saved()
		self._on_close()

	def _on_save_failed(self, message: str) -> None:
		"""Отказ — причина в форме, набранное не теряется."""
		self._save_button.setEnabled(True)
		self._error.fail(message)


def mount_scheduled_editor(
	worker: EngineWorker,
	page: QWidget,
	item: ScheduledPostDto,
	body: QVBoxLayout,
	collapse: Callable[[], None],
	on_saved: Callable[[], None],
) -> None:
	"""Наполняет тело раскрытой карточки формой правки записи.

	Читает запись с сервера целиком; у записи в теме форума — ещё
	и список тем ради названия (не прочитались — покажется номер темы,
	форма всё равно откроется). Пока идёт чтение, в теле стоит
	заглушка: раскрытая пустота выглядит поломкой.

	Args:
		worker: мост к движку.
		page: страница-владелец (для колбэков моста).
		item: запись из списка (адрес — ``item.ref``).
		body: компоновка тела карточки.
		collapse: свернуть карточку (после сохранения или отмены).
		on_saved: вызвать после сохранения (перечитать сообщество).
	"""
	body.addWidget(CaptionLabel("Читаю запись из Telegram…", page))

	def fail(message: str) -> None:
		clear_layout(body)
		body.addWidget(CaptionLabel(f"Не удалось открыть правку: {message}", page))

	def show(draft: ScheduledDraft, topic_title: str | None = None) -> None:
		clear_layout(body)
		body.addWidget(ScheduledEditor(worker, page, draft, topic_title, on_saved, collapse))

	def with_draft(draft: ScheduledDraft) -> None:
		if draft.topic_id is None:
			show(draft)
			return

		def with_topics(topics: list[ForumTopicInfo]) -> None:
			title = next((topic.title for topic in topics if topic.id == draft.topic_id), None)
			show(draft, title)

		run_in_engine(
			worker,
			worker.engine.posts.list_topics(draft.ref.community_id),
			page,
			with_topics,
			# название темы — удобство: без него форма показывает номер
			lambda _message: show(draft),
		)

	run_in_engine(worker, worker.engine.posts.scheduled_draft(item.ref), page, with_draft, fail)
