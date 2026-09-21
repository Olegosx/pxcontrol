"""Задачи сообщества (ADR-0038): виды, постановка, очередь, журнал запусков.

Тесты двух видов (служебные записи, удалённые аккаунты) перенесены
из тестов обслуживания (ADR-0026) без правок по смыслу; сверх них —
журнал запусков, замок «одна задача на сообщество» и параллельность
между сообществами.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import UTC, datetime, timedelta

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, CommunityExecutor, TgAccount
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunitiesService
from pxcontrol.engine.services.tasks import (
	EVENTS_CAP,
	TaskDto,
	TaskJobDto,
	TasksService,
	_TaskJob,
)
from pxcontrol.engine.tasks import (
	DeletedAccountsParams,
	MembersReport,
	RunOutcome,
	ServiceMessagesParams,
	ServiceReport,
	TaskError,
	TaskKind,
	TaskTrigger,
)
from pxcontrol.engine.tasks.deleted_accounts import members_summary
from pxcontrol.engine.tasks.schedule import Schedule, ScheduleKind, next_run, schedule_text
from pxcontrol.engine.tasks.service_messages import (
	DEFAULT_DELETE_LIMIT,
	DEFAULT_DEPTH,
	PAGE_SIZE,
	kind_title,
	selectable_kinds,
	service_summary,
)
from pxcontrol.engine.telegram.lane import LaneLiveState
from pxcontrol.engine.telegram.mtproto import (
	UserbotAccessError,
	UserbotFloodError,
	service_message_kind,
)
from pxcontrol.engine.telegram.rights import (
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	ParticipantStatus,
)
from pxcontrol.engine.telegram.types import (
	CommunityInfo,
	CommunityKind,
	DeletedAccount,
	ExecutorRef,
	ParticipantsPage,
	ServiceMessageInfo,
	ServiceMessageKind,
	ServiceMessagesPage,
)


class _FakeGateway:
	"""Подставной шлюз: страницы истории и учёт удалений."""

	def live_states(self) -> dict[ExecutorRef, LaneLiveState]:
		return dict(getattr(self, "lanes", {}))

	def __init__(
		self,
		pages: list[ServiceMessagesPage] | None = None,
		*,
		can_delete: bool = True,
		can_ban: bool = True,
		member_pages: list[ParticipantsPage] | None = None,
	) -> None:
		self.pages = pages or []
		self.member_pages = member_pages or []
		#: чьими руками шла работа (ADR-0035, этап E: не только публикатора)
		self.used_accounts: list[int] = []
		self.can_delete = can_delete
		self.can_ban = can_ban
		self.deleted: list[list[int]] = []
		self.kicked: list[int] = []
		self.requested: list[int] = []  # offset_id каждого запроса
		#: id, которые Telegram отказывается удалять (защищённые им)
		self.undeletable: set[int] = set()

	async def userbot_check_community(self, account_id: int, chat_ref: str) -> CommunityInfo:
		return CommunityInfo(
			chat_id=chat_ref,
			title="Группа",
			username=None,
			kind=CommunityKind.GROUP,
			rights=ExecutorRights(
				ParticipantStatus.ADMIN,
				AdminRights(delete_messages=self.can_delete, ban_users=self.can_ban),
				ALL_MEMBER_RIGHTS,
			),
		)

	async def userbot_service_messages_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage:
		self.requested.append(offset_id)
		if not self.pages:
			return ServiceMessagesPage(
				messages=[], scanned=0, next_offset_id=None, oldest_date=None
			)
		return self.pages.pop(0)

	async def userbot_delete_messages(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> int:
		self.used_accounts.append(account_id)
		self.deleted.append(list(message_ids))
		return sum(1 for message_id in message_ids if message_id not in self.undeletable)

	async def userbot_participants_page(
		self, account_id: int, chat_id: str, offset: int, limit: int
	) -> ParticipantsPage:
		if not self.member_pages:
			return ParticipantsPage(deleted=[], scanned=0, next_offset=None, total=0)
		return self.member_pages.pop(0)

	async def userbot_kick_participant(
		self, account_id: int, chat_id: str, account: DeletedAccount
	) -> int | None:
		self.kicked.append(account.user_id)
		# в супергруппе исключение порождает служебную запись
		return 9000 + account.user_id


def _page(
	*,
	kinds: list[ServiceMessageKind],
	scanned: int = PAGE_SIZE,
	next_offset_id: int | None = None,
	start_id: int = 100,
) -> ServiceMessagesPage:
	"""Страница со служебными записями перечисленных видов."""
	messages = [
		ServiceMessageInfo(id=start_id + number, kind=kind, date=datetime.now(UTC))
		for number, kind in enumerate(kinds)
	]
	return ServiceMessagesPage(
		messages=messages,
		scanned=scanned,
		next_offset_id=next_offset_id,
		oldest_date=datetime.now(UTC),
	)


async def _community(
	db: Database,
	*,
	with_account: bool = True,
	can_delete: bool = True,
	can_ban: bool = True,
	chat_id: str = "-1001",
) -> int:
	"""Сообщество с исполнителем-публикатором (или без исполнителей вовсе).

	Права задаются строке пула, а не ответу зонда: задачи читают их
	из снимка (ADR-0035, этап E) и в Telegram перед запуском не ходят.
	"""
	async with db.session_factory() as session:
		account_id = None
		if with_account:
			account = TgAccount(label="@ub", phone="+7900", session="s")
			session.add(account)
			await session.flush()
			account_id = account.id
		community = Community(
			title="Группа",
			tg_chat_id=chat_id,
			kind="group",
			default_tg_account_id=account_id,
		)
		session.add(community)
		await session.flush()
		if account_id is not None:
			session.add(
				CommunityExecutor(
					community_id=community.id,
					tg_account_id=account_id,
					status=ParticipantStatus.ADMIN,
					rights=ExecutorRights(
						ParticipantStatus.ADMIN,
						AdminRights(delete_messages=can_delete, ban_users=can_ban),
						ALL_MEMBER_RIGHTS,
					).to_payload(),
					checked_at=datetime.now(UTC),
				)
			)
		await session.commit()
		await session.refresh(community)
		return community.id


def _service(db: Database, gateway: _FakeGateway) -> TasksService:
	"""Сервис задач поверх подставного шлюза."""
	return TasksService(db, gateway, CommunitiesService(db, gateway))  # type: ignore[arg-type]


async def _scan_service(
	service: TasksService, community_id: int, depth: int = DEFAULT_DEPTH
) -> int:
	"""Ставит просмотр служебных записей (запуск «без изменений»)."""
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	return await service.run_now(task.id, ServiceMessagesParams(depth=depth), dry_run=True)


async def _clean_service(
	service: TasksService,
	community_id: int,
	kinds: list[ServiceMessageKind],
	*,
	depth: int = DEFAULT_DEPTH,
	delete_limit: int = DEFAULT_DELETE_LIMIT,
) -> int:
	"""Ставит чистку служебных записей выбранных видов."""
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	params = ServiceMessagesParams(depth=depth, kinds=tuple(kinds), delete_limit=delete_limit)
	return await service.run_now(task.id, params)


async def _scan_members(service: TasksService, community_id: int) -> int:
	"""Ставит поиск удалённых аккаунтов (запуск «без изменений»)."""
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	return await service.run_now(task.id, DeletedAccountsParams(), dry_run=True)


async def _clean_members(service: TasksService, community_id: int, *, limit: int = 20) -> int:
	"""Ставит исключение удалённых аккаунтов."""
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	return await service.run_now(task.id, DeletedAccountsParams(kick_limit=limit))


def _service_report(item: TaskJobDto) -> ServiceReport:
	"""Отчёт по служебным записям завершённого задания."""
	assert isinstance(item.report, ServiceReport)
	return item.report


def _members_report(item: TaskJobDto) -> MembersReport:
	"""Отчёт по участникам завершённого задания."""
	assert isinstance(item.report, MembersReport)
	return item.report


# --- перевод действий Telegram в виды -----------------------------------------


def test_member_actions_are_one_kind() -> None:
	"""Вступления и уходы — один вид: человеку важна группа, а не конструктор."""
	from telethon.tl import types

	for action in (
		types.MessageActionChatAddUser(users=[1]),
		types.MessageActionChatDeleteUser(user_id=1),
		types.MessageActionChatJoinedByLink(inviter_id=1),
		types.MessageActionChatJoinedByRequest(),
	):
		assert service_message_kind(action) is ServiceMessageKind.MEMBERS


def test_forum_topic_actions_are_protected() -> None:
	"""Корень темы форума защищён: удалив его, мы уничтожили бы тему.

	Идентификатор темы — это идентификатор её корневого сообщения:
	именно на него отвечает публикация в тему (ADR-0021).
	"""
	from telethon.tl import types

	assert (
		service_message_kind(types.MessageActionTopicCreate(title="Тема", icon_color=0))
		is ServiceMessageKind.PROTECTED
	)
	assert (
		service_message_kind(types.MessageActionTopicEdit(title="Тема"))
		is ServiceMessageKind.PROTECTED
	)


def test_history_actions_are_protected() -> None:
	"""Создание сообщества и переезд группы — история, а не мусор."""
	from telethon.tl import types

	for action in (
		types.MessageActionChatCreate(title="Чат", users=[1]),
		types.MessageActionChannelCreate(title="Канал"),
		types.MessageActionChatMigrateTo(channel_id=1),
		types.MessageActionChannelMigrateFrom(title="Чат", chat_id=1),
	):
		assert service_message_kind(action) is ServiceMessageKind.PROTECTED


def test_known_groups_are_recognised() -> None:
	"""Закрепления, оформление и видеочаты различаются по видам."""
	from telethon.tl import types

	assert service_message_kind(types.MessageActionPinMessage()) is ServiceMessageKind.PINS
	assert (
		service_message_kind(types.MessageActionChatEditTitle(title="Новое"))
		is ServiceMessageKind.APPEARANCE
	)
	assert (
		service_message_kind(types.MessageActionGroupCallScheduled(call=None, schedule_date=None))
		is ServiceMessageKind.CALLS
	)


def test_unknown_action_falls_into_other() -> None:
	"""Незнакомое действие не теряется и не притворяется знакомым.

	Telegram добавляет виды действий регулярно; «прочее служебное» —
	честный ответ, который видно в отчёте.
	"""
	from telethon.tl import types

	assert service_message_kind(types.MessageActionBoostApply(boosts=1)) is ServiceMessageKind.OTHER


def test_protected_kind_is_never_selectable() -> None:
	"""Защищённые виды отсеиваются при любом наборе галочек."""
	chosen = selectable_kinds(
		[ServiceMessageKind.MEMBERS, ServiceMessageKind.PROTECTED, ServiceMessageKind.MEMBERS]
	)
	assert chosen == (ServiceMessageKind.MEMBERS,)  # и дубли схлопнуты


# --- задачи: строка и параметры --------------------------------------------------


async def test_task_is_created_once_with_defaults(db: Database) -> None:
	"""Задача заводится при первом обращении и дальше возвращается та же."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	first = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	second = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	assert first.id == second.id
	assert first.params == ServiceMessagesParams()
	assert first.enabled is False and first.next_run_at is None
	assert [task.kind for task in await service.list_tasks(community_id)] == [
		TaskKind.SERVICE_MESSAGES
	]


