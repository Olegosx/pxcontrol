"""Задачи сообщества: обзор карточками и настройка одной задачи (ADR-0038).

Вкладка «Задачи» страницы сообщества и содержимое рабочего окна
с дашборда. Два вида (спека ``screens/tasks.md``):

- **обзор** — карточка на каждую доступную задачу: что она делает,
  расписание, итог последнего запуска и тумблер «по расписанию».
  Карточка кликабельна целиком;
- **настройка одной задачи** — строка пути, шапка с двумя запусками
  («без изменений» и настоящий), блоки параметров формой
  «подпись — поле», расписание, одна кнопка «Сохранить» на всё
  и три последних запуска таблицей.

Ручной запуск идёт по одному правилу: сначала «без изменений» — он
ничего не меняет и отвечает числами, — и только потом настоящий
с подтверждением. Порядок не для удобства: удаление необратимо.

Ход работы виден там, где задача живёт: полосой на её карточке
в обзоре и полосой с «Отменить» в её настройке; общей панели очереди
больше нет.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any

from PySide6.QtCore import (
	QAbstractTableModel,
	QModelIndex,
	QObject,
	QPersistentModelIndex,
	QSortFilterProxyModel,
	Qt,
	Signal,
)
from PySide6.QtWidgets import (
	QAbstractItemView,
	QGridLayout,
	QHBoxLayout,
	QHeaderView,
	QSizePolicy,
	QStackedWidget,
	QTableWidgetItem,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	BreadcrumbBar,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	DoubleSpinBox,
	HorizontalSeparator,
	InfoBadge,
	InfoBar,
	InfoBarPosition,
	LineEdit,
	PrimaryPushButton,
	ProgressBar,
	PushButton,
	ScrollArea,
	SearchLineEdit,
	SimpleCardWidget,
	SpinBox,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TableView,
	TableWidget,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto, ExecutorDto
from pxcontrol.engine.services.tasks import TaskDto, TaskJobDto, TaskRunDto
from pxcontrol.engine.tasks import (
	DeletedAccountsParams,
	JoinRequestsParams,
	ReactionChoice,
	ReactionScope,
	ReactionsParams,
	RunOutcome,
	ServiceMessagesParams,
	ServiceReport,
	TaskError,
	TaskKind,
	TaskParams,
	TaskTrigger,
	deleted_accounts,
	join_requests,
	reactions,
	service_messages,
)
from pxcontrol.engine.tasks.schedule import (
	INTERVAL_MINUTES_RANGE,
	Schedule,
	ScheduleKind,
	schedule_text,
)
from pxcontrol.engine.telegram.types import (
	ChatReactions,
	CommunityKind,
	ExecutorRef,
	ReactionOption,
	ServiceMessageKind,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ERROR_TEXT,
	ErrorLabel,
	FlowGrid,
	SaveChoice,
	WorkDialog,
	ask_save_changes,
	clear_layout,
	confirm_delete,
	elide_text,
	error_reporter,
	exec_dialog,
	font_px,
	format_count,
	format_local,
	list_button,
	noop,
	plural,
	section_header,
	status_caption,
	theme_color,
)
from pxcontrol.ui.queue_watcher import QueueView, QueueWatcher

#: Индекс модели, как его отдаёт Qt (временный или постоянный).
_Index = QModelIndex | QPersistentModelIndex

#: Что спросить, уходя с настройки с несохранёнными правками.
SAVE_ON_LEAVE_HINT = (
	"В настройке задачи остались несохранённые изменения. Сохранить их перед уходом?"
)

#: Почему защищённые записи не предлагаются к удалению.
_PROTECTED_HINT = (
	"не удаляются: тема форума — это её первое сообщение, "
	"а создание и переезд сообщества — его история"
)

#: Исход запуска по-русски (журнал).
OUTCOME_WORDS: dict[RunOutcome, str] = {
	RunOutcome.RUNNING: "идёт",
	RunOutcome.DONE: "готово",
	RunOutcome.ERROR: "ошибка",
	RunOutcome.CANCELLED: "отменено",
	RunOutcome.INTERRUPTED: "прервано остановкой",
}

#: Кто запустил, по-русски (журнал).
TRIGGER_WORDS: dict[TaskTrigger, str] = {
	TaskTrigger.MANUAL: "вручную",
	TaskTrigger.SCHEDULE: "по расписанию",
}

#: Сколько запусков читать в окно журнала за раз.
RUNS_SHOWN = 100

#: Сколько последних запусков показывать в настройке задачи (макет).
RUNS_IN_DETAIL = 3

#: Задачи, которым журнал нужен шире трёх строк (см. ``TasksPanel._on_task``).
_WIDE_RUNS = frozenset({TaskKind.REACTIONS, TaskKind.SERVICE_MESSAGES})

#: Названия видов задач по-русски (карточки, заголовки, журнал).
KIND_TITLES: dict[TaskKind, str] = {
	TaskKind.SERVICE_MESSAGES: service_messages.TITLE.noun,
	TaskKind.DELETED_ACCOUNTS: deleted_accounts.TITLE.noun,
	TaskKind.REACTIONS: reactions.TITLE.noun,
	TaskKind.JOIN_REQUESTS: join_requests.TITLE.noun,
}


@dataclass(frozen=True)
class TaskTexts:
	"""Тексты задачи в интерфейсе (спека ``tasks.md``, раздел 9).

	Attributes:
		card: суть одной строкой — на карточке обзора.
		detail: суть в настройке, с пояснением «почему так».
		dry_run: подпись запуска без изменений.
		run: подпись настоящего запуска.
		status: заголовок полосы хода во время настоящего запуска.
		dry_status: то же во время запуска «без изменений».
	"""

	card: str
	detail: str
	dry_run: str
	run: str
	status: str
	dry_status: str


TASK_TEXTS: dict[TaskKind, TaskTexts] = {
	TaskKind.SERVICE_MESSAGES: TaskTexts(
		card="Удаляет «вступил», «закрепил», смену оформления",
		detail=(
			"Удаляет служебные сообщения Telegram. Историю приходится читать "
			"целиком: поиска «только служебные» у Telegram нет."
		),
		dry_run="Просмотреть",
		run="Удалить отмеченное",
		status="Чистка идёт…",
		dry_status="Просмотр идёт…",
	),
	TaskKind.DELETED_ACCOUNTS: TaskTexts(
		card="Находит мёртвые души и исключает порциями",
		detail=("Находит в списке участников удалённые аккаунты и исключает их порциями."),
		dry_run="Найти",
		run="Исключить найденные",
		status="Чистка идёт…",
		dry_status="Поиск идёт…",
	),
	TaskKind.REACTIONS: TaskTexts(
		card="Пользователи пула ставят реакции по кругу",
		detail=(
			"Пользователи пула ставят реакции записям по кругу — по одному пользователю за запуск."
		),
		dry_run="Подобрать записи",
		run="Провести проход",
		status="Проход идёт…",
		dry_status="Подбор идёт…",
	),
	TaskKind.JOIN_REQUESTS: TaskTexts(
		card="Одобряет заявки на вступление по правилам",
		detail=(
			"Разбирает заявки на вступление: принимает, ограничивает или отклоняет по правилам."
		),
		dry_run="Посмотреть заявки",
		run="Разобрать заявки",
		status="Приём идёт…",
		dry_status="Просмотр идёт…",
	),
}

#: Виды расписания в порядке показа и их подписи в выпадающем списке.
SCHEDULE_KINDS: tuple[tuple[ScheduleKind, str], ...] = (
	(ScheduleKind.NONE, "Только по требованию"),
	(ScheduleKind.INTERVAL, "С интервалом"),
	(ScheduleKind.DAILY, "В моменты суток"),
)

#: Размеры по макету (пиксели).
_CARD_MIN_WIDTH = 340  # карточка обзора в сетке
_CARD_SPACING = 10
_REACTION_MIN_WIDTH = 150  # плитка реакции — ширина постоянная
_LABEL_COLUMN = 220  # колонка подписей формы
_NUMBER_WIDTH = 110  # поле числа
_RANGE_WIDTH = 96  # поле числа в диапазоне «от — до»
_COMBO_MAX_WIDTH = 300
_TIMES_WIDTH = 220
_SEARCH_WIDTH = 220
_EMOJI_WIDTH = 24
_WEIGHT_WIDTH = 84
_TABLE_ROW_HEIGHT = 34
_REACTOR_ROW_HEIGHT = 40
_REACTOR_ROWS_SHOWN = 6
_CHECK_COLUMN_WIDTH = 36
_TABLE_FONT_PX = 13
_RUNS_COLUMN_WIDTHS = (120, 180, 130)


class TaskFixTarget(StrEnum):
	"""Куда вести человека чинить ошибку запуска."""

	MEMBERS = "members"  # вкладка «Участники»: пул и права


class ReactorFilter(StrEnum):
	"""Отбор в таблице «Кто ставит»."""

	ALL = "all"
	CHECKED = "checked"
	CAPABLE = "capable"


#: Подписи отбора в выпадающем списке.
REACTOR_FILTERS: tuple[tuple[ReactorFilter, str], ...] = (
	(ReactorFilter.ALL, "Все"),
	(ReactorFilter.CHECKED, "Отмеченные"),
	(ReactorFilter.CAPABLE, "Могут ставить"),
)


# --- правила показа (чистые функции) -----------------------------------------


def available_kinds(kind: CommunityKind) -> tuple[TaskKind, ...]:
	"""Какие задачи доступны сообществу этого вида.

	«Удалённые аккаунты» — только у групп: список участников канала
	Telegram отдаёт иначе, а исключать оттуда мёртвые души незачем.
	Остальные три работают у обоих видов; в канале «Приём заявок»
	принимает заявки как есть — ограничивать права там нечем.
	"""
	if kind is CommunityKind.GROUP:
		return (
			TaskKind.SERVICE_MESSAGES,
			TaskKind.DELETED_ACCOUNTS,
			TaskKind.REACTIONS,
			TaskKind.JOIN_REQUESTS,
		)
	return (TaskKind.SERVICE_MESSAGES, TaskKind.REACTIONS, TaskKind.JOIN_REQUESTS)


def when_text(moment: datetime, now: datetime | None = None) -> str:
	"""Момент по-человечески: «сегодня 04:00», «вчера 18:10», «12.09 07:30»."""
	local = moment.astimezone()
	today = (now or datetime.now(UTC)).astimezone().date()
	if local.date() == today:
		return f"сегодня {local:%H:%M}"
	if local.date() == today - timedelta(days=1):
		return f"вчера {local:%H:%M}"
	return f"{local:%d.%m %H:%M}"


def schedule_caption(task: TaskDto) -> str:
	"""Строка расписания на карточке: что задано и когда следующий запуск.

	Следующий запуск называется только при включённом расписании:
	у выключенного он не назначен, и обещать время было бы неправдой.
	"""
	schedule = task.schedule
	if schedule.kind is ScheduleKind.NONE:
		return "по требованию"
	if schedule.kind is ScheduleKind.DAILY:
		text = "каждый день в " + ", ".join(schedule.times)
	elif schedule.min_minutes == schedule.max_minutes:
		text = f"каждые {schedule.min_minutes} мин"
	else:
		text = f"каждые {schedule.min_minutes}–{schedule.max_minutes} мин"
	if not task.enabled:
		return f"{text} · выключено"
	if task.next_run_at is None:
		return text
	return f"{text} · следующий в {task.next_run_at.astimezone():%H:%M}"


def last_run_caption(run: TaskRunDto | None, now: datetime | None = None) -> str:
	"""Строка итога на карточке: когда был последний запуск и чем кончился."""
	if run is None:
		return "ещё не запускалась"
	when = when_text(run.started_at, now)
	if run.outcome is RunOutcome.ERROR:
		return f"{when} · {run.error or OUTCOME_WORDS[run.outcome]}"
	if run.summary:
		return f"последний: {when} · {run.summary}"
	return f"последний: {when} · {OUTCOME_WORDS[run.outcome]}"


def progress_caption(item: TaskJobDto) -> str:
	"""Строка хода работы: что задание сообщает о себе прямо сейчас."""
	if item.note:
		return str(item.note)
	if item.progress:
		return f"{round(item.progress * 100)} %"
	return "идёт обращение к Telegram"


def error_fix_target(run: TaskRunDto | None) -> TaskFixTarget | None:
	"""Куда вести чинить ошибку запуска; None — чинить не кнопкой.

	Чинятся у нас ошибки про пул и права («некому вести запуск»,
	«нет права…»): их правят на вкладке «Участники» — вводом исполнителя,
	возобновлением или выдачей прав в Telegram. Сетевые отказы и запреты
	Telegram кнопкой не чинятся, и предлагать переход незачем.
	"""
	if run is None or run.outcome is not RunOutcome.ERROR or not run.error:
		return None
	text = run.error.casefold()
	if "некому" in text or "прав" in text or "приостановлен" in text:
		return TaskFixTarget.MEMBERS
	return None


@dataclass(frozen=True)
class TaskForm:
	"""Снимок формы настройки: параметры, расписание и его включённость."""

	params: TaskParams
	schedule: Schedule
	enabled: bool


def form_dirty(saved: TaskForm | None, current: TaskForm) -> bool:
	"""Отличается ли форма от сохранённого (пока не прочитано — нет)."""
	if saved is None:
		return False
	return saved != current


@dataclass(frozen=True)
class ReactorRow:
	"""Строка таблицы «Кто ставит»: исполнитель, отметка и последний проход."""

	executor: ExecutorDto
	checked: bool
	last_at: datetime | None

	@property
	def capable(self) -> bool:
		"""Может ли вести проход прямо сейчас (состоит и не на паузе)."""
		return self.executor.status.in_community and not self.executor.paused


def reactor_state(executor: ExecutorDto) -> str:
	"""Колонка «Состояние»: что мешает вести проход (пусто — ничего)."""
	if executor.paused:
		return "приостановлен"
	if not executor.status.in_community:
		return "не состоит"
	return "может ставить"


def reactor_matches(row: ReactorRow, query: str, mode: ReactorFilter) -> bool:
	"""Проходит ли строка поиск по имени и выбранный отбор."""
	needle = query.strip().casefold()
	if needle and needle not in row.executor.label.casefold():
		return False
	if mode is ReactorFilter.CHECKED:
		return row.checked
	if mode is ReactorFilter.CAPABLE:
		return row.capable
	return True


def last_reaction_times(runs: Sequence[TaskRunDto]) -> dict[ExecutorRef, datetime]:
	"""Когда каждый исполнитель последний раз вёл проход (по журналу)."""
	times: dict[ExecutorRef, datetime] = {}
	for run in runs:
		if run.executor is None or run.dry_run:
			continue
		known = times.get(run.executor)
		if known is None or run.started_at > known:
			times[run.executor] = run.started_at
	return times


def last_scan(runs: Sequence[TaskRunDto]) -> tuple[ServiceReport, datetime] | None:
	"""Отчёт последнего просмотра служебных записей и его момент.

	Берётся просмотр («без изменений»), а не чистка: после чистки числа
	найденного устарели, а человек выбирает виды по тому, что увидел
	при просмотре. Запуски — новые сначала, как их отдаёт журнал.
	"""
	for run in runs:
		if run.dry_run and isinstance(run.report, ServiceReport):
			return run.report, run.started_at
	return None


def scan_caption(report: ServiceReport, at: datetime, now: datetime | None = None) -> str:
	"""Подпись под числами: когда смотрели и сколько сообщений просмотрено."""
	words = plural(report.scanned, "сообщение", "сообщения", "сообщений")
	return (
		f"Числа — по последнему просмотру: {when_text(at, now)}, "
		f"{format_count(report.scanned)} {words}."
	)


def parse_times(text: str) -> tuple[str, ...]:
	"""Моменты суток из поля «через запятую» (пустые куски пропускаются)."""
	return tuple(token.strip() for token in text.split(",") if token.strip())


def next_run_text(task: TaskDto) -> str:
	"""Подпись под блоком расписания: что задано и когда следующий запуск."""
	text = schedule_text(task.schedule)
	if task.schedule.kind is ScheduleKind.NONE:
		return "Расписание не задано — задача запускается только по требованию."
	if not task.enabled:
		return f"Расписание выключено ({text})."
	if task.next_run_at is None:
		return f"Расписание: {text} — следующий запуск не назначен."
	return f"Следующий запуск {format_local(task.next_run_at)} ({text})."


def run_kind_text(run: TaskRunDto) -> str:
	"""Колонка «Запуск»: кто запустил и был ли он «без изменений»."""
	text = TRIGGER_WORDS[run.trigger]
	return f"{text} · без изменений" if run.dry_run else text


def run_result_text(run: TaskRunDto) -> str:
	"""Колонка «Итог»: исход и, если есть, итог отчёта или причина ошибки."""
	word = OUTCOME_WORDS[run.outcome]
	if run.outcome is RunOutcome.ERROR and run.error:
		return f"{word}: {run.error}"
	if run.summary:
		return f"{word} — {run.summary}"
	return word


def run_events_text(run: TaskRunDto) -> str:
	"""События запуска столбцом «время — что случилось»."""
	if not run.events:
		return "Событий не записано."
	return "\n".join(f"{at.astimezone().strftime('%H:%M:%S')}  {text}" for at, text in run.events)


# --- подтверждения (чистые тексты) --------------------------------------------


def _service_rules(params: ServiceMessagesParams) -> str:
	"""Какие виды записей будут удалены — перечнем."""
	return "\n".join(
		f"— {service_messages.kind_title(kind)};"
		for kind in service_messages.selectable_kinds(params.kinds)
	)


def _join_rules(params: JoinRequestsParams) -> str:
	"""Правила разбора заявок словами."""
	rules = ["остальных принять"]
	if params.decline_deleted:
		rules.insert(0, "удалённые аккаунты отклонить")
	if params.restrict_bio_links:
		rules.append("со ссылкой в описании — принять с полным ограничением")
	if params.restrict_personal_channel:
		rules.append("с каналом в профиле — принять с полным ограничением")
	return "; ".join(rules).capitalize() + "."


def schedule_confirmation(
	kind: TaskKind, params: TaskParams, schedule: Schedule, title: str
) -> str:
	"""Что спросить перед включением расписания (ADR-0038).

	Запуск по расписанию идёт без подтверждений и необратим, а спросить
	перед ночным запуском некого — поэтому спрашивают один раз здесь,
	и вопрос перечисляет, что именно будет делаться.
	"""
	when = schedule_text(schedule)
	head = f"Включить «{KIND_TITLES[kind]}» в «{title}» по расписанию ({when})?"
	if isinstance(params, ServiceMessagesParams):
		return (
			f"{head}\n\n{_service_rules(params)}\n\nКаждый запуск удалит не больше "
			f"{params.delete_limit} записей без дополнительного подтверждения. "
			"Удаление необратимо."
		)
	if isinstance(params, DeletedAccountsParams):
		return (
			f"{head}\n\nКаждый запуск исключит не больше {params.kick_limit} удалённых "
			"аккаунтов без дополнительного подтверждения. Исключение необратимо "
			"и уменьшает число участников."
		)
	if isinstance(params, ReactionsParams):
		return (
			f"{head}\n\nПользователей по кругу: {len(params.users)}; охват — "
			f"{reactions.SCOPE_TITLES[params.scope]}; за проход не больше {params.limit} "
			"реакций. Каждый запуск — проход одного пользователя без дополнительного "
			"подтверждения."
		)
	if isinstance(params, JoinRequestsParams):
		return (
			f"{head}\n\n{_join_rules(params)} Каждый запуск разберёт не больше "
			f"{params.limit} заявок без дополнительного подтверждения."
		)
	return head


def run_confirmation(kind: TaskKind, params: TaskParams, title: str) -> str:
	"""Что спросить перед настоящим запуском; пусто — вопроса нет.

	Спрашивают там, где проход необратим: удаление записей, исключение
	участников, разбор заявок. Проход реакций обратим руками человека
	и вопроса не заслуживает.
	"""
	if isinstance(params, ServiceMessagesParams):
		return (
			f"Удалить служебные записи в «{title}»?\n\n{_service_rules(params)}\n\n"
			f"Удаление необратимо. За проход будет удалено не больше "
			f"{params.delete_limit} записей."
		)
	if isinstance(params, DeletedAccountsParams):
		return (
			f"Исключить удалённые аккаунты из «{title}»?\n\nЗа проход будет исключено "
			f"не больше {params.kick_limit}. Исключение необратимо и уменьшит число "
			"участников."
		)
	if isinstance(params, JoinRequestsParams):
		return (
			f"Разобрать заявки в «{title}»?\n\n{_join_rules(params)} За проход — "
			f"не больше {params.limit}. Одобрение и отклонение необратимы."
		)
	return ""


# --- сборки формы -------------------------------------------------------------


def on_change(handler: Callable[[], None], *widgets: QWidget) -> None:
	"""Связывает изменение любого из полей формы с одним обработчиком.

	Поля разные (число, выбор, флажок, строка, тумблер), и сигнал
	у каждого свой; перебор имён держится здесь, чтобы не повторять
	десяток связок в каждом разделе.
	"""
	names = (
		"valueChanged",
		"currentIndexChanged",
		"checkedChanged",
		"stateChanged",
		"textChanged",
	)
	for widget in widgets:
		for name in names:
			signal = getattr(widget, name, None)
			if signal is not None and hasattr(signal, "connect"):
				signal.connect(handler)
				break


@dataclass(frozen=True)
class _FormRow:
	"""Строка формы: коробка подписи и коробка полей."""

	caption: QWidget
	fields: QWidget

	def set_visible(self, visible: bool) -> None:
		"""Показывает или прячет строку целиком — подпись вместе с полями."""
		self.caption.setVisible(visible)
		self.fields.setVisible(visible)


class _FormBlock:
	"""Блок параметров: заголовок и карточка с сеткой «подпись — поле».

	Сетка одна на блок: подписи в первой колонке выровнены по общей
	ширине, поля — своей ширины во второй, лишнее место забирает
	растяжка. Фраз с полем внутри («Удалять не больше: [200]») нет —
	подпись слева, поле справа (спека, раздел 4.1).
	"""

	def __init__(
		self,
		parent: QWidget,
		layout: QVBoxLayout,
		title: str,
		*,
		trailing: Sequence[QWidget] | None = None,
	) -> None:
		layout.addWidget(section_header(parent, title, trailing=trailing))
		card = SimpleCardWidget(parent)
		self.card = card
		self.body = QVBoxLayout(card)
		self.body.setContentsMargins(16, 14, 16, 14)  # макет
		self.body.setSpacing(12)  # макет
		layout.addWidget(card)
		self._parent = parent
		self._grid: QGridLayout | None = None

	def grid(self) -> QGridLayout:
		"""Сетка формы блока (заводится при первой строке)."""
		if self._grid is None:
			grid = QGridLayout()
			grid.setContentsMargins(0, 0, 0, 0)
			grid.setColumnMinimumWidth(0, _LABEL_COLUMN)  # макет
			grid.setHorizontalSpacing(16)  # макет
			grid.setVerticalSpacing(12)  # макет
			grid.setColumnStretch(1, 1)
			self.body.addLayout(grid)
			self._grid = grid
		return self._grid

	def row(self, label: str, *fields: QWidget, note: str = "", unit: str = "") -> _FormRow:
		"""Строка формы: подпись (с уточнением) и поля своей ширины.

		Подпись и поля лежат каждая в своей коробке — строку прячут
		целиком (:meth:`_FormRow.set_visible`), а не одни поля, иначе
		на экране оставалась бы подпись без поля.
		"""
		grid = self.grid()
		line = grid.rowCount()
		caption = QWidget(self._parent)
		column = QVBoxLayout(caption)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(2)
		column.addWidget(BodyLabel(label, caption))
		if note:
			hint = CaptionLabel(note, caption)
			hint.setWordWrap(True)
			column.addWidget(hint)
		grid.addWidget(caption, line, 0, Qt.AlignmentFlag.AlignTop)
		holder = QWidget(self._parent)
		box = QHBoxLayout(holder)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(8)
		for field in fields:
			box.addWidget(field)
		if unit:
			box.addWidget(BodyLabel(unit, holder))
		box.addStretch()
		grid.addWidget(holder, line, 1)
		return _FormRow(caption, holder)

	def add(self, widget: QWidget) -> None:
		"""Виджет во всю ширину блока (таблица, сетка плиток, пояснение)."""
		self.body.addWidget(widget)

	def note(self, text: str) -> CaptionLabel:
		"""Пояснение под содержимым блока."""
		label = CaptionLabel(text, self._parent)
		label.setWordWrap(True)
		self.body.addWidget(label)
		return label


def number_field(parent: QWidget, limits: tuple[int, int], value: int, *, step: int = 1) -> SpinBox:
	"""Поле целого числа своей ширины (макет)."""
	field = SpinBox(parent)
	field.setRange(*limits)
	field.setSingleStep(step)
	field.setValue(value)
	field.setFixedWidth(_NUMBER_WIDTH)  # макет
	return field


def seconds_field(parent: QWidget, limits: tuple[float, float], value: float) -> DoubleSpinBox:
	"""Поле дробного числа в диапазоне «от — до» (макет)."""
	field = DoubleSpinBox(parent)
	field.setRange(*limits)
	field.setSingleStep(0.1)
	field.setValue(value)
	field.setFixedWidth(_RANGE_WIDTH)  # макет
	return field


# --- обзор задач --------------------------------------------------------------


class _TaskCard(CardWidget):
	"""Карточка задачи в обзоре: суть, расписание, итог и тумблер.

	Кнопок нет: карточка открывается целиком, а тумблер включает
	расписание. Строка расписания уступает место полосе хода, пока
	задача работает (спека, раздел 3).
	"""

	def __init__(self, kind: TaskKind, parent: QWidget, on_toggle: Callable[[bool], None]) -> None:
		super().__init__(parent)
		self._kind = kind
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		box = QVBoxLayout(self)
		box.setContentsMargins(16, 14, 16, 14)  # макет
		box.setSpacing(10)  # макет
		head = QHBoxLayout()
		head.setSpacing(12)  # макет
		head.setAlignment(Qt.AlignmentFlag.AlignTop)
		column = QVBoxLayout()
		column.setSpacing(2)
		column.addWidget(StrongBodyLabel(KIND_TITLES[kind], self))
		hint = CaptionLabel(self)
		hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(hint, TASK_TEXTS[kind].card)
		column.addWidget(hint)
		head.addLayout(column, stretch=1)
		self._switch = SwitchButton(self)
		self._switch.setOnText("")
		self._switch.setOffText("")
		self._switch.checkedChanged.connect(on_toggle)
		head.addWidget(self._switch)
		box.addLayout(head)
		box.addWidget(HorizontalSeparator(self))
		self._schedule = BodyLabel(self)
		self._schedule.setFont(font_px(13))  # макет
		self._schedule.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		box.addWidget(self._schedule)
		self._progress = ProgressBar(self)
		self._progress.hide()
		box.addWidget(self._progress)
		result = QHBoxLayout()
		result.setSpacing(8)
		self._error_badge = InfoBadge.error("ошибка", parent=self)
		self._error_badge.hide()
		result.addWidget(self._error_badge)
		self._result = CaptionLabel(self)
		self._result.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		result.addWidget(self._result, stretch=1)
		box.addLayout(result)

	def show_task(
		self, task: TaskDto | None, run: TaskRunDto | None, job: TaskJobDto | None
	) -> None:
		"""Приводит карточку к свежим данным задачи."""
		self._switch.blockSignals(True)
		try:
			self._switch.setChecked(bool(task and task.enabled))
		finally:
			self._switch.blockSignals(False)
		manual = task is not None and task.schedule.kind is ScheduleKind.NONE
		self._switch.setEnabled(task is not None and not manual)
		self._switch.setToolTip("Расписание не задано" if manual else "Запускать по расписанию")
		if job is not None:
			self._schedule.hide()
			self._progress.show()
			self._progress.setValue(int(job.progress * 100))
			self._error_badge.hide()
			elide_text(self._result, progress_caption(job))
			return
		self._progress.hide()
		self._schedule.show()
		elide_text(self._schedule, schedule_caption(task) if task else "читаю задачу…")
		failed = run is not None and run.outcome is RunOutcome.ERROR
		self._error_badge.setVisible(failed)
		elide_text(self._result, last_run_caption(run))


class _TasksOverview(QWidget):
	"""Обзор задач: пояснение и сетка карточек (спека, раздел 3)."""

	def __init__(
		self,
		kinds: Sequence[TaskKind],
		parent: QWidget,
		*,
		on_open: Callable[[TaskKind], None],
		on_toggle: Callable[[TaskKind, bool], None],
	) -> None:
		super().__init__(parent)
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().block_spacing)
		self._hint = CaptionLabel(self)
		self._hint.setWordWrap(True)
		box.addWidget(self._hint)
		self.cards = {kind: _TaskCard(kind, self, partial(on_toggle, kind)) for kind in kinds}
		for kind, card in self.cards.items():
			card.clicked.connect(partial(on_open, kind))
		box.addWidget(
			FlowGrid(
				list(self.cards.values()), self, min_width=_CARD_MIN_WIDTH, spacing=_CARD_SPACING
			)
		)
		box.addStretch()

	def set_publisher(self, label: str | None) -> None:
		"""Пояснение сверху: чьими руками идут задачи."""
		who = f"userbot-публикатор «{label}»" if label else "userbot-публикатор сообщества"
		self._hint.setText(
			f"Задачи выполняет {who}. По расписанию — без подтверждений, в пределах своих лимитов."
		)


# --- расписание ---------------------------------------------------------------


class _ScheduleBlock:
	"""Блок «Расписание» формы задачи: включённость, вид и его поля.

	Не виджет, а сборка: поля раскладываются в форму владельца, своей
	коробки у блока нет (коробку без места в компоновке Qt рисует
	в левом верхнем углу родителя — поверх строки пути).

	Своей кнопки сохранения нет — расписание пишется вместе
	с параметрами одной «Сохранить» (спека, раздел 4.4). Строки вида,
	которого нет, скрываются целиком, а не делаются неактивными:
	неактивное поле обещает, что им когда-то можно будет пользоваться.
	"""

	def __init__(
		self, parent: QWidget, layout: QVBoxLayout, on_changed: Callable[[], None]
	) -> None:
		block = _FormBlock(parent, layout, "Расписание")
		self._switch = SwitchButton(parent)
		self._switch.setOnText("включено")
		self._switch.setOffText("выключено")
		block.row("По расписанию", self._switch)
		self._kind = ComboBox(parent)
		self._kind.setMaximumWidth(_COMBO_MAX_WIDTH)  # макет
		for kind, title in SCHEDULE_KINDS:
			self._kind.addItem(title, userData=kind)
		block.row("Как часто", self._kind)
		self._min = number_field(parent, INTERVAL_MINUTES_RANGE, INTERVAL_MINUTES_RANGE[0])
		self._min.setFixedWidth(_RANGE_WIDTH)  # макет
		self._max = number_field(parent, INTERVAL_MINUTES_RANGE, INTERVAL_MINUTES_RANGE[0])
		self._max.setFixedWidth(_RANGE_WIDTH)  # макет
		self._interval_row = block.row(
			"Пауза между запусками",
			self._min,
			BodyLabel("—", parent),
			self._max,
			unit="мин, случайно",
		)
		self._times = LineEdit(parent)
		self._times.setFixedWidth(_TIMES_WIDTH)  # макет
		self._times.setPlaceholderText("04:00, 16:30…")
		self._times_row = block.row("Моменты", self._times, note="ЧЧ:ММ через запятую")
		self._caption = block.note("")
		self._error = ErrorLabel(parent)
		block.add(self._error)
		# вид меняет состав строк — связь заводится, когда строки уже есть
		self._kind.currentIndexChanged.connect(self._on_kind)
		on_change(on_changed, self._switch, self._kind, self._min, self._max, self._times)
		self._on_kind()

	def present(self, task: TaskDto) -> None:
		"""Раскладывает сохранённое расписание по полям и обновляет подпись."""
		schedule = task.schedule
		for index, (kind, _title) in enumerate(SCHEDULE_KINDS):
			if kind is schedule.kind:
				self._kind.setCurrentIndex(index)
		self._min.setValue(schedule.min_minutes)
		self._max.setValue(schedule.max_minutes)
		self._times.setText(", ".join(schedule.times))
		self._switch.setChecked(task.enabled)
		self._caption.setText(next_run_text(task))
		self._error.succeed()
		self._on_kind()

	@property
	def enabled(self) -> bool:
		"""Включено ли расписание в форме."""
		return bool(self._switch.isChecked())

	def schedule(self) -> Schedule:
		"""Расписание из полей формы (без проверки — её делает движок)."""
		kind = self._kind.currentData()
		return Schedule(
			kind=kind if isinstance(kind, ScheduleKind) else ScheduleKind.NONE,
			min_minutes=self._min.value(),
			max_minutes=self._max.value(),
			times=parse_times(self._times.text()),
		)

	def validate(self) -> bool:
		"""Проверяет расписание перед сохранением (ошибка — под блоком)."""
		schedule = self.schedule()
		try:
			schedule.validate()
			if self.enabled and schedule.kind is ScheduleKind.NONE:
				raise TaskError("Выберите расписание — «только по требованию» включать нечего.")
		except TaskError as exc:
			return self._error.fail(str(exc))
		return self._error.succeed()

	def _on_kind(self, *_args: object) -> None:
		"""Показывает строки выбранного вида, прячет остальные целиком."""
		kind = self._kind.currentData()
		self._interval_row.set_visible(kind is ScheduleKind.INTERVAL)
		self._times_row.set_visible(kind is ScheduleKind.DAILY)


# --- разделы задач ------------------------------------------------------------


class _TaskSection(QWidget):
	"""Блоки параметров одной задачи: форма, подтверждения, отчёт.

	Раздел отвечает только за своё — что показать в блоках и что
	отдать движку; путь, шапку, расписание, сохранение и последние
	запуски строит общий :class:`_TaskDetail`.
	"""

	kind: TaskKind

	def __init__(self, panel: TasksPanel, parent: QWidget, on_changed: Callable[[], None]) -> None:
		super().__init__(parent)
		self._panel = panel
		self._changed = on_changed
		self._box = QVBoxLayout(self)
		self._box.setContentsMargins(0, 0, 0, 0)
		self._box.setSpacing(density.spacing().block_spacing)
		self.build()

	# --- контракт раздела ------------------------------------------------------------

	def build(self) -> None:
		"""Собирает блоки параметров в свою компоновку."""
		raise NotImplementedError

	def apply_params(self, params: TaskParams) -> None:
		"""Раскладывает сохранённые параметры по полям формы."""
		raise NotImplementedError

	def params(self) -> TaskParams:
		"""Параметры из полей формы."""
		raise NotImplementedError

	def valid(self) -> bool:
		"""Годится ли форма для запуска (ошибку раздел показывает сам)."""
		return True

	def show_runs(self, runs: Sequence[TaskRunDto]) -> None:
		"""Принимает последние запуски (у кого в блоках есть их числа)."""

	def show_report(self, item: TaskJobDto) -> None:
		"""Показывает числа завершённого запуска (у кого они есть в блоках)."""

	def community_title(self) -> str:
		"""Название сообщества — для текстов подтверждений."""
		return self._panel.community.title


class _ServiceMessagesSection(_TaskSection):
	"""«Служебные записи»: что удалять строками с числами и границы прохода."""

	kind = TaskKind.SERVICE_MESSAGES

	#: Виды записей в порядке показа (защищённый — последним, без флажка).
	_KINDS: tuple[ServiceMessageKind, ...] = (
		ServiceMessageKind.MEMBERS,
		ServiceMessageKind.PINS,
		ServiceMessageKind.APPEARANCE,
		ServiceMessageKind.CALLS,
		ServiceMessageKind.OTHER,
		ServiceMessageKind.PROTECTED,
	)

	def build(self) -> None:
		self._boxes: dict[ServiceMessageKind, CheckBox] = {}
		self._counts: dict[ServiceMessageKind, BodyLabel] = {}
		self._error = ErrorLabel(self)
		block = _FormBlock(
			self, self._box, "Что удалять", trailing=[CaptionLabel("по просмотру", self)]
		)
		for position, kind in enumerate(self._KINDS):
			if position:
				block.add(HorizontalSeparator(self))
			block.add(self._kind_row(kind))
		self._scan_note = block.note("Просмотр ещё не выполнялся.")
		block.add(self._error)
		limits = _FormBlock(self, self._box, "Границы")
		self._depth = number_field(
			self, service_messages.DEPTH_RANGE, service_messages.DEFAULT_DEPTH, step=500
		)
		limits.row("Просматривать последних", self._depth, unit="сообщений истории")
		self._delete_limit = number_field(
			self,
			service_messages.DELETE_LIMIT_RANGE,
			service_messages.DEFAULT_DELETE_LIMIT,
			step=100,
		)
		limits.row(
			"Удалять за проход не больше",
			self._delete_limit,
			note="сотни действий подряд с аккаунта — риск ограничений",
		)
		on_change(self._changed, self._depth, self._delete_limit)

	def _kind_row(self, kind: ServiceMessageKind) -> QWidget:
		"""Строка вида: флажок (или пояснение у защищённого) и число справа."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(8)
		if kind.removable():
			check = CheckBox(service_messages.kind_title(kind), box)
			self._boxes[kind] = check
			on_change(self._changed, check)
			row.addWidget(check, stretch=1)
		else:
			column = QVBoxLayout()
			column.setSpacing(2)
			locked = CheckBox(service_messages.kind_title(kind), box)
			locked.setEnabled(False)
			column.addWidget(locked)
			hint = CaptionLabel(_PROTECTED_HINT, box)
			hint.setWordWrap(True)
			column.addWidget(hint)
			row.addLayout(column, stretch=1)
		count = BodyLabel("—", box)
		count.setFont(font_px(_TABLE_FONT_PX, tabular=True))  # макет: цифры в столбик
		count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		self._counts[kind] = count
		row.addWidget(count)
		return box

	def apply_params(self, params: TaskParams) -> None:
		if not isinstance(params, ServiceMessagesParams):
			return
		self._depth.setValue(params.depth)
		self._delete_limit.setValue(params.delete_limit)
		for kind, check in self._boxes.items():
			check.setChecked(kind in params.kinds)

	def params(self) -> ServiceMessagesParams:
		return ServiceMessagesParams(
			depth=self._depth.value(),
			kinds=tuple(kind for kind, check in self._boxes.items() if check.isChecked()),
			delete_limit=self._delete_limit.value(),
		)

	def valid(self) -> bool:
		if not self.params().kinds:
			return self._error.fail("Отметьте хотя бы один вид записей.")
		return self._error.succeed()

	def show_runs(self, runs: Sequence[TaskRunDto]) -> None:
		"""Числа последнего просмотра — из журнала, а не только из этого сеанса."""
		scan = last_scan(runs)
		if scan is None:
			return
		report, at = scan
		self._show_scan(report, at)

	def show_report(self, item: TaskJobDto) -> None:
		report = item.report
		if isinstance(report, ServiceReport) and item.dry_run:
			self._show_scan(report, datetime.now(UTC))

	def _show_scan(self, report: ServiceReport, at: datetime) -> None:
		"""Раскладывает числа просмотра по строкам видов."""
		for kind, count in self._counts.items():
			found = report.found.get(kind)
			count.setText("—" if found is None else format_count(found))
		self._scan_note.setText(scan_caption(report, at))


