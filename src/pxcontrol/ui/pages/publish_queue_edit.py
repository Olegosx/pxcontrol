"""Правка элемента очереди отправки прямо в его карточке (ADR-0016, п. 7).

Форма живёт внутри карточки списка и раскрывается кликом по ней — тем же
жестом, каким раскрываются параметры файла на «Видео». Правится всё, что
ещё не ушло в Telegram: текст, вложение (замена, удаление, добавление),
переименование при отправке, тема форума и время публикации. Канал-
получатель не меняется — у другого канала свои темы, лимит файла и право
на отложенную публикацию; такой пост создают заново.

Данные тянутся лениво, при первом раскрытии карточки
(:func:`mount_queue_item_editor`): держать их для всего списка значило бы
десятки запросов на каждый опрос очереди.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	PrimaryPushButton,
	PushButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.posts import PREMIUM_LIMITS, PostDraft, TextLimits
from pxcontrol.engine.services.publish_route import poll_blocker
from pxcontrol.engine.services.video import VideoDirs
from pxcontrol.engine.telegram.rich_text import trimmed
from pxcontrol.engine.telegram.types import (
	ForumTopicInfo,
	LinkPreview,
	MediaKind,
)
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DIM_TEXT,
	CharCounter,
	CollapsibleCard,
	ErrorLabel,
	WhenRow,
	caption_placeholder,
	clear_layout,
	closed_topics_hint,
	kind_label,
	kind_segments,
	plural,
	tinted,
	topic_label,
	topic_row,
	visible_topics,
)
from pxcontrol.ui.pages.markup_editor import MarkupEditor, markup_state
from pxcontrol.ui.pages.media_picker import MediaPicker, over_bot_limit
from pxcontrol.ui.pages.poll_editor import PollEditor
from pxcontrol.ui.pages.post_target import IdentityChoice
from pxcontrol.ui.pages.preview_row import PreviewRow
from pxcontrol.ui.pages.rich_edit import RichPostEdit

#: Подсказка под временем, когда канал публикует только ботом: у бота
#: нет отложенных (ADR-0010/0011), остаётся «сейчас».
_BOT_ONLY_HINT = "Отложенная публикация требует userbot-админа канала — через бота только «сейчас»."

#: Высота поля текста в карточке: форма не должна занимать весь список.
_TEXT_HEIGHT = 120

#: Интервал между рядами формы (макет карточки очереди).
_FORM_SPACING = 10

#: Сноска под формой: канал-получатель в правке не меняется.
_RECIPIENT_NOTE = (
	"Канал получателя не меняется — у другого свои темы, лимит файла "
	"и право на отложенную публикацию."
)


class QueueItemEditor(QWidget):
	"""Форма правки одного элемента очереди — тело его карточки.

	Канал показан строкой и не редактируется. Сохранение возвращает пост
	в работу: «сейчас» уходит в отправку, отложенный — ждать слота.
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
		topics_error: str,
		on_saved: Callable[[], None],
		on_close: Callable[[], None],
	) -> None:
		"""Args:
		worker: мост к движку.
		parent: владелец (карточка списка).
		item_id: элемент очереди, который правим.
		draft: его текущий черновик (из ``PublishQueue.get_draft``).
		community: канал-получатель (темы, вид, возможности).
		limits: пределы длины текста канала (счётчик под полем).
		topics: темы форума; пустой список — не форум или не прочитались.
		topics_error: почему темы не прочитались (пусто — прочитались).
		on_saved: вызвать после успешного сохранения (обновить панель).
		on_close: закрыть форму (свернуть карточку).
		"""
		super().__init__(parent)
		self._worker = worker
		self._item_id = item_id
		self._draft = draft
		self._community = community
		self._limits = limits
		self._caps = community.capabilities
		self._kind = draft.media_kind
		self._on_saved = on_saved
		self._on_close = on_close
		# тема, которую пост сохранит, если ряд выбора скрыт: правка
		# не должна молча переселять пост из темы в общую ленту
		self._fallback_topic_id = draft.topic_id if community.forum else None
		self._build(topics, topics_error)

	# --- сборка ----------------------------------------------------------------

	def _build(self, topics: list[ForumTopicInfo], topics_error: str) -> None:
		"""Собирает форму и заполняет её текущим черновиком."""
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(_FORM_SPACING)
		self._build_topic_row(layout, topics, topics_error)
		# лицо публикации (ADR-0036): ряд виден только у группы, список
		# исполнителей читается у движка; пока не пришёл — лицо прежнее
		self._identity = IdentityChoice(self, layout, self._worker, lambda _id: False)
		self._identity.update_for(self._community, keep=self._draft.identity)
		self._build_kind_segments(layout)
		self._build_file_row(layout)
		self._build_poll_block(layout)
		# поле с оформлением (ADR-0033): правка не теряет разметку —
		# она показана стилями и уезжает сущностями
		self._post_text = RichPostEdit(self, height=_TEXT_HEIGHT)
		self._text = self._post_text.edit
		self._post_text.set_rich(self._draft.rich)
		layout.addWidget(self._post_text)
		self._counter = CharCounter(self, layout, self._text)
		# те же настройки превью, что в форме нового поста (ADR-0033, C3)
		self._preview = PreviewRow(self, layout)
		self._preview.set_preview(self._draft.preview)
		self._preview.changed.connect(self._refresh_preview)
		self._text.textChanged.connect(self._refresh_preview)
		self._build_markup_block(layout)
		# по макету кнопки формы стоят в ряду времени, справа
		self._when_row = WhenRow(
			self,
			layout,
			compact=True,
			trailing=self._build_buttons(),
			on_now_changed=lambda _now: self._refresh_markup(),
		)
		self._when_row.set_schedule_allowed(self._caps.userbot, _BOT_ONLY_HINT)
		self._when_row.set_when(self._draft.when)
		self._error = ErrorLabel(self)
		layout.addWidget(self._error)
		layout.addWidget(tinted(CaptionLabel(_RECIPIENT_NOTE, self), DIM_TEXT))
		self._apply_kind()

	def _build_poll_block(self, layout: QVBoxLayout) -> None:
		"""Поля опроса — тот же блок, что в форме нового поста (ADR-0033, C5).

		Опрос правится, **пока пост ждёт у нас**: отправленный опрос
		Telegram менять не даёт вовсе, и эта форма — последняя
		возможность передумать.
		"""
		self._poll = PollEditor(self)
		self._poll.set_poll(self._draft.poll)
		self._poll.changed.connect(self._refresh_markup)
		self._poll.hide()
		layout.addWidget(self._poll)

	def _build_markup_block(self, layout: QVBoxLayout) -> None:
		"""Блок кнопок под постом — тот же, что на «Публикации» (ADR-0031).

		Правила кнопок обеим формам нужны одни, поэтому редактор общий:
		разойтись им нельзя.
		"""
		self._markup_card = CollapsibleCard("Кнопки под постом", self)
		self._markup = MarkupEditor(self._markup_card)
		self._markup.set_markup(self._draft.markup)
		self._markup.set_markup_first(self._draft.markup_first)
		self._markup.changed.connect(self._refresh_markup)
		self._markup_card.body.addWidget(self._markup)
		layout.addWidget(self._markup_card)

	def _refresh_markup(self) -> None:
		"""Приводит блок кнопок и предел текста к состоянию формы.

		Состояние считает общая :func:`markup_state` — та же, что
		на «Новом посте» и у пакета. Пока правила писались в каждой
		форме отдельно, они разошлись: здесь не знали, что у альбома
		кнопок не бывает, и человек узнавал об этом только отказом
		при сохранении.
		"""
		markup = self._markup.markup()
		self._poll.set_anonymous_forced(
			poll_blocker(False, title=self._community.title, kind=self._community.kind)
		)
		state = markup_state(
			self._community,
			self._limits,
			media=() if self._kind is MediaKind.POLL else self._media.files(),
			scheduled=not self._when_row.is_now(),
			has_markup=markup is not None,
			markup_first=self._markup.markup_first(),
			over_bot_limit=over_bot_limit(self._media.files()),
			poll=self._kind is MediaKind.POLL,
		)
		self._markup.set_mode_available(state.mode_available)
		self._markup.set_blocked(state.blocked)
		self._markup.set_notice(state.notice)
		count = len(markup.buttons) if markup is not None else 0
		self._markup_card.set_summary(
			f"{count} {plural(count, 'кнопка', 'кнопки', 'кнопок')}" if count else "нет"
		)
		is_text = self._kind is MediaKind.NONE
		# предел зависит от маршрута: пост с кнопками отправит бот,
		# а у него пределы базовые (ADR-0031)
		self._counter.set_limit(
			state.limits.text if is_text else state.limits.caption, with_media=not is_text
		)

	def _build_topic_row(
		self, layout: QVBoxLayout, topics: list[ForumTopicInfo], topics_error: str
	) -> None:
		"""Ряд выбора темы форума с предвыбором текущей (ADR-0021/0022)."""
		row = topic_row(self, layout)
		self._topic_box, self._topic_combo, self._topic_hint = row.box, row.combo, row.hint
		if not self._community.forum or not self._caps.userbot:
			self._topic_box.setVisible(False)
			return
		if topics_error:
			# ряд скрыт, но тема поста сохранится прежней (_fallback_topic_id)
			self._topic_box.setVisible(False)
			self._topic_hint.setText(topics_error)
			return
		shown, closed = visible_topics(topics, self._community.default_status)
		self._topic_combo.set_items(shown, label=topic_label, key=lambda topic: topic.id)
		if closed:
			self._topic_hint.setText(closed_topics_hint(closed))
		if self._draft.topic_id is not None and not self._topic_combo.select(
			lambda topic: topic.id == self._draft.topic_id
		):
			# тему удалили или она закрылась для участника: молчать нельзя —
			# сохранение переставит пост в общую ленту
			self._topic_hint.setText("Прежняя тема недоступна — пост уйдёт в общую ленту.")

	def _build_kind_segments(self, layout: QVBoxLayout) -> None:
		"""Сегментный переключатель типа контента (как на «Публикации»).

		В карточке он своей ширины и прижат влево (макет), а не на всю
		строку, как на странице.
		"""
		self._segments = kind_segments(
			self, layout, self._on_kind_changed, current=self._kind.value
		)
		self._segments.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

	def _build_file_row(self, layout: QVBoxLayout) -> None:
		"""Список файлов элемента: замена, удаление, добавление (альбом — C4)."""
		self._media = MediaPicker(self, on_pick=self._pick_file)
		self._media.set_files(self._draft.media)
		self._media.changed.connect(self._on_media_changed)
		layout.addWidget(self._media)

	def _on_media_changed(self) -> None:
		"""Состав файлов изменился: правила кнопок и пределы текста."""
		if not self._media.files() and self._kind is not MediaKind.NONE:
			# файлы убрали все — пост стал текстовым
			self._segments.setCurrentItem(MediaKind.NONE.value)
			self._kind = MediaKind.NONE
			self._apply_kind()
			return
		self._refresh_markup()

	def _build_buttons(self) -> list[QWidget]:
		"""Кнопки формы (встают в ряд времени): сохранение возвращает пост в работу."""
		cancel = PushButton("Отмена", self)
		cancel.setToolTip("Закрыть форму, ничего не меняя")
		cancel.clicked.connect(self._on_close)
		# коротко: полная формулировка «Сохранить и отправить» не влезает
		# в ряд при ширине окна 1160, а подсказка договаривает остальное
		self._save_button = PrimaryPushButton("Сохранить", self)
		self._save_button.setToolTip(
			"Пост вернётся в очередь: «сейчас» — в отправку, отложенный — ждать слота."
		)
		self._save_button.clicked.connect(self._on_save)
		return [cancel, self._save_button]

	# --- поведение формы -------------------------------------------------------

	def _refresh_preview(self) -> None:
		"""Приводит ряд превью к тексту и типу поста."""
		self._preview.refresh(self._post_text.rich(), self._kind is not MediaKind.NONE)

	def _on_kind_changed(self, kind_key: str) -> None:
		"""Меняет состав формы под выбранный тип контента."""
		self._kind = MediaKind(kind_key)
		self._apply_kind()

	def _apply_kind(self) -> None:
		"""Показывает ряды вложения по типу и правит подсказку с пределом."""
		is_text = self._kind is MediaKind.NONE
		is_poll = self._kind is MediaKind.POLL
		self._media.set_kind(MediaKind.NONE if is_poll else self._kind)
		self._poll.setVisible(is_poll)
		# рядом превью управляет он сам (у поста с вложением его нет),
		# здесь — только то, что принадлежит тексту
		for widget in (self._post_text, self._counter.label):
			widget.setVisible(not is_poll)
		self._refresh_preview()
		self._text.setPlaceholderText(caption_placeholder(is_text))
		# подпись к файлу вчетверо короче поста без вложения; предел
		# и доступность кнопок считает общий проход
		self._refresh_markup()

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
		files = self._media.files()
		self._open_file_dialog(str(Path(files[0].path).parent) if files else "")

	def _open_file_dialog(self, start: str | VideoDirs) -> None:
		"""Открывает диалог вложения; ``start`` — папка или VideoDirs."""
		start_dir = start.processed if isinstance(start, VideoDirs) else start
		self._media.open_dialog(start_dir)

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
			self._on_save_done,
			self._on_save_failed,
		)

	def _on_save_done(self) -> None:
		"""Правка принята: обновляем список и закрываем форму."""
		self._on_saved()
		self._on_close()

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
		files = self._media.files()
		if self._kind is MediaKind.POLL:
			return PostDraft(
				community_id=self._draft.community_id,
				poll=self._poll.poll(),
				when=self._when_row.when(),
				topic_id=self._selected_topic_id(),
				markup=self._markup.markup(),
				markup_first=self._markup.markup_first(),
				identity=self._identity.identity(),
			)
		is_text = self._kind is MediaKind.NONE
		rich = trimmed(self._post_text.rich())
		if not is_text and not files:
			raise ValueError(
				f"Выбран тип «{kind_label(self._kind)}», а файлы не выбраны — "
				"выберите файл (или несколько для альбома) либо переключитесь "
				"на «Текст»."
			)
		return PostDraft(
			community_id=self._draft.community_id,
			text=rich.text,
			entities=rich.entities,
			preview=self._preview.preview() if is_text else LinkPreview(),
			media=() if is_text else files,
			when=self._when_row.when(),
			topic_id=self._selected_topic_id(),
			markup=self._markup.markup(),
			markup_first=self._markup.markup_first(),
			identity=self._identity.identity(),
		)

	def _selected_topic_id(self) -> int | None:
		"""Тема из видимого ряда; ряд скрыт — прежняя тема поста."""
		if not self._topic_box.isVisibleTo(self):
			return self._fallback_topic_id
		topic = self._topic_combo.selected()
		return topic.id if topic is not None else None


