"""Страница одного сообщества: путь, шапка, вкладки, всё действующее — по вкладкам.

Страница **одна на всё приложение** (ADR-0041): она живёт в стопке
главного окна без пункта навигации и показывает то сообщество, которое
открыли с дашборда «Каналы и группы» или со страницы аккаунта. Над
шапкой — строка пути (`BreadcrumbBar`): «Каналы и группы › Каналы ›
название»; клик по первым двум её элементам возвращает на дашборд
с нужным разделом. Шапка общая для вкладок: аватар, название, плашка
состояния, подстрочник, «Опубликовать» и меню «…». Ниже —
переключатель вкладок и стопка их тел:

- **Обзор** — справка и статистика (:mod:`community_overview`);
- **Очередь** — вид на очередь отправки этого сообщества с правкой
  поста в карточке (та же ``QueuePanel``, что на «Публикации»);
- **Отложено** — отложенные записи сообщества из Telegram (как
  экран «Отложено», но по одному сообществу);
- **Участники** — исполнители сообщества (тело диалога «Участники…»):
  пул userbot-аккаунтов с ролями и публикатором по умолчанию и бот
  сообщества, назначаемый и отвязываемый здесь же;
- **Задачи** — уборка и журнал запусков (тело окна задач, ADR-0038);
- **Настройки** — активность, пресет видео и времена, проверка
  доступов, пресеты подписи (список и экран пресета в той же вкладке,
  ADR-0042), удаление. Публикаторов здесь нет намеренно: всё, кто
  публикует, — на вкладке «Участники», одним местом.

Тела вкладок строятся лениво, при первом открытии, а панели очередей
присоединяются к наблюдателям главного окна только на время показа
(ADR-0034): десяток страниц сообществ не должен перерисовывать карточки
на невидимых вкладках. Тела с несохранёнными правками («Задачи»,
экран пресета в «Настройках») отпускают человека только через вопрос —
страница узнаёт их по общему признаку ``UnsavedChanges``, а не по
классу. Сигнал ``changed`` уходит после каждой
операции, меняющей данные, — главное окно по нему обновляет дашборд
и подменю навигации.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
	QGridLayout,
	QHBoxLayout,
	QSizePolicy,
	QStackedWidget,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	Action,
	BodyLabel,
	BreadcrumbBar,
	CaptionLabel,
	CardWidget,
	FluentIcon,
	Flyout,
	FlyoutViewBase,
	HorizontalSeparator,
	InfoBadge,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	PushButton,
	RoundMenu,
	ScrollArea,
	SimpleCardWidget,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TitleLabel,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.captions import CaptionPresetDto, FieldDto
from pxcontrol.engine.services.communities import (
	CommunityAccess,
	CommunityDto,
	ExecutorDto,
	JoinResult,
)
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.executor_join import JoinOutcome
from pxcontrol.engine.services.posts import ScheduledList
from pxcontrol.engine.services.publish_queue import EDITABLE_STATUSES, QueueItemDto
from pxcontrol.engine.services.schedule_plan import parse_hhmm
from pxcontrol.engine.services.settings import (
	COMMUNITY_DEFAULT_PRESET,
	COMMUNITY_ENABLED,
	PUBLISH_TIMES,
	SettingKey,
)
from pxcontrol.engine.services.video import PresetDto
from pxcontrol.engine.telegram.types import ExecutorRef, OwnerKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.caption_presets import PresetEditor, PresetList
from pxcontrol.ui.pages.common import (
	LIST_BUTTON_HEIGHT,
	DtoComboBox,
	ErrorLabel,
	FlowGrid,
	FormDialog,
	QueueCounts,
	TabItem,
	UnsavedChanges,
	WorkDialog,
	account_caption,
	bot_caption,
	clear_layout,
	community_kind_caption,
	confirm_delete,
	dim_widget,
	elide_text,
	entity_avatar,
	error_reporter,
	exec_dialog,
	format_local,
	list_button,
	page_layout,
	section_header,
	show_info,
	show_success,
	show_warning,
	tab_strip,
)
from pxcontrol.ui.pages.community_overview import OverviewTab
from pxcontrol.ui.pages.community_state import (
	TASKS_UNAVAILABLE,
	community_group_title,
	community_queue_counts,
	executors_count,
	header_state_text,
	state_badge,
	subtitle_text,
)
from pxcontrol.ui.pages.community_tg_settings import TelegramSettingsScreen
from pxcontrol.ui.pages.executor_text import (
	INVITE_LINK_PROMPT,
	default_candidates,
	executor_rights_rows,
	join_result_text,
	member_badge,
	member_caption,
	remove_executor_text,
)
from pxcontrol.ui.pages.list_view import ListPage, PagerRow, paginate, step_page
from pxcontrol.ui.pages.publish_queue_edit import mount_queue_item_editor
from pxcontrol.ui.pages.publish_queue_view import (
	QueueFilter,
	QueueSort,
	apply_view,
	queue_subtitle,
	slot_chip,
)
from pxcontrol.ui.pages.queue_panel import QueuePanel
from pxcontrol.ui.pages.scheduled_panel import ScheduledPanel, scheduled_subtitle
from pxcontrol.ui.pages.tasks import TaskFixTarget, TasksPanel, open_tasks
from pxcontrol.ui.pages.user_actions import set_bot_paused, set_user_paused
from pxcontrol.ui.queue_watcher import QueueView, QueueWatcher, QueueWatchers

#: Размер логотипа в шапке страницы (пиксели).
_HEADER_LOGO_SIZE = 48

#: Сколько карточек очереди на одной странице вкладки. Меньше, чем в окне
#: «Вся очередь…» (50): над списком здесь шапка и вкладки, а карточки
#: раскрываются формой правки — длинная страница тяжела и глазу, и Qt.
QUEUE_TAB_PAGE_SIZE = 20

#: Ключи вкладок (маршруты сегментов) в порядке показа.
TAB_OVERVIEW = "overview"
TAB_QUEUE = "queue"
TAB_SCHEDULED = "scheduled"
TAB_MEMBERS = "members"
TAB_TASKS = "tasks"
TAB_SETTINGS = "settings"
_TABS = (TAB_OVERVIEW, TAB_QUEUE, TAB_SCHEDULED, TAB_MEMBERS, TAB_TASKS, TAB_SETTINGS)

#: Вкладки, чьё тело сложено по снимку сообщества: «Задачи»
#: решает по нему, есть ли userbot-публикатор, «Участники» держат
#: список аккаунтов на момент сборки. Свежий снимок их пересобирает.
_SNAPSHOT_TABS = (TAB_MEMBERS, TAB_TASKS)


#: Ключ маршрута страницы (``objectName``). Страница одна, поэтому
#: ключ постоянный: пункта навигации у неё нет (ADR-0041, п. 4).
COMMUNITY_PAGE_ROUTE = "community_page"

#: Ключи элементов строки пути (внутренние, наружу не выходят).
_PATH_ROOT = "path_root"
_PATH_GROUP = "path_group"
_PATH_COMMUNITY = "path_community"


def tab_title(key: str, count: int | None = None) -> str:
	"""Подпись вкладки: существительное и число рядом (если есть что считать)."""
	titles = {
		TAB_OVERVIEW: "Обзор",
		TAB_QUEUE: "Очередь",
		TAB_SCHEDULED: "Отложено",
		TAB_MEMBERS: "Участники",
		TAB_TASKS: "Задачи",
		TAB_SETTINGS: "Настройки",
	}
	title = titles[key]
	return f"{title} {count}" if count else title  # число — для подписи без пилюли


def queue_footer_text(view: ListPage[Any]) -> str:
	"""Итоговая строка под очередью сообщества: сколько показано и из скольких."""
	if view.total == 0:
		return ""
	shown = (
		f"В очереди {view.total}"
		if view.pages == 1
		else f"Показаны {view.first}–{view.last} из {view.total}"
	)
	return (
		f"{shown} — ближайшие сначала. Клик по карточке раскрывает правку; "
		"после сохранения пост возвращается в очередь."
	)


def recheck_summary(access: CommunityAccess) -> tuple[bool, str]:
	"""Итог перепроверки доступов: (всё ли в порядке, текст для строки).

	Правило одно на всплывающую плашку и строку вкладки «Настройки»:
	«не удалось проверить» — не приговор правам (нет сети — не потеря
	прав), и различие сохраняется в тексте.
	"""
	if access.userbot_ok is None:
		userbot_text = "не удалось проверить (нет связи или аккаунт не подключён)"
	elif access.userbot_ok:
		userbot_text = f"публикатор — {access.community.default_account_label or '—'}"
	else:
		userbot_text = "публиковать не может — права изменились"
	parts = [f"userbot: {userbot_text}"]
	if access.community.default_bot_id is not None:
		# None у назначенного бота — «не проверили», а не «потерял
		# права»: приговор правам из-за пропавшей сети — неправда
		if access.bot_ok is None:
			bot_text = "проверить не удалось (нет связи или Telegram не ответил)"
		else:
			bot_text = "права на месте" if access.bot_ok else "права потеряны"
		parts.append(f"бот: {bot_text}")
	ok = bool(access.userbot_ok) and access.bot_ok is not False
	return ok, " · ".join(parts)


# --- диалоги ------------------------------------------------------------------------


def usable_accounts(accounts: list[TgAccountDto]) -> list[TgAccountDto]:
	"""Аккаунты, которых можно добавлять участниками: вошедшие и не на паузе.

	Приостановленный (ADR-0029) зонд прав не пройдёт — предлагать его
	значило бы обещать проверку, которая не состоится.
	"""
	return [account for account in accounts if account.logged_in and not account.paused]


def usable_bots(bots: list[BotDto]) -> list[BotDto]:
	"""Боты, которых можно назначить сообществу: не приостановленные.

	Причина та же, что у аккаунтов: к приостановленному (ADR-0029)
	зонд прав не пойдёт.
	"""
	return [bot for bot in bots if not bot.paused]


def read_executors(
	worker: EngineWorker,
	parent: QWidget,
	ready: Callable[[list[TgAccountDto], list[BotDto]], None],
	on_error: Callable[[str], None],
) -> None:
	"""Читает кандидатов вкладки «Участники»: аккаунты, затем ботов.

	Два чтения подряд, а не одно: у движка это разные сервисы, и заводить
	ради экрана общий метод «дай всех исполнителей» значило бы смешивать
	в движке то, что в нём разделено (ADR-0029).
	"""
	run_in_engine(
		worker,
		worker.engine.accounts.list_tg_accounts(),
		parent,
		lambda accounts: run_in_engine(
			worker,
			worker.engine.accounts.list_bots(),
			parent,
			lambda bots: ready(usable_accounts(accounts), usable_bots(bots)),
			on_error,
		),
		on_error,
	)


#: Вкладка «Участники» по макету (пиксели).
_MEMBER_MIN_WIDTH = 340  # минимальная ширина карточки в сетке
_MEMBER_SPACING = 10
_MEMBER_CARD_HEIGHT = 62  # постоянная: карточка не раскрывается
_MEMBER_AVATAR = 32
_CANDIDATE_WIDTH = 220  # список кандидатов в заголовке раздела
_PUBLISHER_COMBO_WIDTH = 150  # список смены публикатора
_RIGHTS_WIDTH = 420  # всплывающая панель прав
_RIGHTS_LABEL_WIDTH = 130
#: Приглушение аватара приостановленного исполнителя (как у выключенного
#: сообщества на дашборде).
_PAUSED_OPACITY = 0.62

#: Разделы пула: вид исполнителя, заголовок, роль в блоке публикатора,
#: пустое состояние и подсказка кнопки ввода.
_MEMBER_SECTIONS: tuple[tuple[OwnerKind, str, str, str, str], ...] = (
	(
		OwnerKind.USER,
		"Пользователи",
		"Пользователь",
		"Пользователей нет — введите вошедший аккаунт.",
		"Если исполнитель ещё не в сообществе, приложение введёт его: "
		"вступит по @имени, по ссылке-приглашению или пригласит своими силами",
	),
	(
		OwnerKind.BOT,
		"Боты",
		"Бот",
		"Ботов нет — введите бота, если нужны кнопки.",
		"Бот сам вступить не может: в группу его пригласит, а в канал "
		"примет администратором исполнитель из пула",
	),
)


class _RightsView(FlyoutViewBase):
	"""Права исполнителя всплывающей панелью (макет «Участники», 1b).

	Карточка пула больше не раскрывается: перечень прав нужен изредка,
	а раскрытие тянуло за собой высоту всего ряда сетки. Панель
	закрывается штатно — кликом мимо.
	"""

	def __init__(self, executor: ExecutorDto, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setFixedWidth(_RIGHTS_WIDTH)  # макет
		layout = QVBoxLayout(self)
		layout.setContentsMargins(*density.spacing().card_margins)
		layout.setSpacing(density.spacing().row_spacing)
		layout.addWidget(StrongBodyLabel(f"Права · {executor.label}", self))
		grid = QGridLayout()
		grid.setHorizontalSpacing(14)  # макет
		grid.setVerticalSpacing(6)  # макет
		for row, (caption, value) in enumerate(executor_rights_rows(executor)):
			label = CaptionLabel(caption, self)
			label.setFixedWidth(_RIGHTS_LABEL_WIDTH)  # макет
			grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
			text = BodyLabel(value, self)
			text.setWordWrap(True)
			grid.addWidget(text, row, 1)
		grid.setColumnStretch(1, 1)
		layout.addLayout(grid)


class _MemberCard(CardWidget):
	"""Карточка исполнителя пула: аватар, имя с плашкой, роль и три значка.

	Высота постоянная (макет): карточка не раскрывается, поэтому
	соседние карточки ряда не растягиваются. В ряду — только
	кнопки-значки: текстовая кнопка съедала колонку имени.
	"""

	def __init__(
		self,
		executor: ExecutorDto,
		parent: QWidget,
		*,
		caption_text: str,
		on_resume: Callable[[], None],
		on_rights: Callable[[QWidget], None],
		on_remove: Callable[[], None],
	) -> None:
		super().__init__(parent)
		self.setFixedHeight(_MEMBER_CARD_HEIGHT)  # макет
		row = QHBoxLayout(self)
		row.setContentsMargins(14, 10, 8, 10)  # макет
		row.setSpacing(10)  # макет
		avatar = entity_avatar(self, executor.owner.id, executor.label, None, _MEMBER_AVATAR)
		if executor.paused:
			dim_widget(avatar, _PAUSED_OPACITY)
		row.addWidget(avatar)
		column = QVBoxLayout()
		column.setSpacing(2)  # макет
		name_row = QHBoxLayout()
		name_row.setSpacing(8)  # макет
		name = StrongBodyLabel(self)
		# «занимай, что дадут», но не шире своего текста: плашка встаёт
		# сразу за именем, а длинное имя сокращается многоточием (тот же
		# приём, что у названия в шапке страницы)
		name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		name.setMaximumWidth(name.fontMetrics().horizontalAdvance(executor.label) + 8)
		elide_text(name, executor.label)
		name_row.addWidget(name, stretch=1)
		badge = member_badge(executor)
		if badge is not None:
			text, level = badge
			plate = InfoBadge(text, self, level)
			plate.adjustSize()
			name_row.addWidget(plate)
		name_row.addStretch()
		column.addLayout(name_row)
		caption = CaptionLabel(self)
		caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(caption, caption_text)
		column.addWidget(caption)
		row.addLayout(column, stretch=1)
		if executor.paused:
			row.addWidget(self._icon(FluentIcon.PLAY, "Возобновить", on_resume))
		rights = TransparentToolButton(FluentIcon.INFO, self)
		rights.setToolTip("Права в сообществе")
		rights.clicked.connect(partial(on_rights, rights))
		row.addWidget(rights)
		row.addWidget(
			self._icon(
				FluentIcon.DELETE,
				"Убрать из пула приложения — в Telegram исполнитель останется",
				on_remove,
			)
		)

	def _icon(
		self, icon: FluentIcon, hint: str, handler: Callable[[], None]
	) -> TransparentToolButton:
		"""Кнопка-значок карточки: штатная, с подсказкой."""
		button = TransparentToolButton(icon, self)
		button.setToolTip(hint)
		button.clicked.connect(handler)
		return button


class _PublisherCard(SimpleCardWidget):
	"""Публикатор по умолчанию одного вида: кто сейчас и список смены."""

	def __init__(
		self,
		kind: OwnerKind,
		role: str,
		panel: QWidget,
		on_change: Callable[[ExecutorDto], None],
	) -> None:
		super().__init__(panel)
		self._kind = kind
		self._on_change = on_change
		layout = QVBoxLayout(self)
		layout.setContentsMargins(14, 12, 14, 12)  # макет
		layout.setSpacing(10)  # макет
		layout.addWidget(BodyLabel(role, self))
		layout.addWidget(HorizontalSeparator(self))
		self._row = QHBoxLayout()
		self._row.setSpacing(10)  # макет
		layout.addLayout(self._row)
		self._combo: DtoComboBox[ExecutorDto] = DtoComboBox(self, placeholder="Сменить…")
		self._combo.setFixedWidth(_PUBLISHER_COMBO_WIDTH)  # макет
		self._combo.currentIndexChanged.connect(self._on_selected)

	def sync(self, executors: list[ExecutorDto], caption: Callable[[ExecutorDto], str]) -> None:
		"""Показывает назначенного и обновляет список смены."""
		clear_layout(self._row)
		current = next(
			(
				executor
				for executor in executors
				if executor.owner.kind is self._kind and executor.is_default
			),
			None,
		)
		if current is None:
			self._row.addWidget(CaptionLabel("не назначен", self), stretch=1)
		else:
			self._row.addWidget(
				entity_avatar(self, current.owner.id, current.label, None, _MEMBER_AVATAR)
			)
			column = QVBoxLayout()
			column.setSpacing(2)
			name = StrongBodyLabel(self)
			name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
			elide_text(name, current.label)
			column.addWidget(name)
			column.addWidget(CaptionLabel(caption(current), self))
			self._row.addLayout(column, stretch=1)
		self._combo.setParent(self)
		self._combo.set_items(
			default_candidates(executors, self._kind),
			label=lambda executor: executor.label,
		)
		self._row.addWidget(self._combo, alignment=Qt.AlignmentFlag.AlignVCenter)

	def _on_selected(self, _index: int) -> None:
		"""Выбор в списке смены — назначение (пункт-заглушка не считается)."""
		executor = self._combo.selected()
		if executor is not None:
			self._on_change(executor)


class _MembersSection:
	"""Раздел пула: заголовок с вводом, сетка карточек, пустое состояние."""

	def __init__(
		self,
		panel: QWidget,
		layout: QVBoxLayout,
		title: str,
		empty: str,
		hint: str,
		on_add: Callable[[], None],
	) -> None:
		self._panel = panel
		self._title = title
		# блок раздела: внутри — интервал строки, между блоками — блока
		block = QVBoxLayout()
		block.setSpacing(density.spacing().row_spacing)
		layout.addLayout(block)
		layout = block
		self._header_box = QVBoxLayout()
		layout.addLayout(self._header_box)
		self.combo: DtoComboBox[Any] = DtoComboBox(panel)
		self.combo.setFixedWidth(_CANDIDATE_WIDTH)  # макет
		self.combo.setFixedHeight(LIST_BUTTON_HEIGHT)  # макет: как у кнопки ввода
		self.button = list_button("Ввести", panel)
		self.button.setToolTip(hint)
		self.button.clicked.connect(on_add)
		self.error = ErrorLabel(panel)
		layout.addWidget(self.error)
		self.grid = FlowGrid([], panel, min_width=_MEMBER_MIN_WIDTH, spacing=_MEMBER_SPACING)
		layout.addWidget(self.grid)
		self.empty = BodyLabel(empty, panel)
		layout.addWidget(self.empty)
		self._cards: list[QWidget] = []
		self.set_count(0)

	def set_count(self, count: int) -> None:
		"""Пересобирает заголовок с числом (ввод переезжает в новый)."""
		clear_layout(self._header_box)
		self.combo.setParent(self._panel)
		self.button.setParent(self._panel)
		self._header_box.addWidget(
			section_header(self._panel, self._title, count, trailing=[self.combo, self.button])
		)

	def set_cards(self, cards: list[QWidget]) -> None:
		"""Меняет карточки раздела; прежние удаляются."""
		for card in self._cards:
			card.setParent(None)
			card.deleteLater()
		self._cards = cards
		self.grid.set_cards(cards)
		self.grid.setVisible(bool(cards))
		self.empty.setVisible(not cards)


class MembersPanel(QWidget):
	"""Исполнители сообщества: публикаторы по умолчанию и пул обоих видов (ADR-0035).

	Порядок сверху вниз (макет «Участники», 1b): **публикатор
	по умолчанию** — по карточке на вид, с именем назначенного и списком
	смены; затем **Пользователи** — userbot-аккаунты (публикует
	назначенный, остальные нужны чтению, реакциям и обслуживанию)
	и **Боты** (публикатор-бот один — запасной путь и единственный, кто
	умеет кнопки под постом, ADR-0031; прочие боты состоят наравне).

	Карточка пула не раскрывается: права показывает всплывающая панель
	по кнопке-значку. Раскрытие тянуло высоту всего ряда сетки, а права
	нужны изредка. Своей прокрутки у панели нет — пул это единицы строк,
	он растёт вместе со страницей.

	Живой список: операции выполняются сразу (движком), список
	перечитывается после каждой, а владелец узнаёт об изменении
	сигналом ``changed``. Один виджет — и вкладка страницы сообщества,
	и содержимое диалога «Участники…» (его открывает дашборд).
	"""

	changed = Signal()

	def __init__(
		self,
		worker: EngineWorker,
		community: CommunityDto,
		accounts: list[TgAccountDto],
		bots: list[BotDto],
		parent: QWidget,
	) -> None:
		"""``accounts`` и ``bots`` — вошедшие аккаунты и активные боты (кандидаты)."""
		super().__init__(parent)
		self._worker = worker
		self._community = community
		self._accounts = accounts
		self._bots = bots
		self._show_error = error_reporter(self)
		self._executors: list[ExecutorDto] = []
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().block_spacing)
		self._build_publisher(layout)
		self._sections: dict[OwnerKind, _MembersSection] = {}
		for kind, title, _role, empty, hint in _MEMBER_SECTIONS:
			self._sections[kind] = _MembersSection(
				self, layout, title, empty, hint, partial(self._on_add, kind)
			)
		self.reload()

	def _build_publisher(self, layout: QVBoxLayout) -> None:
		"""Блок «Публикатор по умолчанию»: заголовок, пояснение, две карточки."""
		block = QVBoxLayout()
		block.setSpacing(density.spacing().row_spacing)
		layout.addLayout(block)
		layout = block
		layout.addWidget(section_header(self, "Публикатор по умолчанию"))
		note = CaptionLabel(
			"Им уходит пост, если при публикации не выбран другой. "
			"Публиковать может любой из пула, у кого есть права.",
			self,
		)
		note.setWordWrap(True)
		layout.addWidget(note)
		self._publishers = {
			kind: _PublisherCard(kind, role, self, self._on_set_default)
			for kind, _title, role, _empty, _hint in _MEMBER_SECTIONS
		}
		layout.addWidget(
			FlowGrid(
				list(self._publishers.values()),
				self,
				min_width=_MEMBER_MIN_WIDTH,
				spacing=_MEMBER_SPACING,
			)
		)

	def reload(self) -> None:
		"""Перечитывает пул исполнителей из движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_executors(self._community.id),
			self,
			self._show_executors,
			self._show_error,
		)

	def _show_executors(self, executors: list[ExecutorDto]) -> None:
		"""Приводит блок публикаторов, оба раздела и списки кандидатов к снимку."""
		self._executors = executors
		for card in self._publishers.values():
			card.sync(executors, self._caption)
		for kind, section in self._sections.items():
			mine = [dto for dto in executors if dto.owner.kind is kind]
			section.set_count(len(mine))
			section.set_cards([self._make_card(executor) for executor in mine])
		taken_accounts = {dto.owner.id for dto in executors if dto.owner.kind is OwnerKind.USER}
		self._sections[OwnerKind.USER].combo.set_items(
			[account for account in self._accounts if account.id not in taken_accounts],
			label=lambda acc: account_caption(acc.display, acc.phone),
			key=lambda acc: acc.id,
		)
		taken_bots = {dto.owner.id for dto in executors if dto.owner.kind is OwnerKind.BOT}
		self._sections[OwnerKind.BOT].combo.set_items(
			[bot for bot in self._bots if bot.id not in taken_bots],
			label=lambda bot: bot_caption(bot.label, bot.username),
			key=lambda bot: bot.id,
		)

	def _caption(self, executor: ExecutorDto) -> str:
		"""Подпись под именем: роль, а у бота — «@имя · роль» (имя — из списка ботов)."""
		username = None
		if executor.owner.kind is OwnerKind.BOT:
			username = next(
				(bot.username for bot in self._bots if bot.id == executor.owner.id), None
			)
		return member_caption(executor, username)

	def _make_card(self, executor: ExecutorDto) -> QWidget:
		"""Карточка исполнителя со своими тремя действиями."""
		return _MemberCard(
			executor,
			self,
			caption_text=self._caption(executor),
			on_resume=partial(self._on_resume, executor),
			on_rights=partial(self._on_rights, executor),
			on_remove=partial(self._on_remove, executor),
		)

	def _on_rights(self, executor: ExecutorDto, anchor: QWidget) -> None:
		"""Показывает права исполнителя всплывающей панелью у кнопки."""
		Flyout.make(_RightsView(executor, self.window()), target=anchor, parent=self.window())

	def _after_change(self, executors: list[ExecutorDto]) -> None:
		"""Операция прошла: перерисовать и сообщить владельцу."""
		self._show_executors(executors)
		self.changed.emit()

	def _on_add(self, kind: OwnerKind) -> None:
		"""«Ввести»: выбранный кандидат своего вида входит в пул."""
		section = self._sections[kind]
		section.error.succeed()
		item = section.combo.selected()
		if item is None:
			section.error.fail(
				"Нет свободных вошедших активных аккаунтов — войдите или возобновите: "
				"«Пользователи и боты»."
				if kind is OwnerKind.USER
				else "Нет свободных активных ботов — добавьте или возобновите: "
				"«Пользователи и боты»."
			)
			return
		if kind is OwnerKind.USER:
			self._add(ExecutorRef(kind, item.id), item.display, "Проверяю права аккаунта…")
		else:
			self._add(ExecutorRef(kind, item.id), item.label, "Проверяю права бота…")

	def _add(self, owner: ExecutorRef, label: str, note: str, invite: str | None = None) -> None:
		"""Вводит исполнителя в сообщество (ADR-0035).

		Зонд прав живой, а ввод меняет состояние в Telegram — поэтому
		человека предупреждают до и извещают после. Приватное сообщество
		без готовой ссылки возвращает исход «нужна ссылка»: тогда её
		просят и повторяют тем же путём.
		"""
		show_info(self, "Проверка", note)
		run_in_engine(
			self._worker,
			self._worker.engine.communities.add_executor(self._community.id, owner, invite),
			self,
			partial(self._after_join, owner, label),
			self._show_error,
		)

	def _after_join(self, owner: ExecutorRef, label: str, result: JoinResult) -> None:
		"""Показывает исход ввода; «нужна ссылка» — просит её и повторяет."""
		if result.outcome is JoinOutcome.NEEDS_LINK:
			self._ask_invite_link(owner, label)
			return
		self._after_change(result.executors)
		if result.outcome is JoinOutcome.REQUESTED:
			show_info(self, "Заявка отправлена", join_result_text(result, label))
		else:
			show_success(self, "Готово", join_result_text(result, label))

	def _ask_invite_link(self, owner: ExecutorRef, label: str) -> None:
		"""Спрашивает ссылку-приглашение — последняя ступень лестницы ввода."""
		dialog = FormDialog(
			"Нужна ссылка-приглашение",
			[("link", "Ссылка t.me/+…")],
			self.window(),
			accept_text="Ввести",
			note=INVITE_LINK_PROMPT,
		)
		if not exec_dialog(dialog):
			return
		link = dialog.value("link").strip()
		if not link:
			self._sections[owner.kind].error.fail("Ссылка не указана — ввести исполнителя нечем.")
			return
		self._add(owner, label, "Вступаю по ссылке…", invite=link)

	def _on_set_default(self, executor: ExecutorDto) -> None:
		"""Смена публикатора по умолчанию — существующая операция движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.set_default_publisher(
				self._community.id, executor.owner
			),
			self,
			lambda _dto: self._reload_and_notify(),
			self._show_error,
		)

	def _on_resume(self, executor: ExecutorDto) -> None:
		"""Возобновляет приостановленного исполнителя — та же операция, что в его разделе."""
		done = self._reload_and_notify
		if executor.owner.kind is OwnerKind.USER:
			run_in_engine(
				self._worker,
				self._worker.engine.accounts.get_tg_account(executor.owner.id),
				self,
				lambda account: set_user_paused(self._worker, self, account, False, done),
				self._show_error,
			)
		else:
			run_in_engine(
				self._worker,
				self._worker.engine.accounts.get_bot(executor.owner.id),
				self,
				lambda bot: set_bot_paused(self._worker, self, bot, False, done),
				self._show_error,
			)

	def _reload_and_notify(self) -> None:
		self.reload()
		self.changed.emit()

	def _on_remove(self, executor: ExecutorDto) -> None:
		if not confirm_delete(
			self,
			remove_executor_text(executor, self._community),
			accept_text="Убрать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.communities.remove_executor(self._community.id, executor.owner),
			self,
			self._after_change,
			self._show_error,
		)


class _MembersDialog(WorkDialog):
	"""Диалог «Участники…» (с дашборда): та же панель, что во вкладке."""

	def __init__(
		self,
		worker: EngineWorker,
		community: CommunityDto,
		accounts: list[TgAccountDto],
		bots: list[BotDto],
		parent: QWidget,
	) -> None:
		super().__init__(f"Участники — {community.title}", parent, size=(560, 600))
		# во вкладке панель прокручивает страница, здесь страницы нет —
		# без прокрутки длинный пул вышел бы за окно (спека, раздел 7)
		area = ScrollArea(self)
		area.setWidget(MembersPanel(worker, community, accounts, bots, area))
		area.setWidgetResizable(True)
		area.enableTransparentBackground()
		self.content.addWidget(area, stretch=1)
		self.add_close_button("Готово")


def open_members(
	worker: EngineWorker,
	community: CommunityDto,
	parent: QWidget,
	on_closed: Callable[[], None],
) -> None:
	"""Открывает диалог исполнителей сообщества (ADR-0022).

	Точка входа для кнопки «Назначить публикатора» на дашборде.
	Кандидаты — вошедшие userbot-аккаунты и активные боты, оба списка
	читаются из движка перед показом; ``on_closed`` зовётся после
	закрытия — вызывающий перечитывает своё состояние.
	"""

	def _open(accounts: list[TgAccountDto], bots: list[BotDto]) -> None:
		exec_dialog(_MembersDialog(worker, community, accounts, bots, parent.window()))
		on_closed()

	read_executors(worker, parent, _open, error_reporter(parent))


class _CommunityPrefsDialog(MessageBoxBase):
	"""Настройки сообщества: пресет видео по умолчанию и времена публикации."""

	_TIMES_HINT = "Через запятую, первое — по умолчанию; пусто — без стандартных."

	def __init__(
		self,
		community_title: str,
		presets: list[PresetDto],
		current_id: int | None,
		times: list[str],
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Настройки", self))
		self.viewLayout.addWidget(BodyLabel(f"«{community_title}»", self))
		self.viewLayout.addWidget(BodyLabel("Пресет видео по умолчанию:", self))
		self._combo: DtoComboBox[PresetDto] = DtoComboBox(self, placeholder="(не задан)")
		self._combo.set_items(presets, label=lambda preset: preset.name)
		if current_id is not None:
			self._combo.select(lambda preset: preset.id == current_id)
		self.viewLayout.addWidget(self._combo)
		self.viewLayout.addWidget(BodyLabel("Времена публикации (ЧЧ:ММ):", self))
		self._times_edit = LineEdit(self)
		self._times_edit.setPlaceholderText("10:00, 18:30…")
		self._times_edit.setText(", ".join(str(t) for t in times))
		self.viewLayout.addWidget(self._times_edit)
		self._times_hint = CaptionLabel(self._TIMES_HINT, self)
		self.viewLayout.addWidget(self._times_hint)
		# ошибка валидации — единой красной подписью (как у всех диалогов),
		# подсказка о формате при этом остаётся на месте
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("Сохранить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def validate(self) -> bool:  # noqa: N802 — API MessageBoxBase
		"""Не даёт сохранить времена в неверном формате (диалог открыт)."""
		try:
			self.times()
		except ValueError as exc:
			return self._error.fail(str(exc))
		return self._error.succeed()

	def preset_id(self) -> int | None:
		"""Идентификатор выбранного пресета (None — «не задан»)."""
		preset = self._combo.selected()
		return preset.id if preset is not None else None

	def times(self) -> list[str]:
		"""Времена публикации из поля — нормализованные «ЧЧ:ММ».

		Raises:
			ValueError: Какое-то из времён не в формате «ЧЧ:ММ».
		"""
		raw = str(self._times_edit.text()).strip()
		if not raw:
			return []
		result = []
		for token in raw.split(","):
			hours, minutes = parse_hhmm(token)
			result.append(f"{hours:02d}:{minutes:02d}")
		return result


# --- вкладки ------------------------------------------------------------------------


class _QueueTab(QWidget):
	"""Вкладка «Очередь»: очередь отправки этого сообщества, ближайшие сначала.

	Та же панель, что на «Публикации»: карточки с прогрессом, «Отмена»
	у живых, «Повторить»/«Убрать» у ошибок, правка поста в раскрытой
	карточке. Из подписи карточки убрано название сообщества — оно
	в шапке страницы.
	"""

	counts_changed = Signal(object)  # QueueCounts
	#: «Вся очередь…» — экран «Очередь» раздела «Публикация» с фильтром.
	view_all_requested = Signal()

	def __init__(
		self, worker: EngineWorker, watcher: QueueWatcher, community: CommunityDto, parent: QWidget
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._community = community
		self._all: list[QueueItemDto] = []  # вся очередь сообщества (до нарезки)
		self._page = 1
		self._view: ListPage[QueueItemDto] = paginate([], 1, QUEUE_TAB_PAGE_SIZE)
		self._last_count = -1  # число в заголовке перестраивается только при смене
		spacing = density.spacing()
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(spacing.row_spacing)
		self._retry_button = list_button("Повторить ошибки", self)
		self._retry_button.setToolTip("Вернуть в очередь все элементы с ошибкой разом")
		self._retry_button.clicked.connect(self._on_retry_errors)
		self._retry_button.hide()
		view_button = list_button("Вся очередь…", self)
		view_button.setToolTip(
			"Вся очередь отправки на экране «Очередь» с фильтром по этому сообществу"
		)
		view_button.clicked.connect(self._on_view_all)
		self._header_box = QVBoxLayout()
		layout.addLayout(self._header_box)
		self._header_trailing = [self._retry_button, view_button]
		self._render_header(0)
		self._empty = BodyLabel(
			"В очереди ничего нет. Создайте пост на «Новом посте» — кнопка "
			"«Опубликовать» в шапке ведёт туда с этим сообществом.",
			self,
		)
		self._empty.setWordWrap(True)
		layout.addWidget(self._empty)
		queue_box = QVBoxLayout()
		queue_box.setSpacing(spacing.list_spacing)
		layout.addLayout(queue_box)
		# итог и перелистывание — общие с окном «Вся очередь…»
		self._pager = PagerRow(self, self._step, compact=True)
		layout.addLayout(self._pager.layout)
		layout.addStretch()
		self._panel = QueuePanel(
			self,
			queue_box,
			watcher=watcher,
			subtitle=lambda item: queue_subtitle(item, with_community=False),
			transform=self._only_this_community,
			on_refreshed=self._on_refreshed,
			editable=lambda item: item.status in EDITABLE_STATUSES,
			fill_body=self._fill_editor,
			leading=self._leading,
			leading_signature=lambda item: (item.when,),  # только метка слота
			compact=True,
			active=False,  # присоединит страница, когда вкладка станет видна
		)

	def set_active(self, active: bool) -> None:
		"""Карточки обновляются, только пока вкладка видна."""
		self._panel.set_active(active)

	def _render_header(self, count: int) -> None:
		"""Заголовок-хайрлайн с числом и кнопками (перестраивается по числу)."""
		clear_layout(self._header_box)
		for widget in self._header_trailing:
			widget.setParent(self)
		self._header_box.addWidget(
			section_header(self, "Очередь отправки", count, trailing=self._header_trailing)
		)

	def _only_this_community(self, items: list[QueueItemDto]) -> list[QueueItemDto]:
		"""Правило показа: только это сообщество, ближайшие сначала, страницами.

		Крючок панели: нарезка на страницы — та же, что в окне «Вся
		очередь…» (зажатый номер возвращается: очередь живая, и страница
		под человеком может исчезнуть). Полный список сообщества
		запоминается для чисел шапки и «Повторить ошибки».
		"""
		self._all = apply_view(items, QueueSort.NEAREST, QueueFilter.ALL, self._community.id)
		self._view = paginate(self._all, self._page, QUEUE_TAB_PAGE_SIZE)
		self._page = self._view.page
		return self._view.items

	def _step(self, delta: int) -> None:
		"""Листает страницу — из кэша наблюдателя, в движок не ходим."""
		self._page = step_page(self._page, delta, self._view.pages)
		self._panel.refresh()

	def _on_refreshed(self, _shown: list[QueueItemDto]) -> None:
		"""После опроса: число в заголовке, кнопка повтора, пустое состояние, итог.

		Числа — по всей очереди сообщества, а не по странице: ошибка
		на третьей странице всё равно ошибка.
		"""
		total = len(self._all)
		if total != self._last_count:
			self._render_header(total)
			self._last_count = total
		errors = sum(1 for item in self._all if item.status is JobStatus.ERROR)
		self._retry_button.setVisible(errors > 0)
		self._empty.setVisible(not self._all)
		self._pager.update(self._view, queue_footer_text(self._view))
		self.counts_changed.emit(community_queue_counts(self._all, self._community.id))

	def _on_retry_errors(self) -> None:
		"""«Повторить ошибки»: то же, что кнопка на каждой карточке, для всех."""
		for item in self._all:
			if item.status is JobStatus.ERROR:
				self._panel.retry(item.id)

	def _on_view_all(self) -> None:
		self.view_all_requested.emit()

	def _fill_editor(self, item_id: int, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет раскрытую карточку формой правки (ADR-0016, п. 7)."""
		mount_queue_item_editor(self._worker, self, item_id, body, collapse, self._panel.poll)

	@staticmethod
	def _leading(item: QueueItemDto, parent: QWidget) -> list[QWidget]:
		"""Начало шапки карточки: только метка слота — логотип здесь лишний."""
		return [slot_chip(item.when, parent, compact=True)]