class _DeletedAccountsSection(_TaskSection):
	"""«Удалённые аккаунты»: единственная граница — сколько исключать за проход."""

	kind = TaskKind.DELETED_ACCOUNTS

	def build(self) -> None:
		block = _FormBlock(self, self._box, "Границы")
		self._kick_limit = number_field(
			self, deleted_accounts.KICK_LIMIT_RANGE, deleted_accounts.DEFAULT_KICK_LIMIT, step=5
		)
		block.row(
			"Исключать за проход не больше",
			self._kick_limit,
			note="резкое падение числа участников бьёт по охватам",
		)
		on_change(self._changed, self._kick_limit)

	def apply_params(self, params: TaskParams) -> None:
		if isinstance(params, DeletedAccountsParams):
			self._kick_limit.setValue(params.kick_limit)

	def params(self) -> DeletedAccountsParams:
		return DeletedAccountsParams(kick_limit=self._kick_limit.value())


class _JoinRequestsSection(_TaskSection):
	"""«Приём заявок»: правила разбора и предел за проход."""

	kind = TaskKind.JOIN_REQUESTS

	def build(self) -> None:
		is_group = self._panel.community.kind is CommunityKind.GROUP
		rules = _FormBlock(self, self._box, "Правила")
		self._decline_deleted = CheckBox("Отклонять заявки удалённых аккаунтов", self)
		self._restrict_bio = CheckBox(
			"Принимать с полным ограничением, если в описании профиля есть ссылка", self
		)
		self._restrict_channel = CheckBox(
			"…и если в профиле указан канал (один запрос к Telegram на заявителя)", self
		)
		for check in (self._decline_deleted, self._restrict_bio, self._restrict_channel):
			rules.add(check)
		for check in (self._restrict_bio, self._restrict_channel):
			check.setEnabled(is_group)
			if not is_group:
				check.setToolTip(
					"В канале ограничить участника нечем — правило действует в группах"
				)
		rules.note(
			"Остальных — принимать. Ограничение прав есть только у групп; "
			"в канале заявки принимаются как есть."
		)
		limits = _FormBlock(self, self._box, "Границы")
		self._limit = number_field(self, join_requests.LIMIT_RANGE, join_requests.DEFAULT_LIMIT)
		limits.row("Разбирать за проход не больше", self._limit, unit="заявок")
		on_change(
			self._changed,
			self._decline_deleted,
			self._restrict_bio,
			self._restrict_channel,
			self._limit,
		)

	def apply_params(self, params: TaskParams) -> None:
		if not isinstance(params, JoinRequestsParams):
			return
		self._decline_deleted.setChecked(params.decline_deleted)
		self._restrict_bio.setChecked(params.restrict_bio_links)
		self._restrict_channel.setChecked(params.restrict_personal_channel)
		self._limit.setValue(params.limit)

	def params(self) -> JoinRequestsParams:
		return JoinRequestsParams(
			decline_deleted=self._decline_deleted.isChecked(),
			restrict_bio_links=self._restrict_bio.isChecked(),
			restrict_personal_channel=self._restrict_channel.isChecked(),
			limit=self._limit.value(),
		)


