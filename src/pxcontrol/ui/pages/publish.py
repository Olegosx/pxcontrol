"""Страница «Публикация»: единая точка создания постов всех типов.

Тип контента выбирается сегментами (текст/фото/видео/аудио/файл).
Отправка идёт через очередь движка (ADR-0016): «Отправить» ставит пост
в хвост и сразу освобождает форму под следующий; очередь видна на
странице, каждый элемент можно отменить.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SegmentedWidget,
	SubtitleLabel,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.captions import (
	CaptionLine,
	TemplateDto,
	TitleParseRules,
	title_from_filename,
)
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.posts import (
	PostDraft,
	PublishCapabilities,
	TextLimits,
	publish_capabilities,
)
from pxcontrol.engine.services.publish_queue import (
	EDITABLE_STATUSES,
	QueueItemDto,
)
from pxcontrol.engine.services.settings import (
	PUBLISH_LAST_COMMUNITY_ID,
	PUBLISH_TIMES,
	TITLE_PARSE_RULES,
)
from pxcontrol.engine.services.video import VideoDirs, VideoFile
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	CommunityKind,
	ForumTopicInfo,
	MediaKind,
	UserbotRole,
	limit_mb,
	text_length_limit,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.captions import CaptionDialog, FieldsDialog
from pxcontrol.ui.pages.common import (
	CONTENT_KINDS,
	CharCounter,
	DtoComboBox,
	QueuePanel,
	WhenRow,
	community_combo_label,
	error_reporter,
	exec_dialog,
	kind_file_filter,
	kind_label,
	noop,
	page_layout,
	pick_dir,
	pick_file,
	show_warning,
	topic_label,
	visible_topics,
)
from pxcontrol.ui.pages.publish_batch import PublishBatchDialog
from pxcontrol.ui.pages.publish_queue_edit import mount_queue_item_editor
from pxcontrol.ui.pages.publish_queue_view import (
	QueueViewDialog,
	queue_leading,
	queue_subtitle,
)

logger = logging.getLogger(__name__)

#: Сколько карточек очереди показывать на странице (хвост ждущих —
#: в сводке числом; всё целиком — кнопка «Вся очередь…», ADR-0016).
_QUEUE_MAX_CARDS = 20


@dataclass
class _BatchSetup:
	"""Собираемые данные пакета отправки (ADR-0015).

	Заполняется по шагам цепочки колбэков (папка → сканирование →
	общий шаблон подписи → времена канала → лимит файла), чтобы
	не таскать длинный список аргументов через каждую функцию.
	"""

	community: CommunityDto
	root: str
	files: list[VideoFile] = field(default_factory=list)
	caption_lines: list[CaptionLine] | None = None
	filename_template_id: int | None = None
	used_values: dict[int, list[str]] = field(default_factory=dict)
	times: list[str] = field(default_factory=list)
	busy: list[datetime] = field(default_factory=list)  # отложки канала (UTC)
	title_rules: TitleParseRules = field(default_factory=TitleParseRules)


def _actor_note(community: CommunityDto) -> str:
	"""Приписка «от чьего имени» для групп (ADR-0022): в группе пост
	выходит от имени аккаунта, участнику действует медленный режим."""
	if community.kind is not CommunityKind.GROUP or not community.default_account_label:
		return ""
	actor = community.default_account_label
	if community.default_role is UserbotRole.MEMBER:
		return f" Пост уйдёт от имени {actor} (участник) — действует медленный режим группы."
	return f" Пост уйдёт от имени {actor} (админ)."


def _community_caps(community: CommunityDto) -> PublishCapabilities:
	"""Возможности публикации канала из DTO — одна точка перевода.

	Правило «бот назначен» = ``bot_id is not None`` живёт здесь,
	а не в трёх местах страницы.
	"""
	return publish_capabilities(community.bot_id is not None, community.userbot_assigned)


class PublishPage(ScrollArea):
	"""Создание публикации: тип контента, канал, текст, время, отправка."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("publish")
		self._worker = worker
		self._show_error = error_reporter(self)
		# канал прошлой публикации: предвыбор после загрузки списка
		self._restore_community_id: int | None = None
		self._kind = MediaKind.NONE
		# пределы длины текста выбранного канала (None — канал не выбран
		# или ответ движка ещё не пришёл: счётчик покажет базовый предел)
		self._limits: TextLimits | None = None
		# аватары сообществ из кэша статистики — для шапок карточек очереди
		self._avatars: dict[int, str | None] = {}
		self._build()
		run_in_engine(
			worker,
			worker.engine.settings.get(PUBLISH_LAST_COMMUNITY_ID),
			self,
			self._on_last_community_loaded,
			noop,
		)

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		layout.addWidget(SubtitleLabel("Публикация", self))
		self._build_kind_segments(layout)
		self._community_combo: DtoComboBox[CommunityDto] = DtoComboBox(self)
		self._community_combo.currentIndexChanged.connect(self._on_community_changed)
		layout.addWidget(self._community_combo)
		self._caps_hint = CaptionLabel("", self)
		layout.addWidget(self._caps_hint)
		self._build_topic_row(layout)
		self._text = TextEdit(self)
		self._text.setPlaceholderText("Текст поста…")
		self._text.setMinimumHeight(120)
		layout.addWidget(self._text)
		self._counter = CharCounter(self, layout, self._text)
		self._build_caption_tools(layout)
		self._build_file_row(layout)
		self._when_row = WhenRow(self, layout)
		self._build_send_row(layout)
		layout.addStretch()
		# после сборки всех полей — сегмент по умолчанию (сигнал трогает форму)
		self._segments.setCurrentItem(MediaKind.NONE.value)

	def _build_topic_row(self, layout: QVBoxLayout) -> None:
		"""Ряд выбора темы форума (виден только форумам с userbot)."""
		self._topic_box = QWidget(self)
		row = QHBoxLayout(self._topic_box)
		row.setContentsMargins(0, 0, 0, 0)
		row.addWidget(BodyLabel("Тема форума:", self._topic_box))
		self._topic_combo: DtoComboBox[ForumTopicInfo] = DtoComboBox(
			self._topic_box, placeholder="Общая лента"
		)
		self._topic_combo.setToolTip(
			"Тема, в которую уйдёт пост; «Общая лента» — General. "
			"Список читается из Telegram при выборе сообщества."
		)
		row.addWidget(self._topic_combo, stretch=1)
		layout.addWidget(self._topic_box)
		self._topic_hint = CaptionLabel("", self._topic_box)
		row.addWidget(self._topic_hint)
		self._topic_box.setVisible(False)

	def _build_kind_segments(self, layout: QVBoxLayout) -> None:
		"""Сегментный переключатель типа контента."""
		self._segments = SegmentedWidget(self)
		for label, kind, _file_filter in CONTENT_KINDS:
			self._segments.addItem(routeKey=kind.value, text=label)
		self._segments.currentItemChanged.connect(self._on_kind_changed)
		layout.addWidget(self._segments)

	def _build_caption_tools(self, layout: QVBoxLayout) -> None:
		"""Кнопки шаблонизатора подписи."""
		row = QHBoxLayout()
		compose = PushButton("Собрать подпись…", self)
		compose.clicked.connect(self._on_compose_caption)
		row.addWidget(compose)
		setup = PushButton("Поля подписи…", self)
		setup.clicked.connect(self._on_setup_fields)
		row.addWidget(setup)
		row.addStretch()
		layout.addLayout(row)

	def _build_file_row(self, layout: QVBoxLayout) -> None:
		"""Строка выбора файла вложения (скрыта для типа «Текст»)."""
		self._file_box = QWidget(self)
		row = QHBoxLayout(self._file_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._file_edit = LineEdit(self._file_box)
		self._file_edit.setPlaceholderText("Файл вложения…")
		self._file_edit.textChanged.connect(self._clear_rename)
		browse = PushButton("Обзор…", self._file_box)
		browse.clicked.connect(self._pick_file)
		row.addWidget(self._file_edit)
		row.addWidget(browse)
		self._file_box.hide()
		layout.addWidget(self._file_box)
		self._build_rename_row(layout)

	def _build_rename_row(self, layout: QVBoxLayout) -> None:
		"""Строка переименования файла при отправке (появляется из подписи)."""
		self._rename_box = QWidget(self)
		row = QHBoxLayout(self._rename_box)
		row.setContentsMargins(0, 0, 0, 0)
		self._rename_check = CheckBox("Переименовать при отправке:", self._rename_box)
		self._rename_check.setChecked(True)
		row.addWidget(self._rename_check)
		self._rename_edit = LineEdit(self._rename_box)
		row.addWidget(self._rename_edit, stretch=1)
		self._rename_box.hide()
		layout.addWidget(self._rename_box)

	def _clear_rename(self, _text: str = "") -> None:
		"""Сбрасывает переименование (файл сменился — имя устарело)."""
		self._rename_edit.clear()
		self._rename_box.hide()

	def _build_send_row(self, layout: QVBoxLayout) -> None:
		"""Кнопки отправки (одиночной и пакетной) и панель очереди под ними."""
		row = QHBoxLayout()
		self._send_button = PrimaryPushButton(FluentIcon.SEND, "Отправить", self)
		self._send_button.clicked.connect(self._on_send)
		row.addWidget(self._send_button)
		batch_button = PushButton(FluentIcon.FOLDER, "Пакет из папки…", self)
		batch_button.setToolTip(
			"Собрать черновики постов из всех видео готовой папки: подписи "
			"по общему шаблону, раскладка времени, правка построчно (ADR-0015)"
		)
		batch_button.clicked.connect(self._on_batch)
		row.addWidget(batch_button)
		view_button = PushButton("Вся очередь…", self)
		view_button.setToolTip(
			"Все элементы очереди отправки с сортировкой и фильтрами "
			f"(на странице — ближайшие {_QUEUE_MAX_CARDS})"
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
			self._worker,
			self,
			queue_box,
			service=lambda: self._worker.engine.publish_queue,
			subtitle=queue_subtitle,
			on_finished=self._on_queue_finished,
			on_refreshed=self._update_queue_summary,
			# длинный хвост ждущих слота (ADR-0016) не раздувает страницу;
			# всё целиком — в диалоге «Вся очередь…»
			max_cards=_QUEUE_MAX_CARDS,
			# правка — прямо в карточке (ADR-0016, п. 7): раскрывается
			# кликом, как параметры файла на «Видео»
			editable=lambda item: item.status in EDITABLE_STATUSES,
			fill_body=self._fill_editor,
			leading=lambda item, parent: queue_leading(
				item, parent, self._avatars.get(item.community_id)
			),
		)

	def _on_queue_view(self) -> None:
		"""Открывает полный просмотр очереди (сортировка и фильтры)."""
		exec_dialog(QueueViewDialog(self._worker, self.window()))

	def _fill_editor(self, item_id: int, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет раскрытую карточку очереди формой правки (ADR-0016)."""
		mount_queue_item_editor(self._worker, self, item_id, body, collapse, self._queue.poll)

	# --- поведение -----------------------------------------------------------------

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет список каналов при каждом открытии страницы."""
		super().showEvent(event)
		self._reload_communities()

	def prefill_media(self, kind: MediaKind, path: str, community_id: int | None = None) -> None:
		"""Подставляет вложение (переход с других страниц, например «Видео»).

		``community_id`` — предвыбор канала (например, выбранного на «Видео»);
		применяется тем же механизмом, что и канал прошлой публикации.
		"""
		self._segments.setCurrentItem(kind.value)
		self._on_kind_changed(kind.value)
		self._file_edit.setText(path)
		if community_id is not None:
			self._restore_community_id = community_id
			self._apply_community_restore()

	def _apply_avatars(self, stats: list[CommunityStatsDto]) -> None:
		"""Раскладывает аватары сообществ и перерисовывает шапки карточек."""
		self._avatars = {item.community_id: item.avatar_path for item in stats}
		self._queue.refresh_leading()

	def _reload_communities(self) -> None:
		"""Просит свежий список каналов и аватары для шапок очереди."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._show_communities,
			self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._apply_avatars,
			# аватар — украшение шапки: без него карточка рисует букву
			noop,
		)

	def _show_communities(self, communities: list[CommunityDto]) -> None:
		"""Обновляет список каналов, сохраняя выбор по id канала.

		Выключенные каналы (настройка ``enabled``) в списке не показываются —
		фильтр презентационный, само правило держит движок (PostsService
		откажет выключенному каналу).
		"""
		self._community_combo.set_items(
			[community for community in communities if community.enabled],
			label=community_combo_label,
			key=lambda community: community.id,
		)
		# успешное восстановление само запускает обработчик смены (сигнал
		# select); явный вызов нужен только когда восстанавливать нечего.
		# Неудача на свежем списке означает устаревший id (канал выключен
		# или удалён) — забываем его, иначе предвыбор «выстрелил» бы позже,
		# при следующей загрузке списка, внезапной сменой канала
		if not self._apply_community_restore():
			self._restore_community_id = None
			self._on_community_changed()

	def _on_last_community_loaded(self, community_id: int | None) -> None:
		"""Пришёл канал прошлой публикации — применяем, если список готов."""
		self._restore_community_id = community_id
		self._apply_community_restore()

	def _apply_community_restore(self) -> bool:
		"""Предвыбирает канал прошлой публикации (один раз).

		Returns:
			True — выбор применён; обработчик смены уже запущен сигналом
			``select`` (см. контракт DtoComboBox.select), звать его не нужно.
		"""
		wanted = self._restore_community_id
		if wanted is None:
			return False
		if self._community_combo.select(lambda community: community.id == wanted):
			self._restore_community_id = None
			return True
		return False

	def _community_or_none(self) -> CommunityDto | None:
		"""Выбранный канал без показа ошибок (для адаптации формы)."""
		return self._community_combo.selected()

	def _on_community_changed(self, _index: int = 0) -> None:
		"""Адаптирует форму под возможности и времена выбранного канала."""
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
		caps = _community_caps(community)
		self._update_topic_row(community, caps)
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
			self._when_row.set_schedule_allowed(False, "Отложенные требуют userbot-админа в канале")
		else:
			self._caps_hint.setText(
				"⚠ Нет способа публикации — проверьте доступы на странице «Каналы»."
			)
			self._when_row.set_schedule_allowed(False, "Нет способа публикации")

	def _apply_limits(self, community_id: int, limits: TextLimits) -> None:
		"""Запоминает пределы длины канала, если он всё ещё выбран."""
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
		if self._limits is None:
			self._counter.set_limit(text_length_limit(premium=False, with_media=with_media))
			return
		self._counter.set_limit(self._limits.caption if with_media else self._limits.text)

	def _update_topic_row(self, community: CommunityDto, caps: PublishCapabilities) -> None:
		"""Показывает и наполняет выбор темы форума (ADR-0021).

		Темы читает только userbot (у Bot API метода нет): форум лишь
		с ботом публикует в общую ленту — ряд темы скрыт, о причине
		скажет подсказка возможностей.
		"""
		if not community.forum or not caps.userbot:
			self._topic_box.setVisible(False)
			self._topic_combo.set_items([], label=lambda topic: topic.title)
			return
		self._topic_box.setVisible(True)
		self._topic_hint.setText("")
		self._topic_combo.set_items([], label=lambda topic: topic.title)
		run_in_engine(
			self._worker,
			self._worker.engine.posts.list_topics(community.id),
			self,
			partial(self._show_topics, community),
			partial(self._on_topics_failed, community.id),
		)

	def _show_topics(self, community: CommunityDto, topics: list[ForumTopicInfo]) -> None:
		"""Наполняет список тем с учётом роли публикатора (ADR-0022).

		General не дублируем — он «Общая лента». В закрытую тему пишет
		только админ: участнику такие темы недоступны для выбора
		(скрываются, причина — в подписи ряда), админу — помечаются.
		"""
		if self._is_stale(community.id):
			return
		shown, closed = visible_topics(topics, community.default_role)
		if closed:
			self._topic_hint.setText(f"Закрытых тем скрыто: {closed} — в них пишет только админ.")
		self._topic_combo.set_items(shown, label=topic_label, key=lambda topic: topic.id)

	def _on_topics_failed(self, community_id: int, message: str) -> None:
		"""Темы не прочитались — публикуем в общую ленту, честно предупредив."""
		if self._is_stale(community_id):
			return
		self._topic_box.setVisible(False)
		self._caps_hint.setText(
			f"{self._caps_hint.text()} Темы форума не загрузились ({message}) — "
			"пост уйдёт в общую ленту."
		)

	def _selected_topic_id(self) -> int | None:
		"""Тема из видимого ряда; скрыт или «Общая лента» — None."""
		if not self._topic_box.isVisibleTo(self):
			return None
		topic = self._topic_combo.selected()
		return topic.id if topic is not None else None

	def _is_stale(self, community_id: int) -> bool:
		"""Пришёл ли ответ движка для уже переключённого канала.

		Пока движок занят (очередь отправки в том же цикле, ADR-0016),
		ответы задерживаются: без проверки подсказка и времена канала A
		перезаписали бы уже показанные данные канала B.
		"""
		return not self._community_combo.is_current_id(community_id)

	def _apply_times(self, community_id: int, times: list[str]) -> None:
		"""Подставляет времена канала, если он всё ещё выбран."""
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

	def _on_kind_changed(self, kind_key: str) -> None:
		"""Меняет состав формы под выбранный тип контента."""
		self._kind = MediaKind(kind_key)
		is_text = self._kind is MediaKind.NONE
		self._file_box.setVisible(not is_text)
		self._text.setPlaceholderText(
			"Текст поста…" if is_text else "Подпись к файлу (необязательно)…"
		)
		# подпись к файлу вчетверо короче поста без вложения
		self._apply_text_limit()

	def _pick_file(self) -> None:
		"""Диалог выбора вложения с фильтром по текущему типу контента.

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
		"""Открывает диалог вложения; ``start`` — папка или VideoDirs."""
		start_dir = start.processed if isinstance(start, VideoDirs) else start
		path = pick_file(self, "Файл вложения", kind_file_filter(self._kind), start_dir=start_dir)
		if path:
			self._file_edit.setText(path)

	# --- подпись по шаблону -----------------------------------------------------

	def _current_community(self) -> CommunityDto | None:
		"""Выбранный канал или None (с показом подсказки)."""
		community = self._community_or_none()
		if community is None:
			self._show_error("Сначала подключите и выберите канал.")
		return community

	def _on_setup_fields(self) -> None:
		"""Открывает настройку полей и шаблонов подписи канала."""
		community = self._current_community()
		if community is not None:
			exec_dialog(FieldsDialog(self._worker, community.id, community.title, self.window()))

	def _on_compose_caption(self) -> None:
		"""Загружает шаблоны канала и открывает диалог сборки."""
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
		media = str(self._file_edit.text()).strip()
		title = ""
		if self._kind is not MediaKind.NONE and media:
			title = title_from_filename(media)
		dialog = CaptionDialog(usable, title, self.window())
		if not exec_dialog(dialog):
			return
		self._text.setPlainText(dialog.caption())
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
		self._rename_edit.setText(filename)
		self._rename_check.setChecked(True)
		self._rename_box.show()

	# --- пакет из папки (ADR-0015) --------------------------------------------------

	def _on_batch(self) -> None:
		"""Пакетная отправка: канал → папка → сканирование → черновики."""
		community = self._current_community()
		if community is None:
			return
		caps = _community_caps(community)
		if not (caps.userbot or caps.bot):
			self._show_error("Нет способа публикации — проверьте доступы на странице «Каналы».")
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.processed_dir_for_community(community.id),
			self,
			partial(self._pick_batch_dir, community),
			self._show_error,
		)

	def _pick_batch_dir(self, community: CommunityDto, start_dir: str) -> None:
		"""Выбор готовой папки (по умолчанию — папка результатов канала)."""
		root = pick_dir(self, "Готовая папка с видео", start_dir=start_dir)
		if root:
			self._scan_batch_root(community, root)

	def _scan_batch_root(self, community: CommunityDto, root: str) -> None:
		"""Сканирует готовую папку и продолжает цепочку пакета."""
		setup = _BatchSetup(community, root)
		run_in_engine(
			self._worker,
			self._worker.engine.video.scan_ready(root),
			self,
			partial(self._on_batch_scanned, setup),
			self._show_error,
		)

	def start_batch_with_folder(self, root: str, community_id: int) -> None:
		"""Пакет из папки, выбранной на другой странице («Видео»).

		Вход с чужой страницы: канал приходит её id (0 — не выбран)
		и предвыбирается в списке каналов этой страницы.
		"""
		community = self._batch_community(community_id)
		if community is not None:
			self._scan_batch_root(community, root)

	def start_batch_with_files(self, paths: list[str], community_id: int) -> None:
		"""Пакет из готового списка файлов (выбор на странице «Видео»).

		Сборку списка (размеры, пропуск исчезнувших, порядок) делает
		движок — источник пакета держит он (ADR-0015), страница только
		показывает результат.
		"""
		community = self._batch_community(community_id)
		if community is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.ready_from_paths(paths),
			self,
			partial(self._on_batch_files_ready, community),
			self._show_error,
		)

	def _on_batch_files_ready(self, community: CommunityDto, files: list[VideoFile]) -> None:
		"""Список собран движком — дальше обычная цепочка пакета."""
		if not files:
			self._show_error("Файлы не найдены на диске — публиковать нечего.")
			return
		# файлы с «Видео» могут лежать в разных подпапках результатов:
		# подписью идёт общий корень, а не папка первого файла
		root = os.path.commonpath([str(Path(f.path).parent) for f in files])
		self._on_batch_scanned(_BatchSetup(community, root), files)

	def _batch_community(self, community_id: int) -> CommunityDto | None:
		"""Канал пакета по id с другой страницы (с предвыбором в списке).

		Страница «Видео» показывает все каналы, а этот список — только
		включённые: неудачный предвыбор означает, что канал недоступен
		для публикации, и пакет отменяется. Иначе выбор молча остался бы
		на прежнем канале и пакет ушёл бы не туда.
		"""
		if community_id and not self._community_combo.select(
			lambda community: community.id == community_id
		):
			self._show_error(
				"Канал недоступен для публикации (выключен или список каналов "
				"ещё загружается) — проверьте настройки канала и повторите."
			)
			return None
		community = self._community_or_none()
		if community is None:
			self._show_error(
				"Канал не выбран (или список каналов ещё загружается) — выберите канал и повторите."
			)
		return community

	def _on_batch_scanned(self, setup: _BatchSetup, files: list[VideoFile]) -> None:
		"""Файлы найдены — общий шаблон подписи (если шаблоны настроены)."""
		if not files:
			InfoBar.info(
				"Видео не найдено",
				f"В папке нет видеофайлов (включая вложенные): {setup.root}",
				parent=self,
			)
			return
		setup.files = files
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(setup.community.id),
			self,
			partial(self._batch_caption_pass, setup),
			self._show_error,
		)

	def _batch_caption_pass(self, setup: _BatchSetup, templates: list[TemplateDto]) -> None:
		"""Один проход диалога подписи: шаблон и общие значения на весь пакет.

		Название у каждой строки будет своё (из имени файла), поэтому поле
		названия в диалоге пустое. Отмена диалога — пакет без подписей,
		а не отмена пакета: подписи правятся построчно дальше.
		"""
		usable = [template for template in templates if template.fields]
		if usable:
			dialog = CaptionDialog(usable, "", self.window())
			if exec_dialog(dialog):
				setup.caption_lines = dialog.lines()
				setup.used_values = dialog.used_values()
				template = next(t for t in usable if t.id == dialog.template_id())
				if template.filename_pattern:
					setup.filename_template_id = template.id
				self._record_template_usage(template.id, setup.used_values)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, setup.community.id),
			self,
			partial(self._batch_times_loaded, setup),
			self._show_error,
		)

	def _batch_times_loaded(self, setup: _BatchSetup, times: list[str]) -> None:
		"""Времена канала получены — читаем существующие отложки.

		Раскладка пропускает занятые слоты, поэтому диалогу нужны
		времена уже созданных в Telegram отложенных записей канала.
		"""
		setup.times = times
		run_in_engine(
			self._worker,
			self._worker.engine.posts.scheduled_times(setup.community.id),
			self,
			partial(self._batch_scheduled_loaded, setup),
			partial(self._batch_scheduled_failed, setup),
		)

	def _batch_scheduled_failed(self, setup: _BatchSetup, message: str) -> None:
		"""Отложки не прочитались — пакет продолжается без их учёта.

		Проверка занятых слотов вспомогательная: отказ userbot не должен
		блокировать пакет, но о слепой раскладке честно предупреждаем.
		"""
		show_warning(
			self,
			"Отложки не прочитаны",
			f"Раскладка не учтёт существующие отложки: {message}",
		)
		self._batch_scheduled_loaded(setup, [])

	def _batch_scheduled_loaded(self, setup: _BatchSetup, scheduled: list[datetime]) -> None:
		"""Отложки получены — заготовка правил разбора имени файла."""
		setup.busy = scheduled
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(TITLE_PARSE_RULES, setup.community.id),
			self,
			partial(self._batch_rules_loaded, setup),
			self._show_error,
		)

	def _batch_rules_loaded(self, setup: _BatchSetup, tokens: list[str]) -> None:
		"""Правила разбора получены — осталась граница размера файла."""
		setup.title_rules = TitleParseRules.from_tokens(tokens)
		caps = _community_caps(setup.community)
		if caps.userbot:
			run_in_engine(
				self._worker,
				self._worker.engine.posts.userbot_limit_bytes(setup.community.id),
				self,
				partial(self._open_batch_dialog, setup, True),
				self._show_error,
			)
		else:
			# запасной бот-путь: лимит 50 МБ и только «сейчас» (ADR-0011)
			self._open_batch_dialog(setup, False, BOT_MAX_FILE_BYTES)

	def _open_batch_dialog(
		self, setup: _BatchSetup, schedule_allowed: bool, limit_bytes: int
	) -> None:
		"""Показывает черновики пакета; принятые ставит в очередь отправки."""
		dialog = PublishBatchDialog(
			self._worker,
			setup.community,
			setup.root,
			setup.files,
			self.window(),
			caption_lines=setup.caption_lines,
			filename_template_id=setup.filename_template_id,
			used_values=setup.used_values,
			community_times=setup.times,
			limit_bytes=limit_bytes,
			# предел подписи канала: у Premium-публикатора он выше базового
			caption_limit=(
				self._limits.caption
				if self._limits is not None
				else text_length_limit(premium=False, with_media=True)
			),
			schedule_allowed=schedule_allowed,
			title_rules=setup.title_rules,
			# отложки приходят из Telegram в UTC, раскладка живёт
			# в местном наивном времени — как ввод пользователя
			busy=[moment.astimezone().replace(tzinfo=None) for moment in setup.busy],
		)
		if not exec_dialog(dialog):
			return
		try:
			drafts = dialog.drafts(setup.community.id, self._selected_topic_id())
		except ValueError as exc:  # страховка: validate диалога это уже проверил
			self._show_error(str(exc))
			return
		if not drafts:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.enqueue_many(drafts),
			self,
			partial(self._on_batch_enqueued, len(drafts)),
			self._show_error,
		)

	def _on_batch_enqueued(self, count: int, _ids: list[int]) -> None:
		"""Пакет принят в очередь — карточки видны сразу, не по таймеру."""
		InfoBar.success("Пакет в очереди", f"Постов: {count}", parent=self)
		self._queue.poll()

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
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(PUBLISH_LAST_COMMUNITY_ID, community.id),
			self,
			noop,
			noop,
		)

	def _draft(self, community_id: int) -> PostDraft:
		"""Собирает черновик публикации из полей формы.

		Raises:
			ValueError: Поля формы не согласованы: выбран тип с вложением,
				а файл не указан, либо время публикации не «ЧЧ:ММ».
		"""
		media = str(self._file_edit.text()).strip() or None
		is_text = self._kind is MediaKind.NONE
		if not is_text and media is None:
			raise ValueError(
				f"Выбран тип «{kind_label(self._kind)}», а файл не указан — "
				"выберите файл или переключитесь на «Текст»."
			)
		return PostDraft(
			community_id=community_id,
			text=str(self._text.toPlainText()).strip(),
			media_path=None if is_text else media,
			media_kind=MediaKind.NONE if is_text else self._kind,
			when=self._when_row.when(),
			rename_to=self._rename_to(),
			topic_id=self._selected_topic_id(),
		)

	def _rename_to(self) -> str | None:
		"""Новое имя файла, если переименование включено и имя задано."""
		if not self._rename_box.isVisibleTo(self) or not self._rename_check.isChecked():
			return None
		return str(self._rename_edit.text()).strip() or None

	def _on_enqueued(self, _item_id: object = None) -> None:
		"""Черновик принят в очередь — чистим форму под следующий пост."""
		self._text.clear()
		self._file_edit.clear()
		self._queue.poll()  # панель очереди обновляется сразу, не по таймеру

	# --- панель очереди -------------------------------------------------------------

	def upload_active(self) -> bool:
		"""Идёт ли отправка прямо сейчас (для подтверждения выхода).

		Ждущие и готовые к отправке не в счёт: очередь персистентна
		(ADR-0016), при выходе они сохранятся и продолжатся при
		следующем запуске.
		"""
		return self._queue.active()

	def _on_queue_finished(self, item: QueueItemDto, done: bool) -> None:
		"""Итоговая плашка завершённого элемента.

		Родитель — окно: опрос живёт всегда, и завершение может прийти
		при скрытой странице — плашка на ней погасла бы незамеченной.
		"""
		if done:
			InfoBar.success(
				"Отложенная запись создана" if item.scheduled else "Опубликовано",
				item.title,
				parent=self.window(),
			)
		else:
			InfoBar.info("Отправка отменена", item.title, parent=self.window())

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