class _ScheduledTab(QWidget):
	"""Вкладка «Отложено»: отложенные записи сообщества из Telegram.

	Те же данные и та же панель, что на экране «Отложено» (правка, «Сейчас»,
	«Удалить» — :class:`ScheduledPanel`), но по одному сообществу
	и в компактных карточках; истина — сам Telegram (ADR-0010), список
	читается при первом открытии вкладки и кнопкой «Обновить».
	"""

	count_changed = Signal(int)

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._community = community
		spacing = density.spacing()
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(spacing.row_spacing)
		self._refresh_button = list_button("Обновить", self)
		self._refresh_button.clicked.connect(self.reload)
		self._header_box = QVBoxLayout()
		layout.addLayout(self._header_box)
		self._render_header(0)
		self._status = CaptionLabel("", self)
		self._status.setWordWrap(True)
		layout.addWidget(self._status)
		box = QVBoxLayout()
		box.setSpacing(spacing.list_spacing)
		layout.addLayout(box)
		layout.addStretch()
		self._panel = ScheduledPanel(
			worker,
			self,
			box,
			subtitle=lambda item: scheduled_subtitle(item, with_community=False),
			community_id=community.id,
			transform=lambda items: sorted(items, key=lambda item: item.scheduled_at),
			on_loading=lambda: self._status.setText("Читаю отложенные из Telegram…"),
			on_loaded=self._on_loaded,
			# только метка слота — логотип сообщества здесь лишний
			leading=lambda item, parent: [slot_chip(item.scheduled_at, parent, compact=True)],
			leading_signature=lambda item: (item.scheduled_at,),
			compact=True,
		)

	def _render_header(self, count: int) -> None:
		clear_layout(self._header_box)
		self._refresh_button.setParent(self)
		self._header_box.addWidget(
			section_header(self, "Отложено в Telegram", count, trailing=[self._refresh_button])
		)

	def reload(self) -> None:
		"""Перечитывает отложенные записи сообщества (обход не дублируется)."""
		self._panel.reload()

	def _on_loaded(self, scheduled: ScheduledList) -> None:
		count = len(scheduled.items)
		self._render_header(count)
		self.count_changed.emit(count)
		if scheduled.unread:
			# честность важнее краткости: прочитать не удалось — список
			# заведомо неполон (истина живёт на сервере Telegram)
			self._status.setText("Не удалось прочитать отложенные записи этого сообщества.")
		elif not scheduled.items:
			self._status.setText(
				"Отложенных записей нет. Создайте пост на «Новом посте» "
				"с временем публикации — его сохранит сервер Telegram."
			)
		else:
			self._status.setText("")