# --- «Реакции»: кто ставит ----------------------------------------------------


class _ReactorsModel(QAbstractTableModel):
	"""Модель таблицы «Кто ставит»: отметка, имя, роль, состояние, последний проход.

	Модель и представление, а не таблица виджетов: исполнителей может
	быть сотня, и строить сотню карточек с флажками ради выбора —
	ровно тот случай, из-за которого затевается переход на модели
	(спека, раздел 5.1).
	"""

	#: Заголовки колонок; первая — флажок без подписи.
	COLUMNS: tuple[str, ...] = ("", "Пользователь", "Роль", "Состояние", "Последняя реакция")

	def __init__(self, on_changed: Callable[[], None], parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._rows: list[ReactorRow] = []
		self._on_changed = on_changed

	def set_rows(self, rows: Sequence[ReactorRow]) -> None:
		"""Меняет строки целиком (список пришёл из движка)."""
		self.beginResetModel()
		self._rows = list(rows)
		self.endResetModel()

	def row(self, position: int) -> ReactorRow:
		"""Строка по номеру (нужна отбору прокси)."""
		return self._rows[position]

	def checked_owners(self) -> tuple[ExecutorRef, ...]:
		"""Отмеченные исполнители в порядке показа."""
		return tuple(row.executor.owner for row in self._rows if row.checked)

	def set_checked(self, owners: set[ExecutorRef]) -> None:
		"""Расставляет отметки по сохранённому выбору."""
		self.beginResetModel()
		self._rows = [
			ReactorRow(row.executor, row.executor.owner in owners, row.last_at)
			for row in self._rows
		]
		self.endResetModel()

	def check_all(self, *, capable_only: bool = True) -> None:
		"""Отмечает всех подходящих («Отметить всех подходящих»)."""
		self.beginResetModel()
		self._rows = [
			ReactorRow(row.executor, row.capable if capable_only else True, row.last_at)
			for row in self._rows
		]
		self.endResetModel()
		self._on_changed()

	# --- контракт модели Qt ---------------------------------------------------------

	def rowCount(self, parent: _Index | None = None) -> int:  # noqa: N802 — API Qt
		return 0 if parent is not None and parent.isValid() else len(self._rows)

	def columnCount(self, parent: _Index | None = None) -> int:  # noqa: N802 — API Qt
		return 0 if parent is not None and parent.isValid() else len(self.COLUMNS)

	def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
		if not index.isValid():
			return None
		row = self._rows[index.row()]
		column = index.column()
		if role == Qt.ItemDataRole.CheckStateRole and column == 0:
			if not row.capable:
				return None  # флажка нет вовсе: причина — в колонке «Состояние»
			return Qt.CheckState.Checked if row.checked else Qt.CheckState.Unchecked
		if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole):
			if column == 1:
				return row.executor.label
			if column == 2:
				return status_caption(row.executor.status)
			if column == 3:
				return reactor_state(row.executor)
			if column == 4:
				if role == Qt.ItemDataRole.UserRole:
					# сортировка — по моменту, а не по его записи словами
					return row.last_at.timestamp() if row.last_at else 0.0
				return when_text(row.last_at) if row.last_at else "—"
		return None

	def setData(  # noqa: N802 — API Qt
		self, index: _Index, value: Any, role: int = Qt.ItemDataRole.EditRole
	) -> bool:
		if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
			return False
		row = self._rows[index.row()]
		if not row.capable:
			return False
		# вид может прийти числом или перечислением — сводим к перечислению
		checked = Qt.CheckState(value) is Qt.CheckState.Checked
		self._rows[index.row()] = ReactorRow(row.executor, checked, row.last_at)
		self.dataChanged.emit(index, index, [role])
		self._on_changed()
		return True

	def flags(self, index: _Index) -> Qt.ItemFlag:
		if not index.isValid():
			return Qt.ItemFlag.NoItemFlags
		row = self._rows[index.row()]
		if not row.capable:
			# приостановленный или не состоящий проход не поведёт —
			# и отметить его нельзя: причина в колонке «Состояние»
			return Qt.ItemFlag.NoItemFlags
		flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
		if index.column() == 0:
			flags |= Qt.ItemFlag.ItemIsUserCheckable
		return flags

	def headerData(  # noqa: N802 — API Qt
		self,
		section: int,
		orientation: Qt.Orientation,
		role: int = Qt.ItemDataRole.DisplayRole,
	) -> Any:
		if orientation is Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
			return self.COLUMNS[section]
		return None


