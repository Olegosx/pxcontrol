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
	PushButton,
	SegmentedWidget,
	SpinBox,
	StrongBodyLabel,
	TableWidget,
	TextEdit,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.tasks import TaskDto, TaskJobDto, TaskRunDto
from pxcontrol.engine.tasks import (
	DeletedAccountsParams,
	MembersReport,
	RunOutcome,
	ServiceMessagesParams,
	ServiceReport,
	TaskKind,
	TaskParams,
	TaskTrigger,
	deleted_accounts,
	service_messages,
)
from pxcontrol.engine.telegram.types import ServiceMessageKind
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

	# --- контракт раздела ------------------------------------------------------------

	def mount(self, task: TaskDto) -> None:
		"""Принимает строку задачи: параметры — в форму, кнопки — включить."""
		self._task = task
		self.apply_params(task.params)
		for button in self._run_buttons:
			button.setEnabled(True)

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
		box.addLayout(self.journal_row(self))

	def apply_params(self, params: TaskParams) -> None:
		if isinstance(params, DeletedAccountsParams):
			self._kick_limit.setValue(params.kick_limit)

	def params(self) -> DeletedAccountsParams:
		return DeletedAccountsParams(kick_limit=self._kick_limit.value())

	def set_status(self, text: str) -> None:
		self._label.setText(text)

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
		]
		for section, title in zip(
			self._sections,
			(service_messages.TITLE.noun, deleted_accounts.TITLE.noun),
			strict=True,
		):
			self._add_page(str(section.kind), title, section)
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
		title = (
			service_messages.TITLE.noun
			if task.kind is TaskKind.SERVICE_MESSAGES
			else deleted_accounts.TITLE.noun
		)
		super().__init__(f"Журнал · {title} · {community.title}", parent)
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