async def test_task_for_unknown_community_is_rejected(db: Database) -> None:
	"""Задачу нельзя завести сообществу, которого нет."""
	service = _service(db, _FakeGateway())
	with pytest.raises(TaskError):
		await service.task(999, TaskKind.SERVICE_MESSAGES)


async def test_run_now_saves_params(db: Database) -> None:
	"""Запуск сохраняет параметры формы: следующий раз форма откроется с ними."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	await service.run_now(task.id, ServiceMessagesParams(depth=300, delete_limit=7), dry_run=True)
	await service.settle()
	saved = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	assert saved.params == ServiceMessagesParams(depth=300, delete_limit=7)


async def test_save_params_checks_ranges(db: Database) -> None:
	"""Негодные параметры не сохраняются — с понятным текстом."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	with pytest.raises(TaskError, match="Предел исключений"):
		await service.save_params(task.id, DeletedAccountsParams(kick_limit=0))


# --- просмотр ------------------------------------------------------------------


async def test_scan_counts_kinds_without_touching_anything(db: Database) -> None:
	"""Просмотр считает записи по видам и ничего не удаляет."""
	gateway = _FakeGateway(
		[
			_page(
				kinds=[ServiceMessageKind.MEMBERS, ServiceMessageKind.MEMBERS],
				next_offset_id=50,
			),
			_page(kinds=[ServiceMessageKind.PINS], scanned=10),
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	job_id = await _scan_service(service, community_id)
	await service.settle()
	item = next(i for i in await service.state() if i.id == job_id)
	assert item.status is JobStatus.DONE
	report = _service_report(item)
	assert report.found == {ServiceMessageKind.MEMBERS: 2, ServiceMessageKind.PINS: 1}
	assert report.scanned == PAGE_SIZE + 10
	assert report.exhausted is True  # история кончилась на второй странице
	assert gateway.deleted == []  # просмотр ничего не трогает


async def test_scan_stops_at_depth(db: Database) -> None:
	"""Проход ограничен глубиной: бесконтрольно историю не читаем."""
	pages = [_page(kinds=[], next_offset_id=index + 1) for index in range(10)]
	gateway = _FakeGateway(pages)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id, depth=PAGE_SIZE * 3)
	await service.settle()
	report = _service_report((await service.state())[0])
	assert report.scanned == PAGE_SIZE * 3
	assert len(gateway.requested) == 3  # ровно три запроса, не десять
	assert report.exhausted is False  # история не кончилась — просто хватит


async def test_depth_out_of_range_is_rejected(db: Database) -> None:
	"""Негодная глубина отклоняется до постановки, с понятным текстом."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	with pytest.raises(TaskError, match="Глубина просмотра"):
		await _scan_service(service, community_id, depth=7)


async def test_community_without_executors_is_rejected(db: Database) -> None:
	"""Без исполнителя-пользователя задачи недоступны — и объясняют почему.

	Бот тут не годится по существу: ни списка участников, ни чужой
	истории Bot API не отдаёт (ADR-0026, п. 8).
	"""
	service = _service(db, _FakeGateway())
	community_id = await _community(db, with_account=False)
	with pytest.raises(TaskError, match="только пользователи"):
		await _scan_service(service, community_id)


# --- чистка ---------------------------------------------------------------------


async def test_clean_removes_only_chosen_kinds(db: Database) -> None:
	"""Удаляются записи выбранных видов; остальные не трогаются."""
	gateway = _FakeGateway(
		[
			_page(
				kinds=[
					ServiceMessageKind.MEMBERS,
					ServiceMessageKind.PINS,
					ServiceMessageKind.MEMBERS,
				],
				scanned=3,
			)
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE
	assert _service_report(item).deleted == 2
	assert gateway.deleted == [[100, 102]]  # закрепление осталось на месте


async def test_clean_respects_delete_limit(db: Database) -> None:
	"""Потолок за проход соблюдается, и об остатке сказано честно."""
	gateway = _FakeGateway(
		[
			_page(kinds=[ServiceMessageKind.MEMBERS] * 5, scanned=5, next_offset_id=90),
			_page(kinds=[ServiceMessageKind.MEMBERS] * 5, scanned=5),
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS], delete_limit=3)
	await service.settle()
	report = _service_report((await service.state())[0])
	assert report.deleted == 3
	assert report.limited is True
	assert gateway.deleted == [[100, 101, 102]]


async def test_clean_counts_refused_as_skipped(db: Database) -> None:
	"""Запись, которую Telegram не даёт удалить, — пропуск, а не сбой."""
	gateway = _FakeGateway([_page(kinds=[ServiceMessageKind.MEMBERS] * 2, scanned=2)])
	gateway.undeletable = {101}
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE  # проход не провалился
	report = _service_report(item)
	assert (report.deleted, report.skipped) == (1, 1)


async def test_clean_requires_delete_right(db: Database) -> None:
	"""Без права удалять чистка не начинается — по снимку прав, без зонда.

	Право уже хранится (ADR-0035), и задача ищет в пуле того, кому
	удалять разрешено; отказ звучит при нажатии, а не в журнале.
	"""
	service = _service(db, _FakeGateway())
	community_id = await _community(db, can_delete=False)
	with pytest.raises(TaskError, match="удалять чужие сообщения"):
		await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	assert await service.state() == []  # задание даже не поставлено


async def test_task_takes_a_capable_executor_not_only_publisher(db: Database) -> None:
	"""Право есть у другого исполнителя пула — работу делает он (ADR-0035, этап E)."""
	from pxcontrol.engine.db.models import CommunityExecutor as ExecutorRow

	gateway = _FakeGateway([_page(kinds=[ServiceMessageKind.MEMBERS])])
	service = _service(db, gateway)
	community_id = await _community(db, can_delete=False)  # публикатор удалять не может
	async with db.session_factory() as session:
		helper = TgAccount(label="@helper", phone="+7901", session="s")
		session.add(helper)
		await session.flush()
		session.add(
			ExecutorRow(
				community_id=community_id,
				tg_account_id=helper.id,
				status=ParticipantStatus.ADMIN,
				rights=ExecutorRights(
					ParticipantStatus.ADMIN,
					AdminRights(delete_messages=True),
					ALL_MEMBER_RIGHTS,
				).to_payload(),
				checked_at=datetime.now(UTC),
			)
		)
		await session.commit()
		helper_id = helper.id
	await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	assert gateway.deleted, "чистка прошла — нашёлся способный исполнитель"
	assert gateway.used_accounts == [helper_id], "работала не публикатор, а способный"


async def test_unread_rights_are_clarified_by_a_probe(db: Database) -> None:
	"""Снимка прав не было — уточняем живым зондом, а не отказываем (ADR-0035)."""
	from sqlalchemy import select

	from pxcontrol.engine.db.models import CommunityExecutor as ExecutorRow

	gateway = _FakeGateway([_page(kinds=[ServiceMessageKind.MEMBERS])])
	service = _service(db, gateway)
	community_id = await _community(db, can_delete=False)
	async with db.session_factory() as session:  # снимка не было вовсе
		row = (
			await session.execute(
				select(ExecutorRow).where(ExecutorRow.community_id == community_id)
			)
		).scalar_one()
		row.rights = ExecutorRights(ParticipantStatus.ADMIN).to_payload()
		row.checked_at = None
		await session.commit()
	await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	assert gateway.deleted, "зонд подтвердил права — чистка пошла"


async def test_clean_without_kinds_is_rejected(db: Database) -> None:
	"""Пустой набор видов (или только защищённые) — отказ до постановки."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	with pytest.raises(TaskError, match="не выбрано|Не выбрано"):
		await _clean_service(service, community_id, [ServiceMessageKind.PROTECTED])


async def test_flood_stops_the_pass(db: Database) -> None:
	"""Флуд-лимит прекращает проход: настойчивость удлиняет срок (ADR-0017)."""

	class _FloodingGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)

	gateway = _FloodingGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id)
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.ERROR
	assert item.error is not None and "подождать" in item.error
	assert item.report is None  # неполный отчёт не сохраняется