class _ReactorsProxy(QSortFilterProxyModel):
	"""Отбор и сортировка таблицы «Кто ставит» (правило — ``reactor_matches``)."""

	def __init__(self, model: _ReactorsModel, parent: QObject | None = None) -> None:
		super().__init__(parent)
		self._model = model
		self.setSourceModel(model)
		self.setSortRole(Qt.ItemDataRole.UserRole)
		self._query = ""
		self._mode = ReactorFilter.ALL

	def set_filter(self, query: str, mode: ReactorFilter) -> None:
		"""Меняет поиск и отбор."""
		self._query = query
		self._mode = mode
		self.invalidateFilter()

	def filterAcceptsRow(self, source_row: int, _parent: _Index) -> bool:  # noqa: N802 — API Qt
		return reactor_matches(self._model.row(source_row), self._query, self._mode)


class _ReactionsSection(_TaskSection):
	"""«Реакции»: кто ставит таблицей, какие реакции плитками, каким записям."""

	kind = TaskKind.REACTIONS

	def build(self) -> None:
		self._pending: ReactionsParams | None = None
		self._reaction_rows: dict[str, tuple[CheckBox, SpinBox]] = {}
		self._error = ErrorLabel(self)
		self._build_reactors()
		self._build_reactions()
		self._build_scope()
		self._box.addWidget(self._error)

	# --- блок «Кто ставит» ----------------------------------------------------------

	def _build_reactors(self) -> None:
		block = _FormBlock(self, self._box, "Кто ставит")
		row = QHBoxLayout()
		row.setSpacing(10)  # макет
		self._search = SearchLineEdit(self)
		self._search.setFixedWidth(_SEARCH_WIDTH)  # макет
		self._search.setPlaceholderText("Поиск по имени")
		self._search.textChanged.connect(self._on_filter)
		row.addWidget(self._search)
		self._filter = ComboBox(self)
		for mode, title in REACTOR_FILTERS:
			self._filter.addItem(title, userData=mode)
		self._filter.currentIndexChanged.connect(self._on_filter)
		row.addWidget(self._filter)
		self._counter = CaptionLabel(self)
		row.addWidget(self._counter)
		row.addStretch()
		mark_all = PushButton("Отметить всех подходящих", self)
		mark_all.setToolTip("Отметить всех, кто состоит в сообществе и не приостановлен")
		mark_all.clicked.connect(self._on_mark_all)
		row.addWidget(mark_all)
		holder = QWidget(self)
		holder.setLayout(row)
		block.add(holder)
		self._model = _ReactorsModel(self._on_reactors_changed, self)
		self._proxy = _ReactorsProxy(self._model, self)
		self._table = TableView(self)
		self._table.setModel(self._proxy)
		self._table.setSortingEnabled(True)
		self._table.sortByColumn(4, Qt.SortOrder.DescendingOrder)
		self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
		self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
		self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
		self._table.setWordWrap(False)
		vertical = self._table.verticalHeader()
		if vertical is not None:
			vertical.hide()
			vertical.setDefaultSectionSize(_REACTOR_ROW_HEIGHT)  # макет
		header = self._table.horizontalHeader()
		if header is not None:
			header.setStretchLastSection(True)
			header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
			header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
			# колонка флажка — своей ширины: «по содержимому» её перебило бы
			header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
		self._table.setColumnWidth(0, _CHECK_COLUMN_WIDTH)  # макет
		height = _REACTOR_ROWS_SHOWN * _REACTOR_ROW_HEIGHT
		if header is not None:
			height += header.height()
		self._table.setFixedHeight(height)  # макет: 6 строк, дальше прокрутка
		block.add(self._table)
		block.note("Отмеченные ставят реакции по очереди, по одному за запуск.")

	def _on_filter(self, *_args: object) -> None:
		"""Поиск и отбор — клиентские, как на дашборде."""
		mode = self._filter.currentData()
		self._proxy.set_filter(
			self._search.text(), mode if isinstance(mode, ReactorFilter) else ReactorFilter.ALL
		)

	def _on_mark_all(self) -> None:
		self._model.check_all()
		self._update_counter()

	def _on_reactors_changed(self) -> None:
		self._update_counter()
		self._changed()

	def _update_counter(self) -> None:
		total = self._model.rowCount()
		self._counter.setText(f"отмечено {len(self._model.checked_owners())} из {total}")

	# --- блок «Какие реакции» -------------------------------------------------------

	def _build_reactions(self) -> None:
		block = _FormBlock(self, self._box, "Какие реакции и их вес")
		self._reactions_grid = FlowGrid(
			[], self, min_width=_REACTION_MIN_WIDTH, spacing=_CARD_SPACING, stretch=False
		)
		block.add(self._reactions_grid)
		self._reactions_note = block.note("Читаю разрешённые реакции…")

	def _reaction_tile(self, option: ReactionOption) -> QWidget:
		"""Плитка реакции постоянной ширины: флажок, эмодзи и вес."""
		tile = QWidget(self)
		tile.setFixedWidth(_REACTION_MIN_WIDTH)  # макет
		row = QHBoxLayout(tile)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(10)  # макет
		check = CheckBox(tile)
		check.setToolTip(option.title + (" · Premium" if option.premium else ""))
		row.addWidget(check)
		emoji = BodyLabel(option.emoji, tile)
		emoji.setFont(font_px(18))  # макет
		emoji.setFixedWidth(_EMOJI_WIDTH)  # макет
		row.addWidget(emoji)
		weight = SpinBox(tile)
		weight.setRange(*reactions.WEIGHT_RANGE)
		weight.setValue(100)
		weight.setFixedWidth(_WEIGHT_WIDTH)  # макет
		row.addWidget(weight)
		self._reaction_rows[option.emoji] = (check, weight)
		on_change(self._changed, check, weight)
		return tile

	# --- блок «Каким записям» -------------------------------------------------------

	def _build_scope(self) -> None:
		block = _FormBlock(self, self._box, "Каким записям")
		self._scope = ComboBox(self)
		self._scope.setMaximumWidth(_COMBO_MAX_WIDTH)  # макет
		for scope, title in reactions.SCOPE_TITLES.items():
			self._scope.addItem(title, userData=scope)
		block.row("Охват", self._scope)
		self._random_count = number_field(
			self, reactions.RANDOM_COUNT_RANGE, reactions.DEFAULT_RANDOM_COUNT
		)
		self._random_row = block.row("Сколько случайных", self._random_count, unit="записей")
		self._depth = number_field(self, reactions.DEPTH_RANGE, reactions.DEFAULT_DEPTH)
		block.row("Просматривать последних", self._depth, unit="записей ленты")
		self._limit = number_field(self, reactions.LIMIT_RANGE, reactions.DEFAULT_LIMIT)
		block.row("За проход не больше", self._limit, unit="реакций")
		self._pause_min = seconds_field(self, reactions.PAUSE_RANGE, reactions.DEFAULT_PAUSE_S[0])
		self._pause_max = seconds_field(self, reactions.PAUSE_RANGE, reactions.DEFAULT_PAUSE_S[1])
		block.row(
			"Пауза между реакциями",
			self._pause_min,
			BodyLabel("—", self),
			self._pause_max,
			unit="с",
		)
		self._premium_double = CheckBox("ставит две разные реакции", self)
		block.row("Premium", self._premium_double)
		on_change(
			self._changed,
			self._scope,
			self._random_count,
			self._depth,
			self._limit,
			self._pause_min,
			self._pause_max,
			self._premium_double,
		)
		# охват меняет состав строк — связь заводится, когда строки уже есть
		self._scope.currentIndexChanged.connect(self._on_scope)
		self._on_scope()

	def _on_scope(self, *_args: object) -> None:
		"""Число случайных записей нужно только охвату «случайные»."""
		is_random = self._scope.currentData() is ReactionScope.RANDOM_WITHOUT_MINE
		self._random_row.set_visible(is_random)

	# --- списки из движка -----------------------------------------------------------

	def show_reactors(self, executors: list[ExecutorDto], runs: Sequence[TaskRunDto]) -> None:
		"""Раскладывает пользователей пула строками таблицы."""
		times = last_reaction_times(runs)
		chosen = set(self._pending.users) if self._pending else set()
		self._model.set_rows(
			[
				ReactorRow(executor, executor.owner in chosen, times.get(executor.owner))
				for executor in executors
			]
		)
		self._update_counter()

	def show_reaction_options(self, allowed: ChatReactions) -> None:
		"""Раскладывает разрешённые реакции плитками."""
		self._reaction_rows.clear()
		self._reactions_grid.set_cards([self._reaction_tile(option) for option in allowed.options])
		self._reactions_note.setText(
			"В сообществе реакции запрещены — задача невыполнима"
			if not allowed.options
			else "Вес относительный: 50 и 50 — то же, что 100 и 100; 0 — не выпадает."
		)
		self._apply_pending()

	def options_failed(self, message: str) -> None:
		"""Перечень реакций не прочитался — причина на месте, форма живая."""
		self._reactions_note.setText(f"Разрешённые реакции не прочитаны: {message}")

	def _apply_pending(self) -> None:
		"""Сохранённые параметры — по плиткам, когда те уже на экране."""
		params = self._pending
		if params is None:
			return
		weights = {choice.emoji: choice.weight for choice in params.reactions}
		for emoji, (check, weight) in self._reaction_rows.items():
			check.setChecked(emoji in weights)
			if emoji in weights:
				weight.setValue(weights[emoji])

	# --- контракт раздела ------------------------------------------------------------

	def apply_params(self, params: TaskParams) -> None:
		if not isinstance(params, ReactionsParams):
			return
		self._pending = params
		for index in range(self._scope.count()):
			if self._scope.itemData(index) is params.scope:
				self._scope.setCurrentIndex(index)
		self._random_count.setValue(params.random_count)
		self._depth.setValue(params.depth)
		self._limit.setValue(params.limit)
		self._pause_min.setValue(params.pause_min_s)
		self._pause_max.setValue(params.pause_max_s)
		self._premium_double.setChecked(params.premium_double)
		self._model.set_checked(set(params.users))
		self._update_counter()
		self._apply_pending()

	def params(self) -> ReactionsParams:
		scope = self._scope.currentData()
		chosen = tuple(
			ReactionChoice(emoji, weight.value())
			for emoji, (check, weight) in self._reaction_rows.items()
			if check.isChecked()
		)
		pending = self._pending
		return ReactionsParams(
			# пока перечень реакций не пришёл, плиток нет — сохранённый
			# выбор остаётся прежним, а не стирается пустотой
			users=self._model.checked_owners(),
			reactions=chosen if self._reaction_rows or pending is None else pending.reactions,
			scope=scope if isinstance(scope, ReactionScope) else ReactionScope.ALL_WITHOUT_MINE,
			random_count=self._random_count.value(),
			depth=self._depth.value(),
			limit=self._limit.value(),
			pause_min_s=self._pause_min.value(),
			pause_max_s=self._pause_max.value(),
			premium_double=self._premium_double.isChecked(),
		)

	def valid(self) -> bool:
		params = self.params()
		if not params.users:
			return self._error.fail("Отметьте хотя бы одного пользователя.")
		if not any(choice.weight > 0 for choice in params.reactions):
			return self._error.fail("Отметьте хотя бы одну реакцию с ненулевым весом.")
		return self._error.succeed()


