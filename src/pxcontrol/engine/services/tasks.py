"""Задачи сообщества: постановка, очередь, журнал запусков (ADR-0038).

Наследник обслуживания сообществ (ADR-0026). Что было **проходом
обслуживания**, стало **запуском задачи**: у задачи есть строка в базе
с параметрами и расписанием, у каждого запуска — строка журнала с тем,
кто запустил, чьими руками шла работа и чем кончилось. Виды задач —
в пакете :mod:`pxcontrol.engine.tasks`; сервис знает только их контракт
(:class:`~pxcontrol.engine.tasks.model.TaskSpec`).

Жизненный цикл заданий держит общий каркас (:mod:`pxcontrol.engine.jobs`,
ADR-0025), темп обращений к Telegram — дорожка аккаунта (ADR-0024):
между страницами она пропускает вперёд публикацию, а флуд-лимит
останавливает запуск целиком. Задачи ведут только пользователи
(ADR-0026): список участников и историю чужими глазами боту Telegram
не отдаёт.

Очередь — с несколькими слотами: задачи разных сообществ идут
одновременно, а в одном сообществе в один момент — не больше одной
(замок задания — сообщество, ADR-0036). Исполнитель выбирается
**в момент запуска** по свежему снимку прав и живой занятости дорожек
(диспетчер, ADR-0036); при постановке лишь проверяется, что способный
в пуле есть, — отказ должен звучать сразу, а не через час в журнале.

**Планировщик** — одна периодическая задача движка с тиком в минуту
(:class:`~pxcontrol.engine.periodic.PeriodicTask`, как у опроса
статистики): берёт задачи с наступившим сроком и ставит их запуск.
Следующий момент **хранится** в задаче и пересчитывается после каждого
запуска — любого, по кнопке или по расписанию: пауза между проходами
считается от конца последнего. Пропущенные за время выключения
запуски догоняются один раз. Журнал старше срока хранения убирается
раз в сутки.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, CommunityTask, TaskRun, TgAccount
from pxcontrol.engine.db.types import as_utc, as_utc_optional
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.jobs import Job, JobCancelled, JobQueue, JobStatus
from pxcontrol.engine.periodic import PeriodicTask
from pxcontrol.engine.services.abilities import ACTION_WORDS, ExecutorAction, can
from pxcontrol.engine.services.accounts import account_display
from pxcontrol.engine.services.communities import (
	CommunitiesService,
	CommunityDto,
	ExecutorDto,
)
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.tasks import (
	MembersReport,
	RunEvent,
	RunOutcome,
	TaskContext,
	TaskError,
	TaskKind,
	TaskParams,
	TaskReport,
	TaskTrigger,
	spec_of,
)
from pxcontrol.engine.tasks.schedule import Schedule, ScheduleKind, next_run
from pxcontrol.engine.telegram.types import (
	ChatReactions,
	DeletedAccount,
	ExecutorRef,
	JoinRequest,
	JoinRequestsPage,
	OwnerKind,
	ParticipantsPage,
	ReactionsPage,
	ServiceMessagesPage,
)

logger = logging.getLogger(__name__)

#: Сколько задач может идти одновременно — в разных сообществах
#: (в одном не больше одной: замок задания). Потолок открытых
#: сообществ, а не запросов: темп внутри аккаунта держит дорожка.
#: Число — по прецеденту потолка загрузок (ADR-0036); подлежит
#: проверке живьём и в настройки не выносится (ADR-0013).
PARALLEL_TASKS = 3

#: Сколько событий журнала держит один запуск. Массовая задача пишет
#: строку на каждое действие, и без предела строка запуска росла бы
#: без края; сотни — это уже подробнее, чем нужно для разбора.
EVENTS_CAP = 500

#: Шаг планировщика: как часто проверять, кому пора. Минута — с запасом
#: мельче любого интервала; сама проверка без сети дешёвая.
SCHEDULER_TICK_S = 60

#: Срок хранения журнала запусков, дни — как у истории статистики
#: (ADR-0027): разбирать инцидент старше квартала уже не по чему.
RUNS_KEEP_DAYS = 90

#: Как часто убирать журнал старше срока хранения: раз в сутки —
#: уборка это пишущая транзакция, а хранение измеряется месяцами.
PRUNE_EVERY = timedelta(days=1)

#: Сколько ждать планировщик при остановке движка (ADR-0020): между
#: постановками он выходит сразу, сама постановка — запись в базу.
_SHUTDOWN_TIMEOUT_S = 10.0


class _TasksPort(Protocol):
	"""Часть шлюза Telegram, нужная всем видам задач (для подмены в тестах).

	Объединение портов видов: сервис отдаёт шлюз виду целиком, а тот
	видит через свой, более узкий протокол только нужные ему методы.
	"""

	async def userbot_service_messages_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage: ...

	async def userbot_delete_messages(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> int: ...

	async def userbot_participants_page(
		self, account_id: int, chat_id: str, offset: int, limit: int
	) -> ParticipantsPage: ...

	async def userbot_kick_participant(
		self, account_id: int, chat_id: str, account: DeletedAccount
	) -> int | None: ...

	async def userbot_available_reactions(self, account_id: int, chat_id: str) -> ChatReactions: ...

	async def userbot_reactions_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ReactionsPage: ...

	async def userbot_send_reaction(
		self, account_id: int, chat_id: str, message_id: int, emojis: Sequence[str]
	) -> None: ...

	def userbot_premium(self, account_id: int | None) -> bool: ...

	async def userbot_join_requests_page(
		self, account_id: int, chat_id: str, offset: tuple[datetime, int] | None, limit: int
	) -> JoinRequestsPage: ...

	async def userbot_handle_join_request(
		self, account_id: int, chat_id: str, request: JoinRequest, *, approve: bool
	) -> None: ...

	async def userbot_restrict_fully(
		self, account_id: int, chat_id: str, request: JoinRequest
	) -> None: ...

	async def userbot_has_personal_channel(self, account_id: int, request: JoinRequest) -> bool: ...


@dataclass(frozen=True)
class TaskDto:
	"""Задача сообщества для интерфейса.

	Attributes:
		id: идентификатор строки задачи.
		community_id: сообщество.
		kind: вид.
		params: параметры вида (типизированная структура).
		enabled: расписание действует.
		schedule: расписание.
		next_run_at: следующий запуск по расписанию; None — не назначен.
		last_run_at: когда задача запускалась в последний раз.
		cursor: состояние вида между запусками; None — ещё не было.
	"""

	id: int
	community_id: int
	kind: TaskKind
	params: TaskParams
	enabled: bool
	schedule: Schedule
	next_run_at: datetime | None
	last_run_at: datetime | None
	cursor: dict[str, Any] | None = None


@dataclass(frozen=True)
class TaskRunDto:
	"""Запуск задачи — строка журнала для интерфейса.

	Attributes:
		id: идентификатор запуска.
		task_id: задача.
		kind: вид задачи.
		trigger: кто запустил.
		dry_run: запуск «без изменений».
		executor: чьими руками шла работа; None — не выбран.
		executor_label: его человеческое имя; None — не выбран или удалён.
		started_at: начало.
		finished_at: конец; None — ещё идёт (или приложение упало).
		outcome: исход.
		summary: итог одной строкой (по отчёту вида); пусто — отчёта нет.
		error: текст ошибки при исходе «ошибка».
		events: события запуска.
		report: отчёт вида целиком; None — отчёта нет (запуск не дошёл
			до итога). Нужен форме задачи: числа последнего просмотра
			восстанавливаются из журнала, а не живут только до перезапуска.
	"""

	id: int
	task_id: int
	kind: TaskKind
	trigger: TaskTrigger
	dry_run: bool
	executor: ExecutorRef | None
	executor_label: str | None
	started_at: datetime
	finished_at: datetime | None
	outcome: RunOutcome
	summary: str
	error: str | None
	events: tuple[RunEvent, ...]
	report: TaskReport | None = None


@dataclass(frozen=True)
class TaskJobDto:
	"""Задание очереди задач для панели хода работы.

	Attributes:
		id: идентификатор задания (для отмены и снятия с показа).
		task_id: задача, которую несёт задание.
		kind: вид задачи.
		title: что и где делается («Просмотр служебных записей · Мой канал»).
		community_id: сообщество-цель.
		status: состояние задания (общий набор очередей, ADR-0025).
		progress: доля выполнения 0.0..1.0.
		error: текст ошибки (для статуса ERROR).
		note: пометка состояния (ход просмотра).
		dry_run: запуск «без изменений».
		report: отчёт вида; None — ещё не закончено или прервано.
	"""

	id: int
	task_id: int
	kind: TaskKind
	title: str
	community_id: int
	status: JobStatus
	progress: float
	error: str | None
	note: str | None
	dry_run: bool
	report: TaskReport | None = None


class _TaskJob(Job):
	"""Задание очереди: один запуск задачи."""

	def __init__(
		self,
		job_id: int,
		task: TaskDto,
		community: CommunityDto,
		*,
		dry_run: bool,
		trigger: TaskTrigger,
	) -> None:
		super().__init__(job_id)
		self.task = task
		self.community = community
		self.dry_run = dry_run
		self.trigger = trigger
		#: строка журнала этого запуска (заводится в начале работы)
		self.run_id: int | None = None
		self.executor: ExecutorDto | None = None
		self.report: TaskReport | None = None
		self.events: list[RunEvent] = []
		#: состояние вида после запуска (None — вид ничего не менял)
		self.cursor: dict[str, Any] | None = None
		# в одном сообществе в один момент идёт не больше одной задачи
		self.locks = frozenset({("community", community.id)})

	def dto(self) -> TaskJobDto:
		"""Снимок задания для интерфейса."""
		return TaskJobDto(
			id=self.id,
			task_id=self.task.id,
			kind=self.task.kind,
			title=f"{spec_of(self.task.kind).title(dry_run=self.dry_run)} · {self.community.title}",
			community_id=self.community.id,
			status=self.status,
			progress=self.progress,
			error=self.error,
			note=self.card_note(),
			dry_run=self.dry_run,
			report=self.report,
		)


class TasksService:
	"""Задачи сообщества: настройка, запуск по требованию, очередь, журнал."""

	def __init__(
		self,
		db: Database,
		gateway: _TasksPort,
		communities: CommunitiesService,
		on_members_report: Callable[[int, int, int, datetime], Awaitable[None]] | None = None,
		settings: SettingsService | None = None,
		tz: tzinfo | None = None,
		rng: random.Random | None = None,
	) -> None:
		"""``on_members_report`` — крючок «запомни итог прохода по удалённым
		аккаунтам» (сообщество, найдено, исключено, когда): движок передаёт
		запись в кэш статистики, чтобы «Обзор» показывал число мёртвых душ
		и дату прохода (ADR-0027). Сбой крючка задание не роняет.
		``settings`` — общий сервис настроек (None — свой, для тестов):
		планировщик пропускает выключенные сообщества; ``tz`` — зона
		моментов суток (None — местная; тесты передают UTC); ``rng`` —
		источник случайности интервалов (тесты передают свой)."""
		self._db = db
		self._gateway = gateway
		self._communities = communities
		self._on_members_report = on_members_report
		self._settings = settings if settings is not None else SettingsService(db)
		self._tz: tzinfo = tz if tz is not None else (datetime.now(UTC).astimezone().tzinfo or UTC)
		self._rng = rng if rng is not None else random.Random()
		self._pruned_at: datetime | None = None
		self._scheduler = PeriodicTask(
			self.run_due,
			name="Планировщик задач",
			interval_s=SCHEDULER_TICK_S,
			shutdown_timeout_s=_SHUTDOWN_TIMEOUT_S,
		)
		self._jobs: JobQueue[_TaskJob] = JobQueue(
			self._run_job,
			name="Задачи",
			# задания не переживают перезапуск: истина — сам Telegram,
			# повторный запуск даст ту же картину; журнал запусков при этом
			# остаётся — исход недоделанного дописан крючком записи
			cancel_pending_on_shutdown=True,
			concurrency=PARALLEL_TASKS,
			record=self._record,
		)

	# --- задачи ------------------------------------------------------------------

	async def task(self, community_id: int, kind: TaskKind) -> TaskDto:
		"""Задача сообщества этого вида; отсутствующая заводится с умолчаниями.

		Raises:
			TaskError: Сообщество не найдено.
		"""
		await self._community(community_id)
		async with self._db.session_factory() as session:
			row = await self._row_in_session(session, community_id, kind)
			if row is None:
				spec = spec_of(kind)
				row = CommunityTask(
					community_id=community_id,
					kind=str(kind),
					enabled=False,
					params=spec.params_to_payload(spec.default_params()),
					schedule=Schedule().to_payload(),
				)
				session.add(row)
				await session.commit()
				await session.refresh(row)
				logger.info("Задача «%s» заведена сообществу id=%s.", kind, community_id)
			return _task_dto(row)

	async def list_tasks(self, community_id: int) -> list[TaskDto]:
		"""Заведённые задачи сообщества (в порядке заведения)."""
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(CommunityTask)
						.where(CommunityTask.community_id == community_id)
						.order_by(CommunityTask.id)
					)
				)
				.scalars()
				.all()
			)
			return [_task_dto(row) for row in rows]

	async def save_params(self, task_id: int, params: TaskParams) -> TaskDto:
		"""Сохраняет параметры задачи (проверив их границы).

		Raises:
			TaskError: Задача не найдена или параметры негодны.
		"""
		async with self._db.session_factory() as session:
			row = await self._task_in_session(session, task_id)
			spec = spec_of(TaskKind(row.kind))
			spec.validate(params, dry_run=True)
			row.params = spec.params_to_payload(params)
			await session.commit()
			await session.refresh(row)
			return _task_dto(row)

	async def save_task(
		self, task_id: int, params: TaskParams, schedule: Schedule, *, enabled: bool
	) -> TaskDto:
		"""Сохраняет параметры и расписание задачи одним разом.

		Форма настройки задачи одна, и «Сохранить» у неё одна: параметры
		и расписание связаны (запуск по расписанию идёт с этими
		параметрами), поэтому сохраняться они должны вместе — иначе
		между двумя записями есть мгновение, когда расписание уже новое,
		а параметры ещё старые, и ночной запуск взял бы их.

		Raises:
			TaskError: Задача не найдена, параметры или расписание
				негодны, включается «только по требованию».
		"""
		schedule.validate()
		if enabled and schedule.kind is ScheduleKind.NONE:
			raise TaskError("Выберите расписание — «только по требованию» включать нечего.")
		async with self._db.session_factory() as session:
			row = await self._task_in_session(session, task_id)
			spec = spec_of(TaskKind(row.kind))
			# параметры проверяются под тот запуск, который им предстоит:
			# по расписанию он обычный, а не «без изменений»
			spec.validate(params, dry_run=not enabled)
			row.params = spec.params_to_payload(params)
			row.schedule = schedule.to_payload()
			row.enabled = enabled
			row.next_run_at = (
				next_run(schedule, datetime.now(UTC), self._tz, self._rng) if enabled else None
			)
			await session.commit()
			await session.refresh(row)
			logger.info(
				"Задача id=%s сохранена: расписание %s, %s; следующий запуск %s.",
				row.id,
				schedule.kind,
				"включено" if enabled else "выключено",
				row.next_run_at,
			)
			return _task_dto(row)

	async def save_schedule(self, task_id: int, schedule: Schedule, *, enabled: bool) -> TaskDto:
		"""Сохраняет расписание и назначает следующий запуск.

		Включить можно только настоящее расписание («только по требованию»
		с включённым флагом — противоречие, и оно отклоняется). Включение
		проверяет сохранённые параметры под **обычный** запуск: чистка
		по расписанию без выбранных видов записей была бы пустой работой,
		и честнее сказать об этом при сохранении. Следующий момент
		считается от «сейчас» и хранится; выключение его снимает.

		Raises:
			TaskError: Задача не найдена, расписание негодно, включается
				«только по требованию» или параметры не годятся для запуска.
		"""
		schedule.validate()
		if enabled and schedule.kind is ScheduleKind.NONE:
			raise TaskError("Выберите расписание — «только по требованию» включать нечего.")
		async with self._db.session_factory() as session:
			row = await self._task_in_session(session, task_id)
			if enabled:
				spec = spec_of(TaskKind(row.kind))
				spec.validate(spec.params_from_payload(row.params), dry_run=False)
			row.schedule = schedule.to_payload()
			row.enabled = enabled
			row.next_run_at = (
				next_run(schedule, datetime.now(UTC), self._tz, self._rng) if enabled else None
			)
			await session.commit()
			await session.refresh(row)
			logger.info(
				"Задача id=%s: расписание %s, %s; следующий запуск %s.",
				row.id,
				schedule.kind,
				"включено" if enabled else "выключено",
				row.next_run_at,
			)
			return _task_dto(row)

	# --- планировщик ------------------------------------------------------------

	def start_scheduler(self) -> None:
		"""Запускает планировщик (при старте движка)."""
		self._scheduler.start()

	async def run_due(self, now: datetime | None = None) -> int:
		"""Один тик планировщика: ставит запуски задач с наступившим сроком.

		Задача с идущим или ждущим заданием (по кнопке или прошлый тик)
		не удваивается: срок останется в прошлом, и следующий тик после
		конца задания поставит её — а конец задания и так пересчитает
		срок по расписанию. Выключенное сообщество пропускается; отказ
		«некому» при постановке не гасит тик — он остаётся исходом
		задания в журнале, чтобы человек его увидел.

		Returns:
			Сколько запусков поставлено.
		"""
		now = now if now is not None else datetime.now(UTC)
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		async with self._db.session_factory() as session:
			rows = (
				(
					await session.execute(
						select(CommunityTask)
						.where(CommunityTask.enabled.is_(True))
						.where(CommunityTask.next_run_at.is_not(None))
						.where(CommunityTask.next_run_at <= now)
						.order_by(CommunityTask.next_run_at)
					)
				)
				.scalars()
				.all()
			)
			due = [_task_dto(row) for row in rows]
		queued = {job.task.id for job in self._jobs.all() if not job.status.finished()}
		started = 0
		for task in due:
			if self._scheduler.stopping:
				break
			if task.id in queued:
				continue
			if not enabled.get(task.community_id, COMMUNITY_ENABLED.default):
				continue
			try:
				community = await self._community(task.community_id)
			except TaskError:
				logger.warning(
					"Планировщик: сообщество id=%s задачи id=%s не найдено.",
					task.community_id,
					task.id,
				)
				continue
			self._put(
				_TaskJob(
					self._jobs.new_id(),
					task,
					community,
					dry_run=False,
					trigger=TaskTrigger.SCHEDULE,
				)
			)
			started += 1
		await self._prune_if_due(now)
		return started

	async def _prune_if_due(self, now: datetime) -> None:
		"""Убирает журнал старше срока хранения — не чаще раза в сутки."""
		if self._pruned_at is not None and now - self._pruned_at < PRUNE_EVERY:
			return
		self._pruned_at = now
		await self.prune_runs(now)

	async def prune_runs(self, now: datetime, keep_days: int = RUNS_KEEP_DAYS) -> int:
		"""Удаляет запуски, начатые раньше срока хранения.

		Returns:
			Сколько строк убрано.
		"""
		threshold = now - timedelta(days=keep_days)
		async with self._db.session_factory() as session:
			result = await session.execute(delete(TaskRun).where(TaskRun.started_at < threshold))
			await session.commit()
		# число задетых строк — у курсора результата; типизация SQLAlchemy
		# знает его только у CursorResult, а execute объявлен шире
		removed = int(getattr(result, "rowcount", 0) or 0)
		if removed:
			logger.info("Журнал задач: убрано %d запусков старше %d дней.", removed, keep_days)
		return removed

	async def run_now(self, task_id: int, params: TaskParams, *, dry_run: bool = False) -> int:
		"""Сохраняет параметры и ставит запуск задачи по требованию.

		Параметры проверяются под этот запуск (чистке нужен хотя бы один
		вид записей, просмотру — нет), способный исполнитель ищется
		в пуле прямо сейчас: отказ «некому» должен звучать при нажатии
		кнопки, а не через час в журнале. Кто именно поведёт работу,
		решается в момент запуска заново (ADR-0036).

		Returns:
			Идентификатор задания очереди.

		Raises:
			TaskError: Задача не найдена, параметры негодны или в пуле
				нет исполнителя с нужным правом.
		"""
		async with self._db.session_factory() as session:
			row = await self._task_in_session(session, task_id)
			spec = spec_of(TaskKind(row.kind))
			spec.validate(params, dry_run=dry_run)
			row.params = spec.params_to_payload(params)
			await session.commit()
			await session.refresh(row)
			task = _task_dto(row)
		community, _executor = await self._pick(task, dry_run=dry_run)
		return self._put(
			_TaskJob(
				self._jobs.new_id(), task, community, dry_run=dry_run, trigger=TaskTrigger.MANUAL
			)
		)

	# --- журнал запусков -----------------------------------------------------------

	async def runs(self, task_id: int, limit: int = 100) -> list[TaskRunDto]:
		"""Запуски задачи, новые сначала."""
		async with self._db.session_factory() as session:
			task_row = await self._task_in_session(session, task_id)
			kind = TaskKind(task_row.kind)
			rows = (
				(
					await session.execute(
						select(TaskRun)
						.where(TaskRun.task_id == task_id)
						.order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
						.limit(limit)
					)
				)
				.scalars()
				.all()
			)
			labels = await self._executor_labels(session, rows)
		return [_run_dto(row, kind, labels) for row in rows]

	@staticmethod
	async def _executor_labels(
		session: AsyncSession, rows: Sequence[TaskRun]
	) -> dict[ExecutorRef, str]:
		"""Имена исполнителей запусков — одним чтением по видам."""
		account_ids = {r.executor_id for r in rows if r.executor_kind == OwnerKind.USER}
		bot_ids = {r.executor_id for r in rows if r.executor_kind == OwnerKind.BOT}
		labels: dict[ExecutorRef, str] = {}
		if account_ids:
			accounts = (
				(await session.execute(select(TgAccount).where(TgAccount.id.in_(account_ids))))
				.scalars()
				.all()
			)
			for account in accounts:
				labels[ExecutorRef(OwnerKind.USER, account.id)] = account_display(
					account.label,
					account.username,
					account.first_name,
					account.last_name,
					account.phone,
				)
		if bot_ids:
			bots = (await session.execute(select(Bot).where(Bot.id.in_(bot_ids)))).scalars().all()
			for bot in bots:
				labels[ExecutorRef(OwnerKind.BOT, bot.id)] = bot.label
		return labels

	# --- очередь -------------------------------------------------------------------

	async def subscribe(self, listener: Callable[[int], None]) -> None:
		"""Подписывает интерфейс на изменения очереди (ADR-0034).

		Корутина, а не метод: подписка ложится в цикл событий движка,
		откуда и приходят уведомления. Снимок — ``state()``; доля
		выполнения в версию не входит.
		"""
		self._jobs.subscribe(listener)

	async def state(self) -> list[TaskJobDto]:
		"""Снимок очереди задач для интерфейса."""
		return [job.dto() for job in self._jobs.all()]

	async def cancel(self, item_id: int) -> None:
		"""Отменяет задание: ожидающее убирается, идущее — прекращается.

		Идущее задание останавливается между шагами: рвать обращение
		к Telegram посреди удаления незачем — часть записей уже удалена,
		и честный отчёт дороже мгновенной остановки.
		"""
		job = self._jobs.get(item_id)
		if job is None:
			return
		if job.status is JobStatus.PENDING:
			job.status = JobStatus.CANCELLED
			logger.info("Задача id=%s отменена (ждала).", item_id)
		elif job.status is JobStatus.RUNNING:
			self._jobs.request_cancel(job)

	async def drop_community(self, community_id: int) -> None:
		"""Снимает задания удалённого сообщества (ADR-0026).

		Задание держит снимок сообщества и работает по его
		``tg_chat_id``, от строки в БД не завися: без этого шага уборка
		продолжала бы удалять записи и исключать участников в Telegram
		для сущности, которой в приложении уже нет. Ожидающие снимаются
		сразу, идущему взводится отмена — оно остановится между шагами.
		"""
		for job in self._jobs.all():
			if job.community.id != community_id:
				continue
			if job.status is JobStatus.PENDING:
				job.status = JobStatus.CANCELLED
			elif job.status is JobStatus.RUNNING:
				self._jobs.request_cancel(job)
			else:
				continue
			logger.info("Задача id=%s снята: сообщество «%s» удалено.", job.id, job.community.title)

	async def retry(self, item_id: int) -> None:
		"""Повторяет задание с ошибкой (параметры — прежние)."""
		job = self._jobs.get(item_id)
		if job is None or job.status is not JobStatus.ERROR:
			return
		job.run_id = None
		job.executor = None
		job.report = None
		job.events = []
		self._jobs.reset_for_retry(job)
		logger.info("Задача id=%s возвращена в очередь на повтор.", item_id)

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённое задание из списка (живые не трогаются)."""
		job = self._jobs.get(item_id)
		if job is not None and job.status.finished():
			self._jobs.remove(job)

	async def settle(self) -> None:
		"""Дожидается простоя очереди (детерминированная точка для тестов)."""
		await self._jobs.wait_idle()

	async def shutdown(self) -> None:
		"""Гасит планировщик, затем очередь при остановке движка (ADR-0020).

		Планировщик — первым: после него новых заданий не появится,
		а очередь доигрывает начатое своим путём.
		"""
		await self._scheduler.shutdown()
		await self._jobs.shutdown()

	# --- постановка ---------------------------------------------------------------

	def _put(self, job: _TaskJob) -> int:
		"""Ставит готовое задание в очередь и будит воркера."""
		self._jobs.add(job)
		self._jobs.ensure_worker()
		logger.info(
			"Задача «%s» сообщества «%s»: %s (id=%s).",
			job.task.kind,
			job.community.title,
			"без изменений" if job.dry_run else "запуск",
			job.id,
		)
		return job.id

	async def _community(self, community_id: int) -> CommunityDto:
		"""Снимок сообщества.

		Raises:
			TaskError: Сообщество не найдено.
		"""
		try:
			return await self._communities.get_community(community_id)
		except EngineError as exc:
			raise TaskError(str(exc)) from exc

	async def _capable_users(
		self, community_id: int, action: ExecutorAction
	) -> tuple[CommunityDto, list[ExecutorDto]]:
		"""Сообщество и пользователи пула, способные на действие (ADR-0035).

		Порядок — диспетчера (ADR-0036): свободный раньше занятого
		загрузкой, при равенстве — публикатор по умолчанию. Выбор идёт
		по хранимому снимку прав; устаревший снимок поправит отказ
		сервера: для удаления он и так считается пропуском пачки.

		Raises:
			TaskError: Сообщество не найдено или способного
				исполнителя-пользователя в пуле нет.
		"""
		community = await self._community(community_id)
		try:
			capable = await self._communities.executors_for(community_id, action)
		except EngineError as exc:
			raise TaskError(str(exc)) from exc
		users = [executor for executor in capable if executor.owner.kind is OwnerKind.USER]
		if not users and await self._refresh_unread_rights(community_id):
			capable = await self._communities.executors_for(community_id, action)
			users = [executor for executor in capable if executor.owner.kind is OwnerKind.USER]
		if not users:
			raise TaskError(
				f"В пуле «{community.title}» некому {ACTION_WORDS[action]}: задачи ведут "
				"только пользователи (список участников и чужую историю бот прочитать "
				"не может), и нужное право должно быть у них. Выдайте его в Telegram "
				"и перепроверьте доступы."
			)
		return community, users

	async def _pick(self, task: TaskDto, *, dry_run: bool) -> tuple[CommunityDto, ExecutorDto]:
		"""Сообщество и исполнитель этого запуска — по правилу вида.

		Способных даёт пул в порядке диспетчера, а кого из них взять,
		решает вид (первого — уборка; следующего по кругу из названных —
		реакции). Названные, но неспособные сейчас исполнители — отказ
		с их перечислением: человек должен понять, кого вернуть в строй.

		Raises:
			TaskError: Сообщество не найдено, способных нет или среди
				названных видом нет ни одного способного.
		"""
		spec = spec_of(task.kind)
		community, users = await self._capable_users(
			task.community_id, spec.action(task.params, dry_run=dry_run)
		)
		executor = spec.choose_executor(task.params, task.cursor, users)
		if executor is None:
			raise TaskError(
				"Некому вести этот запуск: нужный исполнитель приостановлен, не состоит "
				"в сообществе или лишён нужного права (реакции — у названных "
				"пользователей; приём заявок с ограничением — ещё и право исключать). "
				"Выберите других или верните этих в строй."
			)
		return community, executor

	async def reaction_options(self, community_id: int) -> ChatReactions:
		"""Какие реакции разрешены в сообществе — для формы задачи реакций.

		Читает состоящий пользователь пула по диспетчеру: перечень
		у сообщества один, чей аккаунт спросит — не важно.

		Raises:
			TaskError: Сообщество не найдено или некому прочитать.
			UserbotUnavailableError: Telegram отказал.
		"""
		community, users = await self._capable_users(community_id, ExecutorAction.READ_HISTORY)
		return await self._gateway.userbot_available_reactions(
			users[0].owner.id, community.tg_chat_id
		)

	async def reactors(self, community_id: int) -> list[ExecutorDto]:
		"""Пользователи пула, которым по снимку прав можно ставить реакции.

		Приостановленные тоже в списке: их выбирают, чтобы не терять
		настройку, а вести проход они не будут, пока на паузе.
		"""
		community = await self._community(community_id)
		executors = await self._communities.list_executors(community_id)
		return [
			executor
			for executor in executors
			if executor.owner.kind is OwnerKind.USER
			and can(executor.rights, ExecutorAction.REACT, community.kind)
		]

	async def _refresh_unread_rights(self, community_id: int) -> bool:
		"""Уточняет права живым зондом, если снимка у кого-то ещё не было.

		Зонд здесь **уточняющий, а не обязательный** (ADR-0035, этап E).
		Нужен он ровно строкам, пережившим миграцию: в них перенесено
		то, что подтверждала прежняя модель (право публиковать), а прав
		удалять и исключать там нет — не потому, что их отобрали, а
		потому, что их никто не читал. Отказывать по такому снимку
		значило бы соврать.

		Returns:
			True — снимок уточняли (стоит спросить подбор заново).
		"""
		executors = await self._communities.list_executors(community_id)
		unread = [
			executor
			for executor in executors
			if executor.owner.kind is OwnerKind.USER and executor.checked_at is None
		]
		if not unread:
			return False
		logger.info(
			"Задачи сообщества id=%s: права %d исполнителей не читались — перепроверяю доступы.",
			community_id,
			len(unread),
		)
		await self._communities.recheck_community(community_id)
		return True

	# --- выполнение -----------------------------------------------------------------

	async def _run_job(self, job: _TaskJob) -> None:
		"""Выполняет один запуск; исход записывает каркас через крючок.

		Строка журнала заводится до выбора исполнителя: отказ «некому»
		в момент запуска — тоже исход, и он должен быть виден в журнале.

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
			TaskError: Способного исполнителя в пуле больше нет.
			UserbotUnavailableError: Telegram отказал (в том числе
				флуд-лимитом) — запуск прекращается, отчёт не строится.
		"""
		spec = spec_of(job.task.kind)
		job.run_id = await self._open_run(job)
		_community, executor = await self._pick(job.task, dry_run=job.dry_run)
		job.executor = executor
		await self._mark_executor(job.run_id, executor.owner)
		self._log(job, f"исполнитель — {executor.label}")
		ctx = TaskContext(
			job.community,
			executor,
			dry_run=job.dry_run,
			cursor=dict(job.task.cursor) if job.task.cursor is not None else None,
			progress=lambda fraction, note: self._progress(job, fraction, note),
			log=lambda text: self._log(job, text),
			check_stop=lambda: self._check_stop(job),
			sleep=self._jobs.wait_stop,
		)
		try:
			job.report = await spec.run(ctx, self._gateway, job.task.params)
		finally:
			# состояние вида сохраняется и при обрыве: вид сам решает,
			# что записать до шага, который может не состояться
			job.cursor = ctx.cursor
		await self._after_run(job)

	@staticmethod
	def _progress(job: _TaskJob, fraction: float, note: str | None) -> None:
		"""Ход работы для карточки задания."""
		job.progress = fraction
		job.note = note

	@staticmethod
	def _log(job: _TaskJob, text: str) -> None:
		"""Событие запуска: в журнал приложения и в строку запуска.

		Предел вместимости держит хвост списка в границах: последнее
		событие — самое ценное для разбора, поэтому вытесняются ранние.
		"""
		logger.info("Задача id=%s: %s", job.id, text)
		job.events.append((datetime.now(UTC), text))
		if len(job.events) > EVENTS_CAP:
			del job.events[: len(job.events) - EVENTS_CAP]

	def _check_stop(self, job: _TaskJob) -> None:
		"""Прерывает запуск между шагами по отмене или остановке.

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
		"""
		if job.cancel_requested or self._jobs.stopping:
			raise JobCancelled

	async def _after_run(self, job: _TaskJob) -> None:
		"""Отдаёт итог прохода по участникам крючку (если он есть).

		Сбой записи — в журнал: запуск состоялся, его отчёт на экране
		не зависит от того, запомнил ли его кэш статистики.
		"""
		report = job.report
		if self._on_members_report is None or not isinstance(report, MembersReport):
			return
		try:
			await self._on_members_report(
				job.community.id, report.found, report.removed, datetime.now(UTC)
			)
		except Exception:  # noqa: BLE001 — итог задачи важнее его записи
			logger.exception("Задача id=%s: итог по удалённым аккаунтам не записан в кэш.", job.id)

	# --- журнал: запись ------------------------------------------------------------

	async def _open_run(self, job: _TaskJob) -> int:
		"""Заводит строку запуска со статусом «идёт»."""
		async with self._db.session_factory() as session:
			row = TaskRun(
				task_id=job.task.id,
				trigger=str(job.trigger),
				dry_run=job.dry_run,
				started_at=datetime.now(UTC),
				outcome=str(RunOutcome.RUNNING),
			)
			session.add(row)
			await session.commit()
			return row.id

	async def _mark_executor(self, run_id: int, owner: ExecutorRef) -> None:
		"""Дописывает в строку запуска выбранного исполнителя."""
		async with self._db.session_factory() as session:
			row = await session.get(TaskRun, run_id)
			if row is None:
				return
			row.executor_kind = str(owner.kind)
			row.executor_id = owner.id
			await session.commit()

	async def _record(self, job: _TaskJob, status: JobStatus, error: str | None) -> None:
		"""Крючок каркаса «запиши исход» — до смены статуса в памяти (ADR-0025).

		Отмена человеком и остановка движка — разные исходы: флаг отмены
		взводит только человек, остановку выдаёт признак очереди.
		"""
		if not status.finished() or job.run_id is None:
			return
		if status is JobStatus.CANCELLED:
			outcome = RunOutcome.CANCELLED if job.cancel_requested else RunOutcome.INTERRUPTED
		elif status is JobStatus.ERROR:
			outcome = RunOutcome.ERROR
		else:
			outcome = RunOutcome.DONE
		spec = spec_of(job.task.kind)
		now = datetime.now(UTC)
		async with self._db.session_factory() as session:
			row = await session.get(TaskRun, job.run_id)
			if row is not None:
				row.finished_at = now
				row.outcome = str(outcome)
				row.error = error
				row.report = spec.report_to_payload(job.report) if job.report is not None else None
				row.events = [[at.isoformat(), text] for at, text in job.events]
			task_row = await session.get(CommunityTask, job.task.id)
			if task_row is not None:
				task_row.last_run_at = now
				if job.cursor is not None:
					task_row.cursor = job.cursor
				# следующий момент — от конца этого запуска, каким бы он
				# ни был: пауза между проходами считается между ними
				if task_row.enabled:
					task_row.next_run_at = next_run(
						Schedule.from_payload(task_row.schedule), now, self._tz, self._rng
					)
			await session.commit()
		if job.trigger is TaskTrigger.SCHEDULE and status is JobStatus.ERROR:
			# у запуска по расписанию «повторить» — это следующий срок,
			# а карточка с ошибкой копилась бы в панели каждый интервал;
			# причина уже в журнале запусков — оттуда её и читают
			self._jobs.remove(job)

	# --- строки ---------------------------------------------------------------------

	@staticmethod
	async def _row_in_session(
		session: AsyncSession, community_id: int, kind: TaskKind
	) -> CommunityTask | None:
		"""Строка задачи сообщества этого вида (None — не заведена)."""
		return (
			await session.execute(
				select(CommunityTask).where(
					CommunityTask.community_id == community_id, CommunityTask.kind == str(kind)
				)
			)
		).scalar_one_or_none()

	@staticmethod
	async def _task_in_session(session: AsyncSession, task_id: int) -> CommunityTask:
		"""Строка задачи по id.

		Raises:
			TaskError: Задача не найдена.
		"""
		row = await session.get(CommunityTask, task_id)
		if row is None:
			raise TaskError("Задача не найдена — возможно, сообщество уже удалено.")
		return row


