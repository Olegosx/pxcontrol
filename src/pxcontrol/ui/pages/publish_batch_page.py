"""Экран «Пакет»: черновики постов из готовой папки видео (ADR-0015).

Вторая стадия раздела «Публикация» (ADR-0032, п. 4). Раньше пакет был
режимом внутри формы поста: кнопка на форме вела цепочку подготовки
и открывала рабочее окно с черновиками. Теперь у него свой экран, а всё
остальное в ADR-0015 не меняется — те же черновики построчно, общий
шаблон подписи, раскладка времени стратегиями, атомарная постановка
``enqueue_many``.

Экран отвечает за три вещи, которых у редактора строк нет:

- **адресат пакета** — сообщество и тема форума (общий блок
  :mod:`post_target`, тот же, что у формы поста);
- **подготовка источника** — цепочка обращений к движку: сканирование
  папки, общий шаблон подписи, времена сообщества, занятые отложки,
  правила разбора имени, пределы текста и размера файла;
- **кнопки под постом** — одна клавиатура на весь пакет (ADR-0031):
  правила их доступности зависят от маршрута, а маршрут — от самого
  «тяжёлого» черновика пакета (отложенного и с крупным файлом).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	FluentIcon,
	PrimaryPushButton,
	PushButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import CaptionLine, TemplateDto, TitleParseRules
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.posts import TextLimits
from pxcontrol.engine.services.settings import PUBLISH_TIMES, TITLE_PARSE_RULES
from pxcontrol.engine.services.video import VideoFile
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	limit_mb,
	text_length_limit,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.captions import CaptionDialog
from pxcontrol.ui.pages.common import (
	CollapsibleCard,
	ErrorLabel,
	clear_layout,
	error_reporter,
	exec_dialog,
	noop,
	pick_dir,
	plural,
	show_info,
	show_success,
	show_warning,
)
from pxcontrol.ui.pages.markup_editor import MarkupEditor, MarkupState, markup_state
from pxcontrol.ui.pages.post_target import CommunityChoice, TopicChoice
from pxcontrol.ui.pages.publish_batch import BatchEditor
from pxcontrol.ui.pages.publish_stages import PublishStage
from pxcontrol.ui.pages.stage_page import StagePage

#: Пределы, пока сообщество не ответило: базовые — они не обещают лишнего.
_BASE_LIMITS = TextLimits(
	text=text_length_limit(premium=False, with_media=False),
	caption=text_length_limit(premium=False, with_media=True),
)


@dataclass
class _BatchSetup:
	"""Собираемые данные пакета отправки (ADR-0015).

	Заполняется по шагам цепочки колбэков (папка → сканирование →
	общий шаблон подписи → времена сообщества → отложки → правила
	разбора имени → пределы), чтобы не таскать длинный список
	аргументов через каждую функцию.
	"""

	community: CommunityDto
	root: str
	files: list[VideoFile] = field(default_factory=list)
	caption_lines: list[CaptionLine] | None = None
	filename_template_id: int | None = None
	used_values: dict[int, list[str]] = field(default_factory=dict)
	times: list[str] = field(default_factory=list)
	busy: list[datetime] = field(default_factory=list)  # отложки сообщества (UTC)
	title_rules: TitleParseRules = field(default_factory=TitleParseRules)
	limits: TextLimits | None = None


def source_text(root: str, files: int) -> str:
	"""Подпись источника пакета: папка и сколько файлов в ней нашлось."""
	if not root:
		return "Источник не выбран."
	return f"Папка: {root} · {files} {plural(files, 'файл', 'файла', 'файлов')}"


class BatchStagePage(StagePage):
	"""Экран «Пакет»: адресат, источник, черновики построчно, постановка."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(PublishStage.BATCH, parent)
		self._worker = worker
		self._show_error = error_reporter(self)
		self._editor: BatchEditor | None = None
		self._setup: _BatchSetup | None = None
		body = QWidget(self)
		layout = QVBoxLayout(body)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().row_spacing)
		self._build_target(layout)
		self._build_source(layout)
		self._editor_box = QVBoxLayout()
		layout.addLayout(self._editor_box, stretch=1)
		self._build_markup(layout)
		self._error = ErrorLabel(body)
		layout.addWidget(self._error)
		self._build_send(layout)
		# растяжка снизу — обязательная, а не косметическая: без неё
		# лишнюю высоту экрана забирают подписи с переносом слов
		# (подсказка возможностей, строка источника), и форма
		# расползается пустотами. Вес нулевой: когда пакет собран,
		# место достаётся списку черновиков (он со stretch=1)
		layout.addStretch()
		self.mount(body)
		self._community.restore_last()
		self._render_source()

	# --- сборка ------------------------------------------------------------------

	def _build_target(self, layout: QVBoxLayout) -> None:
		"""Адресат пакета: сообщество и тема форума (общие с формой поста)."""
		self._community = CommunityChoice(self, self._worker)
		self._community.chosen.connect(self._on_community_changed)
		layout.addWidget(self._community)
		self._caps_hint = CaptionLabel("", self)
		self._caps_hint.setWordWrap(True)
		layout.addWidget(self._caps_hint)
		self._topics = TopicChoice(
			self, layout, self._worker, self._community, on_failed=self._on_topics_failed
		)

	def _build_source(self, layout: QVBoxLayout) -> None:
		"""Строка источника: выбор готовой папки и подпись выбранного."""
		row = QHBoxLayout()
		pick = PushButton(FluentIcon.FOLDER, "Готовая папка с видео…", self)
		pick.setToolTip(
			"Собрать черновики из всех видео папки (включая вложенные): "
			"подписи по общему шаблону, раскладка времени, правка построчно"
		)
		pick.clicked.connect(self._on_pick_folder)
		row.addWidget(pick)
		self._source = BodyLabel("", self)
		self._source.setWordWrap(True)
		row.addWidget(self._source, stretch=1)
		layout.addLayout(row)

	def _build_markup(self, layout: QVBoxLayout) -> None:
		"""Блок кнопок под постом — одна клавиатура на весь пакет (ADR-0031)."""
		self._markup_card = CollapsibleCard("Кнопки под постом", self)
		self._markup = MarkupEditor(self._markup_card)
		self._markup.changed.connect(self._refresh_markup)
		self._markup_card.body.addWidget(self._markup)
		layout.addWidget(self._markup_card)

	def _build_send(self, layout: QVBoxLayout) -> None:
		"""Кнопка постановки всего пакета в очередь отправки."""
		row = QHBoxLayout()
		self._send = PrimaryPushButton(FluentIcon.SEND, "Поставить в очередь", self)
		self._send.setToolTip("Отмеченные строки уходят в очередь отправки одним пакетом")
		self._send.clicked.connect(self._on_send)
		self._send.setEnabled(False)
		row.addWidget(self._send)
		row.addStretch()
		layout.addLayout(row)

	# --- жизнь экрана ---------------------------------------------------------------

	def set_active(self, active: bool) -> None:
		"""Показ экрана обновляет список сообществ (могли включить новое)."""
		if active:
			self._community.reload(self._show_error)

	# --- входы с других экранов -------------------------------------------------------

	def start_with_folder(self, root: str, community_id: int) -> None:
		"""Пакет из папки, выбранной на «Видео» (ADR-0015)."""
		community = self._community_for(community_id)
		if community is not None:
			self._scan(community, root)

	def start_with_files(self, paths: list[str], community_id: int) -> None:
		"""Пакет из готового списка файлов (выбор на странице «Видео»).

		Сборку списка (размеры, пропуск исчезнувших, порядок) делает
		движок — источник пакета держит он (ADR-0015), экран только
		показывает результат.
		"""
		community = self._community_for(community_id)
		if community is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.ready_from_paths(paths),
			self,
			partial(self._on_files_ready, community),
			self._show_error,
		)

	def _community_for(self, community_id: int) -> CommunityDto | None:
		"""Сообщество пакета по id с другой страницы (с предвыбором в списке).

		Страница «Видео» показывает все сообщества, а этот список —
		только включённые: неудачный предвыбор означает, что сообщество
		недоступно для публикации, и пакет отменяется. Иначе выбор молча
		остался бы на прежнем сообществе и пакет ушёл бы не туда.
		"""
		if community_id:
			self._community.want(community_id)
			if not self._community.is_current_id(community_id):
				self._show_error(
					"Сообщество недоступно для публикации (выключено или список "
					"ещё загружается) — проверьте его настройки и повторите."
				)
				return None
		community = self._community.current()
		if community is None:
			self._show_error(
				"Сообщество не выбрано (или список ещё загружается) — "
				"выберите сообщество и повторите."
			)
		return community

	def _on_files_ready(self, community: CommunityDto, files: list[VideoFile]) -> None:
		"""Список собран движком — дальше обычная цепочка подготовки."""
		if not files:
			self._show_error("Файлы не найдены на диске — публиковать нечего.")
			return
		# файлы с «Видео» могут лежать в разных подпапках результатов:
		# подписью идёт общий корень, а не папка первого файла
		root = os.path.commonpath([str(Path(file.path).parent) for file in files])
		self._on_scanned(_BatchSetup(community, root), files)

	# --- подготовка источника ----------------------------------------------------------

	def _on_pick_folder(self) -> None:
		"""«Готовая папка с видео…»: сообщество → папка → сканирование."""
		community = self._community.current()
		if community is None:
			self._show_error("Сначала подключите и выберите сообщество.")
			return
		caps = community.capabilities
		if not (caps.userbot or caps.bot):
			self._show_error("Нет способа публикации — проверьте доступы на странице сообщества.")
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.processed_dir_for_community(community.id),
			self,
			partial(self._pick_dir, community),
			self._show_error,
		)

	def _pick_dir(self, community: CommunityDto, start_dir: str) -> None:
		"""Выбор готовой папки (по умолчанию — папка результатов сообщества)."""
		root = pick_dir(self, "Готовая папка с видео", start_dir=start_dir)
		if root:
			self._scan(community, root)

	def _scan(self, community: CommunityDto, root: str) -> None:
		"""Сканирует готовую папку и продолжает цепочку подготовки."""
		setup = _BatchSetup(community, root)
		run_in_engine(
			self._worker,
			self._worker.engine.video.scan_ready(root),
			self,
			partial(self._on_scanned, setup),
			self._show_error,
		)

	def _on_scanned(self, setup: _BatchSetup, files: list[VideoFile]) -> None:
		"""Файлы найдены — общий шаблон подписи (если шаблоны настроены)."""
		if not files:
			show_info(
				self,
				"Видео не найдено",
				f"В папке нет видеофайлов (включая вложенные): {setup.root}",
			)
			return
		setup.files = files
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_templates(setup.community.id),
			self,
			partial(self._caption_pass, setup),
			self._show_error,
		)

	def _caption_pass(self, setup: _BatchSetup, templates: list[TemplateDto]) -> None:
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
				template = next(item for item in usable if item.id == dialog.template_id())
				if template.filename_pattern:
					setup.filename_template_id = template.id
				run_in_engine(
					self._worker,
					self._worker.engine.captions.record_usage(template.id, setup.used_values),
					self,
					noop,
					self._show_error,
				)
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, setup.community.id),
			self,
			partial(self._times_loaded, setup),
			self._show_error,
		)

	def _times_loaded(self, setup: _BatchSetup, times: list[str]) -> None:
		"""Времена сообщества получены — читаем существующие отложки.

		Раскладка пропускает занятые слоты, поэтому редактору нужны
		времена уже созданных в Telegram отложенных записей.
		"""
		setup.times = times
		run_in_engine(
			self._worker,
			self._worker.engine.posts.scheduled_times(setup.community.id),
			self,
			partial(self._scheduled_loaded, setup),
			partial(self._scheduled_failed, setup),
		)

	def _scheduled_failed(self, setup: _BatchSetup, message: str) -> None:
		"""Отложки не прочитались — пакет продолжается без их учёта.

		Проверка занятых слотов вспомогательная: отказ userbot не должен
		блокировать пакет, но о слепой раскладке честно предупреждаем.
		"""
		show_warning(
			self,
			"Отложки не прочитаны",
			f"Раскладка не учтёт существующие отложки: {message}",
		)
		self._scheduled_loaded(setup, [])

	def _scheduled_loaded(self, setup: _BatchSetup, scheduled: list[datetime]) -> None:
		"""Отложки получены — заготовка правил разбора имени файла."""
		setup.busy = scheduled
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(TITLE_PARSE_RULES, setup.community.id),
			self,
			partial(self._rules_loaded, setup),
			self._show_error,
		)

	def _rules_loaded(self, setup: _BatchSetup, tokens: list[str]) -> None:
		"""Правила разбора получены — остались пределы текста сообщества."""
		setup.title_rules = TitleParseRules.from_tokens(tokens)
		run_in_engine(
			self._worker,
			self._worker.engine.posts.text_limits(setup.community.id),
			self,
			partial(self._limits_loaded, setup),
			self._show_error,
		)

	def _limits_loaded(self, setup: _BatchSetup, limits: TextLimits) -> None:
		"""Пределы текста получены — осталась граница размера файла."""
		setup.limits = limits
		caps = setup.community.capabilities
		if caps.userbot:
			run_in_engine(
				self._worker,
				self._worker.engine.posts.userbot_limit_bytes(setup.community.id),
				self,
				partial(self._mount_editor, setup),
				self._show_error,
			)
		else:
			# запасной бот-путь: лимит 50 МБ и только «сейчас» (ADR-0011)
			self._mount_editor(setup, BOT_MAX_FILE_BYTES)

	def _mount_editor(self, setup: _BatchSetup, limit_bytes: int) -> None:
		"""Ставит на экран редактор черновиков собранного пакета."""
		clear_layout(self._editor_box)
		self._setup = setup
		editor = BatchEditor(
			self._worker,
			setup.community,
			setup.root,
			setup.files,
			self,
			caption_lines=setup.caption_lines,
			filename_template_id=setup.filename_template_id,
			used_values=setup.used_values,
			community_times=setup.times,
			limit_bytes=limit_bytes,
			caption_limit=self._caption_limit(),
			schedule_allowed=setup.community.capabilities.userbot,
			title_rules=setup.title_rules,
			# отложки приходят из Telegram в UTC, раскладка живёт
			# в местном наивном времени — как ввод пользователя
			busy=[moment.astimezone().replace(tzinfo=None) for moment in setup.busy],
		)
		editor.changed.connect(self._on_editor_changed)
		self._editor = editor
		self._editor_box.addWidget(editor)
		self._render_source()
		self._on_editor_changed()

	# --- показ --------------------------------------------------------------------------

	def _render_source(self) -> None:
		"""Подпись источника и доступность постановки."""
		setup = self._setup
		self._source.setText(
			source_text(setup.root, len(setup.files)) if setup else source_text("", 0)
		)
		self._send.setEnabled(self._editor is not None)

	def _on_community_changed(self) -> None:
		"""Сообщество сменилось: адресат, подсказка возможностей, кнопки.

		Собранный пакет при смене сообщества снимается: у другого
		сообщества свои темы, свой предел файла и своё право на отложку —
		молча отправить в него черновики, собранные под прежнее, нельзя.
		"""
		community = self._community.current()
		self._topics.update_for(community)
		if community is None:
			self._caps_hint.setText("")
		elif community.capabilities.userbot:
			self._caps_hint.setText(
				"Публикация через userbot: «сейчас» и отложенные, раскладка времени по стратегиям."
			)
		elif community.capabilities.bot:
			self._caps_hint.setText(
				"Публикация через бота: только «сейчас» (для отложенных нужен "
				f"userbot-админ), файлы до {limit_mb(BOT_MAX_FILE_BYTES)} МБ."
			)
		else:
			self._caps_hint.setText(
				"⚠ Нет способа публикации — проверьте доступы на странице сообщества."
			)
		if self._setup is not None and (
			community is None or community.id != self._setup.community.id
		):
			self._drop_editor()
		self._refresh_markup()

	def _on_topics_failed(self, message: str) -> None:
		"""Темы не прочитались — пакет уйдёт в общую ленту, честно предупредив."""
		self._caps_hint.setText(
			f"{self._caps_hint.text()} Темы форума не загрузились ({message}) — "
			"пакет уйдёт в общую ленту."
		)

	def _on_editor_changed(self) -> None:
		"""Состав или время строк изменились: правила кнопок и пределы."""
		self._refresh_markup()

	def _markup_state(self) -> MarkupState | None:
		"""Состояние кнопок и пределов по самому «тяжёлому» черновику пакета.

		Пакет ставится атомарно (ADR-0015), клавиатура у всех постов одна
		(ADR-0032, п. 4) — значит и правила считать надо по строгому
		случаю: если хоть один пост отложенный или хоть один файл боту
		не по силам, маршрут пакета — с дорисовкой кнопок. Правила общие
		с формой поста: считает их :func:`markup_state`.

		None — сообщество ещё не выбрано.
		"""
		community = self._community.current()
		if community is None:
			return None
		limits = self._setup.limits if self._setup is not None else None
		return markup_state(
			community,
			limits or _BASE_LIMITS,
			# у пакета каждая строка — свой пост с одним файлом:
			# альбомов здесь не бывает
			scheduled=self._scheduled(),
			has_markup=self._markup.markup() is not None,
			markup_first=self._markup.markup_first(),
			over_bot_limit=self._over_bot_limit(),
		)

	def _scheduled(self) -> bool:
		"""Есть ли в пакете отложенные посты."""
		return self._editor is not None and self._editor.any_scheduled()

	def _over_bot_limit(self) -> bool:
		"""Есть ли в пакете файл, который бот залить не сможет."""
		return self._editor is not None and self._editor.any_over(BOT_MAX_FILE_BYTES)

	def _caption_limit(self) -> int:
		"""Предел длины подписи строки по маршруту пакета.

		Сообщество ещё не выбрано или пределы не приехали — показываем
		базовый предел Telegram: он не обещает лишнего.
		"""
		state = self._markup_state()
		if state is None:
			return _BASE_LIMITS.caption
		return state.limits.caption

	def _refresh_markup(self) -> None:
		"""Приводит блок кнопок и счётчики строк к состоянию пакета."""
		community = self._community.current()
		if community is None:
			self._markup.set_blocked("Сначала выберите сообщество — от него зависят кнопки.")
			self._markup.set_notice("")
			return
		state = self._markup_state()
		if state is None:  # pragma: no cover — сообщество проверено выше
			return
		markup = self._markup.markup()
		self._markup.set_mode_available(state.mode_available)
		self._markup.set_blocked(state.blocked)
		self._markup.set_notice(state.notice)
		count = len(markup.buttons) if markup is not None else 0
		self._markup_card.set_summary(
			f"{count} {plural(count, 'кнопка', 'кнопки', 'кнопок')}" if count else "нет"
		)
		if self._editor is not None:
			self._editor.set_caption_limit(self._caption_limit())

	# --- постановка ------------------------------------------------------------------

	def _on_send(self) -> None:
		"""Ставит отмеченные черновики в очередь отправки одним пакетом."""
		editor, setup = self._editor, self._setup
		if editor is None or setup is None:
			return
		if not editor.validate():
			return
		try:
			drafts = editor.drafts(
				setup.community.id,
				self._topics.topic_id(),
				self._markup.markup(),
				self._markup.markup_first(),
			)
		except ValueError as exc:  # страховка: validate это уже проверил
			self._error.fail(str(exc))
			return
		if not drafts:
			return
		self._error.succeed()
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.enqueue_many(drafts),
			self,
			partial(self._on_enqueued, len(drafts)),
			self._fail,
		)

	def _fail(self, message: str) -> None:
		"""Отказ движка строкой под пакетом (``fail`` возвращает bool — мосту нужен None)."""
		self._error.fail(message)

	def _on_enqueued(self, count: int, _ids: list[int]) -> None:
		"""Пакет принят: экран освобождается под следующий источник."""
		show_success(self, "Пакет в очереди", f"Постов: {count}")
		self._community.remember_last()
		self._drop_editor()

	def _drop_editor(self) -> None:
		"""Снимает собранный пакет с экрана (поставлен или сменился адресат)."""
		clear_layout(self._editor_box)
		self._editor = None
		self._setup = None
		self._render_source()