# --- настройка одной задачи ---------------------------------------------------


class _TaskDetail(QWidget):
	"""Настройка задачи: путь, шапка с запусками, блоки, расписание, запуски.

	Сохранение одно на всё (спека, раздел 4.4): параметры и расписание
	пишутся вместе, потому что запуск по расписанию идёт именно с этими
	параметрами. Пока форма совпадает с сохранённым, обе кнопки ряда
	неактивны — правило ``form_dirty``.
	"""

	def __init__(
		self,
		panel: TasksPanel,
		parent: QWidget,
		*,
		on_back: Callable[[], None],
		on_fix: Callable[[TaskFixTarget], None],
	) -> None:
		super().__init__(parent)
		self._panel = panel
		self._on_fix = on_fix
		self._task: TaskDto | None = None
		self._saved: TaskForm | None = None
		self._section: _TaskSection | None = None
		self._job: TaskJobDto | None = None
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(16)  # макет
		self._path = BreadcrumbBar(self)
		self._building_path = False
		self._path.currentItemChanged.connect(partial(self._on_path, on_back))
		layout.addWidget(self._path, alignment=Qt.AlignmentFlag.AlignLeft)
		layout.addLayout(self._build_header())
		layout.addWidget(self._build_progress())
		self._error_box = QVBoxLayout()
		layout.addLayout(self._error_box)
		self._section_box = QVBoxLayout()
		self._section_box.setContentsMargins(0, 0, 0, 0)
		self._section_box.setSpacing(density.spacing().block_spacing)
		layout.addLayout(self._section_box)
		self._schedule = _ScheduleBlock(self, layout, self._on_form_changed)
		layout.addLayout(self._build_save_row())
		self._runs_box = QVBoxLayout()
		self._runs_box.setSpacing(density.spacing().row_spacing)
		layout.addLayout(self._runs_box)
		layout.addStretch()

	# --- каркас --------------------------------------------------------------------

	def _build_header(self) -> QHBoxLayout:
		"""Шапка: название, суть и две кнопки запуска."""
		row = QHBoxLayout()
		row.setSpacing(12)  # макет
		column = QVBoxLayout()
		column.setSpacing(2)
		self._title = SubtitleLabel(self)
		column.addWidget(self._title)
		self._hint = CaptionLabel(self)
		self._hint.setWordWrap(True)
		column.addWidget(self._hint)
		row.addLayout(column, stretch=1)
		self._dry_run = PushButton(self)
		self._dry_run.clicked.connect(partial(self._launch, True))
		row.addWidget(self._dry_run, alignment=Qt.AlignmentFlag.AlignTop)
		self._run = PrimaryPushButton(self)
		self._run.clicked.connect(partial(self._launch, False))
		row.addWidget(self._run, alignment=Qt.AlignmentFlag.AlignTop)
		return row

	def _build_progress(self) -> QWidget:
		"""Полоса хода работы с отменой — видна, только пока задача работает."""
		card: QWidget = SimpleCardWidget(self)
		row = QHBoxLayout(card)
		row.setContentsMargins(14, 10, 14, 10)  # макет
		row.setSpacing(12)  # макет
		self._progress_title = StrongBodyLabel(self)
		row.addWidget(self._progress_title)
		self._progress_note = CaptionLabel(self)
		row.addWidget(self._progress_note)
		self._progress = ProgressBar(card)
		row.addWidget(self._progress, stretch=1)
		cancel = PushButton("Отменить", card)
		cancel.clicked.connect(self._on_cancel)
		row.addWidget(cancel)
		card.hide()
		self._progress_card = card
		return card

	def _build_save_row(self) -> QHBoxLayout:
		"""Ряд сохранения: одна кнопка на параметры и расписание."""
		row = QHBoxLayout()
		row.setSpacing(8)  # макет
		row.addStretch()
		self._revert = PushButton("Отменить изменения", self)
		self._revert.clicked.connect(self._on_revert)
		row.addWidget(self._revert)
		self._save = PushButton("Сохранить", self)
		self._save.clicked.connect(self._on_save)
		row.addWidget(self._save)
		self._set_dirty(False)
		return row

	# --- показ задачи ---------------------------------------------------------------

	def show_task(self, task: TaskDto, runs: Sequence[TaskRunDto]) -> None:
		"""Показывает задачу: блоки раздела, расписание, запуски, ошибку."""
		fresh = self._task is None or self._task.kind is not task.kind
		self._task = task
		# путь — при каждом показе: клик по «Задачи» снимает у строки пути
		# всё правее себя (так устроен BreadcrumbBar), и повторное открытие
		# той же задачи осталось бы с одним «Задачи»
		self.render_path()
		if fresh:
			self._mount_section(task.kind)
			texts = TASK_TEXTS[task.kind]
			self._title.setText(KIND_TITLES[task.kind])
			self._hint.setText(texts.detail)
			self._dry_run.setText(texts.dry_run)
			self._run.setText(texts.run)
		section = self._section
		if section is not None:
			section.apply_params(task.params)
			section.show_runs(runs)
		self._schedule.present(task)
		self._saved = TaskForm(task.params, task.schedule, task.enabled)
		self._set_dirty(False)
		self._render_runs(runs)
		self._render_error(runs[0] if runs else None)

	def _mount_section(self, kind: TaskKind) -> None:
		"""Меняет блоки параметров на блоки этой задачи."""
		clear_layout(self._section_box)
		section = SECTIONS[kind](self._panel, self, self._on_form_changed)
		self._section_box.addWidget(section)
		self._section = section

	def render_path(self) -> None:
		"""Строка пути: «Задачи» › название открытой задачи."""
		if self._task is None:
			return
		kind = self._task.kind
		self._building_path = True
		try:
			self._path.clear()
			self._path.addItem("tasks", "Задачи")
			self._path.addItem(str(kind), KIND_TITLES[kind])
		finally:
			self._building_path = False

	def _on_path(self, on_back: Callable[[], None], route_key: str) -> None:
		if not self._building_path and route_key == "tasks":
			on_back()

	def _render_runs(self, runs: Sequence[TaskRunDto]) -> None:
		"""Последние запуски таблицей; без запусков — строка вместо неё."""
		clear_layout(self._runs_box)
		journal = list_button("Весь журнал…", self)
		journal.clicked.connect(self._on_journal)
		self._runs_box.addWidget(section_header(self, "Последние запуски", trailing=[journal]))
		if not runs:
			self._runs_box.addWidget(CaptionLabel("Запусков ещё не было.", self))
			return
		self._runs_box.addWidget(runs_table(self, runs[:RUNS_IN_DETAIL]))

	def _render_error(self, run: TaskRunDto | None) -> None:
		"""Строка ошибки последнего запуска — с переходом туда, где её чинят."""
		clear_layout(self._error_box)
		if run is None or run.outcome is not RunOutcome.ERROR:
			return
		bar = InfoBar.error(
			title="Последний запуск не удался",
			content=f"{when_text(run.started_at)}: {run.error or 'причина не записана'}",
			isClosable=False,
			duration=-1,
			position=InfoBarPosition.NONE,
			parent=self,
		)
		target = error_fix_target(run)
		if target is not None:
			fix = PushButton("Участники…", bar)
			fix.clicked.connect(partial(self._on_fix, target))
			bar.addWidget(fix)
		self._error_box.addWidget(bar)

	# --- ход работы -----------------------------------------------------------------

	def set_job(self, job: TaskJobDto | None) -> None:
		"""Показывает или прячет полосу хода работы этой задачи."""
		self._job = job
		self._progress_card.setVisible(job is not None)
		self._dry_run.setEnabled(job is None)
		self._run.setEnabled(job is None)
		if job is None:
			return
		texts = TASK_TEXTS[job.kind]
		self._progress_title.setText(texts.dry_status if job.dry_run else texts.status)
		self._progress_note.setText(progress_caption(job))
		self._progress.setValue(int(job.progress * 100))

	def _on_cancel(self) -> None:
		if self._job is not None:
			self._panel.cancel(self._job.id)

	# --- форма ----------------------------------------------------------------------

	def _current_form(self) -> TaskForm | None:
		section = self._section
		if section is None:
			return None
		return TaskForm(section.params(), self._schedule.schedule(), self._schedule.enabled)

	def _on_form_changed(self) -> None:
		current = self._current_form()
		self._set_dirty(current is not None and form_dirty(self._saved, current))

	def _set_dirty(self, dirty: bool) -> None:
		self._dirty = dirty
		self._save.setEnabled(dirty)
		self._revert.setEnabled(dirty)

	@property
	def dirty(self) -> bool:
		"""Есть ли несохранённые правки."""
		return self._dirty

	@property
	def kind(self) -> TaskKind | None:
		"""Вид открытой задачи; None — настройку ещё не открывали."""
		return self._task.kind if self._task is not None else None

	@property
	def section(self) -> _TaskSection | None:
		"""Блоки открытой задачи (нужны спискам «Реакций»)."""
		return self._section

	def _on_revert(self) -> None:
		"""Возвращает форму к сохранённому снимку."""
		if self._task is not None:
			self.show_task(self._task, self._panel.runs_of(self._task.kind))

	def _on_save(self) -> None:
		"""Кнопка «Сохранить»."""
		self.save()

	def save(self, then: Callable[[], None] | None = None) -> None:
		"""Сохраняет параметры и расписание одним вызовом движка.

		``then`` — что сделать после ответа движка (уход с настройки):
		сохранение асинхронное, и уходить раньше ответа нельзя — при
		отказе движка человек остался бы без правок и без причины.
		Проверка не прошла или включение не подтвердили — ``then``
		не зовётся, человек остаётся с правками на месте.

		Вопрос о расписании задаётся только при его **включении**:
		запуск по расписанию идёт без подтверждений, и спросить нужно
		один раз, а не при каждом сохранении включённого.
		"""
		task, section, saved = self._task, self._section, self._saved
		if task is None or section is None or not self._schedule.validate():
			return
		enabled = self._schedule.enabled
		schedule = self._schedule.schedule()
		params = section.params()
		turning_on = enabled and (saved is None or not saved.enabled)
		if turning_on and not confirm_delete(
			self,
			schedule_confirmation(task.kind, params, schedule, self._panel.community.title),
			accept_text="Включить",
		):
			return
		self._panel.save_task(task, params, schedule, enabled=enabled, then=then)

	def discard(self) -> None:
		"""Отбрасывает правки: форма возвращается к сохранённому."""
		self._on_revert()

	def _launch(self, dry_run: bool) -> None:
		"""Ставит запуск с текущими значениями формы."""
		task, section = self._task, self._section
		if task is None or section is None or not section.valid():
			return
		params = section.params()
		if not dry_run:
			text = run_confirmation(task.kind, params, self._panel.community.title)
			if text and not confirm_delete(self, text, accept_text=TASK_TEXTS[task.kind].run):
				return
		self._panel.launch(task, params, dry_run=dry_run)

	def _on_journal(self) -> None:
		if self._task is not None:
			self._panel.open_journal(self._task)

	def show_report(self, item: TaskJobDto) -> None:
		"""Отдаёт отчёт разделу, если открыта его задача."""
		section = self._section
		if section is not None and self._task is not None and self._task.kind is item.kind:
			section.show_report(item)