def _task_dto(row: CommunityTask) -> TaskDto:
	"""Снимок задачи из строки."""
	kind = TaskKind(row.kind)
	return TaskDto(
		id=row.id,
		community_id=row.community_id,
		kind=kind,
		params=spec_of(kind).params_from_payload(row.params),
		enabled=row.enabled,
		schedule=Schedule.from_payload(row.schedule),
		next_run_at=as_utc_optional(row.next_run_at),
		last_run_at=as_utc_optional(row.last_run_at),
		cursor=dict(row.cursor) if isinstance(row.cursor, dict) else None,
	)


def _run_dto(row: TaskRun, kind: TaskKind, labels: dict[ExecutorRef, str]) -> TaskRunDto:
	"""Строка журнала из строки запуска."""
	spec = spec_of(kind)
	executor = (
		ExecutorRef(OwnerKind(row.executor_kind), row.executor_id)
		if row.executor_kind is not None and row.executor_id is not None
		else None
	)
	payload = row.report if isinstance(row.report, dict) else None
	report = spec.report_from_payload(payload) if payload else None
	summary = spec.summary(report, dry_run=row.dry_run) if report is not None else ""
	events = tuple(
		(datetime.fromisoformat(at), str(text))
		for at, text in (row.events or [])
		if isinstance(at, str)
	)
	return TaskRunDto(
		id=row.id,
		task_id=row.task_id,
		kind=kind,
		trigger=TaskTrigger(row.trigger),
		dry_run=row.dry_run,
		executor=executor,
		executor_label=labels.get(executor) if executor is not None else None,
		started_at=as_utc(row.started_at),
		finished_at=as_utc_optional(row.finished_at),
		outcome=RunOutcome(row.outcome),
		summary=summary,
		error=row.error,
		events=events,
		report=report,
	)