# --- страница -------------------------------------------------------------------------


class _SettingsTab(QWidget):
	"""Тело вкладки «Настройки»: обзор и экраны правки в той же вкладке.

	Виды в стопке, как у «Задач»: обзор (строки настроек — их рисует
	страница в :attr:`rows` и :attr:`tail` — и список пресетов подписи
	между ними) и экраны правки со строкой пути «Настройки › …»: экран
	пресета подписи (ADR-0042) и настройки сообщества в Telegram
	(ADR-0043). Уход с экрана с правками — через вопрос (:meth:`leave`,
	признак ``UnsavedChanges``).

	Сигналы экрана настроек в Telegram — наружу, странице:
	``members_requested`` (править некому — на «Участники»)
	и ``community_changed`` (сохранение изменило сообщество).
	"""

	members_requested = Signal()
	community_changed = Signal()

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._show_error = error_reporter(self)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self._stack = QStackedWidget(self)
		layout.addWidget(self._stack)
		self._overview = self._build_overview(worker, community)
		self._editor = PresetEditor(worker, community, self)
		self._editor.presets_changed.connect(self._presets.reload)
		self._editor.closed.connect(partial(self._show_page, self._overview))
		self._telegram = TelegramSettingsScreen(worker, community, self)
		self._telegram.members_requested.connect(self.members_requested)
		self._telegram.community_changed.connect(self.community_changed)
		for page in (self._overview, self._editor, self._telegram):
			self._stack.addWidget(page)
		for screen in (self._editor, self._telegram):
			screen.back_requested.connect(self.show_overview)
		self._show_page(self._overview)

	def _build_overview(self, worker: EngineWorker, community: CommunityDto) -> QWidget:
		"""Обзор: строки страницы, пресеты подписи, хвост с удалением."""
		overview = QWidget(self)
		column = QVBoxLayout(overview)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(density.spacing().block_spacing)
		self.rows = self._sub_layout(column)
		self._presets = PresetList(worker, community, overview)
		self._presets.open_requested.connect(self.open_preset)
		column.addWidget(self._presets)
		self.tail = self._sub_layout(column)
		column.addStretch()
		return overview

	@staticmethod
	def _sub_layout(column: QVBoxLayout) -> QVBoxLayout:
		box = QVBoxLayout()
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().list_spacing)
		column.addLayout(box)
		return box

	def _show_page(self, page: QWidget) -> None:
		"""Показывает вид стопки; скрытые не участвуют в её высоте.

		Штатный ``QStackedWidget`` меряет все страницы разом (как
		в «Задачах»): под коротким обзором оставалась бы пустота высотой
		в самый длинный экран правки.
		"""
		for widget in (self._overview, self._editor, self._telegram):
			policy = QSizePolicy.Policy.Preferred if widget is page else QSizePolicy.Policy.Ignored
			widget.setSizePolicy(policy, policy)
		self._stack.setCurrentWidget(page)

	def set_active(self, active: bool) -> None:
		"""Вкладка показана — пресеты перечитываются."""
		if active:
			self._presets.reload()

	def update_community(self, community: CommunityDto) -> None:
		"""Свежий снимок сообщества (имя канала — в предпросмотре имени файла)."""
		self._presets.community = community
		self._editor.community = community
		self._telegram.community = community

	def open_preset(self, preset: CaptionPresetDto | None) -> None:
		"""Открывает экран пресета (None — новый) с полями сообщества."""
		others = self._presets.presets
		run_in_engine(
			self._worker,
			self._worker.engine.captions.list_fields(self._presets.community.id),
			self,
			partial(self._show_editor, preset, others),
			self._show_error,
		)

	def _show_editor(
		self,
		preset: CaptionPresetDto | None,
		others: list[CaptionPresetDto],
		pool: list[FieldDto],
	) -> None:
		self._editor.open(preset, pool, others)
		self._show_page(self._editor)

	def open_telegram(self) -> None:
		"""Открывает экран настроек сообщества в Telegram (читает их заново)."""
		self._telegram.open()
		self._show_page(self._telegram)

	def _screen(self) -> PresetEditor | TelegramSettingsScreen | None:
		"""Открытый экран правки (None — показан обзор)."""
		current = self._stack.currentWidget()
		if current is self._editor or current is self._telegram:
			return current
		return None

	def show_overview(self) -> None:
		"""Путь «Настройки»: к обзору; с правками — через вопрос."""
		screen = self._screen()
		stay = screen.render_path if screen is not None else None
		self.leave(partial(self._show_page, self._overview), stay=stay)

	@property
	def dirty(self) -> bool:
		"""Есть ли на открытом экране правки несохранённые правки."""
		screen = self._screen()
		return screen is not None and screen.dirty

	def leave(self, then: Callable[[], None], *, stay: Callable[[], None] | None = None) -> None:
		"""Уход со вкладки: правки открытого экрана — только через вопрос."""
		screen = self._screen()
		if screen is None or not screen.dirty:
			then()
			return
		screen.leave(then, stay=stay)