def runs_table(parent: QWidget, runs: Sequence[TaskRunDto]) -> TableWidget:
	"""Таблица запусков: когда, запуск, исполнитель, итог (ошибка — цветом)."""
	table = TableWidget(parent)
	table.setColumnCount(4)
	table.setHorizontalHeaderLabels(["Когда", "Запуск", "Исполнитель", "Итог"])
	table.setBorderRadius(6)
	table.setWordWrap(False)
	table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
	table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
	table.setRowCount(len(runs))
	vertical = table.verticalHeader()
	if vertical is not None:
		vertical.hide()
		vertical.setDefaultSectionSize(_TABLE_ROW_HEIGHT)
	header = table.horizontalHeader()
	if header is not None:
		header.setStretchLastSection(True)
		for column, width in enumerate(_RUNS_COLUMN_WIDTHS):
			table.setColumnWidth(column, width)  # макет
	for index, run in enumerate(runs):
		cells = (
			format_local(run.started_at),
			run_kind_text(run),
			run.executor_label or "—",
			run_result_text(run),
		)
		failed = run.outcome is RunOutcome.ERROR
		for column, text in enumerate(cells):
			item = QTableWidgetItem(text)
			if failed and column == len(cells) - 1:
				item.setForeground(theme_color(ERROR_TEXT))
			item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
			table.setItem(index, column, item)
	table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
	table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
	height = len(runs) * _TABLE_ROW_HEIGHT + (header.height() if header is not None else 0) + 2
	table.setFixedHeight(height)
	return table