async def test_cancel_stops_between_pages(db: Database) -> None:
	"""Отмена прекращает проход между страницами, не рвя обращение."""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[ServiceMessageKind.MEMBERS], scanned=1, next_offset_id=90)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	job_id = await _scan_service(service, community_id)
	while not gateway.requested:
		await asyncio.sleep(0)
	await service.cancel(job_id)
	release.set()
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.CANCELLED
	assert len(gateway.requested) == 1  # вторая страница не запрашивалась


async def test_scan_and_clean_queue_up(db: Database) -> None:
	"""Задания одного сообщества идут по очереди — одна задача за раз."""
	gateway = _FakeGateway(
		[
			_page(kinds=[ServiceMessageKind.MEMBERS], scanned=1),
			_page(kinds=[ServiceMessageKind.MEMBERS], scanned=1),
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	first = await _scan_service(service, community_id)
	second = await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	items = {item.id: item for item in await service.state()}
	assert isinstance(items[first].report, ServiceReport)
	assert isinstance(items[second].report, ServiceReport)
	assert all(item.status is JobStatus.DONE for item in items.values())


async def test_interrupted_clean_leaves_a_trace_in_the_log(
	db: Database, caplog: pytest.LogCaptureFixture
) -> None:
	"""Прерванная чистка сообщает журналу, что успела удалить.

	Отчёт при обрыве не строится намеренно (ADR-0026, п. 7 — лучше
	«не получилось», чем правдоподобная неправда), но удаление
	необратимо: без записи в журнале разбирать инцидент нечем.
	"""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			if len(self.requested) > 1:
				await release.wait()
			return _page(kinds=[ServiceMessageKind.MEMBERS], scanned=1, next_offset_id=90)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	job_id = await _clean_service(service, community_id, [ServiceMessageKind.MEMBERS])
	while len(gateway.requested) < 2:
		await asyncio.sleep(0)
	with caplog.at_level("INFO", logger="pxcontrol.engine.services.tasks"):
		await service.cancel(job_id)
		release.set()
		await service.settle()

	item = (await service.state())[0]
	assert item.status is JobStatus.CANCELLED
	assert item.report is None  # неполный отчёт не сохраняется
	# зато в журнале осталось, сколько записей успели удалить
	summaries = [msg for r in caplog.records if "удалено" in (msg := r.getMessage())]
	assert len(summaries) == 1
	assert re.search(r"удалено ([1-9]\d*)", summaries[0])
	# и в журнале запусков — тоже: отмена записана с событиями
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	(run,) = await service.runs(task.id)
	assert run.outcome is RunOutcome.CANCELLED
	assert any("удалено" in text for _at, text in run.events)


# --- журнал запусков ---------------------------------------------------------------


async def test_run_is_recorded_in_journal(db: Database) -> None:
	"""Каждый запуск оставляет строку: кто, чьими руками, чем кончилось."""
	gateway = _FakeGateway([_page(kinds=[ServiceMessageKind.MEMBERS], scanned=1)])
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id)
	await service.settle()
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	(run,) = await service.runs(task.id)
	assert run.trigger is TaskTrigger.MANUAL
	assert run.dry_run is True
	assert run.outcome is RunOutcome.DONE
	assert run.finished_at is not None and run.finished_at >= run.started_at
	assert run.executor_label == "@ub"  # чьими руками шла работа
	assert "Найдено служебных записей: 1" in run.summary
	assert any("исполнитель" in text for _at, text in run.events)
	assert task.last_run_at is not None


async def test_failed_run_is_recorded_with_reason(db: Database) -> None:
	"""Запуск с ошибкой попадает в журнал с причиной."""

	class _FloodingGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)

	service = _service(db, _FloodingGateway())
	community_id = await _community(db)
	await _scan_service(service, community_id)
	await service.settle()
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	(run,) = await service.runs(task.id)
	assert run.outcome is RunOutcome.ERROR
	assert run.error is not None and "подождать" in run.error
	assert run.summary == ""  # отчёта нет — и строка честно пуста


