"""Правка вышедшего поста прямо в его карточке — текст и кнопки.

Форма живёт внутри карточки ленты и раскрывается кликом, как у элемента
очереди (ADR-0016, п. 7) и у отложенной записи (ADR-0028). Пост уже
в ленте, поэтому правок ровно две, и делают их **разные исполнители**
(матрица стадии 4 в маршрутах публикации):

- **текст (подпись)** правит публикатор; кнопки под постом при этом
  сохраняются — правка их не трогает;
- **кнопки** ставит, меняет и снимает только бот, и только в канале:
  права «изменять сообщения» у групп не существует.

Поэтому «Сохранить» может отправить два обращения — но лишь то, что
человек действительно изменил: правка текста без нужды не трогает
клавиатуру, и наоборот.

Вложение и тема здесь не меняются: замена файла — загрузка
с прогрессом, то есть задание очереди, а не правка формы (то же
решение, что у отложенных записей в ADR-0028); адресата темы
у ``messages.editMessage`` нет вовсе.

Пост читается с сервера при раскрытии карточки: его могли поправить
из другого клиента Telegram, а истина живёт в самом сообществе
(ADR-0010).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, PrimaryPushButton, PushButton, TextEdit

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import PublishedDraft, PublishedPostDto, PublishedRef
from pxcontrol.engine.telegram.markup import PostMarkup
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DIM_TEXT,
	CharCounter,
	CollapsibleCard,
	ErrorLabel,
	caption_placeholder,
	clear_layout,
	kind_label,
	plural,
	tinted,
)
from pxcontrol.ui.pages.markup_editor import MarkupEditor

#: Высота поля текста в карточке: форма не должна занимать всю ленту.
_TEXT_HEIGHT = 120

#: Интервал между рядами формы (как у формы правки элемента очереди).
_FORM_SPACING = 10

#: Сноска под формой: что здесь не правится и почему.
_LIMITS_NOTE = (
	"Вложение и тема форума не меняются: замена файла — это загрузка "
	"с прогрессом, то есть задание очереди, а не правка формы."
)


def attachment_note(draft: PublishedDraft) -> str:
	"""Строка о вложении поста (пустая — обычный текстовый пост).

	Чистая функция: то, что не правится, подписывается, а не прячется —
	иначе человек искал бы, куда делось видео из его поста.
	"""
	if draft.media_kind is MediaKind.OTHER:
		return "вложение без подписи (опрос, геопозиция…) — правятся только кнопки"
	if draft.media_kind is not MediaKind.NONE:
		return f"вложение: {kind_label(draft.media_kind).lower()}"
	return ""


def markup_state_note(draft: PublishedDraft) -> str:
	"""Что сказать о нынешней клавиатуре поста над её редактором.

	Кнопки не наших видов (callback, переход в бота) поставили не мы:
	показать их полями формы нельзя, и обещать правку «как есть» —
	значит соврать. Такую клавиатуру заменяют целиком, и об этом
	говорится прямо.
	"""
	if draft.markup_blocker is not None:
		return draft.markup_blocker
	if draft.buttons and draft.markup is None:
		return (
			f"Под постом {draft.buttons} "
			f"{plural(draft.buttons, 'кнопка', 'кнопки', 'кнопок')} не нашего вида — "
			"показать их полями нельзя. Сохранение заменит клавиатуру целиком "
			"(пустая — снимет кнопки)."
		)
	if draft.buttons:
		return "Пустая клавиатура снимает кнопки под постом."
	return "Кнопки поставит бот сообщества — публикатор их не трогает."


class PublishedEditor(QWidget):
	"""Форма правки одного вышедшего поста — тело его карточки."""

	def __init__(
		self,
		worker: EngineWorker,
		parent: QWidget,
		draft: PublishedDraft,
		on_saved: Callable[[], None],
		on_close: Callable[[], None],
	) -> None:
		"""Args:
		worker: мост к движку.
		parent: владелец (карточка ленты).
		draft: пост, как его отдал сервер (``PostsService.published_draft``).
		on_saved: вызвать после сохранения (перечитать ленту).
		on_close: закрыть форму (свернуть карточку).
		"""
		super().__init__(parent)
		self._worker = worker
		self._draft = draft
		self._on_saved = on_saved
		self._on_close = on_close
		self._build()

	def _build(self) -> None:
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(_FORM_SPACING)
		note = attachment_note(self._draft)
		if note:
			layout.addWidget(tinted(CaptionLabel(note, self), DIM_TEXT))
		with_media = self._draft.media_kind is not MediaKind.NONE
		self._text = TextEdit(self)
		self._text.setPlainText(self._draft.text)
		self._text.setPlaceholderText(caption_placeholder(not with_media))
		self._text.setFixedHeight(_TEXT_HEIGHT)
		self._text.setEnabled(self._draft.text_editable)
		layout.addWidget(self._text)
		self._counter = CharCounter(self, layout, self._text, self._draft.text_limit)
		self._build_markup(layout)
		self._error = ErrorLabel(self)
		layout.addWidget(self._error)
		layout.addWidget(tinted(CaptionLabel(_LIMITS_NOTE, self), DIM_TEXT))
		layout.addLayout(self._build_buttons())

	def _build_markup(self, layout: QVBoxLayout) -> None:
		"""Блок кнопок поста: заполнен нынешней клавиатурой, если она наша."""
		card = CollapsibleCard("Кнопки под постом", self)
		count = self._draft.buttons
		card.set_summary(
			f"{count} {plural(count, 'кнопка', 'кнопки', 'кнопок')}" if count else "нет"
		)
		self._markup = MarkupEditor(card)
		if self._draft.markup is not None:
			self._markup.set_markup(self._draft.markup)
		self._markup.set_notice(markup_state_note(self._draft))
		self._markup.set_blocked(self._draft.markup_blocker)
		card.body.addWidget(self._markup)
		layout.addWidget(card)

	def _build_buttons(self) -> QHBoxLayout:
		row = QHBoxLayout()
		row.addStretch()
		cancel = PushButton("Отмена", self)
		cancel.setToolTip("Закрыть форму, ничего не меняя")
		cancel.clicked.connect(self._on_close)
		row.addWidget(cancel)
		self._save_button = PrimaryPushButton("Сохранить", self)
		self._save_button.setToolTip("Применить изменения к посту в Telegram")
		self._save_button.clicked.connect(self._on_save)
		row.addWidget(self._save_button)
		return row

	# --- сохранение ------------------------------------------------------------

	def _text_changed(self) -> bool:
		"""Изменил ли человек текст поста."""
		return self._draft.text_editable and self._text.toPlainText().strip() != self._draft.text

	def _markup_changed(self) -> bool:
		"""Изменил ли человек клавиатуру поста.

		Чужую клавиатуру (не наших видов) сравнивать не с чем: пустой
		редактор при непустых кнопках означал бы «снять», и человек
		должен получить это только по своей воле — поэтому изменением
		считается лишь непустой набор.
		"""
		if self._draft.markup_blocker is not None:
			return False
		markup = self._markup.markup()
		if not self._draft.markup_ours:
			return markup is not None
		return markup != self._draft.markup

	def _on_save(self) -> None:
		"""Отправляет только то, что действительно изменилось."""
		if not (self._text_changed() or self._markup_changed()):
			self._error.fail("Ничего не изменилось — правка не нужна.")
			return
		self._save_button.setEnabled(False)
		if self._text_changed():
			run_in_engine(
				self._worker,
				self._worker.engine.posts.edit_published(
					self._draft, self._text.toPlainText().strip()
				),
				self,
				self._after_text,
				self._failed,
			)
			return
		self._save_markup()

	def _after_text(self) -> None:
		"""Текст изменён: если правились и кнопки — теперь их черёд."""
		if self._markup_changed():
			self._save_markup()
			return
		self._done()

	def _save_markup(self) -> None:
		"""Отдаёт клавиатуру боту (пустая — снять кнопки)."""
		markup: PostMarkup | None = self._markup.markup()
		run_in_engine(
			self._worker,
			self._worker.engine.posts.set_published_markup(
				PublishedRef(self._draft.ref.community_id, self._draft.ref.message_id),
				markup if markup is not None else PostMarkup(),
			),
			self,
			self._done,
			self._failed,
		)

	def _done(self) -> None:
		"""Правка принята: перечитать ленту и закрыть форму."""
		self._on_saved()
		self._on_close()

	def _failed(self, message: str) -> None:
		"""Отказ — причина в форме, набранное не теряется."""
		self._save_button.setEnabled(True)
		self._error.fail(message)


def mount_published_editor(
	worker: EngineWorker,
	page: QWidget,
	item: PublishedPostDto,
	body: QVBoxLayout,
	collapse: Callable[[], None],
	on_saved: Callable[[], None],
) -> None:
	"""Наполняет тело раскрытой карточки формой правки вышедшего поста.

	Пост читается с сервера целиком: снимку ленты верить нельзя, его
	могли поправить из другого клиента. Пока идёт чтение, в теле стоит
	заглушка — раскрытая пустота выглядит поломкой.

	Args:
		worker: мост к движку.
		page: страница-владелец (для колбэков моста).
		item: пост из ленты (адрес — сообщество и номер).
		body: компоновка тела карточки.
		collapse: свернуть карточку (после сохранения или отмены).
		on_saved: вызвать после сохранения (перечитать ленту).
	"""
	body.addWidget(CaptionLabel("Читаю пост из Telegram…", page))

	def fail(message: str) -> None:
		clear_layout(body)
		body.addWidget(CaptionLabel(f"Не удалось открыть правку: {message}", page))

	def show(draft: PublishedDraft) -> None:
		clear_layout(body)
		body.addWidget(PublishedEditor(worker, page, draft, on_saved, collapse))

	run_in_engine(
		worker,
		worker.engine.posts.published_draft(PublishedRef(item.community_id, item.message_id)),
		page,
		show,
		fail,
	)