def mount_queue_item_editor(
	worker: EngineWorker,
	page: QWidget,
	item_id: int,
	body: QVBoxLayout,
	collapse: Callable[[], None],
	on_saved: Callable[[], None],
) -> None:
	"""Наполняет тело раскрытой карточки формой правки элемента.

	Цепочка чтений: черновик → сообщество → пределы длины текста → темы
	форума (последние — только у форума с userbot-публикатором). Пока
	идёт чтение, в теле стоит заглушка: раскрытая пустота выглядит
	поломкой. Темы не прочитались — форма всё равно откроется, но ряд
	выбора темы будет скрыт, а пост сохранит свою прежнюю тему.

	Args:
		worker: мост к движку.
		page: страница-владелец (для колбэков моста).
		item_id: элемент очереди.
		body: компоновка тела карточки.
		collapse: свернуть карточку (после сохранения или отмены).
		on_saved: вызвать после сохранения (обновить панель).
	"""
	waiting = CaptionLabel("Читаю пост…", page)
	body.addWidget(waiting)

	def fail(message: str) -> None:
		"""Данные не прочитались: честная причина вместо пустого тела."""
		clear_layout(body)
		body.addWidget(CaptionLabel(f"Не удалось открыть правку: {message}", page))

	def show(
		draft: PostDraft,
		community: CommunityDto,
		limits: TextLimits,
		topics: list[ForumTopicInfo],
		topics_error: str = "",
	) -> None:
		clear_layout(body)
		body.addWidget(
			QueueItemEditor(
				worker,
				page,
				item_id,
				draft,
				community,
				limits,
				topics,
				topics_error,
				on_saved,
				collapse,
			)
		)

	def with_limits(draft: PostDraft, community: CommunityDto, limits: TextLimits) -> None:
		caps = community.capabilities
		if not community.forum or not caps.userbot:
			show(draft, community, limits, [])
			return
		run_in_engine(
			worker,
			worker.engine.posts.list_topics(community.id),
			page,
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
		# пределы — потолок Telegram (ADR-0037), у движка их спрашивать незачем
		with_limits(draft, community, PREMIUM_LIMITS)

	def with_draft(draft: PostDraft) -> None:
		run_in_engine(
			worker,
			worker.engine.communities.get_community(draft.community_id),
			page,
			lambda community: with_community(draft, community),
			fail,
		)

	run_in_engine(worker, worker.engine.publish_queue.get_draft(item_id), page, with_draft, fail)
