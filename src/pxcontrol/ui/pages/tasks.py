"""Задачи сообщества: разделы по видам, запуск, ход работы, журнал (ADR-0038).

Вкладка «Задачи» страницы сообщества и содержимое рабочего окна
с дашборда. Разделы-сегменты — по одному на вид задачи: **служебные
записи** («такой-то вступил», «сообщение закреплено») и **удалённые
аккаунты** — мёртвые души в списке участников. У каждого раздела свои
параметры, свой итог и своя кнопка «Журнал…» — запуски этой задачи
в этом сообществе.

Ручной запуск идёт по одному правилу: сначала «Просмотреть» — он
ничего не меняет и отвечает числами, — и только потом удаление
с подтверждением. Порядок не для удобства: удаление необратимо.

Ход работы виден панелью очереди (той же, что на «Публикации»
и «Видео»): прогресс, отмена, повтор после ошибки.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QAbstractItemView,
	QHBoxLayout,
	QHeaderView,
	QStackedWidget,
	QTableWidgetItem,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	ComboBox,
	DoubleSpinBox,
	LineEdit,
	PushButton,
	SegmentedWidget,
	SpinBox,
	StrongBodyLabel,
	SwitchButton,
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
	JoinRequestsReport,
	MembersReport,
	ReactionChoice,
	ReactionScope,
	ReactionsParams,
	ReactionsReport,
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
	OwnerKind,
	ReactionOption,
	ServiceMessageKind,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ErrorLabel,
	WorkDialog,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
	format_local,
	list_area,
)
from pxcontrol.ui.pages.queue_panel import QueuePanel
from pxcontrol.ui.queue_watcher import QueueWatcher

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

#: Сколько запусков читать в журнал за раз.
RUNS_SHOWN = 100

#: Названия видов задач по-русски (заголовки сегментов и журнала).
KIND_TITLES: dict[TaskKind, str] = {
	TaskKind.SERVICE_MESSAGES: service_messages.TITLE.noun,
	TaskKind.DELETED_ACCOUNTS: deleted_accounts.TITLE.noun,
	TaskKind.REACTIONS: reactions.TITLE.noun,
	TaskKind.JOIN_REQUESTS: join_requests.TITLE.noun,
}


def reactor_label(executor: ExecutorDto) -> str:
	"""Подпись пользователя в списке реагирующих: имя и состояние."""
	notes = []
	if executor.paused:
		notes.append("приостановлен")
	if not executor.status.in_community:
		notes.append("не состоит")
	return f"{executor.label} ({', '.join(notes)})" if notes else executor.label


#: Виды расписания в порядке показа и их подписи в выпадающем списке.
SCHEDULE_KINDS: tuple[tuple[ScheduleKind, str], ...] = (
	(ScheduleKind.NONE, "Только по требованию"),
	(ScheduleKind.INTERVAL, "С интервалом (минуты, случайно в промежутке)"),
	(ScheduleKind.DAILY, "В моменты суток (ЧЧ:ММ через запятую)"),
)


def next_run_text(task: TaskDto) -> str:
	"""Строка о расписании под формой: что задано и когда следующий запуск."""
	text = schedule_text(task.schedule)
	if task.schedule.kind is ScheduleKind.NONE:
		return f"Расписание: {text}"
	if not task.enabled:
		return f"Расписание выключено ({text})"
	if task.next_run_at is None:
		return f"Расписание: {text} — следующий запуск не назначен"
	return f"Расписание: {text} — следующий запуск {format_local(task.next_run_at)}"


def parse_times(text: str) -> tuple[str, ...]:
	"""Моменты суток из поля «через запятую» (пустые куски пропускаются)."""
	return tuple(token.strip() for token in text.split(",") if token.strip())


#: Высота строки таблицы журнала и кегль (как у списка дашборда).
_TABLE_ROW_HEIGHT = 34
_TABLE_FONT_PX = 13


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


class _TaskSection(QWidget):
	"""Раздел одного вида задачи: форма параметров, запуск, итог, журнал.

	Строка задачи читается из движка при показе (заводится с умолчаниями,
	если её ещё не было), и до её прихода кнопки запуска неактивны:
	запускать нечего — параметры и номер задачи ещё не известны.
	"""

	kind: TaskKind

	def __init__(self, panel: TasksPanel, page_parent: QWidget) -> None:
		super().__init__(page_parent)
		self._panel = panel
		self._task: TaskDto | None = None
		self._run_buttons: list[PushButton] = []
		self._schedule: _ScheduleEditor

	# --- контракт раздела ------------------------------------------------------------

	def mount(self, task: TaskDto) -> None:
		"""Принимает строку задачи: параметры и расписание — в форму, кнопки — включить."""
		self._task = task
		self.apply_params(task.params)
		self._schedule.present(task)
		for button in self._run_buttons:
			button.setEnabled(True)

	def confirm_schedule(self, schedule: Schedule) -> bool:
		"""Подтверждение включения расписания — один раз при сохранении.

		Обычный запуск по расписанию необратим (удаляет, исключает),
		а спросить перед каждым ночным запуском некого (ADR-0038);
		раздел перечисляет, что именно будет делаться.
		"""
		raise NotImplementedError

	def apply_params(self, params: TaskParams) -> None:
		"""Раскладывает сохранённые параметры по полям формы."""
		raise NotImplementedError

	def params(self) -> TaskParams:
		"""Параметры из полей формы."""
		raise NotImplementedError

	def show_report(self, item: TaskJobDto) -> None:
		"""Показывает отчёт завершённого запуска."""
		raise NotImplementedError

	def set_status(self, text: str) -> None:
		"""Строка состояния раздела («Просмотр идёт…», «Не удалось: …»)."""
		raise NotImplementedError

	# --- общее -------------------------------------------------------------------

	@property
	def task(self) -> TaskDto | None:
		"""Строка задачи (None — ещё не прочитана)."""
		return self._task

	def run_button(self, text: str, parent: QWidget) -> PushButton:
		"""Кнопка запуска: неактивна, пока задача не прочитана."""
		button = PushButton(text, parent)
		button.setEnabled(False)
		self._run_buttons.append(button)
		return button

	def journal_row(self, parent: QWidget) -> QHBoxLayout:
		"""Строка с кнопкой «Журнал…» — запуски этой задачи."""
		row = QHBoxLayout()
		row.addStretch()
		button = self.run_button("Журнал…", parent)
		button.clicked.connect(self._open_journal)
		row.addWidget(button)
		return row

	def schedule_block(self, parent: QWidget) -> QWidget:
		"""Блок расписания: вид, границы, включено, «Сохранить расписание»."""
		self._schedule = _ScheduleEditor(parent, on_save=self._on_save_schedule)
		self._run_buttons.append(self._schedule.save_button)
		return self._schedule

	def _on_save_schedule(self, schedule: Schedule, enabled: bool) -> None:
		"""Сохраняет расписание через панель (с подтверждением при включении)."""
		if self._task is None:
			return
		if enabled and not self.confirm_schedule(schedule):
			return
		self._panel.save_schedule(self, self._task, schedule, enabled=enabled)

	def _open_journal(self) -> None:
		"""Открывает окно журнала запусков этой задачи."""
		if self._task is not None:
			self._panel.open_journal(self._task)

	def launch(self, *, dry_run: bool, status_text: str) -> None:
		"""Ставит запуск с параметрами из формы (через панель)."""
		if self._task is None:
			return
		self._panel.launch(
			self, self._task, self.params(), dry_run=dry_run, status_text=status_text
		)


class _ServiceMessagesSection(_TaskSection):
	"""Раздел «служебные записи»: глубина, просмотр, виды галочками, чистка."""

	kind = TaskKind.SERVICE_MESSAGES

	def __init__(self, panel: TasksPanel, page_parent: QWidget) -> None:
		super().__init__(panel, page_parent)
		self._boxes: dict[ServiceMessageKind, CheckBox] = {}
		self._chosen: tuple[ServiceMessageKind, ...] = service_messages.DEFAULT_KINDS
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(service_messages.TITLE.hint, self)
		hint.setWordWrap(True)
		box.addWidget(hint)
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Просмотреть последних сообщений:", self))
		self._depth = SpinBox(self)
		self._depth.setRange(*service_messages.DEPTH_RANGE)
		self._depth.setSingleStep(500)
		self._depth.setValue(service_messages.DEFAULT_DEPTH)
		self._depth.setToolTip(
			"У Telegram нет поиска «только служебные» — историю приходится "
			"читать целиком, поэтому у прохода есть глубина"
		)
		row.addWidget(self._depth)
		row.addStretch()
		scan = self.run_button("Просмотреть", self)
		scan.clicked.connect(lambda: self.launch(dry_run=True, status_text="Просмотр идёт…"))
		row.addWidget(scan)
		box.addLayout(row)
		self._label = BodyLabel("Просмотр ещё не выполнялся.", self)
		self._label.setWordWrap(True)
		box.addWidget(self._label)
		area, self._kinds_box = list_area(self, density.spacing().list_spacing)
		box.addWidget(area, stretch=1)
		# проверка заполнения — красной подписью рядом с формой, а не
		# всплывающей плашкой: плашка уезжает и не привязана к месту,
		# где человеку нужно что-то поправить (единый приём проекта)
		self._error = ErrorLabel(self)
		box.addWidget(self._error)
		box.addLayout(self._clean_row())
		box.addWidget(self.schedule_block(self))
		box.addLayout(self.journal_row(self))

	def _clean_row(self) -> QHBoxLayout:
		"""Предел удаления и кнопка чистки записей."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Удалять за проход не больше:", self))
		self._delete_limit = SpinBox(self)
		self._delete_limit.setRange(*service_messages.DELETE_LIMIT_RANGE)
		self._delete_limit.setSingleStep(100)
		self._delete_limit.setValue(service_messages.DEFAULT_DELETE_LIMIT)
		self._delete_limit.setToolTip(
			"Сотни однотипных действий подряд с пользовательского аккаунта "
			"Telegram не приветствует — предел сдерживает поток"
		)
		row.addWidget(self._delete_limit)
		row.addStretch()
		self._clean_button = PushButton("Удалить выбранное", self)
		self._clean_button.setEnabled(False)
		self._clean_button.clicked.connect(self._on_clean)
		row.addWidget(self._clean_button)
		return row

	def apply_params(self, params: TaskParams) -> None:
		if not isinstance(params, ServiceMessagesParams):
			return
		self._depth.setValue(params.depth)
		self._delete_limit.setValue(params.delete_limit)
		self._chosen = params.kinds

	def params(self) -> ServiceMessagesParams:
		chosen = tuple(kind for kind, box in self._boxes.items() if box.isChecked())
		return ServiceMessagesParams(
			depth=self._depth.value(),
			# пока найденное не показано, галочек нет — сохранённый набор
			# видов остаётся прежним, а не стирается пустотой
			kinds=chosen if self._boxes else self._chosen,
			delete_limit=self._delete_limit.value(),
		)

	def set_status(self, text: str) -> None:
		self._label.setText(text)

	def confirm_schedule(self, schedule: Schedule) -> bool:
		params = self.params()
		chosen = service_messages.selectable_kinds(params.kinds)
		if not chosen:
			self._error.fail("Для чистки по расписанию отметьте хотя бы один вид записей.")
			return False
		self._error.succeed()
		names = "\n".join(f"— {service_messages.kind_title(kind)};" for kind in chosen)
		return confirm_delete(
			self,
			f"Включить чистку служебных записей в «{self._panel.community.title}» "
			f"по расписанию ({schedule_text(schedule)})?\n\n{names}\n\n"
			f"Каждый запуск удалит не больше {params.delete_limit} записей без "
			"дополнительного подтверждения. Удаление необратимо.",
			accept_text="Включить",
		)

	def _on_clean(self) -> None:
		"""Спрашивает подтверждение и ставит чистку записей."""
		chosen = [kind for kind, box in self._boxes.items() if box.isChecked()]
		if not chosen:
			self._error.fail("Отметьте хотя бы один вид записей.")
			return
		self._error.succeed()
		names = "\n".join(f"— {service_messages.kind_title(kind)};" for kind in chosen)
		community = self._panel.community
		if not confirm_delete(
			self,
			f"Удалить служебные записи в «{community.title}»?\n\n{names}\n\n"
			f"Удаление необратимо. За проход будет удалено не больше "
			f"{self._delete_limit.value()} записей.",
		):
			return
		self._clean_button.setEnabled(False)
		self.launch(dry_run=False, status_text="Чистка идёт…")

	def show_report(self, item: TaskJobDto) -> None:
		"""Показывает найденные записи: строка на вид, галочка у удаляемых."""
		report = item.report
		if not isinstance(report, ServiceReport):
			return
		self._label.setText(service_messages.service_summary(report))
		clear_layout(self._kinds_box)
		self._boxes.clear()
		if report.deleted or report.skipped:
			# после чистки числа устарели — честнее попросить посмотреть заново
			self._clean_button.setEnabled(False)
			return
		for kind, count in sorted(report.found.items(), key=lambda pair: -pair[1]):
			self._kinds_box.addWidget(self._kind_row(kind, count))
		self._clean_button.setEnabled(report.removable > 0)

	def _kind_row(self, kind: ServiceMessageKind, count: int) -> QWidget:
		"""Строка вида: галочка (если удаляем), название и число."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		if kind.removable():
			check = CheckBox(service_messages.kind_title(kind), box)
			check.setChecked(kind in self._chosen)
			self._boxes[kind] = check
			row.addWidget(check, stretch=1)
		else:
			label = BodyLabel(f"{service_messages.kind_title(kind)} — {_PROTECTED_HINT}", box)
			label.setWordWrap(True)
			row.addWidget(label, stretch=1)
		row.addWidget(StrongBodyLabel(str(count), box))
		return box


class _DeletedAccountsSection(_TaskSection):
	"""Раздел «удалённые аккаунты»: поиск, предел, исключение."""

	kind = TaskKind.DELETED_ACCOUNTS

	def __init__(self, panel: TasksPanel, page_parent: QWidget) -> None:
		super().__init__(panel, page_parent)
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(deleted_accounts.TITLE.hint, self)
		hint.setWordWrap(True)
		box.addWidget(hint)
		row = QHBoxLayout()
		scan = self.run_button("Найти удалённые аккаунты", self)
		scan.clicked.connect(lambda: self.launch(dry_run=True, status_text="Поиск идёт…"))
		row.addWidget(scan)
		row.addStretch()
		box.addLayout(row)
		self._label = BodyLabel("Поиск ещё не выполнялся.", self)
		self._label.setWordWrap(True)
		box.addWidget(self._label)
		box.addStretch()
		kick = QHBoxLayout()
		kick.addWidget(BodyLabel("Исключать за проход не больше:", self))
		self._kick_limit = SpinBox(self)
		self._kick_limit.setRange(*deleted_accounts.KICK_LIMIT_RANGE)
		self._kick_limit.setSingleStep(5)
		self._kick_limit.setValue(deleted_accounts.DEFAULT_KICK_LIMIT)
		self._kick_limit.setToolTip(
			"Число участников видно всем: резкое падение бьёт по охватам, "
			"поэтому чистить лучше порциями"
		)
		kick.addWidget(self._kick_limit)
		kick.addStretch()
		self._kick_button = PushButton("Исключить удалённые", self)
		self._kick_button.setEnabled(False)
		self._kick_button.clicked.connect(self._on_clean)
		kick.addWidget(self._kick_button)
		box.addLayout(kick)
		box.addWidget(self.schedule_block(self))
		box.addLayout(self.journal_row(self))

	def apply_params(self, params: TaskParams) -> None:
		if isinstance(params, DeletedAccountsParams):
			self._kick_limit.setValue(params.kick_limit)

	def params(self) -> DeletedAccountsParams:
		return DeletedAccountsParams(kick_limit=self._kick_limit.value())

	def set_status(self, text: str) -> None:
		self._label.setText(text)

	def confirm_schedule(self, schedule: Schedule) -> bool:
		limit = self._kick_limit.value()
		return confirm_delete(
			self,
			f"Включить исключение удалённых аккаунтов из «{self._panel.community.title}» "
			f"по расписанию ({schedule_text(schedule)})?\n\n"
			f"Каждый запуск исключит не больше {limit} без дополнительного "
			"подтверждения. Исключение необратимо и уменьшает число участников.",
			accept_text="Включить",
		)

	def _on_clean(self) -> None:
		"""Спрашивает подтверждение и ставит исключение."""
		limit = self._kick_limit.value()
		community = self._panel.community
		if not confirm_delete(
			self,
			f"Исключить удалённые аккаунты из «{community.title}»?\n\n"
			f"За проход будет исключено не больше {limit}. Исключение "
			"необратимо и уменьшит число участников.",
			accept_text="Исключить",
		):
			return
		self._kick_button.setEnabled(False)
		self.launch(dry_run=False, status_text="Чистка идёт…")

	def show_report(self, item: TaskJobDto) -> None:
		"""Показывает итог по участникам."""
		report = item.report
		if not isinstance(report, MembersReport):
			return
		self._label.setText(deleted_accounts.members_summary(report))
		# после исключения числа устарели: пусть поищет заново
		self._kick_button.setEnabled(report.removed == 0 and report.found > 0)


class _ReactionsSection(_TaskSection):
	"""Раздел «реакции»: кто ставит, какие, каким записям, с какими паузами.

	Два списка приходят из движка позже строки задачи — пользователи
	с правом реагировать и разрешённые в сообществе реакции; сохранённые
	параметры раскладываются по галочкам, когда списки на месте.
	"""

	kind = TaskKind.REACTIONS

	def __init__(self, panel: TasksPanel, page_parent: QWidget) -> None:
		super().__init__(panel, page_parent)
		self._user_boxes: dict[int, CheckBox] = {}
		self._reaction_rows: dict[str, tuple[CheckBox, SpinBox]] = {}
		self._pending: ReactionsParams | None = None
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(reactions.TITLE.hint, self)
		hint.setWordWrap(True)
		box.addWidget(hint)
		columns = QHBoxLayout()
		box.addLayout(columns, stretch=1)
		users_column = QVBoxLayout()
		users_column.addWidget(StrongBodyLabel("Кто ставит (по кругу)", self))
		self._users_note = CaptionLabel("Читаю пользователей…", self)
		users_column.addWidget(self._users_note)
		users_area, self._users_box = list_area(self, density.spacing().list_spacing)
		users_column.addWidget(users_area, stretch=1)
		columns.addLayout(users_column, stretch=1)
		reactions_column = QVBoxLayout()
		reactions_column.addWidget(StrongBodyLabel("Какие реакции и их вес, %", self))
		self._reactions_note = CaptionLabel("Читаю разрешённые реакции…", self)
		reactions_column.addWidget(self._reactions_note)
		reactions_area, self._reactions_box = list_area(self, density.spacing().list_spacing)
		reactions_column.addWidget(reactions_area, stretch=1)
		columns.addLayout(reactions_column, stretch=1)
		box.addLayout(self._scope_row())
		box.addLayout(self._limits_row())
		box.addLayout(self._pause_row())
		self._label = BodyLabel("Проход ещё не выполнялся.", self)
		self._label.setWordWrap(True)
		box.addWidget(self._label)
		self._error = ErrorLabel(self)
		box.addWidget(self._error)
		box.addLayout(self._run_row())
		box.addWidget(self.schedule_block(self))
		box.addLayout(self.journal_row(self))

	def _scope_row(self) -> QHBoxLayout:
		"""Охват: каким записям ставить, и число случайных."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Каким записям:", self))
		self._scope = ComboBox(self)
		for scope, title in reactions.SCOPE_TITLES.items():
			self._scope.addItem(title, userData=scope)
		self._scope.currentIndexChanged.connect(self._on_scope)
		row.addWidget(self._scope, stretch=1)
		self._random_label = BodyLabel("сколько:", self)
		row.addWidget(self._random_label)
		self._random_count = SpinBox(self)
		self._random_count.setRange(*reactions.RANDOM_COUNT_RANGE)
		self._random_count.setValue(reactions.DEFAULT_RANDOM_COUNT)
		row.addWidget(self._random_count)
		self._on_scope()
		return row

	def _limits_row(self) -> QHBoxLayout:
		"""Границы прохода: глубина просмотра и потолок реакций."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Просматривать последних записей:", self))
		self._depth = SpinBox(self)
		self._depth.setRange(*reactions.DEPTH_RANGE)
		self._depth.setValue(reactions.DEFAULT_DEPTH)
		row.addWidget(self._depth)
		row.addWidget(BodyLabel("реакций за проход не больше:", self))
		self._limit = SpinBox(self)
		self._limit.setRange(*reactions.LIMIT_RANGE)
		self._limit.setValue(reactions.DEFAULT_LIMIT)
		row.addWidget(self._limit)
		row.addStretch()
		return row

	def _pause_row(self) -> QHBoxLayout:
		"""Пауза между реакциями и две реакции у Premium."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Пауза между реакциями от", self))
		self._pause_min = DoubleSpinBox(self)
		self._pause_min.setRange(*reactions.PAUSE_RANGE)
		self._pause_min.setSingleStep(0.1)
		self._pause_min.setValue(reactions.DEFAULT_PAUSE_S[0])
		row.addWidget(self._pause_min)
		row.addWidget(BodyLabel("до", self))
		self._pause_max = DoubleSpinBox(self)
		self._pause_max.setRange(*reactions.PAUSE_RANGE)
		self._pause_max.setSingleStep(0.1)
		self._pause_max.setValue(reactions.DEFAULT_PAUSE_S[1])
		row.addWidget(self._pause_max)
		row.addWidget(BodyLabel("с", self))
		row.addStretch()
		self._premium_double = CheckBox("Premium ставит две реакции", self)
		self._premium_double.setToolTip(
			"Аккаунт с подпиской Premium может поставить несколько реакций — "
			"выпадут две разные из набора"
		)
		row.addWidget(self._premium_double)
		return row

	def _run_row(self) -> QHBoxLayout:
		"""Кнопки: подобрать записи (без изменений) и провести проход."""
		row = QHBoxLayout()
		row.addStretch()
		preview = self.run_button("Подобрать записи", self)
		preview.setToolTip("Прочитать ленту и посчитать подходящие записи — реакции не ставятся")
		preview.clicked.connect(lambda: self._launch_checked(dry_run=True))
		row.addWidget(preview)
		run = self.run_button("Провести проход", self)
		run.setToolTip("Следующий по кругу пользователь поставит реакции прямо сейчас")
		run.clicked.connect(lambda: self._launch_checked(dry_run=False))
		row.addWidget(run)
		return row

	def _launch_checked(self, *, dry_run: bool) -> None:
		"""Проверяет заполнение формы и ставит запуск."""
		params = self.params()
		if not params.users:
			self._error.fail("Отметьте хотя бы одного пользователя.")
			return
		if not any(choice.weight > 0 for choice in params.reactions):
			self._error.fail("Отметьте хотя бы одну реакцию с ненулевым весом.")
			return
		self._error.succeed()
		self.launch(dry_run=dry_run, status_text="Подбор идёт…" if dry_run else "Проход идёт…")

	def _on_scope(self, *_args: object) -> None:
		"""Число случайных записей нужно только охвату «случайные»."""
		is_random = self._scope.currentData() is ReactionScope.RANDOM_WITHOUT_MINE
		self._random_label.setVisible(is_random)
		self._random_count.setVisible(is_random)

	# --- списки из движка ---------------------------------------------------------

	def mount(self, task: TaskDto) -> None:
		super().mount(task)
		self._panel.read_reactors(self.show_reactors)
		self._panel.read_reaction_options(self.show_reaction_options, self._options_failed)

	def show_reactors(self, executors: list[ExecutorDto]) -> None:
		"""Раскладывает пользователей с правом реагировать галочками."""
		clear_layout(self._users_box)
		self._user_boxes.clear()
		for executor in executors:
			check = CheckBox(reactor_label(executor), self)
			self._user_boxes[executor.owner.id] = check
			self._users_box.addWidget(check)
		self._users_note.setText(
			"Пользователей с правом реагировать в пуле нет — введите их на «Участниках»"
			if not executors
			else "Отмеченные ставят реакции по очереди, по одному за запуск"
		)
		self._apply_pending()

	def show_reaction_options(self, allowed: ChatReactions) -> None:
		"""Раскладывает разрешённые реакции: галочка и вес."""
		clear_layout(self._reactions_box)
		self._reaction_rows.clear()
		for option in allowed.options:
			self._reactions_box.addWidget(self._reaction_row(option))
		self._reactions_note.setText(
			"В сообществе реакции запрещены — задача невыполнима"
			if not allowed.options
			else "Вес относительный: 50 и 50 — то же, что 100 и 100; 0 — не выпадает"
		)
		self._apply_pending()

	def _options_failed(self, message: str) -> None:
		"""Перечень реакций не прочитался — причина на месте, форма живая."""
		self._reactions_note.setText(f"Разрешённые реакции не прочитаны: {message}")

	def _reaction_row(self, option: ReactionOption) -> QWidget:
		"""Строка реакции: галочка с эмодзи и названием, вес."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		title = f"{option.emoji}  {option.title}" + ("  · Premium" if option.premium else "")
		check = CheckBox(title, box)
		row.addWidget(check, stretch=1)
		weight = SpinBox(box)
		weight.setRange(*reactions.WEIGHT_RANGE)
		weight.setValue(100)
		row.addWidget(weight)
		self._reaction_rows[option.emoji] = (check, weight)
		return box

	def _apply_pending(self) -> None:
		"""Сохранённые параметры — по галочкам, когда списки уже на экране."""
		params = self._pending
		if params is None:
			return
		chosen_users = {user.id for user in params.users}
		for account_id, check in self._user_boxes.items():
			check.setChecked(account_id in chosen_users)
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
		self._apply_pending()

	def params(self) -> ReactionsParams:
		scope = self._scope.currentData()
		users = tuple(
			ExecutorRef(OwnerKind.USER, account_id)
			for account_id, check in self._user_boxes.items()
			if check.isChecked()
		)
		chosen = tuple(
			ReactionChoice(emoji, weight.value())
			for emoji, (check, weight) in self._reaction_rows.items()
			if check.isChecked()
		)
		pending = self._pending
		return ReactionsParams(
			# пока списки не пришли, галочек нет — сохранённый выбор
			# остаётся прежним, а не стирается пустотой
			users=users if self._user_boxes or pending is None else pending.users,
			reactions=chosen if self._reaction_rows or pending is None else pending.reactions,
			scope=scope if isinstance(scope, ReactionScope) else ReactionScope.ALL_WITHOUT_MINE,
			random_count=self._random_count.value(),
			depth=self._depth.value(),
			limit=self._limit.value(),
			pause_min_s=self._pause_min.value(),
			pause_max_s=self._pause_max.value(),
			premium_double=self._premium_double.isChecked(),
		)

	def set_status(self, text: str) -> None:
		self._label.setText(text)

	def confirm_schedule(self, schedule: Schedule) -> bool:
		params = self.params()
		if not params.users or not any(choice.weight > 0 for choice in params.reactions):
			self._error.fail("Для расписания отметьте пользователей и хотя бы одну реакцию.")
			return False
		self._error.succeed()
		return confirm_delete(
			self,
			f"Включить реакции в «{self._panel.community.title}» по расписанию "
			f"({schedule_text(schedule)})?\n\nПользователей по кругу: {len(params.users)}; "
			f"охват — {reactions.SCOPE_TITLES[params.scope]}; за проход не больше "
			f"{params.limit} реакций. Каждый запуск — проход одного пользователя без "
			"дополнительного подтверждения.",
			accept_text="Включить",
		)

	def show_report(self, item: TaskJobDto) -> None:
		report = item.report
		if isinstance(report, ReactionsReport):
			self._label.setText(reactions.reactions_summary(report, dry_run=item.dry_run))


class _JoinRequestsSection(_TaskSection):
	"""Раздел «приём заявок»: удалённые, ссылки в профиле, потолок, запуск.

	Правила ограничения есть только у групп: в канале ограничить
	участника нечем, поэтому у канала эти флажки неактивны с объяснением.
	"""

	kind = TaskKind.JOIN_REQUESTS

	def __init__(self, panel: TasksPanel, page_parent: QWidget) -> None:
		super().__init__(panel, page_parent)
		is_group = panel.community.kind is CommunityKind.GROUP
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(join_requests.TITLE.hint, self)
		hint.setWordWrap(True)
		box.addWidget(hint)
		self._decline_deleted = CheckBox("Отклонять заявки удалённых аккаунтов", self)
		self._decline_deleted.setChecked(True)
		box.addWidget(self._decline_deleted)
		self._restrict_bio = CheckBox(
			"Принимать с полным ограничением, если в описании профиля есть ссылка", self
		)
		self._restrict_channel = CheckBox(
			"…и если в профиле указан канал (один запрос к Telegram на заявителя)", self
		)
		for check in (self._restrict_bio, self._restrict_channel):
			check.setEnabled(is_group)
			if not is_group:
				check.setToolTip(
					"В канале ограничить участника нечем — правило действует в группах"
				)
			box.addWidget(check)
		if not is_group:
			note = CaptionLabel(
				"Ограничение прав есть только у групп; в канале заявки принимаются как есть.", self
			)
			note.setWordWrap(True)
			box.addWidget(note)
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Разбирать за проход не больше:", self))
		self._limit = SpinBox(self)
		self._limit.setRange(*join_requests.LIMIT_RANGE)
		self._limit.setValue(join_requests.DEFAULT_LIMIT)
		row.addWidget(self._limit)
		row.addStretch()
		preview = self.run_button("Посмотреть заявки", self)
		preview.setToolTip("Прочитать ожидающие заявки и посчитать решения — ничего не меняется")
		preview.clicked.connect(lambda: self.launch(dry_run=True, status_text="Просмотр идёт…"))
		row.addWidget(preview)
		run = self.run_button("Разобрать заявки", self)
		run.clicked.connect(self._on_run)
		row.addWidget(run)
		box.addLayout(row)
		self._label = BodyLabel("Просмотр ещё не выполнялся.", self)
		self._label.setWordWrap(True)
		box.addWidget(self._label)
		box.addStretch()
		box.addWidget(self.schedule_block(self))
		box.addLayout(self.journal_row(self))

	def _on_run(self) -> None:
		"""Подтверждение и обычный запуск: одобрение необратимо."""
		params = self.params()
		if not confirm_delete(
			self,
			f"Разобрать заявки в «{self._panel.community.title}»?\n\n"
			f"{self._rules_text(params)} За проход — не больше {params.limit}. "
			"Одобрение и отклонение необратимы.",
			accept_text="Разобрать",
		):
			return
		self.launch(dry_run=False, status_text="Приём идёт…")

	@staticmethod
	def _rules_text(params: JoinRequestsParams) -> str:
		"""Правила задачи словами — для подтверждений."""
		rules = ["остальных принять"]
		if params.decline_deleted:
			rules.insert(0, "удалённые аккаунты отклонить")
		if params.restrict_bio_links:
			rules.append("со ссылкой в описании — принять с полным ограничением")
		if params.restrict_personal_channel:
			rules.append("с каналом в профиле — принять с полным ограничением")
		return "; ".join(rules).capitalize() + "."

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

	def set_status(self, text: str) -> None:
		self._label.setText(text)

	def confirm_schedule(self, schedule: Schedule) -> bool:
		params = self.params()
		return confirm_delete(
			self,
			f"Включить приём заявок в «{self._panel.community.title}» по расписанию "
			f"({schedule_text(schedule)})?\n\n{self._rules_text(params)} Каждый запуск "
			f"разберёт не больше {params.limit} заявок без дополнительного подтверждения.",
			accept_text="Включить",
		)

	def show_report(self, item: TaskJobDto) -> None:
		report = item.report
		if isinstance(report, JoinRequestsReport):
			self._label.setText(join_requests.join_requests_summary(report, dry_run=item.dry_run))


class _ScheduleEditor(QWidget):
	"""Форма расписания раздела: вид, границы, включено, сохранение.

	Поля вида показываются по выбранному виду: у интервала — две границы
	в минутах (равные — интервал постоянный), у моментов суток — строка
	«ЧЧ:ММ» через запятую (формат тот же, что у времён публикации).
	Подпись под формой — сохранённое расписание и следующий запуск.
	"""

	def __init__(self, parent: QWidget, *, on_save: Callable[[Schedule, bool], None]) -> None:
		super().__init__(parent)
		self._on_save = on_save
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Расписание:", self))
		self._kind = ComboBox(self)
		for kind, title in SCHEDULE_KINDS:
			self._kind.addItem(title, userData=kind)
		self._kind.currentIndexChanged.connect(self._on_kind)
		row.addWidget(self._kind, stretch=1)
		self._switch = SwitchButton(self)
		self._switch.setOnText("включено")
		self._switch.setOffText("выключено")
		row.addWidget(self._switch)
		box.addLayout(row)
		self._interval = QWidget(self)
		interval = QHBoxLayout(self._interval)
		interval.setContentsMargins(0, 0, 0, 0)
		interval.addWidget(BodyLabel("Пауза между запусками от", self._interval))
		self._min = SpinBox(self._interval)
		self._min.setRange(*INTERVAL_MINUTES_RANGE)
		interval.addWidget(self._min)
		interval.addWidget(BodyLabel("до", self._interval))
		self._max = SpinBox(self._interval)
		self._max.setRange(*INTERVAL_MINUTES_RANGE)
		interval.addWidget(self._max)
		interval.addWidget(BodyLabel("мин — случайно в промежутке", self._interval))
		interval.addStretch()
		box.addWidget(self._interval)
		self._daily = QWidget(self)
		daily = QHBoxLayout(self._daily)
		daily.setContentsMargins(0, 0, 0, 0)
		daily.addWidget(BodyLabel("В моменты суток:", self._daily))
		self._times = LineEdit(self._daily)
		self._times.setPlaceholderText("04:00, 16:30…")
		daily.addWidget(self._times, stretch=1)
		box.addWidget(self._daily)
		footer = QHBoxLayout()
		self._caption = CaptionLabel("", self)
		self._caption.setWordWrap(True)
		footer.addWidget(self._caption, stretch=1)
		self.save_button = PushButton("Сохранить расписание", self)
		self.save_button.setEnabled(False)
		self.save_button.clicked.connect(self._save)
		footer.addWidget(self.save_button)
		box.addLayout(footer)
		self._error = ErrorLabel(self)
		box.addWidget(self._error)
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

	def schedule(self) -> Schedule:
		"""Расписание из полей формы (без проверки — её делает движок)."""
		kind = self._kind.currentData()
		return Schedule(
			kind=kind if isinstance(kind, ScheduleKind) else ScheduleKind.NONE,
			min_minutes=self._min.value(),
			max_minutes=self._max.value(),
			times=parse_times(self._times.text()),
		)

	def _on_kind(self, *_args: object) -> None:
		"""Показывает поля выбранного вида, прячет остальные."""
		kind = self._kind.currentData()
		self._interval.setVisible(kind is ScheduleKind.INTERVAL)
		self._daily.setVisible(kind is ScheduleKind.DAILY)

	def _save(self) -> None:
		"""Проверяет расписание и отдаёт его владельцу."""
		schedule = self.schedule()
		enabled = self._switch.isChecked()
		try:
			schedule.validate()
			if enabled and schedule.kind is ScheduleKind.NONE:
				raise TaskError("Выберите расписание — «только по требованию» включать нечего.")
		except TaskError as exc:
			self._error.fail(str(exc))
			return
		self._error.succeed()
		self._on_save(schedule, enabled)


class TasksPanel(QWidget):
	"""Тело задач: сегменты видов, их разделы и панель хода работы.

	Один и тот же виджет живёт во вкладке страницы сообщества
	и в рабочем окне с дашборда. Панель хода работы — зритель
	наблюдателя очереди задач при главном окне (ADR-0034); вкладка
	присоединяет и отсоединяет её через :meth:`set_active`
	(невидимая вкладка карточки не обновляет), окно живёт присоединённым.
	"""

	def __init__(
		self, worker: EngineWorker, watcher: QueueWatcher, community: CommunityDto, parent: QWidget
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._watcher = watcher
		self.community = community
		self._show_error = error_reporter(self)
		# id задания → раздел, который ждёт его исхода. Чужие задания
		# (окно открывали для другого сообщества) в словарь не попадают
		# и на этот экран не влияют
		self._jobs: dict[int, _TaskSection] = {}
		self._build()

	def set_active(self, active: bool) -> None:
		"""Присоединяет панель хода работы к наблюдателю или отсоединяет."""
		self._panel.set_active(active)

	# --- каркас -------------------------------------------------------------------

	def _build(self) -> None:
		"""Каркас: сегменты видов, их разделы и панель хода."""
		spacing = density.spacing()
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(spacing.row_spacing)
		self._segments = SegmentedWidget(self)
		self._pages = QStackedWidget(self)
		#: ключ раздела → его страница: связь явная, а не по номеру в стопке
		self._by_key: dict[str, QWidget] = {}
		layout.addWidget(self._segments)
		layout.addWidget(self._pages, stretch=1)
		self._sections: list[_TaskSection] = [
			_ServiceMessagesSection(self, self),
			_DeletedAccountsSection(self, self),
			_ReactionsSection(self, self),
			_JoinRequestsSection(self, self),
		]
		for section in self._sections:
			self._add_page(str(section.kind), KIND_TITLES[section.kind], section)
			self._read_task(section)
		self._segments.currentItemChanged.connect(self._show)
		self._segments.setCurrentItem(str(self._sections[0].kind))
		layout.addWidget(CaptionLabel("Ход работы", self))
		queue_box = QVBoxLayout()
		queue_box.setSpacing(spacing.list_spacing)
		layout.addLayout(queue_box)
		self._panel = QueuePanel(
			self,
			queue_box,
			watcher=self._watcher,
			subtitle=self._job_subtitle,
			on_finished=self._on_job_finished,
			# задание с ошибкой очередь не покидает — до `on_finished`
			# оно не доходит, и без этой сверки строка раздела навсегда
			# осталась бы «Просмотр идёт…»
			on_refreshed=self._on_jobs_refreshed,
		)

	def _add_page(self, key: str, title: str, page: QWidget) -> None:
		"""Добавляет раздел: сегмент сверху и страницу в стопке."""
		self._pages.addWidget(page)
		self._by_key[key] = page
		self._segments.addItem(routeKey=key, text=title, onClick=lambda: self._show(key))

	def _show(self, key: str) -> None:
		"""Показывает страницу раздела — по ключу, а не по номеру в стопке."""
		page = self._by_key.get(key)
		if page is not None:
			self._pages.setCurrentWidget(page)

	def _read_task(self, section: _TaskSection) -> None:
		"""Читает строку задачи раздела из движка (заводя её при нужде)."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.task(self.community.id, section.kind),
			self,
			section.mount,
			self._show_error,
		)

	# --- запуск и исходы ------------------------------------------------------------

	def launch(
		self,
		section: _TaskSection,
		task: TaskDto,
		params: TaskParams,
		*,
		dry_run: bool,
		status_text: str,
	) -> None:
		"""Ставит запуск задачи и запоминает, какой раздел ждёт исхода."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.run_now(task.id, params, dry_run=dry_run),
			self,
			lambda job_id: self._track(job_id, section, status_text),
			self._show_error,
		)

	def _track(self, job_id: int, section: _TaskSection, text: str) -> None:
		"""Запоминает, какой раздел ждёт исхода этого задания."""
		self._jobs[job_id] = section
		section.set_status(text)

	def save_schedule(
		self, section: _TaskSection, task: TaskDto, schedule: Schedule, *, enabled: bool
	) -> None:
		"""Сохраняет расписание задачи и показывает разделу свежую строку."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.save_schedule(task.id, schedule, enabled=enabled),
			self,
			section.mount,
			self._show_error,
		)

	def read_reactors(self, ready: Callable[[list[ExecutorDto]], None]) -> None:
		"""Читает пользователей пула с правом реагировать."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.reactors(self.community.id),
			self,
			ready,
			self._show_error,
		)

	def read_reaction_options(
		self, ready: Callable[[ChatReactions], None], failed: Callable[[str], None]
	) -> None:
		"""Читает разрешённые в сообществе реакции (одно обращение к Telegram)."""
		run_in_engine(
			self._worker,
			self._worker.engine.tasks.reaction_options(self.community.id),
			self,
			ready,
			failed,
		)

	def open_journal(self, task: TaskDto) -> None:
		"""Открывает окно журнала запусков задачи."""
		exec_dialog(TaskRunsDialog(self._worker, task, self.community, self.window()))

	def _on_job_finished(self, item: TaskJobDto, done: bool) -> None:
		"""Панель сообщила об исходе задания — забираем отчёт.

		Панель снимает завершённые с показа, поэтому отчёт берётся
		здесь: иначе он исчез бы вместе с карточкой. Сюда доходят
		только исходы, покидающие очередь (готово и отменено);
		ошибку ловит :meth:`_on_jobs_refreshed`.
		"""
		section = self._jobs.pop(item.id, None)
		if section is None:
			return  # задание другого окна
		if not done:
			section.set_status("Отменено — числа не обновлялись.")
			return
		section.show_report(item)

	def _on_jobs_refreshed(self, items: list[Any]) -> None:
		"""Замечает задание, остановившееся на ошибке.

		Элемент с ошибкой остаётся в очереди (его повторяют или
		убирают руками), поэтому реакции на завершение панель для него
		не зовёт. Без этой сверки раздел показывал бы «идёт…» до
		закрытия окна, а причина отказа была бы видна только в журнале.
		"""
		for item in items:
			if item.status is not JobStatus.ERROR:
				continue
			section = self._jobs.pop(item.id, None)
			if section is not None:
				section.set_status(f"Не удалось: {item.error}")

	@staticmethod
	def _job_subtitle(item: Any) -> str:
		"""Подпись карточки задания в панели хода работы.

		Показывает исход, а не только ход работы: у задания с ошибкой
		причина обязана быть на карточке — рядом с кнопкой «Повторить».
		"""
		if item.status is JobStatus.ERROR:
			return f"ошибка: {item.error}"
		if item.status is JobStatus.CANCELLED:
			return "отменено"
		if item.status is JobStatus.DONE:
			return "готово"
		if item.note:
			return str(item.note)
		return "идёт обращение к Telegram"


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
	"""Рабочее окно задач (с дашборда): та же панель, что во вкладке."""

	def __init__(
		self, worker: EngineWorker, watcher: QueueWatcher, community: CommunityDto, parent: QWidget
	) -> None:
		super().__init__(f"Задачи · {community.title}", parent)
		self.content.addWidget(TasksPanel(worker, watcher, community, self), stretch=1)


def open_tasks(
	worker: EngineWorker, watcher: QueueWatcher, community: CommunityDto, parent: QWidget
) -> None:
	"""Открывает окно задач сообщества.

	``watcher`` — наблюдатель очереди задач при главном окне (ADR-0034):
	окно и вкладка страницы сообщества смотрят на одну очередь.
	"""
	exec_dialog(TasksDialog(worker, watcher, community, parent.window()))
