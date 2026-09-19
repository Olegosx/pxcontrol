"""Экран «Новый пост»: единая точка создания постов всех типов.

Первая стадия раздела «Публикация» (ADR-0032) — форма поста. Тип
контента выбирается сегментами (текст/фото/видео/аудио/файл). Отправка
идёт через очередь движка (ADR-0016): «Отправить» ставит пост в хвост
и сразу освобождает форму под следующий; ближайшие карточки очереди
видны под формой, всё целиком — на соседнем экране «Очередь».

Панель очереди здесь — **зритель**: завершённые задания снимает
и плашку об исходе показывает наблюдатель очереди при главном окне
(:mod:`pxcontrol.ui.queue_watcher`). Иначе владение очередью зависело
бы от того, открыт ли этот экран, — а стадии разъехались по экранам,
и открыт он далеко не всегда.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	FluentIcon,
	PrimaryPushButton,
	PushButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.captions import (
	TemplateDto,
	title_from_filename,
)
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.posts import (
	MediaFile,
	PostDraft,
	TextLimits,
)
from pxcontrol.engine.services.publish_queue import (
	EDITABLE_STATUSES,
	QueueItemDto,
)
from pxcontrol.engine.services.publish_route import (
	poll_blocker,
)
from pxcontrol.engine.services.settings import (
	PUBLISH_TIMES,
)
from pxcontrol.engine.services.video import VideoDirs
from pxcontrol.engine.telegram.rich_text import trimmed
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	CommunityKind,
	LinkPreview,
	MediaKind,
	UserbotRole,
	limit_mb,
	text_length_limit,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.captions import CaptionDialog, FieldsDialog
from pxcontrol.ui.pages.common import (
	CharCounter,
	CollapsibleCard,
	WhenRow,
	caption_placeholder,
	error_reporter,
	exec_dialog,
	kind_label,
	kind_segments,
	noop,
	plural,
)
from pxcontrol.ui.pages.markup_editor import MarkupEditor, MarkupState, markup_state
from pxcontrol.ui.pages.media_picker import MediaPicker, over_bot_limit
from pxcontrol.ui.pages.poll_editor import PollEditor
from pxcontrol.ui.pages.post_target import CommunityChoice, TopicChoice
from pxcontrol.ui.pages.preview_row import PreviewRow
from pxcontrol.ui.pages.publish_queue_edit import mount_queue_item_editor
from pxcontrol.ui.pages.publish_queue_view import (
	queue_leading,
	queue_subtitle,
)
from pxcontrol.ui.pages.publish_stages import PublishStage
from pxcontrol.ui.pages.queue_panel import QueuePanel
from pxcontrol.ui.pages.rich_edit import RichPostEdit
from pxcontrol.ui.pages.stage_page import StagePage
from pxcontrol.ui.queue_watcher import QueueWatcher

#: Пределы, пока сообщество не ответило: базовые — они не обещают лишнего.
_BASE_LIMITS = TextLimits(
	text=text_length_limit(premium=False, with_media=False),
	caption=text_length_limit(premium=False, with_media=True),
)

#: Сколько карточек очереди показывать на странице (хвост ждущих —
#: в сводке числом; всё целиком — кнопка «Вся очередь…», ADR-0016).
_QUEUE_MAX_CARDS = 20


def _actor_note(community: CommunityDto) -> str:
	"""Приписка «от чьего имени» для групп (ADR-0022): в группе пост
	выходит от имени аккаунта, участнику действует медленный режим."""
	if community.kind is not CommunityKind.GROUP or not community.default_account_label:
		return ""
	actor = community.default_account_label
	if community.default_role is UserbotRole.MEMBER:
		return f" Пост уйдёт от имени {actor} (участник) — действует медленный режим группы."
	return f" Пост уйдёт от имени {actor} (админ)."


class PublishPage(StagePage):
	"""Создание публикации: тип контента, сообщество, текст, время, отправка.

	Экран стадии, как и соседние (ADR-0032): заголовок, подсказка
	и правило «экран видно — экран работает» достаются от общей рамки.
	Пока эта страница жила отдельно, правило её не касалось — и панель
	очереди под формой опрашивала движок круглосуточно, даже когда
	экран не виден, вторым опросчиком поверх наблюдателя главного окна.
	"""

	#: «Вся очередь…» — соседний экран раздела «Очередь».
	queue_requested = Signal()

	def __init__(
		self, worker: EngineWorker, watcher: QueueWatcher, parent: QWidget | None = None
	) -> None:
		"""``watcher`` — наблюдатель очереди отправки при главном окне (ADR-0034)."""
		super().__init__(PublishStage.NEW_POST, parent)
		self._worker = worker
		self._watcher = watcher
		self._show_error = error_reporter(self)
		# канал прошлой публикации: предвыбор после загрузки списка
		self._kind = MediaKind.NONE
		# пределы длины текста выбранного канала (None — канал не выбран
		# или ответ движка ещё не пришёл: счётчик покажет базовый предел)
		self._limits: TextLimits | None = None
		# аватары сообществ из кэша статистики — для шапок карточек очереди
		self._avatars: dict[int, str | None] = {}
		self._build()
		self._community.restore_last()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = self.body_layout()
		self._build_kind_segments(layout)
		self._community = CommunityChoice(self, self._worker)
		self._community.chosen.connect(self._on_community_changed)
		layout.addWidget(self._community)
		self._caps_hint = CaptionLabel("", self)
		layout.addWidget(self._caps_hint)
		self._topics = TopicChoice(
			self, layout, self._worker, self._community, on_failed=self._on_topics_failed
		)
		# поле с оформлением (ADR-0033): человек выделяет текст и жмёт
		# стиль, разметка живёт сущностями рядом с видимым текстом
		self._post_text = RichPostEdit(self)
		self._text = self._post_text.edit
		self._text.setPlaceholderText(caption_placeholder(True))
		self._text.setMinimumHeight(120)
		layout.addWidget(self._post_text)
		self._counter = CharCounter(self, layout, self._text)
		# превью ссылки (ADR-0033, C3): только у поста без вложения
		self._preview = PreviewRow(self, layout)
		self._preview.changed.connect(self._refresh_preview)
		self._text.textChanged.connect(self._refresh_preview)
		self._build_caption_tools(layout)
		self._build_file_row(layout)
		self._build_poll_block(layout)
		self._build_markup_block(layout)
		self._when_row = WhenRow(self, layout, on_now_changed=self._on_when_changed)
		self._build_send_row(layout)
		layout.addStretch()
		# после сборки всех полей — сегмент по умолчанию (сигнал трогает форму)
		self._segments.setCurrentItem(MediaKind.NONE.value)

	def _build_kind_segments(self, layout: QVBoxLayout) -> None:
		"""Сегментный переключатель типа контента."""
		self._segments = kind_segments(self, layout, self._on_kind_changed)

	def _build_caption_tools(self, layout: QVBoxLayout) -> None:
		"""Кнопки шаблонизатора подписи."""
		self._caption_tools = QWidget(self)
		row = QHBoxLayout(self._caption_tools)
		row.setContentsMargins(0, 0, 0, 0)
		compose = PushButton("Собрать подпись…", self)
		compose.clicked.connect(self._on_compose_caption)
		row.addWidget(compose)
		setup = PushButton("Поля подписи…", self)
		setup.clicked.connect(self._on_setup_fields)
		row.addWidget(setup)
		row.addStretch()
		layout.addWidget(self._caption_tools)

	def _build_file_row(self, layout: QVBoxLayout) -> None:
		"""Список файлов поста: один — вложение, несколько — альбом (C4)."""
		self._media = MediaPicker(self, on_pick=self._pick_file)
		self._media.changed.connect(self._on_media_changed)
		self._media.hide()
		layout.addWidget(self._media)

	def _build_poll_block(self, layout: QVBoxLayout) -> None:
		"""Поля опроса: вопрос, варианты, правила голосования (C5)."""
		self._poll = PollEditor(self)
		self._poll.changed.connect(self._refresh_markup)
		self._poll.hide()
		layout.addWidget(self._poll)

	def _build_markup_block(self, layout: QVBoxLayout) -> None:
		"""Блок кнопок под постом (свёрнут: кнопки нужны не каждому посту).

		Правила кнопок приходят из движка (ADR-0031): что мешает их
		поставить и что меняется в посте, когда они есть, — интерфейс
		только показывает, а не решает сам.
		"""
		self._markup_card = CollapsibleCard("Кнопки под постом", self)
		self._markup = MarkupEditor(self._markup_card)
		self._markup.changed.connect(self._refresh_markup)
		self._markup_card.body.addWidget(self._markup)
		layout.addWidget(self._markup_card)

	def _on_when_changed(self, _now: bool) -> None:
		"""Смена «сейчас ↔ отложенно»: у отложенных кнопки пока недоступны."""
		self._refresh_markup()

	def _on_media_changed(self) -> None:
		"""Состав файлов изменился: правила кнопок, превью и маршрут."""
		self._refresh_markup()
		self._refresh_preview()

	def _markup_state(self) -> MarkupState | None:
		"""Состояние кнопок и пределов для нынешнего черновика.

		None — сообщество ещё не выбрано: правила зависят от него,
		и выдумывать их форме нечем.
		"""
		community = self._community_or_none()
		if community is None:
			return None
		return markup_state(
			community,
			self._limits or _BASE_LIMITS,
			# у опроса файлов не бывает — правила альбома к нему не идут
			media=() if self._kind is MediaKind.POLL else self._media.files(),
			scheduled=not self._when_row.is_now(),
			has_markup=self._markup.markup() is not None,
			markup_first=self._markup.markup_first(),
			over_bot_limit=over_bot_limit(self._media.files()),
			poll=self._kind is MediaKind.POLL,
		)

	def _refresh_markup(self) -> None:
		"""Приводит блок кнопок к текущему состоянию формы.

		Одним заходом: можно ли кнопки, что изменится в посте из-за них
		и какой предел показывать счётчику. Само состояние считает общая
		:func:`markup_state` — правила у трёх форм поста одни, и писать
		их здесь своими словами значит однажды с ними разойтись.
		"""
		community = self._community_or_none()
		if community is None:
			self._markup.set_blocked("Сначала выберите сообщество — от него зависят кнопки.")
			self._markup.set_notice("")
			return
		# правило опроса тоже от сообщества: в канале он только анонимный
		self._poll.set_anonymous_forced(
			poll_blocker(False, title=community.title, kind=community.kind)
		)
		state = self._markup_state()
		if state is None:  # pragma: no cover — сообщество проверено выше
			return
		self._markup.set_mode_available(state.mode_available)
		self._markup.set_blocked(state.blocked)
		self._markup.set_notice(state.notice)
		markup = self._markup.markup()
		count = len(markup.buttons) if markup is not None else 0
		self._markup_card.set_summary(
			f"{count} {plural(count, 'кнопка', 'кнопки', 'кнопок')}" if count else "нет"
		)
		self._apply_text_limit()

	def _build_send_row(self, layout: QVBoxLayout) -> None:
		"""Кнопки отправки (одиночной и пакетной) и панель очереди под ними."""
		row = QHBoxLayout()
		self._send_button = PrimaryPushButton(FluentIcon.SEND, "Отправить", self)
		self._send_button.clicked.connect(self._on_send)
		row.addWidget(self._send_button)
		view_button = PushButton("Вся очередь…", self)
		view_button.setToolTip(
			"Экран «Очередь»: все элементы очереди отправки с сортировкой "
			f"и фильтрами (здесь — ближайшие {_QUEUE_MAX_CARDS})"
		)
		view_button.clicked.connect(self._on_queue_view)
		row.addWidget(view_button)
		row.addStretch()
		layout.addLayout(row)
		self._queue_summary = CaptionLabel("", self)
		self._queue_summary.hide()
		layout.addWidget(self._queue_summary)
		queue_box = QVBoxLayout()
		queue_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(queue_box)
		self._queue = QueuePanel(
			self,
			queue_box,
			watcher=self._watcher,
			subtitle=queue_subtitle,
			on_refreshed=self._update_queue_summary,
			# длинный хвост ждущих слота (ADR-0016) не раздувает форму;
			# всё целиком — на экране «Очередь»
			max_cards=_QUEUE_MAX_CARDS,
			# правка — прямо в карточке (ADR-0016, п. 7): раскрывается
			# кликом, как параметры файла на «Видео»
			editable=lambda item: item.status in EDITABLE_STATUSES,
			fill_body=self._fill_editor,
			leading=lambda item, parent: queue_leading(
				item, parent, self._avatars.get(item.community_id)
			),
			active=False,  # присоединится, когда экран станет виден
		)

	def _on_queue_view(self) -> None:
		"""Полный просмотр очереди — соседний экран раздела."""
		self.queue_requested.emit()

	def _fill_editor(self, item_id: int, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет раскрытую карточку очереди формой правки (ADR-0016)."""
		mount_queue_item_editor(self._worker, self, item_id, body, collapse, self._queue.poll)

	# --- поведение -----------------------------------------------------------------

	def set_active(self, active: bool) -> None:
		"""Экран показан или скрыт: список каналов и карточки очереди.

		Панель очереди под формой — зритель наблюдателя главного окна
		(ADR-0034): скрытая она карточки не обновляет, показанная берёт
		снимок из кэша.
		"""
		self._queue.set_active(active)
		if active:
			self._reload_communities()

	def prefill_media(self, kind: MediaKind, path: str, community_id: int | None = None) -> None:
		"""Подставляет вложение (переход с других страниц, например «Видео»).

		``community_id`` — предвыбор канала (например, выбранного на «Видео»);
		применяется тем же механизмом, что и канал прошлой публикации.
		"""
		self._segments.setCurrentItem(kind.value)
		self._on_kind_changed(kind.value)
		self._media.set_files((MediaFile(path, kind),))
		if community_id is not None:
			self.select_community(community_id)

	def _apply_avatars(self, stats: list[CommunityStatsDto]) -> None:
		"""Раскладывает аватары сообществ и перерисовывает шапки карточек."""
		self._avatars = {item.community_id: item.avatar_path for item in stats}
		self._queue.refresh_leading()

	def _reload_communities(self) -> None:
		"""Просит свежий список сообществ и аватары для шапок очереди."""
		self._community.reload(self._show_error)
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._apply_avatars,
			# аватар — украшение шапки: без него карточка рисует букву
			noop,
		)

	def select_community(self, community_id: int | None) -> None:
		"""Предвыбирает сообщество (переход с дашборда, прошлая публикация)."""
		self._community.want(community_id)

	def _community_or_none(self) -> CommunityDto | None:
		"""Выбранное сообщество без показа ошибок (для адаптации формы)."""
		return self._community.current()

	def _on_community_changed(self) -> None:
		"""Адаптирует форму под возможности и времена выбранного сообщества."""
		community = self._community_or_none()
		if community is None:
			self._caps_hint.setText("")
			self._when_row.set_schedule_allowed(True)
			self._when_row.set_times([])
			self._limits = None
			self._apply_text_limit()
			return
		run_in_engine(
			self._worker,
			self._worker.engine.posts.text_limits(community.id),
			self,
			partial(self._apply_limits, community.id),
			noop,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, community.id),
			self,
			partial(self._apply_times, community.id),
			noop,
		)
		caps = community.capabilities
		self._topics.update_for(community)
		if caps.userbot:
			# лимит зависит от Premium userbot — узнаём у движка
			self._caps_hint.setText(
				"Публикация через userbot: все типы контента, «сейчас» и отложенные."
				+ _actor_note(community)
			)
			run_in_engine(
				self._worker,
				self._worker.engine.posts.userbot_limit_gb(community.id),
				self,
				partial(self._show_userbot_limit, community.id),
				noop,
			)
			self._when_row.set_schedule_allowed(True)
		elif caps.bot:
			self._caps_hint.setText(
				f"Публикация через бота: файлы до {limit_mb(BOT_MAX_FILE_BYTES)} "
				"МБ, только «сейчас» (для отложенных нужен userbot-админ)."
			)
			self._when_row.set_schedule_allowed(
				False, "Отложенные требуют userbot-админа в сообществе"
			)
		else:
			self._caps_hint.setText(
				"⚠ Нет способа публикации — проверьте доступы на странице сообщества."
			)
			self._when_row.set_schedule_allowed(False, "Нет способа публикации")
		# сообщество сменилось — сменились и правила кнопок
		self._refresh_markup()

	def _apply_limits(self, community_id: int, limits: TextLimits) -> None:
		"""Запоминает пределы длины сообщества, если оно всё ещё выбрано."""
		if self._is_stale(community_id):
			return
		self._limits = limits
		self._apply_text_limit()

	def _apply_text_limit(self) -> None:
		"""Ставит счётчику предел по типу поста и выбранному каналу.

		Канал ещё не выбран (или пределы не приехали) — показываем
		базовый предел Telegram: он не обещает лишнего.
		"""
		with_media = self._kind is not MediaKind.NONE
		state = self._markup_state()
		if self._limits is None or state is None:
			self._counter.set_limit(text_length_limit(premium=False, with_media=with_media))
			return
		# пост с кнопками уходит ботом — у него пределы базовые (ADR-0031)
		self._counter.set_limit(state.limits.caption if with_media else state.limits.text)

	def _on_topics_failed(self, message: str) -> None:
		"""Темы не прочитались — пост уйдёт в общую ленту, честно предупредив."""
		self._caps_hint.setText(
			f"{self._caps_hint.text()} Темы форума не загрузились ({message}) — "
			"пост уйдёт в общую ленту."
		)

	def _is_stale(self, community_id: int) -> bool:
		"""Пришёл ли ответ движка для уже переключённого сообщества."""
		return self._community.is_stale(community_id)

	def _apply_times(self, community_id: int, times: list[str]) -> None:
		"""Подставляет времена сообщества, если оно всё ещё выбрано."""
		if not self._is_stale(community_id):
			self._when_row.set_times(times)

	def _show_userbot_limit(self, community_id: int, limit_gb: int) -> None:
		"""Дописывает лимит файла в подсказку (2 ГБ; 4 — с Premium)."""
		if self._is_stale(community_id):
			return
		premium = " (Premium)" if limit_gb >= 4 else ""
		community = self._community_or_none()
		self._caps_hint.setText(
			"Публикация через userbot: все типы контента, файлы "
			f"до {limit_gb} ГБ{premium}, «сейчас» и отложенные."
			+ (_actor_note(community) if community is not None else "")
		)

	def _refresh_preview(self) -> None:
		"""Приводит ряд превью к тексту и типу поста.

		У опроса превью ссылки не бывает — показывать ему негде,
		и ряд прячется так же, как у поста с вложением.
		"""
		self._preview.refresh(self._post_text.rich(), self._kind is not MediaKind.NONE)

	def _on_kind_changed(self, kind_key: str) -> None:
		"""Меняет состав формы под выбранный тип контента.

		У опроса своих полей нет ни у текста, ни у файла и наоборот:
		форма показывает то, из чего состоит выбранный пост, а не всё
		сразу с оговорками (ADR-0033, C5).
		"""
		self._kind = MediaKind(kind_key)
		is_text = self._kind is MediaKind.NONE
		is_poll = self._kind is MediaKind.POLL
		self._media.set_kind(MediaKind.NONE if is_poll else self._kind)
		self._poll.setVisible(is_poll)
		for widget in (self._post_text, self._counter.label, self._caption_tools):
			widget.setVisible(not is_poll)
		self._refresh_preview()
		self._text.setPlaceholderText(caption_placeholder(is_text))
		# подпись к файлу вчетверо короче поста без вложения; маршрут
		# и доступность кнопок тоже зависят от типа и файла
		self._refresh_markup()

	def _pick_file(self) -> None:
		"""Диалог выбора файлов с фильтром по текущему типу контента.

		Для видео диалог открывается в папке результатов обработки
		выбранного канала (подпапка его пресета по умолчанию); для
		остальных типов стартовая папка — на усмотрение Qt (позже).
		"""
		if self._kind is MediaKind.VIDEO:
			community = self._community_or_none()
			if community is not None:
				run_in_engine(
					self._worker,
					self._worker.engine.video.processed_dir_for_community(community.id),
					self,
					self._open_file_dialog,
					self._show_error,
				)
			else:
				run_in_engine(
					self._worker,
					self._worker.engine.video.dirs_for(""),
					self,
					self._open_file_dialog,
					self._show_error,
				)
			return
		self._open_file_dialog("")

	def _open_file_dialog(self, start: str | VideoDirs) -> None:
		"""Открывает диалог файлов; ``start`` — папка или VideoDirs."""
		start_dir = start.processed if isinstance(start, VideoDirs) else start
		self._media.open_dialog(start_dir)

	# --- подпись по шаблону -----------------------------------------------------

	def _current_community(self) -> CommunityDto | None:
		"""Выбранное сообщество или None (с показом подсказки)."""
		community = self._community_or_none()
		if community is None:
			self._show_error("Сначала подключите и выберите сообщество.")
		return community

	def _on_setup_fields(self) -> None:
		"""Открывает настройку полей и шаблонов подписи сообщества."""
		community = self._current_community()
		if community is not None:
			exec_dialog(FieldsDialog(self._worker, community.id, community.title, self.window()))

	def _on_compose_caption(self) -> None:
		"""Загружает шаблоны сообщества и открывает диалог сборки."""
		community = self._current_community()
		if community is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(community.id),
			self,
			self._open_caption_dialog,
			self._show_error,
		)

	def _open_caption_dialog(self, templates: list[TemplateDto]) -> None:
		"""Собирает подпись по шаблону и вставляет её в поле текста."""
		# пустой (например, только что созданный) шаблон не должен
		# блокировать сборку по остальным — в диалог идут пригодные
		usable = [template for template in templates if template.fields]
		if not usable:
			self._show_error("Сначала настройте поля и шаблон — кнопка «Поля подписи…».")
			return
		files = self._media.files()
		media = files[0].path if files else ""
		title = ""
		if self._kind is not MediaKind.NONE and media:
			title = title_from_filename(media)
		dialog = CaptionDialog(usable, title, self.window())
		if not exec_dialog(dialog):
			return
		self._post_text.set_rich(dialog.caption())
		self._record_template_usage(dialog.template_id(), dialog.used_values())
		self._suggest_rename(templates, dialog, media)

	def _record_template_usage(self, template_id: int, used_values: dict[int, list[str]]) -> None:
		"""Запоминает использованные значения шаблона (для предвыбора)."""
		run_in_engine(
			self._worker,
			self._worker.engine.captions.record_usage(template_id, used_values),
			self,
			noop,
			self._show_error,
		)

	def _suggest_rename(
		self, templates: list[TemplateDto], dialog: CaptionDialog, media: str
	) -> None:
		"""Предлагает имя файла по шаблону имени (если он задан)."""
		template = next(t for t in templates if t.id == dialog.template_id())
		community = self._current_community()
		if not (template.filename_pattern and media and community):
			return
		if self._kind is MediaKind.NONE:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.captions.render_filename(
				template.id,
				community.id,
				dialog.title(),
				dialog.used_values(),
				media,
			),
			self,
			self._show_rename_suggestion,
			self._show_error,
		)

	def _show_rename_suggestion(self, filename: str) -> None:
		"""Показывает строку переименования с вычисленным именем."""
		self._media.suggest_rename(filename)

	# --- отправка через очередь ---------------------------------------------------

	def _on_send(self) -> None:
		"""Ставит черновик в очередь отправки; форма сразу свободна."""
		community = self._current_community()
		if community is None:
			return
		try:
			draft = self._draft(community.id)
		except ValueError as exc:  # поля формы не согласованы (файл, время)
			self._show_error(str(exc))
			return
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.enqueue(draft),
			self,
			self._on_enqueued,
			self._show_error,
		)
		self._community.remember_last()

	def _draft(self, community_id: int) -> PostDraft:
		"""Собирает черновик публикации из полей формы.

		Raises:
			ValueError: Поля формы не согласованы: выбран тип с вложением,
				а файл не указан, либо время публикации не «ЧЧ:ММ».
		"""
		files = self._media.files()
		if self._kind is MediaKind.POLL:
			return PostDraft(
				community_id=community_id,
				poll=self._poll.poll(),
				when=self._when_row.when(),
				topic_id=self._topics.topic_id(),
				markup=self._markup.markup(),
				markup_first=self._markup.markup_first(),
			)
		is_text = self._kind is MediaKind.NONE
		# видимый текст и его разметка — одной точкой, чтобы они
		# не разъехались между проверкой и сборкой черновика
		rich = trimmed(self._post_text.rich())
		if not is_text and not files:
			raise ValueError(
				f"Выбран тип «{kind_label(self._kind)}», а файлы не выбраны — "
				"выберите файл (или несколько для альбома) либо переключитесь "
				"на «Текст»."
			)
		return PostDraft(
			community_id=community_id,
			text=rich.text,
			entities=rich.entities,
			media=() if is_text else files,
			when=self._when_row.when(),
			topic_id=self._topics.topic_id(),
			preview=self._preview.preview() if is_text else LinkPreview(),
			markup=self._markup.markup(),
			markup_first=self._markup.markup_first(),
		)

	def _on_enqueued(self, _item_id: object = None) -> None:
		"""Черновик принят в очередь — чистим форму под следующий пост.

		Чистится содержимое: текст, файлы, опрос. Кнопки и настройки
		превью **остаются намеренно** — серию однотипных постов с одной
		клавиатурой набирают подряд, и стирать её каждый раз значило бы
		заставлять собирать заново. Сохранённые кнопки видны сводкой
		на свёрнутой карточке блока, так что они не уходят молча.
		"""
		self._post_text.clear()
		self._media.clear()
		self._poll.clear()
		self._queue.poll()  # панель очереди обновляется сразу, не по таймеру

	# --- панель очереди -------------------------------------------------------------

	def _update_queue_summary(self, items: list[QueueItemDto]) -> None:
		"""Сводка над карточками: отправка, очередь, ждущие слота, ошибки."""
		if not items:
			self._queue_summary.hide()
			return
		waiting = sum(1 for item in items if item.status is JobStatus.WAITING)
		pending = sum(1 for item in items if item.status in (JobStatus.PENDING, JobStatus.RUNNING))
		errors = sum(1 for item in items if item.status is JobStatus.ERROR)
		parts = []
		if pending:
			parts.append(f"к отправке {pending}")
		if waiting:
			parts.append(f"ждут слота отложек {waiting}")
		if errors:
			parts.append(f"ошибок {errors}")
		text = "Очередь отправки: " + ", ".join(parts)
		if len(items) > _QUEUE_MAX_CARDS:
			text += f" · показаны ближайшие {_QUEUE_MAX_CARDS}"
		self._queue_summary.setText(text)
		self._queue_summary.show()