#: Раздел параметров по виду задачи (карта вместо цепочки условий).
SECTIONS: dict[TaskKind, type[_TaskSection]] = {
	TaskKind.SERVICE_MESSAGES: _ServiceMessagesSection,
	TaskKind.DELETED_ACCOUNTS: _DeletedAccountsSection,
	TaskKind.REACTIONS: _ReactionsSection,
	TaskKind.JOIN_REQUESTS: _JoinRequestsSection,
}


class TasksPanel(QWidget):
	"""Тело задач: обзор карточками и настройка одной задачи (ADR-0038).

	Один и тот же виджет живёт во вкладке страницы сообщества и в рабочем
	окне с дашборда. Ход работы приходит от наблюдателя очереди задач
	при главном окне (ADR-0034): вкладка присоединяется и отсоединяется
	через :meth:`set_active` (невидимая вкладка ничего не перерисовывает),
	окно живёт присоединённым.
	"""

	#: Ошибку чинят на другой вкладке — владелец знает, как туда попасть.
	fix_requested = Signal(str)

	def __init__(
		self, worker: EngineWorker, watcher: QueueWatcher, community: CommunityDto, parent: QWidget
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._watcher = watcher
		self.community = community
		self._show_error = error_reporter(self)
		self._kinds = available_kinds(community.kind)
		self._tasks: dict[TaskKind, TaskDto] = {}
		self._runs: dict[TaskKind, list[TaskRunDto]] = {}
		self._jobs: dict[TaskKind, TaskJobDto] = {}
		# задания, о чьей ошибке уже узнали: снимаем их с очереди один раз
		self._seen_errors: set[int] = set()
		self._view = QueueView(on_state=self._on_jobs, on_finished=self._on_job_finished)
		self._build()
		self.reload()

	def _build(self) -> None:
		"""Каркас: стопка из двух страниц — обзор и настройка."""
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().block_spacing)
		self._stack = QStackedWidget(self)
		self._overview = _TasksOverview(
			self._kinds, self, on_open=self.open_task, on_toggle=self._on_toggle
		)
		self._overview.set_publisher(self.community.default_account_label)
		self._detail = _TaskDetail(self, self, on_back=self.show_overview, on_fix=self._on_fix)
		self._stack.addWidget(self._overview)
		self._stack.addWidget(self._detail)
		layout.addWidget(self._stack)
		self._show_page(self._overview)

	def _show_page(self, page: QWidget) -> None:
		"""Показывает страницу стопки; скрытая не участвует в её высоте.

		Штатный ``QStackedWidget`` меряет все страницы разом, и под
		короткой настройкой оставалась бы пустота высотой в обзор.
		"""
		for widget in (self._overview, self._detail):
			policy = QSizePolicy.Policy.Preferred if widget is page else QSizePolicy.Policy.Ignored
			widget.setSizePolicy(policy, policy)
		self._stack.setCurrentWidget(page)

	def set_active(self, active: bool) -> None:
		"""Присоединяет панель к наблюдателю очереди задач или отсоединяет."""
		if active:
			self._watcher.attach(self, self._view)
			self.reload()
		else:
			self._watcher.detach(self._view)

	# --- данные ---------------------------------------------------------------------

	def reload(self) -> None:
		"""Перечитывает задачи сообщества и их последние запуски."""
		for kind in self._kinds:
			run_in_engine(
				self._worker,
				self._worker.engine.tasks.task(self.community.id, kind),
				self,
				partial(self._on_task, kind),
				self._show_error,
			)

	def _on_task(self, kind: TaskKind, task: TaskDto) -> None:
		"""Строка задачи получена — читаем её последние запуски."""
		self._tasks[kind] = task
		self._refresh_card(kind)
		# «Реакции» показывают, когда каждый исполнитель ходил последний раз,
		# а «Служебные записи» — числа последнего просмотра; то и другое
		# считается по журналу, поэтому запусков им нужно больше трёх
		limit = RUNS_SHOWN if kind in _WIDE_RUNS else RUNS_IN_DETAIL
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.runs(task.id, limit),
			self,
			partial(self._on_runs, kind),
			self._show_error,
		)

	def _on_runs(self, kind: TaskKind, runs: list[TaskRunDto]) -> None:
		self._runs[kind] = runs
		self._refresh_card(kind)
		task = self._tasks.get(kind)
		if task is not None and self._detail.kind is kind and not self._detail.dirty:
			self._detail.show_task(task, runs)

	def runs_of(self, kind: TaskKind) -> list[TaskRunDto]:
		"""Последние запуски задачи (то, что уже прочитано)."""
		return self._runs.get(kind, [])

	def _last_run(self, kind: TaskKind) -> TaskRunDto | None:
		runs = self._runs.get(kind)
		return runs[0] if runs else None

	def _refresh_card(self, kind: TaskKind) -> None:
		"""Приводит карточку обзора к свежим данным задачи."""
		card = self._overview.cards.get(kind)
		if card is not None:
			card.show_task(self._tasks.get(kind), self._last_run(kind), self._jobs.get(kind))

	# --- переходы -------------------------------------------------------------------

	def open_task(self, kind: TaskKind) -> None:
		"""Открывает настройку задачи (клик по карточке обзора)."""
		task = self._tasks.get(kind)
		if task is None:
			return
		self._detail.show_task(task, self.runs_of(kind))
		self._detail.set_job(self._jobs.get(kind))
		self._read_reaction_lists(kind)
		self._show_page(self._detail)

	def show_overview(self) -> None:
		"""Путь «Задачи»: к обзору; с несохранёнными правками — через вопрос."""
		self.leave(partial(self._show_page, self._overview), stay=self._detail.render_path)

	@property
	def dirty(self) -> bool:
		"""Есть ли в открытой настройке несохранённые правки."""
		return self._stack.currentWidget() is self._detail and self._detail.dirty

	def leave(self, then: Callable[[], None], *, stay: Callable[[], None] | None = None) -> None:
		"""Уход с настройки: сразу — без правок, иначе по ответу человека.

		Одно правило на все уходы из спеки (раздел 4.4): путь «Задачи»,
		другая вкладка, другое сообщество. «Сохранить» уводит только после
		ответа движка (``then`` зовёт сохранение), «Не сохранять» —
		отбрасывает правки и уводит, «Отмена» — остаётся (``stay`` —
		что вернуть на место, например строку пути или вкладку).
		"""
		if not self.dirty:
			then()
			return
		choice = ask_save_changes(self, SAVE_ON_LEAVE_HINT)
		if choice is SaveChoice.SAVE:
			self._detail.save(then=then)
		elif choice is SaveChoice.DISCARD:
			self._detail.discard()
			then()
		elif stay is not None:
			stay()

	def _read_reaction_lists(self, kind: TaskKind) -> None:
		"""Списки «Реакций» приходят из движка позже строки задачи."""
		section = self._detail.section
		if kind is not TaskKind.REACTIONS or not isinstance(section, _ReactionsSection):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.reactors(self.community.id),
			self,
			lambda users: section.show_reactors(users, self.runs_of(kind)),
			self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.reaction_options(self.community.id),
			self,
			section.show_reaction_options,
			section.options_failed,
		)

	def _on_fix(self, target: TaskFixTarget) -> None:
		"""Кнопка в строке ошибки: владелец переключит нужную вкладку."""
		self.fix_requested.emit(str(target))

	# --- операции -------------------------------------------------------------------

	def _on_toggle(self, kind: TaskKind, enabled: bool) -> None:
		"""Тумблер карточки: включает или выключает расписание задачи."""
		task = self._tasks.get(kind)
		if task is None or task.enabled == enabled:
			return
		if enabled and not confirm_delete(
			self,
			schedule_confirmation(kind, task.params, task.schedule, self.community.title),
			accept_text="Включить",
		):
			self._refresh_card(kind)  # тумблер возвращается на место
			return
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.save_schedule(task.id, task.schedule, enabled=enabled),
			self,
			partial(self._on_saved, kind),
			partial(self._on_toggle_failed, kind),
		)

	def _on_toggle_failed(self, kind: TaskKind, message: str) -> None:
		"""Движок отказал (например, параметры не годятся) — сказать и вернуть тумблер."""
		self._show_error(message)
		self._refresh_card(kind)

	def save_task(
		self,
		task: TaskDto,
		params: TaskParams,
		schedule: Schedule,
		*,
		enabled: bool,
		then: Callable[[], None] | None = None,
	) -> None:
		"""Сохраняет параметры и расписание; ``then`` — после успеха."""

		def saved(fresh: TaskDto) -> None:
			self._on_saved(task.kind, fresh)
			if then is not None:
				then()

		run_in_engine(
			self._worker,
			self._worker.engine.tasks.save_task(task.id, params, schedule, enabled=enabled),
			self,
			saved,
			self._show_error,
		)

	def _on_saved(self, kind: TaskKind, task: TaskDto) -> None:
		"""Свежая строка задачи: карточка и открытая настройка."""
		self._tasks[kind] = task
		self._refresh_card(kind)
		if self._detail.kind is kind:
			self._detail.show_task(task, self.runs_of(kind))

	def launch(self, task: TaskDto, params: TaskParams, *, dry_run: bool) -> None:
		"""Ставит запуск задачи с текущими значениями формы."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.run_now(task.id, params, dry_run=dry_run),
			self,
			noop,
			self._show_error,
		)

	def cancel(self, job_id: int) -> None:
		"""Отменяет идущее задание («Отменить» в полосе хода)."""
		run_in_engine(
			self._worker, self._worker.engine.tasks.cancel(job_id), self, noop, self._show_error
		)

	def open_journal(self, task: TaskDto) -> None:
		"""Открывает окно журнала запусков задачи."""
		exec_dialog(TaskRunsDialog(self._worker, task, self.community, self.window()))

	# --- ход работы -----------------------------------------------------------------

	def _on_jobs(self, items: list[Any]) -> None:
		"""Снимок очереди задач: полосы хода на карточках и в настройке."""
		mine = [item for item in items if item.community_id == self.community.id]
		self._jobs = {item.kind: item for item in mine if item.status is not JobStatus.ERROR}
		for item in mine:
			if item.status is JobStatus.ERROR and item.id not in self._seen_errors:
				# задание с ошибкой очередь не покидает; причина уже
				# записана в журнал запусков, и держать его невидимым
				# в очереди незачем — снимаем и перечитываем задачу
				self._seen_errors.add(item.id)
				self._dismiss(item.id)
				self._read_task(item.kind)
		for kind in self._kinds:
			self._refresh_card(kind)
		open_kind = self._detail.kind
		self._detail.set_job(self._jobs.get(open_kind) if open_kind is not None else None)

	def _dismiss(self, job_id: int) -> None:
		run_in_engine(
			self._worker, self._worker.engine.tasks.dismiss(job_id), self, noop, self._show_error
		)

	def _read_task(self, kind: TaskKind) -> None:
		"""Перечитывает одну задачу (после исхода запуска)."""
		if kind in self._kinds:
			run_in_engine(
				self._worker,
				self._worker.engine.tasks.task(self.community.id, kind),
				self,
				partial(self._on_task, kind),
				self._show_error,
			)

	def _on_job_finished(self, item: Any, done: bool) -> None:
		"""Задание покинуло очередь: отчёт — разделу, свежий журнал — всем."""
		if item.community_id != self.community.id:
			return
		if done:
			self._detail.show_report(item)
		self._read_task(item.kind)


class TaskRunsDialog(WorkDialog):
	"""Журнал запусков одной задачи: таблица запусков и события выбранного."""

	def __init__(
		self, worker: EngineWorker, task: TaskDto, community: CommunityDto, parent: QWidget
	) -> None:
		super().__init__(f"Журнал · {KIND_TITLES[task.kind]} · {community.title}", parent)
		self._worker = worker
		self._task = task
		self._runs: list[TaskRunDto] = []
		self._show_error = error_reporter(self)
		self._table = TableWidget(self)
		self._table.setColumnCount(4)
		self._table.setHorizontalHeaderLabels(["Когда", "Запуск", "Исполнитель", "Итог"])
		self._table.setBorderVisible(True)
		self._table.setWordWrap(False)
		self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
		self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
		self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
		vertical = self._table.verticalHeader()
		if vertical is not None:
			vertical.hide()
			vertical.setDefaultSectionSize(_TABLE_ROW_HEIGHT)
		header = self._table.horizontalHeader()
		if header is not None:
			header.setStretchLastSection(True)
			header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
			header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
		self._table.itemSelectionChanged.connect(self._on_selected)
		self.content.addWidget(self._table, stretch=2)
		self.content.addWidget(CaptionLabel("События выбранного запуска", self))
		self._events = TextEdit(self)
		self._events.setReadOnly(True)
		self.content.addWidget(self._events, stretch=1)
		refresh = PushButton("Обновить", self)
		refresh.clicked.connect(self.reload)
		self.buttons.addWidget(refresh)
		self.add_close_button()
		self.reload()

	def reload(self) -> None:
		"""Перечитывает журнал из движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.runs(self._task.id, RUNS_SHOWN),
			self,
			self._show,
			self._show_error,
		)

	def _show(self, runs: list[TaskRunDto]) -> None:
		"""Заполняет таблицу: новые запуски сверху."""
		self._runs = runs
		self._table.setRowCount(len(runs))
		for index, run in enumerate(runs):
			cells = (
				format_local(run.started_at),
				run_kind_text(run),
				run.executor_label or "—",
				run_result_text(run),
			)
			for column, text in enumerate(cells):
				item = QTableWidgetItem(text)
				item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
				self._table.setItem(index, column, item)
		self._events.setPlainText("" if runs else "Запусков ещё не было.")
		if runs:
			self._table.selectRow(0)

	def _on_selected(self) -> None:
		"""Показывает события выбранного запуска."""
		rows = {index.row() for index in self._table.selectedIndexes()}
		if not rows:
			return
		row = min(rows)
		if 0 <= row < len(self._runs):
			self._events.setPlainText(run_events_text(self._runs[row]))