async def test_engine_stop_is_recorded_as_interrupted(db: Database) -> None:
	"""Остановка движка посреди запуска — исход «прервано», а не «отменено»."""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[ServiceMessageKind.MEMBERS], scanned=1, next_offset_id=90)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id)
	while not gateway.requested:
		await asyncio.sleep(0)
	stopping = asyncio.create_task(service.shutdown())
	await asyncio.sleep(0)
	release.set()
	await stopping
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	(run,) = await service.runs(task.id)
	assert run.outcome is RunOutcome.INTERRUPTED


async def test_retry_opens_a_new_journal_row(db: Database) -> None:
	"""Повтор после ошибки — новый запуск в журнале, старый остаётся."""
	calls = 0

	class _FlakyGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			nonlocal calls
			calls += 1
			if calls == 1:
				raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
			return _page(kinds=[], scanned=1)

	service = _service(db, _FlakyGateway())
	community_id = await _community(db)
	job_id = await _scan_service(service, community_id)
	await service.settle()
	await service.retry(job_id)
	await service.settle()
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	runs = await service.runs(task.id)
	assert [run.outcome for run in runs] == [RunOutcome.DONE, RunOutcome.ERROR]  # новые сверху


def test_run_events_are_capped() -> None:
	"""Событий у запуска не больше предела; вытесняются ранние, не последние."""
	from pxcontrol.engine.services.communities import CommunityDto

	community = CommunityDto(
		id=1,
		title="Группа",
		username=None,
		tg_chat_id="-1001",
		default_bot_id=None,
		default_bot_label=None,
		enabled=True,
	)
	task = TaskDto(
		id=1,
		community_id=1,
		kind=TaskKind.SERVICE_MESSAGES,
		params=ServiceMessagesParams(),
		enabled=False,
		schedule={"kind": "none"},
		next_run_at=None,
		last_run_at=None,
	)
	job = _TaskJob(1, task, community, dry_run=True, trigger=TaskTrigger.MANUAL)
	for number in range(EVENTS_CAP + 20):
		TasksService._log(job, f"событие {number}")
	assert len(job.events) == EVENTS_CAP
	assert job.events[-1][1] == f"событие {EVENTS_CAP + 19}"