class CommunityPage(ScrollArea):
	"""Страница сообщества: шапка, вкладки, всё действующее — по вкладкам.

	Сигналы: ``changed`` — после каждой операции, меняющей данные
	(главное окно обновляет дашборд и подменю); ``publish_requested`` —
	«Опубликовать» в шапке (главное окно открывает «Публикацию»
	с этим сообществом).
	"""

	changed = Signal()
	publish_requested = Signal(int)
	#: «Вся очередь…» вкладки — экран «Очередь» с фильтром по этому сообществу.
	queue_requested = Signal(int)
	#: Клик по строке пути: вернуться на дашборд. Значение — вид
	#: сообщества (его раздел) или None (раздел «все»).
	dashboard_requested = Signal(object)  # CommunityKind | None

	def __init__(
		self,
		worker: EngineWorker,
		watchers: QueueWatchers,
		community: CommunityDto,
		parent: QWidget | None = None,
	) -> None:
		"""``watchers`` — наблюдатели очередей при главном окне (ADR-0034):
		вкладке «Очередь» нужен наблюдатель отправки, «Задачам» —
		задач."""
		super().__init__(parent)
		self.setObjectName(COMMUNITY_PAGE_ROUTE)
		self._worker = worker
		self._watchers = watchers
		self._community = community
		self._show_error = error_reporter(self)
		self._counts = QueueCounts()
		self._stats: CommunityStatsDto | None = None
		self._scheduled_count: int | None = None
		# итог последней проверки доступов — живёт, пока открыто приложение
		self._recheck_text: str | None = None
		self._tabs: dict[str, QWidget] = {}
		self._current_tab = TAB_OVERVIEW
		# сборка строки пути шлёт тот же сигнал, что и клик по ней
		self._building_path = False
		self._build()
		self._render_path()
		self._render_header()
		self._render_settings()
		# числа очереди в шапке — из кэша наблюдателя по его уведомлениям
		# (ADR-0034): страница не запрашивает очередь при показе
		watchers.publish.attach(self, QueueView(on_state=self._on_queue_state))

	@property
	def community_id(self) -> int:
		"""Идентификатор сообщества этой страницы."""
		return self._community.id

	def show_community(self, community: CommunityDto) -> None:
		"""Показывает сообщество на этой странице (ADR-0041, п. 4).

		То же сообщество — обычное обновление снимком. Другое — тела
		вкладок снимаются целиком (они собраны по прежнему сообществу,
		а панели очередей держат его карточки), страница открывается
		на «Обзоре», числа очереди и статистики сбрасываются: чужие
		они показывать не должны ни мгновения.
		"""
		if community.id == self._community.id:
			self.update_community(community)
			return
		self._community = community
		self._counts = QueueCounts()
		self._stats = None
		self._scheduled_count = None
		self._recheck_text = None
		self._drop_all_tabs()
		self._mount_tab(TAB_SETTINGS)  # его строки рисует _render_settings
		self._render_path()
		self._render_header()
		self._render_settings()
		self._segments.setCurrentItem(TAB_OVERVIEW)
		self._show_tab(TAB_OVERVIEW)

	def _drop_all_tabs(self) -> None:
		"""Снимает тела всех вкладок вместе с их подписками на наблюдателей."""
		for key in list(self._tabs):
			body = self._tabs.pop(key)
			_set_active(body, False)
			self._body.removeWidget(body)
			body.setParent(None)
			body.deleteLater()
		self._current_tab = TAB_OVERVIEW

	def update_community(self, community: CommunityDto) -> None:
		"""Обновляет страницу свежим снимком (синхронизация главного окна).

		Тела вкладок строятся один раз и живут до конца сеанса, а часть
		из них сложена по снимку: «Задачи» решают по нему, есть ли
		userbot-публикатор, «Участники» держат список аккаунтов на момент
		сборки. Назначили публикатора на «Участниках» — «Задачи»
		до перезапуска твердило бы, что его нет; отвязали — наоборот,
		осталось бы рабочим. Поэтому такие тела снимаются: следующее
		открытие соберёт их по свежему снимку. Видимая вкладка
		пересобирается сразу, иначе человек смотрел бы на устаревшее.
		"""
		self._community = community
		self._render_path()
		self._render_header()
		self._render_settings()
		self._render_tab_titles()
		overview = self._tabs.get(TAB_OVERVIEW)
		if isinstance(overview, OverviewTab):
			overview.update_community(community)
		settings = self._tabs.get(TAB_SETTINGS)
		if isinstance(settings, _SettingsTab):
			settings.update_community(community)
		self._drop_snapshot_tabs()

	def _drop_snapshot_tabs(self) -> None:
		"""Снимает тела вкладок, сложенных по прежнему снимку сообщества."""
		for key in _SNAPSHOT_TABS:
			body = self._tabs.get(key)
			if body is None:
				continue
			if isinstance(body, UnsavedChanges) and body.dirty:
				# открыта настройка с правками: пересборка стёрла бы
				# их молча; тело пересоберётся при следующем снимке
				continue
			self._tabs.pop(key)
			_set_active(body, False)
			self._body.removeWidget(body)
			body.deleteLater()
			if key == self._current_tab:
				fresh = self._mount_tab(key)
				self._body.addWidget(fresh)
				fresh.show()
				_set_active(fresh, True)

	# --- сборка -----------------------------------------------------------------

	def _build(self) -> None:
		"""Каркас: строка пути, шапка, сегменты вкладок, стопка тел."""
		layout = page_layout(self)
		# путь и шапка — одним блоком: между ними интервал строки,
		# а не блока (navigation.md, раздел 5)
		top = QVBoxLayout()
		top.setSpacing(density.spacing().row_spacing)
		self._path = BreadcrumbBar(self)
		self._path.currentItemChanged.connect(self._on_path_clicked)
		top.addWidget(self._path, alignment=Qt.AlignmentFlag.AlignLeft)
		self._header_box = QVBoxLayout()
		top.addLayout(self._header_box)
		layout.addLayout(top)
		# полоса вкладок — общая с разделами приложения (подписи с полосой
		# под активной, разделитель под всей полосой, как в макете)
		self._segments = tab_strip(self, layout)
		# тело вкладки — единственный виджет в этой компоновке: скрытые
		# вкладки в ней не живут, и высота страницы считается по видимой.
		# Штатный QStackedWidget мерит все страницы разом — длинная
		# очередь растягивала бы и «Обзор», и «Настройки»
		self._body = QVBoxLayout()
		self._body.setContentsMargins(0, 0, 0, 0)
		layout.addLayout(self._body, stretch=1)
		self._tab_items: dict[str, TabItem] = {}
		for key in _TABS:
			item = TabItem(tab_title(key), self._segments)
			self._tab_items[key] = item
			# без обработчика клика: библиотека зовёт его с флагом, которого
			# обработчик не ждёт, а переключение и так идёт по currentItemChanged
			self._segments.addWidget(key, item)
		self._segments.currentItemChanged.connect(self._on_tab_requested)
		self._mount_tab(TAB_SETTINGS)
		self._segments.setCurrentItem(TAB_OVERVIEW)
		self._show_tab(TAB_OVERVIEW)

	def _render_path(self) -> None:
		"""Строка пути: дашборд › раздел вида › название сообщества.

		Собирается заново на каждое сообщество: клик по элементу пути
		снимает всё, что правее него (так устроен ``BreadcrumbBar``).
		"""
		community = self._community
		self._building_path = True
		try:
			self._path.clear()
			self._path.addItem(_PATH_ROOT, "Каналы и группы")
			self._path.addItem(_PATH_GROUP, community_group_title(community.kind))
			self._path.addItem(_PATH_COMMUNITY, community.title)
		finally:
			self._building_path = False

	def _on_path_clicked(self, route_key: str) -> None:
		"""Клик по строке пути: дашборд целиком или его раздел."""
		if self._building_path or route_key == _PATH_COMMUNITY:
			return
		self.dashboard_requested.emit(None if route_key == _PATH_ROOT else self._community.kind)

	def _render_header(self) -> None:
		"""Шапка: логотип, название с плашкой, подстрочник, «Опубликовать», «…»."""
		clear_layout(self._header_box)
		community = self._community
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(14)
		row.setAlignment(Qt.AlignmentFlag.AlignTop)
		avatar = self._stats.avatar_path if self._stats is not None else None
		row.addWidget(
			entity_avatar(box, community.id, community.title, avatar, _HEADER_LOGO_SIZE),
			alignment=Qt.AlignmentFlag.AlignTop,
		)
		column = QVBoxLayout()
		column.setSpacing(4)
		title_row = QHBoxLayout()
		title_row.setSpacing(10)
		title = TitleLabel(box)
		# «занимай, что дадут», но не больше ширины своего текста: тогда
		# плашка встаёт сразу за названием, а длинное название сокращается
		# многоточием, а не выталкивает плашку к кнопкам
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		title.setMaximumWidth(title.fontMetrics().horizontalAdvance(community.title) + 8)
		elide_text(title, community.title)
		# коэффициент растяжения обязателен: без него «занимай, что дадут»
		# получает ноль — всё свободное место уходит растяжке в конце
		title_row.addWidget(title, stretch=1)
		state, text = header_state_text(community, self._counts)
		title_row.addWidget(state_badge(box, state, text))
		title_row.addStretch()
		column.addLayout(title_row)
		participants = self._stats.participants if self._stats is not None else None
		subtitle = CaptionLabel(box)
		subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(
			subtitle,
			f"{community_kind_caption(community)} · {subtitle_text(community, participants)}",
		)
		column.addWidget(subtitle)
		row.addLayout(column, stretch=1)
		publish = PrimaryPushButton("Опубликовать", box)
		publish.setToolTip("Открыть «Публикацию» с этим сообществом")
		publish.clicked.connect(lambda: self.publish_requested.emit(community.id))
		row.addWidget(publish, alignment=Qt.AlignmentFlag.AlignTop)
		more = TransparentToolButton(FluentIcon.MORE, box)
		more.setToolTip("Проверить доступы, обслуживание, удаление")
		more.clicked.connect(partial(self._show_menu, more))
		row.addWidget(more, alignment=Qt.AlignmentFlag.AlignTop)
		self._header_box.addWidget(box)

	def _show_menu(self, anchor: QWidget) -> None:
		"""Меню «…»: редкие и необратимые действия шапки."""
		menu = RoundMenu(parent=self)
		recheck = Action("Проверить доступы", menu)
		recheck.triggered.connect(self._recheck)
		menu.addAction(recheck)
		tasks = Action("Задачи…", menu)
		tasks.setEnabled(self._community.userbot_assigned)
		if not self._community.userbot_assigned:
			tasks.setToolTip(TASKS_UNAVAILABLE)
		tasks.triggered.connect(self._on_open_tasks)
		menu.addAction(tasks)
		menu.addSeparator()
		delete = Action(FluentIcon.DELETE, "Удалить из приложения…", menu)
		delete.triggered.connect(self._on_delete)
		menu.addAction(delete)
		menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

	def _render_tab_titles(self) -> None:
		"""Числа-пилюли рядом с подписями вкладок (у активной — акцентом)."""
		counts = {
			TAB_QUEUE: self._counts.planned + self._counts.errors,
			TAB_SCHEDULED: self._scheduled_count,
			TAB_MEMBERS: executors_count(self._community),
		}
		for key, item in self._tab_items.items():
			item.set_count(counts.get(key), active=key == self._current_tab)

	# --- вкладки ------------------------------------------------------------------

	def leave(self, then: Callable[[], None]) -> None:
		"""Уход со страницы на другое сообщество: правки вкладок — через вопрос.

		Смена сообщества снимает тела вкладок, и открытая настройка
		задачи или пресета с правками пропала бы молча (спека «Задачи»,
		раздел 4.4). Тел с правками может быть несколько — вопрос
		задаётся по очереди, уход — после последнего ответа.
		"""
		pending = [
			body for body in self._tabs.values() if isinstance(body, UnsavedChanges) and body.dirty
		]
		self._leave_each(pending, then)

	def _leave_each(self, pending: Sequence[UnsavedChanges], then: Callable[[], None]) -> None:
		"""Уводит с тел по очереди; «Отмена» на любом останавливает уход."""
		if not pending:
			then()
			return
		pending[0].leave(partial(self._leave_each, pending[1:], then))

	def _on_tab_requested(self, key: str) -> None:
		"""Клик по вкладке: из тела с правками — только через вопрос.

		Переключатель к этому мгновению уже стоит на новой вкладке —
		его возвращают на место до ответа, а переключают заново, когда
		правки сохранены или отброшены; «Отмена» оставляет всё как было.
		"""
		leaving = self._tabs.get(self._current_tab)
		if key != self._current_tab and isinstance(leaving, UnsavedChanges) and leaving.dirty:
			self._segments.setCurrentItem(self._current_tab)
			leaving.leave(partial(self._segments.setCurrentItem, key))
			return
		self._show_tab(key)

	def _show_tab(self, key: str) -> None:
		"""Показывает вкладку; тело строится при первом открытии."""
		if key not in _TABS:
			return
		previous = self._tabs.get(self._current_tab)
		self._current_tab = key
		self._render_tab_titles()  # пилюля активной вкладки — акцентом
		body = self._mount_tab(key)
		if previous is body:
			_set_active(body, True)
			return
		if previous is not None:
			_set_active(previous, False)
			self._body.removeWidget(previous)
			previous.hide()
		self._body.addWidget(body)
		body.show()
		_set_active(body, True)

	def _mount_tab(self, key: str) -> QWidget:
		"""Тело вкладки: строится при первом обращении, дальше — из памяти.

		Вне показа тело скрыто и в компоновке не участвует (ни в высоте,
		ни в обновлении карточек); его состояние — набранный текст, страница
		очереди — переживает переключения.
		"""
		body = self._tabs.get(key)
		if body is None:
			body = self._build_tab(key)
			body.hide()
			self._tabs[key] = body
		return body

	def _build_tab(self, key: str) -> QWidget:
		"""Фабрика тел вкладок."""
		if key == TAB_QUEUE:
			tab = _QueueTab(self._worker, self._watchers.publish, self._community, self)
			tab.counts_changed.connect(self._on_queue_counts)
			tab.view_all_requested.connect(lambda: self.queue_requested.emit(self._community.id))
			return tab
		if key == TAB_SCHEDULED:
			scheduled = _ScheduledTab(self._worker, self._community, self)
			scheduled.count_changed.connect(self._on_scheduled_count)
			scheduled.reload()
			return scheduled
		if key == TAB_MEMBERS:
			return self._members_tab()
		if key == TAB_TASKS:
			return self._tasks_tab()
		if key == TAB_SETTINGS:
			settings = _SettingsTab(self._worker, self._community, self)
			settings.members_requested.connect(partial(self._segments.setCurrentItem, TAB_MEMBERS))
			settings.community_changed.connect(self._refresh)
			self._settings_rows = settings.rows
			self._settings_tail = settings.tail
			return settings
		return OverviewTab(self._worker, self._community, self)

	def _members_tab(self) -> QWidget:
		"""Вкладка «Участники»: панель строится после чтения кандидатов."""
		holder = QWidget(self)
		layout = QVBoxLayout(holder)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.addWidget(CaptionLabel("Читаю аккаунты…", holder))

		def mount(accounts: list[TgAccountDto], bots: list[BotDto]) -> None:
			clear_layout(layout)
			panel = MembersPanel(self._worker, self._community, accounts, bots, holder)
			panel.changed.connect(self._refresh)
			layout.addWidget(panel, stretch=1)

		read_executors(self._worker, self, mount, self._show_error)
		return holder

	def _tasks_tab(self) -> QWidget:
		"""Вкладка «Задачи»: панель или объяснение, почему нельзя."""
		if self._community.userbot_assigned:
			panel = TasksPanel(self._worker, self._watchers.tasks, self._community, self)
			# ошибку запуска чинят правами и пулом — там же, где они видны
			panel.fix_requested.connect(self._show_fix_tab)
			return panel
		box = QWidget(self)
		layout = QVBoxLayout(box)
		layout.setContentsMargins(0, 24, 0, 0)
		layout.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(
			"Задачи доступны только сообществу с userbot-публикатором: "
			"боту недоступны история ленты и список участников.",
			box,
		)
		hint.setWordWrap(True)
		layout.addWidget(hint)
		go = PushButton("Участники — назначить публикатора", box)
		go.clicked.connect(partial(self._segments.setCurrentItem, TAB_MEMBERS))
		layout.addWidget(go, alignment=Qt.AlignmentFlag.AlignLeft)
		layout.addStretch()
		return box

	def _show_fix_tab(self, target: str) -> None:
		"""Переход из строки ошибки задачи на вкладку, где её чинят."""
		if target == TaskFixTarget.MEMBERS:
			self._segments.setCurrentItem(TAB_MEMBERS)

	def _on_queue_counts(self, counts: QueueCounts) -> None:
		"""Панель очереди сообщила свежие числа — шапка и вкладка."""
		if counts != self._counts:
			self._counts = counts
			self._render_header()
			self._render_tab_titles()

	def _on_scheduled_count(self, count: int) -> None:
		if count != self._scheduled_count:
			self._scheduled_count = count
			self._render_tab_titles()

	# --- показ страницы: чтение кэшей ---------------------------------------------

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Читает кэш статистики и оживляет вкладку.

		Очередь не запрашивается: страница — постоянный зритель
		наблюдателя, и числа шапки уже свежие.
		"""
		super().showEvent(event)
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._on_stats,
			self._show_error,
		)
		_set_active(self._tabs.get(self._current_tab), True)

	def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 — API Qt
		"""Невидимая страница карточки не обновляет и снимки не читает."""
		super().hideEvent(event)
		_set_active(self._tabs.get(self._current_tab), False)

	def _on_queue_state(self, items: list[QueueItemDto]) -> None:
		self._on_queue_counts(community_queue_counts(items, self._community.id))

	def _on_stats(self, stats: list[CommunityStatsDto]) -> None:
		mine = next((item for item in stats if item.community_id == self._community.id), None)
		self._stats = mine
		if mine is not None and self._scheduled_count is None:
			self._scheduled_count = mine.scheduled_count
		self._render_header()
		self._render_tab_titles()

	# --- вкладка «Настройки» ----------------------------------------------------------

	def _render_settings(self) -> None:
		"""Строки настроек, ниже — пресеты подписи и — за хайрлайном — удаление.

		Список пресетов живёт между строками и хвостом постоянно (его
		рисует тело вкладки), а строки пересобираются по снимку.
		"""
		if not hasattr(self, "_settings_rows"):
			return
		rows = self._settings_rows
		clear_layout(rows)
		rows.addWidget(self._enabled_row())
		rows.addWidget(self._telegram_row())
		rows.addWidget(self._prefs_row())
		rows.addWidget(self._recheck_row())
		tail = self._settings_tail
		clear_layout(tail)
		tail.addSpacing(density.spacing().row_spacing)
		tail.addWidget(HorizontalSeparator(self))
		tail.addSpacing(density.spacing().row_spacing)
		tail.addWidget(self._delete_row())

	def _action_row(self, text: str, actions: list[QWidget], hint: str | None = None) -> QWidget:
		"""Строка настроек: состояние сверху, пояснение снизу, действия справа."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 2, 0, 2)
		column = QVBoxLayout()
		column.setSpacing(2)
		label = BodyLabel(text, box)
		label.setWordWrap(True)
		column.addWidget(label)
		if hint:
			caption = CaptionLabel(hint, box)
			caption.setWordWrap(True)
			column.addWidget(caption)
		row.addLayout(column, stretch=1)
		for widget in actions:
			row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignTop)
		return box

	def _enabled_row(self) -> QWidget:
		"""Активность: участие в публикации и опросе расписания."""
		switch = SwitchButton(self)
		switch.setChecked(self._community.enabled)
		switch.setToolTip("Активно: участвует в публикации и опросе расписания")
		switch.checkedChanged.connect(self._on_toggle_enabled)
		return self._action_row(
			"Активность", [switch], "Участвует в публикации и в опросе расписания"
		)

	def _telegram_row(self) -> QWidget:
		"""Настройки самого сообщества в Telegram (ADR-0043) — отдельным экраном."""
		action = PushButton("Изменить…", self)
		action.setToolTip("Название, описание, фото, доступ, реакции, разрешения участников")
		action.clicked.connect(self._on_open_telegram)
		return self._action_row(
			"Настройки в Telegram",
			[action],
			"То, что меняется в официальном клиенте, — без захода в него",
		)

	def _on_open_telegram(self) -> None:
		settings = self._tabs.get(TAB_SETTINGS)
		if isinstance(settings, _SettingsTab):
			settings.open_telegram()

	def _prefs_row(self) -> QWidget:
		"""Настройки публикации: пресет видео и времена."""
		action = PushButton("Изменить…", self)
		action.setToolTip("Пресет видео по умолчанию и времена публикации")
		action.clicked.connect(self._on_open_prefs)
		return self._action_row(
			"Пресет видео по умолчанию и времена публикации",
			[action],
			"Подставляются в «Видео» и «Публикацию» по умолчанию",
		)

	def _recheck_row(self) -> QWidget:
		"""Перепроверка доступов; итог последней проверки — строкой."""
		action = PushButton("Проверить доступы", self)
		action.clicked.connect(self._recheck)
		hint = self._recheck_text or "В этом запуске доступы ещё не проверялись"
		return self._action_row(
			"Доступы публикаторов: роли участников, права бота, свойства сообщества",
			[action],
			hint,
		)

	def _delete_row(self) -> QWidget:
		"""Удаление сообщества из приложения — единственная красная обводка."""
		action = PushButton(FluentIcon.DELETE, "Удалить из приложения…", self)
		action.clicked.connect(self._on_delete)
		return self._action_row(
			"Удаление убирает сообщество только из приложения",
			[action],
			"Подписчики, записи и права в Telegram не меняются",
		)

	# --- операции ---------------------------------------------------------------

	def _refresh(self, *_args: object) -> None:
		"""Перечитывает снимок сообщества и сообщает об изменениях.

		Сообщество могло быть удалено (ошибка «не найден») — тогда
		показывать его страницу нечем, и она просит окно вернуть
		человека на дашборд (ADR-0041).
		"""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.get_community(self._community.id),
			self,
			self._on_refreshed,
			lambda _message: self._leave(),
		)

	def _leave(self) -> None:
		"""Сообщества больше нет: обновить дашборд и вернуться на него."""
		self.changed.emit()
		self.dashboard_requested.emit(self._community.kind)

	def _on_refreshed(self, community: CommunityDto) -> None:
		self.update_community(community)
		self.changed.emit()

	def _on_toggle_enabled(self, checked: bool) -> None:
		"""Включает/выключает сообщество (публикация и расписание)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(COMMUNITY_ENABLED, self._community.id, checked),
			self,
			self._refresh,
			self._on_toggle_failed,
		)

	def _on_toggle_failed(self, message: str) -> None:
		"""Ошибка записи флага: показать и вернуть странице правду из БД."""
		self._show_error(message)
		self._refresh()

	def _on_open_tasks(self) -> None:
		"""Меню «…» → окно задач (та же панель, что во вкладке)."""
		open_tasks(
			self._worker,
			self._watchers.tasks,
			self._community,
			self,
			on_members=partial(self._segments.setCurrentItem, TAB_MEMBERS),
		)

	def _recheck(self) -> None:
		"""Перепроверяет оба способа администрирования."""
		show_info(self, "Проверка", f"Проверяю доступы «{self._community.title}»…")
		run_in_engine(
			self._worker,
			self._worker.engine.communities.recheck_community(self._community.id),
			self,
			self._on_rechecked,
			self._show_error,
		)

	def _on_rechecked(self, access: CommunityAccess) -> None:
		"""Показывает итог перепроверки плашкой и строкой, обновляет страницу."""
		ok, summary = recheck_summary(access)
		if ok:
			show_success(self, access.community.title, summary)
		else:
			show_warning(self, access.community.title, summary)
		self._recheck_text = f"проверено {format_local(_now())} · {summary}"
		self._refresh()

	# --- настройки (пресет, времена) ---------------------------------------------

	def _on_open_prefs(self) -> None:
		"""Открывает настройки (цепочка: пресеты → пресет → времена)."""
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_presets(),
			self,
			self._on_presets_loaded,
			self._show_error,
		)

	def _on_presets_loaded(self, presets: list[PresetDto]) -> None:
		"""Пресеты получены — узнаём текущий выбор сообщества."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(COMMUNITY_DEFAULT_PRESET, self._community.id),
			self,
			partial(self._on_current_preset_loaded, presets),
			self._show_error,
		)

	def _on_current_preset_loaded(self, presets: list[PresetDto], current_id: int | None) -> None:
		"""Текущий пресет получен — узнаём времена публикации."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, self._community.id),
			self,
			partial(self._open_prefs_dialog, presets, current_id),
			self._show_error,
		)

	def _open_prefs_dialog(
		self, presets: list[PresetDto], current_id: int | None, times: list[str]
	) -> None:
		"""Диалог настроек; сохранение — одной транзакцией движка."""
		dialog = _CommunityPrefsDialog(
			self._community.title, presets, current_id, times, self.window()
		)
		if not exec_dialog(dialog):
			return
		# обе настройки — одна пользовательская операция: движок пишет их
		# одной транзакцией (set_for_many), успех сообщается по факту записи
		items: list[tuple[SettingKey[Any], Any]] = [
			(COMMUNITY_DEFAULT_PRESET, dialog.preset_id()),
			(PUBLISH_TIMES, dialog.times()),
		]
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for_many(self._community.id, items),
			self,
			self._on_prefs_saved,
			self._show_error,
		)

	def _on_prefs_saved(self, _result: object = None) -> None:
		show_success(self, "Готово", f"Настройки «{self._community.title}» сохранены.")

	# --- бот ---------------------------------------------------------------------

	# --- удаление ----------------------------------------------------------------

	def _on_delete(self) -> None:
		if not confirm_delete(self, f"Удалить «{self._community.title}» из приложения?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.delete_community(self._community.id),
			self,
			lambda *_a: self._leave(),
			self._show_error,
		)


def _set_active(tab: QWidget | None, active: bool) -> None:
	"""Оживляет или усыпляет тело вкладки (если у него есть что оживлять).

	Панели очередей присоединяются к наблюдателю и отсоединяются,
	«Обзор» перечитывает свой снимок при показе.
	"""
	setter = getattr(tab, "set_active", None)
	if callable(setter):
		setter(active)


def _now() -> Any:
	"""Текущий момент (UTC) — для строки «проверено …»."""
	from datetime import UTC, datetime

	return datetime.now(UTC)


__all__ = [
	"COMMUNITY_PAGE_ROUTE",
	"CommunityPage",
	"MembersPanel",
	"open_members",
	"queue_footer_text",
	"recheck_summary",
	"tab_title",
]