class TasksDialog(WorkDialog):
	"""Рабочее окно задач (с дашборда): та же панель, что во вкладке.

	Панель лежит в прокрутке: настройка «Реакций» длиннее окна,
	а во вкладке её прокручивает страница, здесь — окно. Вкладок
	в окне нет, поэтому переход «чинить ошибку на Участниках» закрывает
	окно и зовёт того, кто его открыл (``on_members``).
	"""

	def __init__(
		self,
		worker: EngineWorker,
		watcher: QueueWatcher,
		community: CommunityDto,
		parent: QWidget,
		on_members: Callable[[], None] | None = None,
	) -> None:
		super().__init__(f"Задачи · {community.title}", parent)
		self._on_members = on_members
		panel = TasksPanel(worker, watcher, community, self)
		# окно живёт присоединённым к наблюдателю, пока открыто; после
		# закрытия окно удаляется, и наблюдатель отсеивает его сам
		panel.set_active(True)
		panel.fix_requested.connect(self._on_fix)
		area = ScrollArea(self)
		area.setWidget(panel)
		area.setWidgetResizable(True)
		area.enableTransparentBackground()
		self.content.addWidget(area, stretch=1)

	def _on_fix(self, target: str) -> None:
		"""«Участники…» в строке ошибки: закрыть окно и открыть исполнителей."""
		if target == TaskFixTarget.MEMBERS and self._on_members is not None:
			self.accept()
			self._on_members()


def open_tasks(
	worker: EngineWorker,
	watcher: QueueWatcher,
	community: CommunityDto,
	parent: QWidget,
	on_members: Callable[[], None] | None = None,
) -> None:
	"""Открывает окно задач сообщества.

	``watcher`` — наблюдатель очереди задач при главном окне (ADR-0034):
	окно и вкладка страницы сообщества смотрят на одну очередь.
	``on_members`` — куда вести чинить ошибку запуска правами и пулом.
	"""
	exec_dialog(TasksDialog(worker, watcher, community, parent.window(), on_members))