# --- очередь: замок сообщества и параллельность -----------------------------------


async def test_one_task_per_community_at_a_time(db: Database) -> None:
	"""В одном сообществе задачи идут по одной: вторая ждёт первую."""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[], scanned=1)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id)
	await _scan_members(service, community_id)
	while not gateway.requested:
		await asyncio.sleep(0)
	for _ in range(5):
		await asyncio.sleep(0)
	statuses = sorted(item.status for item in await service.state())
	assert statuses == [JobStatus.PENDING, JobStatus.RUNNING]  # вторая не начата
	release.set()
	await service.settle()
	assert all(item.status is JobStatus.DONE for item in await service.state())


async def test_tasks_of_different_communities_run_side_by_side(db: Database) -> None:
	"""Задачи разных сообществ не ждут друг друга (слоты очереди, ADR-0036)."""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[], scanned=1)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	first = await _community(db, chat_id="-1001")
	second = await _community(db, chat_id="-1002")
	await _scan_service(service, first)
	await _scan_service(service, second)
	while len(gateway.requested) < 2:
		await asyncio.sleep(0)
	assert {item.status for item in await service.state()} == {JobStatus.RUNNING}
	release.set()
	await service.settle()


async def test_deleting_community_stops_its_tasks(db: Database) -> None:
	"""Удаление сообщества снимает его задания.

	Задание держит снимок сообщества и работает по его ``tg_chat_id``,
	от строки в БД не завися: без снятия уборка продолжала бы удалять
	записи в Telegram для сущности, которой в приложении уже нет.
	"""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[ServiceMessageKind.MEMBERS], scanned=1, next_offset_id=90)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	running = await _scan_service(service, community_id)
	waiting = await _scan_service(service, community_id)
	while not gateway.requested:
		await asyncio.sleep(0)

	await service.drop_community(community_id)
	release.set()
	await service.settle()

	items = {item.id: item for item in await service.state()}
	assert items[waiting].status is JobStatus.CANCELLED  # ждавшее снято сразу
	assert items[running].status is JobStatus.CANCELLED  # идущее остановлено
	assert len(gateway.requested) == 1  # вторая страница не запрашивалась


async def test_task_rows_die_with_community(db: Database) -> None:
	"""Задачи и журнал уходят каскадом вместе с сообществом."""
	from sqlalchemy import func, select

	from pxcontrol.engine.db.models import CommunityTask, TaskRun

	gateway = _FakeGateway([_page(kinds=[], scanned=1)])
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id)
	await service.settle()
	await CommunitiesService(db, gateway).delete_community(community_id)  # type: ignore[arg-type]
	async with db.session_factory() as session:
		tasks = (
			await session.execute(select(func.count()).select_from(CommunityTask))
		).scalar_one()
		runs = (await session.execute(select(func.count()).select_from(TaskRun))).scalar_one()
	assert (tasks, runs) == (0, 0)


# --- тексты (чистые функции) -------------------------------------------------------


def test_service_summary_tells_what_was_seen() -> None:
	"""Итог просмотра называет число, глубину и границу по дате."""
	moment = datetime(2026, 3, 12, 10, 30, tzinfo=UTC)
	report = ServiceReport(
		found={ServiceMessageKind.MEMBERS: 12},
		scanned=2000,
		oldest_date=moment,
		exhausted=False,
	)
	text = service_summary(report)
	assert "12" in text
	assert "2000" in text  # видно, насколько глубоко смотрели
	assert "2026" in text  # и до какого числа дошли


def test_service_summary_says_history_is_over() -> None:
	"""Кончившаяся история — отдельная формулировка, а не «просмотрено N»."""
	report = ServiceReport(
		found={ServiceMessageKind.PINS: 3}, scanned=42, oldest_date=None, exhausted=True
	)
	assert "целиком" in service_summary(report)


def test_service_summary_for_empty_result() -> None:
	"""Пустой результат не притворяется находкой."""
	assert "не найдено" in service_summary(ServiceReport(scanned=500))


def test_service_summary_reports_skipped_and_limit() -> None:
	"""Итог чистки честен про пропуски и про упёршийся потолок."""
	text = service_summary(ServiceReport(deleted=40, skipped=2, scanned=900, limited=True))
	assert "40" in text
	assert "не дал удалить: 2" in text
	assert "повторите" in text  # человеку сказано, что осталось ещё


def test_every_kind_has_human_title() -> None:
	"""У каждого вида есть человеческое название — без «ServiceMessageKind.OTHER»."""
	for kind in ServiceMessageKind:
		title = kind_title(kind)
		assert title and not title.startswith("ServiceMessageKind")


def test_report_survives_json_round_trip() -> None:
	"""Отчёт переживает запись в журнал и чтение обратно без потерь."""
	from pxcontrol.engine.tasks import spec_of

	spec = spec_of(TaskKind.SERVICE_MESSAGES)
	report = ServiceReport(
		found={ServiceMessageKind.MEMBERS: 2, ServiceMessageKind.PINS: 1},
		scanned=300,
		deleted=2,
		limited=True,
		oldest_date=datetime(2026, 3, 12, 10, 30, tzinfo=UTC),
	)
	assert spec.report_from_payload(spec.report_to_payload(report)) == report
	members = spec_of(TaskKind.DELETED_ACCOUNTS)
	original = MembersReport(found=3, scanned=10, total=10, removed=2, skipped=1, capped=False)
	assert members.report_from_payload(members.report_to_payload(original)) == original


def test_params_survive_json_round_trip_and_ignore_junk() -> None:
	"""Параметры переживают запись и чтение; битое значение — к умолчанию."""
	from pxcontrol.engine.tasks import spec_of

	spec = spec_of(TaskKind.SERVICE_MESSAGES)
	params = ServiceMessagesParams(depth=500, kinds=(ServiceMessageKind.PINS,), delete_limit=9)
	assert spec.params_from_payload(spec.params_to_payload(params)) == params
	junk = spec.params_from_payload({"depth": "много", "kinds": ["pins", "нет такого"]})
	assert junk == ServiceMessagesParams(kinds=(ServiceMessageKind.PINS,))


def test_journal_texts_for_ui() -> None:
	"""Строки журнала: кто запустил, исход с итогом или причиной, события."""
	from pxcontrol.engine.services.tasks import TaskRunDto
	from pxcontrol.ui.pages.tasks import run_events_text, run_kind_text, run_result_text

	at = datetime(2026, 3, 12, 10, 30, tzinfo=UTC)

	def run(outcome: RunOutcome, *, summary: str = "", error: str | None = None) -> TaskRunDto:
		return TaskRunDto(
			id=1,
			task_id=1,
			kind=TaskKind.SERVICE_MESSAGES,
			trigger=TaskTrigger.MANUAL,
			dry_run=True,
			executor=None,
			executor_label=None,
			started_at=at,
			finished_at=at,
			outcome=outcome,
			summary=summary,
			error=error,
			events=((at, "исполнитель — @ub"),),
		)

	assert run_kind_text(run(RunOutcome.DONE)) == "вручную · без изменений"
	assert run_result_text(run(RunOutcome.DONE, summary="Найдено: 3")) == "готово — Найдено: 3"
	assert run_result_text(run(RunOutcome.ERROR, error="флуд")) == "ошибка: флуд"
	assert run_result_text(run(RunOutcome.INTERRUPTED)) == "прервано остановкой"
	assert "исполнитель — @ub" in run_events_text(run(RunOutcome.DONE))


# --- удалённые аккаунты ---------------------------------------------------------


def _members(
	*, deleted: list[int], scanned: int, next_offset: int | None, total: int | None
) -> ParticipantsPage:
	"""Страница участников с удалёнными учётками."""
	return ParticipantsPage(
		# хеш доступа приходит вместе с участником и нужен для исключения
		deleted=[DeletedAccount(user_id, access_hash=user_id * 10) for user_id in deleted],
		scanned=scanned,
		next_offset=next_offset,
		total=total,
	)


async def test_scan_members_finds_deleted_without_kicking(db: Database) -> None:
	"""Поиск мёртвых душ никого не исключает."""
	gateway = _FakeGateway(
		member_pages=[
			_members(deleted=[11, 12], scanned=200, next_offset=200, total=350),
			_members(deleted=[13], scanned=150, next_offset=None, total=350),
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_members(service, community_id)
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE
	report = _members_report(item)
	assert (report.found, report.scanned, report.total) == (3, 350, 350)
	assert report.exhausted is True
	assert report.capped is False  # список отдан целиком
	assert gateway.kicked == []


async def test_scan_members_notices_telegram_cap(db: Database) -> None:
	"""Если Telegram отдал меньше, чем участников, — это видно в отчёте."""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[], scanned=200, next_offset=None, total=10_000)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _scan_members(service, community_id)
	await service.settle()
	assert _members_report((await service.state())[0]).capped is True


async def test_clean_members_respects_limit(db: Database) -> None:
	"""Потолок исключений соблюдается: число участников падает порциями."""
	gateway = _FakeGateway(
		member_pages=[
			_members(deleted=[11, 12, 13, 14], scanned=200, next_offset=200, total=400),
			_members(deleted=[15], scanned=200, next_offset=None, total=400),
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_members(service, community_id, limit=2)
	await service.settle()
	report = _members_report((await service.state())[0])
	assert report.removed == 2
	assert report.limited is True
	assert gateway.kicked == [11, 12]


async def test_clean_members_sweeps_its_own_service_notes(db: Database) -> None:
	"""Чистка убирает записи «удалил участника», которые сама породила."""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11, 12], scanned=10, next_offset=None, total=10)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_members(service, community_id, limit=10)
	await service.settle()
	report = _members_report((await service.state())[0])
	assert report.removed == 2
	assert gateway.deleted == [[9011, 9012]]  # одной пачкой, а не по одной
	assert report.service_left == 0


async def test_clean_members_reports_notes_left_without_delete_right(db: Database) -> None:
	"""Без права удалять записи об исключении остаются — и это сказано."""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11], scanned=10, next_offset=None, total=10)],
	)
	service = _service(db, gateway)
	community_id = await _community(db, can_delete=False)
	await _clean_members(service, community_id, limit=10)
	await service.settle()
	report = _members_report((await service.state())[0])
	assert (report.removed, report.service_left) == (1, 1)
	assert gateway.deleted == []  # без права даже не пробуем


async def test_clean_members_requires_ban_right(db: Database) -> None:
	"""Без права исключать чистка не начинается (по снимку прав)."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db, can_ban=False)
	with pytest.raises(TaskError, match="исключать участников"):
		await _clean_members(service, community_id)
	assert await service.state() == []


async def test_kick_limit_allows_single_account(db: Database) -> None:
	"""Потолок в одну штуку допустим: убрать один мёртвый аккаунт можно."""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11, 12], scanned=10, next_offset=None, total=10)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_members(service, community_id, limit=1)
	await service.settle()
	assert _members_report((await service.state())[0]).removed == 1


async def test_kick_limit_zero_is_rejected(db: Database) -> None:
	"""Ноль исключений — не проход, а недоразумение: отказ с объяснением."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	with pytest.raises(TaskError, match="Предел исключений"):
		await _clean_members(service, community_id, limit=0)


def test_members_summary_mentions_cap_and_leftovers() -> None:
	"""Итог по участникам честен про предел выдачи и оставшиеся записи."""
	capped = members_summary(MembersReport(found=0, scanned=200, total=10_000, capped=True))
	assert "список не отдаёт" in capped
	left = members_summary(MembersReport(found=3, removed=3, service_left=3))
	assert "осталось в ленте: 3" in left


async def test_clean_members_skips_account_telegram_refuses(db: Database) -> None:
	"""Отказ по одной учётке — пропуск, а не конец прохода (ADR-0026)."""

	class _PickyGateway(_FakeGateway):
		async def userbot_kick_participant(
			self, account_id: int, chat_id: str, account: DeletedAccount
		) -> int | None:
			if account.user_id == 12:
				raise UserbotAccessError("Этого участника исключить нельзя.")
			return await super().userbot_kick_participant(account_id, chat_id, account)

	gateway = _PickyGateway(
		member_pages=[_members(deleted=[11, 12, 13], scanned=10, next_offset=None, total=10)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await _clean_members(service, community_id, limit=10)
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE  # проход не провалился
	report = _members_report(item)
	assert (report.removed, report.skipped) == (2, 1)
	assert gateway.kicked == [11, 13]


async def test_members_report_reaches_stats_hook(db: Database) -> None:
	"""Итог по удалённым аккаунтам уходит крючком в кэш статистики (ADR-0027)."""
	recorded: list[tuple[int, int, int]] = []

	async def hook(community_id: int, found: int, removed: int, at: datetime) -> None:
		recorded.append((community_id, found, removed))

	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11, 12], scanned=10, next_offset=None, total=10)]
	)
	service = TasksService(db, gateway, CommunitiesService(db, gateway), on_members_report=hook)  # type: ignore[arg-type]
	community_id = await _community(db)
	await _clean_members(service, community_id, limit=1)
	await service.settle()
	assert recorded == [(community_id, 2, 1)]


def test_members_summary_reports_refusals() -> None:
	"""Итог по участникам называет и отказы Telegram."""
	text = members_summary(MembersReport(found=3, removed=2, skipped=1))
	assert "Исключено удалённых аккаунтов: 2" in text
	assert "не дал исключить: 1" in text


# --- расписание (этап B) ---------------------------------------------------------


class _FixedRandom(random.Random):
	"""Источник случайности, отдающий нижнюю границу: тесты предсказуемы."""

	def uniform(self, a: float, b: float) -> float:
		return a


def _scheduled_service(db: Database, gateway: _FakeGateway) -> TasksService:
	"""Сервис задач с предсказуемым интервалом и зоной UTC."""
	return TasksService(  # type: ignore[arg-type]
		db, gateway, CommunitiesService(db, gateway), tz=UTC, rng=_FixedRandom()
	)


def test_schedule_survives_json_round_trip_and_ignores_junk() -> None:
	"""Расписание переживает запись и чтение; битое — к умолчаниям."""
	interval = Schedule(ScheduleKind.INTERVAL, min_minutes=35, max_minutes=96)
	assert Schedule.from_payload(interval.to_payload()) == interval
	daily = Schedule(ScheduleKind.DAILY, times=("04:00", "16:30"))
	assert Schedule.from_payload(daily.to_payload()) == daily
	assert Schedule.from_payload({"kind": "weekly"}).kind is ScheduleKind.NONE
	assert Schedule.from_payload("мусор") == Schedule()
	junk = Schedule.from_payload({"kind": "daily", "times": ["04:00", "25:99", 7]})
	assert junk.times == ("04:00",)


def test_schedule_validation_names_the_problem() -> None:
	"""Перевёрнутый интервал, пустые и битые моменты — отказ с текстом."""
	with pytest.raises(TaskError, match="верхняя граница меньше"):
		Schedule(ScheduleKind.INTERVAL, min_minutes=90, max_minutes=30).validate()
	with pytest.raises(TaskError, match="Интервал"):
		Schedule(ScheduleKind.INTERVAL, min_minutes=0, max_minutes=30).validate()
	with pytest.raises(TaskError, match="хотя бы один момент"):
		Schedule(ScheduleKind.DAILY).validate()
	with pytest.raises(TaskError, match="ЧЧ:ММ"):
		Schedule(ScheduleKind.DAILY, times=("4 утра",)).validate()
	Schedule().validate()  # «только по требованию» всегда годится


def test_next_run_interval_is_drawn_once_within_bounds() -> None:
	"""Интервал — от конца запуска, случайно в границах; равные границы — точно."""
	after = datetime(2026, 3, 12, 10, 0, tzinfo=UTC)
	schedule = Schedule(ScheduleKind.INTERVAL, min_minutes=35, max_minutes=96)
	moment = next_run(schedule, after, UTC, random.Random(7))
	assert moment is not None
	assert after + timedelta(minutes=35) <= moment <= after + timedelta(minutes=96)
	fixed = Schedule(ScheduleKind.INTERVAL, min_minutes=60, max_minutes=60)
	assert next_run(fixed, after, UTC, random.Random(7)) == after + timedelta(hours=1)
	assert next_run(Schedule(), after, UTC) is None


def test_next_run_daily_takes_nearest_moment_after_now() -> None:
	"""Ближайший момент суток сегодня, иначе первый по порядку завтра."""
	schedule = Schedule(ScheduleKind.DAILY, times=("16:30", "04:00"))
	morning = datetime(2026, 3, 12, 10, 0, tzinfo=UTC)
	assert next_run(schedule, morning, UTC) == datetime(2026, 3, 12, 16, 30, tzinfo=UTC)
	evening = datetime(2026, 3, 12, 20, 0, tzinfo=UTC)
	assert next_run(schedule, evening, UTC) == datetime(2026, 3, 13, 4, 0, tzinfo=UTC)
	exact = datetime(2026, 3, 12, 16, 30, tzinfo=UTC)  # ровно в момент — уже прошёл
	assert next_run(schedule, exact, UTC) == datetime(2026, 3, 13, 4, 0, tzinfo=UTC)


def test_schedule_text_reads_naturally() -> None:
	"""Расписание по-русски: интервал, постоянный интервал, моменты, ничего."""
	assert "35–96 мин" in schedule_text(Schedule(ScheduleKind.INTERVAL, 35, 96))
	assert schedule_text(Schedule(ScheduleKind.INTERVAL, 60, 60)) == "каждые 60 мин"
	assert schedule_text(Schedule(ScheduleKind.DAILY, times=("04:00",))) == "ежедневно в 04:00"
	assert schedule_text(Schedule()) == "только по требованию"


async def test_save_schedule_assigns_next_run_and_disable_clears_it(db: Database) -> None:
	"""Включение назначает следующий запуск; выключение снимает его."""
	service = _scheduled_service(db, _FakeGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	before = datetime.now(UTC)
	saved = await service.save_schedule(
		task.id, Schedule(ScheduleKind.INTERVAL, 35, 96), enabled=True
	)
	assert saved.enabled is True
	assert saved.next_run_at is not None
	assert saved.next_run_at >= before + timedelta(minutes=35) - timedelta(seconds=1)
	disabled = await service.save_schedule(
		task.id, Schedule(ScheduleKind.INTERVAL, 35, 96), enabled=False
	)
	assert disabled.enabled is False and disabled.next_run_at is None


async def test_enabling_without_a_schedule_is_rejected(db: Database) -> None:
	"""«Только по требованию» включать нечего — отказ с объяснением."""
	service = _scheduled_service(db, _FakeGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	with pytest.raises(TaskError, match="Выберите расписание"):
		await service.save_schedule(task.id, Schedule(), enabled=True)


async def test_enabling_clean_schedule_requires_chosen_kinds(db: Database) -> None:
	"""Чистка по расписанию без видов записей — пустая работа: отказ при сохранении."""
	service = _scheduled_service(db, _FakeGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	await service.save_params(task.id, ServiceMessagesParams(kinds=()))
	with pytest.raises(TaskError, match="Не выбрано"):
		await service.save_schedule(
			task.id, Schedule(ScheduleKind.DAILY, times=("04:00",)), enabled=True
		)


async def test_scheduler_starts_due_task_and_reschedules(db: Database) -> None:
	"""Тик планировщика ставит запуск по сроку, а конец запуска назначает новый."""
	gateway = _FakeGateway(member_pages=[])
	service = _scheduled_service(db, gateway)
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	await service.save_schedule(task.id, Schedule(ScheduleKind.INTERVAL, 1, 1), enabled=True)
	assert await service.run_due(datetime.now(UTC)) == 0  # срок ещё не наступил
	later = datetime.now(UTC) + timedelta(hours=1)
	assert await service.run_due(later) == 1
	await service.settle()
	(run,) = await service.runs(task.id)
	assert run.trigger is TaskTrigger.SCHEDULE
	assert run.dry_run is False
	assert run.outcome is RunOutcome.DONE
	fresh = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	assert fresh.next_run_at is not None
	assert fresh.next_run_at > run.finished_at  # следующий — от конца запуска
	assert await service.run_due(datetime.now(UTC)) == 0  # и он ещё не наступил


async def test_scheduler_skips_disabled_community(db: Database) -> None:
	"""Выключенное сообщество не запускается по расписанию."""
	from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService

	service = _scheduled_service(db, _FakeGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	await service.save_schedule(task.id, Schedule(ScheduleKind.INTERVAL, 1, 1), enabled=True)
	await SettingsService(db).set_for(COMMUNITY_ENABLED, community_id, False)
	assert await service.run_due(datetime.now(UTC) + timedelta(hours=1)) == 0


async def test_scheduler_does_not_stack_on_running_task(db: Database) -> None:
	"""Пока идёт запуск задачи, тик не ставит второй."""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def userbot_service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[], scanned=1)

	gateway = _SlowGateway()
	service = _scheduled_service(db, gateway)
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	await service.save_schedule(task.id, Schedule(ScheduleKind.INTERVAL, 1, 1), enabled=True)
	await _scan_service(service, community_id)  # ручной запуск уже идёт
	while not gateway.requested:
		await asyncio.sleep(0)
	assert await service.run_due(datetime.now(UTC) + timedelta(hours=1)) == 0
	release.set()
	await service.settle()
	assert len(await service.state()) == 1


async def test_scheduled_error_leaves_queue_but_stays_in_journal(db: Database) -> None:
	"""Ошибка запуска по расписанию не копится карточками: её «повтор» — следующий срок."""

	class _FloodingGateway(_FakeGateway):
		async def userbot_participants_page(
			self, account_id: int, chat_id: str, offset: int, limit: int
		) -> ParticipantsPage:
			raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)

	service = _scheduled_service(db, _FloodingGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	await service.save_schedule(task.id, Schedule(ScheduleKind.INTERVAL, 1, 1), enabled=True)
	assert await service.run_due(datetime.now(UTC) + timedelta(hours=1)) == 1
	await service.settle()
	assert await service.state() == []  # карточки с ошибкой в панели нет
	(run,) = await service.runs(task.id)
	assert run.outcome is RunOutcome.ERROR and run.error is not None
	fresh = await service.task(community_id, TaskKind.DELETED_ACCOUNTS)
	assert fresh.next_run_at is not None  # следующий срок назначен


async def test_prune_removes_runs_older_than_retention(db: Database) -> None:
	"""Журнал старше срока хранения убирается, свежий остаётся."""
	from pxcontrol.engine.db.models import TaskRun

	gateway = _FakeGateway([_page(kinds=[], scanned=1)])
	service = _scheduled_service(db, gateway)
	community_id = await _community(db)
	await _scan_service(service, community_id)
	await service.settle()
	task = await service.task(community_id, TaskKind.SERVICE_MESSAGES)
	async with db.session_factory() as session:
		session.add(
			TaskRun(
				task_id=task.id,
				trigger="manual",
				started_at=datetime.now(UTC) - timedelta(days=100),
				outcome="done",
			)
		)
		await session.commit()
	assert len(await service.runs(task.id)) == 2
	assert await service.prune_runs(datetime.now(UTC)) == 1
	assert len(await service.runs(task.id)) == 1


def test_schedule_texts_for_ui() -> None:
	"""Подпись под формой: вид, включено ли, следующий запуск; разбор моментов."""
	from pxcontrol.ui.pages.tasks import next_run_text, parse_times

	def task(schedule: Schedule, *, enabled: bool, next_at: datetime | None) -> TaskDto:
		return TaskDto(
			id=1,
			community_id=1,
			kind=TaskKind.SERVICE_MESSAGES,
			params=ServiceMessagesParams(),
			enabled=enabled,
			schedule=schedule,
			next_run_at=next_at,
			last_run_at=None,
		)

	at = datetime(2026, 3, 12, 10, 30, tzinfo=UTC)
	assert next_run_text(task(Schedule(), enabled=False, next_at=None)) == (
		"Расписание: только по требованию"
	)
	off = next_run_text(
		task(Schedule(ScheduleKind.DAILY, times=("04:00",)), enabled=False, next_at=None)
	)
	assert off.startswith("Расписание выключено")
	on = next_run_text(task(Schedule(ScheduleKind.INTERVAL, 35, 96), enabled=True, next_at=at))
	assert "следующий запуск" in on and "2026" in on
	assert parse_times(" 04:00, 16:30 ,,") == ("04:00", "16:30")
