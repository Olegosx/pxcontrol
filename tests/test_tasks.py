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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, CommunityExecutor, TgAccount
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunitiesService, ExecutorDto
from pxcontrol.engine.services.tasks import (
	EVENTS_CAP,
	TaskDto,
	TaskJobDto,
	TaskRunDto,
	TasksService,
	_TaskJob,
)
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
	TaskTrigger,
)
from pxcontrol.engine.tasks.deleted_accounts import members_summary
from pxcontrol.engine.tasks.join_requests import join_requests_summary, profile_has_links
from pxcontrol.engine.tasks.reactions import (
	next_user,
	pick_reaction,
	pick_reactions,
	reactions_summary,
	rotation,
	select_targets,
)
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
	UserbotJoinRequestError,
	UserbotReactionError,
	service_message_kind,
)
from pxcontrol.engine.telegram.rights import (
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	ParticipantStatus,
)
from pxcontrol.engine.telegram.types import (
	ChatReactions,
	ChatReactionsMode,
	CommunityInfo,
	CommunityKind,
	DeletedAccount,
	ExecutorRef,
	JoinRequest,
	JoinRequestsPage,
	OwnerKind,
	ParticipantsPage,
	ReactablePost,
	ReactionOption,
	ReactionsPage,
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
	can_invite: bool = True,
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
						# право приглашать — для приёма заявок (ADR-0040); у прочих
						# видов оно ничего не решает
						AdminRights(
							delete_messages=can_delete, ban_users=can_ban, invite_users=can_invite
						),
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
	# отчёт отдаётся целиком: форма восстанавливает по нему числа просмотра
	assert isinstance(run.report, ServiceReport)
	assert run.report.found == {ServiceMessageKind.MEMBERS: 1}


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
	assert next_run_text(task(Schedule(), enabled=False, next_at=None)).startswith(
		"Расписание не задано"
	)
	off = next_run_text(
		task(Schedule(ScheduleKind.DAILY, times=("04:00",)), enabled=False, next_at=None)
	)
	assert off.startswith("Расписание выключено")
	on = next_run_text(task(Schedule(ScheduleKind.INTERVAL, 35, 96), enabled=True, next_at=at))
	assert "Следующий запуск" in on and "2026" in on

	# строка расписания на карточке обзора — короче: без «Расписание:»
	from pxcontrol.ui.pages.tasks import schedule_caption

	assert schedule_caption(task(Schedule(), enabled=False, next_at=None)) == "по требованию"
	daily = task(Schedule(ScheduleKind.DAILY, times=("04:00",)), enabled=True, next_at=at)
	assert schedule_caption(daily).startswith("каждый день в 04:00 · следующий в ")
	interval = task(Schedule(ScheduleKind.INTERVAL, 30, 60), enabled=False, next_at=None)
	assert schedule_caption(interval) == "каждые 30–60 мин · выключено"
	assert parse_times(" 04:00, 16:30 ,,") == ("04:00", "16:30")


# --- реакции (этап C, ADR-0039) -----------------------------------------------------


class _ReactionsGateway(_FakeGateway):
	"""Подставной шлюз реакций: перечень, лента с реакциями, отправка."""

	def __init__(
		self,
		*,
		mode: ChatReactionsMode = ChatReactionsMode.ALL,
		options: tuple[str, ...] = ("👍", "🔥", "❤"),
		feed: list[ReactionsPage] | None = None,
		premium: bool = False,
	) -> None:
		super().__init__()
		self.mode = mode
		self.options = options
		self.feed = feed or []
		self.premium = premium
		self.sent: list[tuple[int, int, tuple[str, ...]]] = []
		self.refused: set[int] = set()  # записи, на которые Telegram не принимает реакцию

	async def userbot_available_reactions(self, account_id: int, chat_id: str) -> ChatReactions:
		return ChatReactions(
			self.mode, tuple(ReactionOption(emoji=e, title=e) for e in self.options)
		)

	async def userbot_reactions_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ReactionsPage:
		self.requested.append(offset_id)
		if not self.feed:
			return ReactionsPage(posts=[], scanned=0, next_offset_id=None)
		return self.feed.pop(0)

	async def userbot_send_reaction(
		self, account_id: int, chat_id: str, message_id: int, emojis: Sequence[str]
	) -> None:
		if message_id in self.refused:
			raise UserbotReactionError("Telegram не принял реакцию: REACTION_INVALID")
		self.sent.append((account_id, message_id, tuple(emojis)))

	def userbot_premium(self, account_id: int | None) -> bool:
		return self.premium


def _post(message_id: int, mine: tuple[str, ...] = ()) -> ReactablePost:
	return ReactablePost(id=message_id, date=datetime.now(UTC), mine=mine)


def _feed(*ids: int, mine: dict[int, tuple[str, ...]] | None = None) -> list[ReactionsPage]:
	"""Одна страница ленты с записями по номерам."""
	mine = mine or {}
	posts = [_post(i, mine.get(i, ())) for i in ids]
	return [ReactionsPage(posts=posts, scanned=len(posts), next_offset_id=None)]


async def _add_user(db: Database, community_id: int, label: str) -> int:
	"""Ещё один пользователь пула с правом реагировать (администратор)."""
	async with db.session_factory() as session:
		account = TgAccount(label=label, phone="+7902", session="s")
		session.add(account)
		await session.flush()
		session.add(
			CommunityExecutor(
				community_id=community_id,
				tg_account_id=account.id,
				status=ParticipantStatus.ADMIN,
				rights=ExecutorRights(
					ParticipantStatus.ADMIN, AdminRights(), ALL_MEMBER_RIGHTS
				).to_payload(),
				checked_at=datetime.now(UTC),
			)
		)
		await session.commit()
		return account.id


def _reaction_params(*user_ids: int, **overrides: Any) -> ReactionsParams:
	"""Параметры реакций без пауз (тесты не ждут настоящих секунд)."""
	base: dict[str, Any] = {
		"users": tuple(ExecutorRef(OwnerKind.USER, i) for i in user_ids),
		"reactions": (ReactionChoice("👍", 70), ReactionChoice("🔥", 30)),
		"pause_min_s": 0.0,
		"pause_max_s": 0.0,
	}
	base.update(overrides)
	return ReactionsParams(**base)


async def _account_id(db: Database, community_id: int) -> int:
	"""Аккаунт публикатора сообщества из теста."""
	from sqlalchemy import select

	async with db.session_factory() as session:
		return (
			await session.execute(
				select(Community.default_tg_account_id).where(Community.id == community_id)
			)
		).scalar_one()


def test_pick_reaction_respects_weights_and_skips_zero() -> None:
	"""Вес — вероятность: нулевой не выпадает, больший выпадает чаще."""
	rng = random.Random(3)
	choices = (ReactionChoice("👍", 90), ReactionChoice("🔥", 10), ReactionChoice("❤", 0))
	drawn = [pick_reaction(choices, rng) for _ in range(500)]
	assert "❤" not in drawn
	assert drawn.count("👍") > drawn.count("🔥") * 3
	assert pick_reaction((ReactionChoice("❤", 0),), rng) is None
	assert set(pick_reactions(choices, 2, rng)) == {"🔥", "👍"}  # две разные


def test_select_targets_by_scope_skips_posts_with_my_reaction() -> None:
	"""Охваты: все без моей реакции, последняя, случайные — с потолком за проход."""
	rng = random.Random(1)
	posts = [_post(5), _post(4, ("👍",)), _post(3), _post(2), _post(1)]
	everything = select_targets(
		posts, ReactionScope.ALL_WITHOUT_MINE, random_count=3, limit=10, rng=rng
	)
	assert [p.id for p in everything] == [5, 3, 2, 1]
	capped = select_targets(posts, ReactionScope.ALL_WITHOUT_MINE, random_count=3, limit=2, rng=rng)
	assert [p.id for p in capped] == [5, 3]
	last = select_targets(posts, ReactionScope.LAST_POST, random_count=3, limit=10, rng=rng)
	assert [p.id for p in last] == [5]
	reacted_last = [_post(9, ("🔥",)), _post(8)]
	assert (
		select_targets(reacted_last, ReactionScope.LAST_POST, random_count=1, limit=1, rng=rng)
		== []
	)
	some = select_targets(
		posts, ReactionScope.RANDOM_WITHOUT_MINE, random_count=2, limit=10, rng=rng
	)
	assert len(some) == 2 and all(not p.mine for p in some)


def test_user_rotation_survives_shrunken_list() -> None:
	"""Курсор по кругу; список пользователей мог уменьшиться — номер по модулю."""
	users = (ExecutorRef(OwnerKind.USER, 1), ExecutorRef(OwnerKind.USER, 2))
	assert next_user(users, None) == 0
	assert next_user(users, {"next": 1}) == 1
	assert next_user(users, {"next": 5}) == 1
	assert [u.id for u in rotation(users, 1)] == [2, 1]


def test_reactions_params_and_report_survive_json() -> None:
	"""Параметры и отчёт реакций переживают запись и чтение."""
	from pxcontrol.engine.tasks import spec_of

	spec = spec_of(TaskKind.REACTIONS)
	params = _reaction_params(1, 2, scope=ReactionScope.RANDOM_WITHOUT_MINE, premium_double=True)
	assert spec.params_from_payload(spec.params_to_payload(params)) == params
	report = ReactionsReport(executor="@a", scanned=10, candidates=3, reacted=2, by_emoji={"👍": 2})
	assert spec.report_from_payload(spec.report_to_payload(report)) == report
	assert (
		spec.params_from_payload({"users": ["x"], "reactions": [{"weight": 5}]})
		== ReactionsParams()
	)


def test_reactions_validation_names_the_problem() -> None:
	"""Без пользователей, без реакций с весом, с перевёрнутой паузой — отказ."""
	from pxcontrol.engine.tasks import spec_of

	spec = spec_of(TaskKind.REACTIONS)
	with pytest.raises(TaskError, match="пользователя"):
		spec.validate(ReactionsParams(reactions=(ReactionChoice("👍", 1),)), dry_run=False)
	with pytest.raises(TaskError, match="ненулевым весом"):
		spec.validate(_reaction_params(1, reactions=(ReactionChoice("👍", 0),)), dry_run=False)
	with pytest.raises(TaskError, match="верхняя граница"):
		spec.validate(_reaction_params(1, pause_min_s=2.0, pause_max_s=1.0), dry_run=False)


async def test_reactions_pass_reacts_to_posts_without_mine(db: Database) -> None:
	"""Проход ставит реакции записям без реакции пользователя и пишет отчёт."""
	gateway = _ReactionsGateway(feed=_feed(5, 4, 3, mine={4: ("👍",)}))
	service = _service(db, gateway)
	community_id = await _community(db)
	account_id = await _account_id(db, community_id)
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(account_id))
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE, item.error
	report = item.report
	assert isinstance(report, ReactionsReport)
	assert (report.candidates, report.reacted, report.skipped) == (2, 2, 0)
	assert [(m, len(e)) for _a, m, e in gateway.sent] == [(5, 1), (3, 1)]
	assert all(emoji in ("👍", "🔥") for _a, _m, emojis in gateway.sent for emoji in emojis)
	assert sum(report.by_emoji.values()) == 2
	(run,) = await service.runs(task.id)
	assert "Реакций поставлено: 2" in run.summary


async def test_reactions_dry_run_only_counts(db: Database) -> None:
	"""Запуск «без изменений» считает подходящие записи, ничего не ставя."""
	gateway = _ReactionsGateway(feed=_feed(5, 4))
	service = _service(db, gateway)
	community_id = await _community(db)
	account_id = await _account_id(db, community_id)
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(account_id), dry_run=True)
	await service.settle()
	report = (await service.state())[0].report
	assert isinstance(report, ReactionsReport)
	assert (report.candidates, report.reacted) == (2, 0)
	assert gateway.sent == []


async def test_reactions_users_take_turns_across_runs(db: Database) -> None:
	"""Пользователи идут по кругу: курсор задачи переживает запуски."""
	gateway = _ReactionsGateway(feed=_feed(1) + _feed(2) + _feed(3))
	service = _service(db, gateway)
	community_id = await _community(db)
	first = await _account_id(db, community_id)
	second = await _add_user(db, community_id, "@second")
	task = await service.task(community_id, TaskKind.REACTIONS)
	for _ in range(3):
		await service.run_now(task.id, _reaction_params(first, second))
		await service.settle()
	assert [account for account, _m, _e in gateway.sent] == [first, second, first]
	fresh = await service.task(community_id, TaskKind.REACTIONS)
	assert fresh.cursor == {"next": 1}


async def test_reactions_skip_user_who_cannot_react_now(db: Database) -> None:
	"""Названный, но приостановленный пользователь пропускается — очередь идёт дальше."""
	gateway = _ReactionsGateway(feed=_feed(1))
	service = _service(db, gateway)
	community_id = await _community(db)
	first = await _account_id(db, community_id)
	second = await _add_user(db, community_id, "@second")
	async with db.session_factory() as session:  # первого приостановил человек (ADR-0029)
		account = await session.get(TgAccount, first)
		assert account is not None
		account.paused = True
		await session.commit()
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(first, second))
	await service.settle()
	assert [account for account, _m, _e in gateway.sent] == [second]


async def test_reactions_refuse_when_no_named_user_is_capable(db: Database) -> None:
	"""Все названные неспособны — отказ при постановке с объяснением."""
	service = _service(db, _ReactionsGateway())
	community_id = await _community(db)
	task = await service.task(community_id, TaskKind.REACTIONS)
	with pytest.raises(TaskError, match="Некому вести"):
		await service.run_now(task.id, _reaction_params(999))


async def test_reactions_forbidden_in_community_is_an_error(db: Database) -> None:
	"""Реакции запрещены в сообществе — запуск с честной ошибкой в журнале."""
	gateway = _ReactionsGateway(mode=ChatReactionsMode.NONE, feed=_feed(1))
	service = _service(db, gateway)
	community_id = await _community(db)
	account_id = await _account_id(db, community_id)
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(account_id))
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.ERROR
	assert item.error is not None and "запрещены" in item.error


async def test_reactions_drop_emojis_not_allowed_here(db: Database) -> None:
	"""Реакция не из перечня сообщества не ставится, остальные — ставятся."""
	gateway = _ReactionsGateway(mode=ChatReactionsMode.SOME, options=("🔥",), feed=_feed(1, 2))
	service = _service(db, gateway)
	community_id = await _community(db)
	account_id = await _account_id(db, community_id)
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(account_id))
	await service.settle()
	assert [e for _a, _m, e in gateway.sent] == [("🔥",), ("🔥",)]
	(run,) = await service.runs(task.id)
	assert any("не разрешены" in text for _at, text in run.events)


async def test_reactions_refusal_per_post_is_a_skip(db: Database) -> None:
	"""Отказ Telegram по одной записи — пропуск, проход продолжается."""
	gateway = _ReactionsGateway(feed=_feed(3, 2, 1))
	gateway.refused = {2}
	service = _service(db, gateway)
	community_id = await _community(db)
	account_id = await _account_id(db, community_id)
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(account_id))
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE
	report = item.report
	assert isinstance(report, ReactionsReport)
	assert (report.reacted, report.skipped) == (2, 1)


async def test_reactions_premium_puts_two_when_asked(db: Database) -> None:
	"""С Premium и флажком — две разные реакции; без флажка — одна."""
	gateway = _ReactionsGateway(feed=_feed(1) + _feed(2), premium=True)
	service = _service(db, gateway)
	community_id = await _community(db)
	account_id = await _account_id(db, community_id)
	task = await service.task(community_id, TaskKind.REACTIONS)
	await service.run_now(task.id, _reaction_params(account_id, premium_double=True))
	await service.settle()
	await service.run_now(task.id, _reaction_params(account_id, premium_double=False))
	await service.settle()
	assert [len(e) for _a, _m, e in gateway.sent] == [2, 1]
	assert len(set(gateway.sent[0][2])) == 2


async def test_reactors_and_reaction_options_for_the_form(db: Database) -> None:
	"""Форма получает пользователей с правом реагировать и перечень реакций."""
	gateway = _ReactionsGateway(mode=ChatReactionsMode.SOME, options=("🔥", "👍"))
	service = _service(db, gateway)
	community_id = await _community(db)
	reactors = await service.reactors(community_id)
	assert [r.label for r in reactors] == ["@ub"]
	options = await service.reaction_options(community_id)
	assert [o.emoji for o in options.options] == ["🔥", "👍"]


def test_reactions_summary_and_reactor_label() -> None:
	"""Итог прохода одной строкой и подпись пользователя в форме."""
	from pxcontrol.ui.pages.tasks import reactor_state

	report = ReactionsReport(
		executor="@ub", scanned=50, candidates=4, reacted=3, skipped=1, by_emoji={"👍": 3}
	)
	text = reactions_summary(report, dry_run=False)
	assert "поставлено: 3 из 4" in text and "не принял: 1" in text and "👍 3" in text
	assert "Подходящих записей: 4" in reactions_summary(report, dry_run=True)
	rights = ExecutorRights(ParticipantStatus.MEMBER, AdminRights(), ALL_MEMBER_RIGHTS)
	executor = ExecutorDto(
		owner=ExecutorRef(OwnerKind.USER, 1),
		label="@a",
		status=ParticipantStatus.MEMBER,
		rights=rights,
		is_default=False,
		paused=True,
		can_publish=False,
	)
	assert reactor_state(executor) == "приостановлен"


# --- приём заявок (этап D, ADR-0040) ----------------------------------------------


def _request(
	user_id: int, *, bio: str | None = None, deleted: bool = False, label: str | None = None
) -> JoinRequest:
	return JoinRequest(
		user_id=user_id,
		access_hash=user_id * 10,
		date=datetime.now(UTC),
		bio=bio,
		deleted=deleted,
		label=label or f"@u{user_id}",
	)


class _JoinRequestsGateway(_FakeGateway):
	"""Подставной шлюз заявок: страницы, решения, ограничения, каналы в профиле."""

	def __init__(self, pages: list[JoinRequestsPage] | None = None) -> None:
		super().__init__()
		self.pages = pages or []
		self.decisions: list[tuple[int, bool]] = []
		self.restricted: list[int] = []
		self.with_channel: set[int] = set()
		self.profile_reads: list[int] = []
		self.missing: set[int] = set()

	async def userbot_join_requests_page(
		self, account_id: int, chat_id: str, offset: tuple[datetime, int] | None, limit: int
	) -> JoinRequestsPage:
		self.requested.append(limit)
		if not self.pages:
			return JoinRequestsPage(requests=[], total=0, next_offset=None)
		return self.pages.pop(0)

	async def userbot_handle_join_request(
		self, account_id: int, chat_id: str, request: JoinRequest, *, approve: bool
	) -> None:
		if request.user_id in self.missing:
			raise UserbotJoinRequestError(
				"Telegram не дал обработать заявку: HIDE_REQUESTER_MISSING"
			)
		self.decisions.append((request.user_id, approve))

	async def userbot_restrict_fully(
		self, account_id: int, chat_id: str, request: JoinRequest
	) -> None:
		self.restricted.append(request.user_id)

	async def userbot_has_personal_channel(self, account_id: int, request: JoinRequest) -> bool:
		self.profile_reads.append(request.user_id)
		return request.user_id in self.with_channel


def _requests_page(*requests: JoinRequest, total: int | None = None) -> JoinRequestsPage:
	return JoinRequestsPage(
		requests=list(requests),
		total=total if total is not None else len(requests),
		next_offset=None,
	)


async def _run_join(
	service: TasksService, community_id: int, params: JoinRequestsParams, *, dry_run: bool = False
) -> JoinRequestsReport:
	"""Ставит запуск приёма заявок и возвращает отчёт."""
	task = await service.task(community_id, TaskKind.JOIN_REQUESTS)
	await service.run_now(task.id, params, dry_run=dry_run)
	await service.settle()
	item = (await service.state())[-1]
	assert item.status is JobStatus.DONE, item.error
	assert isinstance(item.report, JoinRequestsReport)
	return item.report


def test_profile_links_are_recognised() -> None:
	"""Ссылка в описании: адрес, t.me, домен, @упоминание; пустое — нет."""
	assert profile_has_links("пишите https://example.com")
	assert profile_has_links("канал t.me/joinchat/abc")
	assert profile_has_links("заходи на example.ru за скидкой")
	assert profile_has_links("менеджер @sales_bot")
	assert not profile_has_links("люблю котиков и кофе")
	assert not profile_has_links(None)
	assert not profile_has_links("")


def test_join_requests_params_and_report_survive_json() -> None:
	"""Параметры и отчёт приёма заявок переживают запись и чтение."""
	from pxcontrol.engine.tasks import spec_of

	spec = spec_of(TaskKind.JOIN_REQUESTS)
	params = JoinRequestsParams(decline_deleted=False, restrict_bio_links=True, limit=7)
	assert spec.params_from_payload(spec.params_to_payload(params)) == params
	report = JoinRequestsReport(found=5, reviewed=5, approved=3, restricted=1, declined=2)
	assert spec.report_from_payload(spec.report_to_payload(report)) == report
	with pytest.raises(TaskError, match="Предел заявок"):
		spec.validate(JoinRequestsParams(limit=0), dry_run=False)


async def test_join_requests_pass_declines_deleted_and_approves_rest(db: Database) -> None:
	"""Удалённые отклоняются, остальные принимаются; отчёт и журнал честны."""
	gateway = _JoinRequestsGateway(
		[_requests_page(_request(1), _request(2, deleted=True), _request(3))]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	report = await _run_join(service, community_id, JoinRequestsParams())
	assert (report.found, report.reviewed, report.approved, report.declined) == (3, 3, 2, 1)
	assert gateway.decisions == [(1, True), (2, False), (3, True)]
	assert gateway.restricted == []
	task = await service.task(community_id, TaskKind.JOIN_REQUESTS)
	(run,) = await service.runs(task.id)
	assert "Принято: 2" in run.summary and "отклонено удалённых: 1" in run.summary


async def test_join_requests_leave_deleted_alone_when_asked(db: Database) -> None:
	"""Без флажка удалённые не трогаются — ни принять, ни отклонить."""
	gateway = _JoinRequestsGateway([_requests_page(_request(2, deleted=True))])
	service = _service(db, gateway)
	community_id = await _community(db)
	report = await _run_join(service, community_id, JoinRequestsParams(decline_deleted=False))
	assert (report.approved, report.declined) == (0, 0)
	assert gateway.decisions == []


async def test_join_requests_restrict_links_in_group_only(db: Database) -> None:
	"""В группе заявитель со ссылкой в био принят с ограничением; в канале — как есть."""
	page = [_requests_page(_request(1, bio="see t.me/spam"), _request(2, bio="просто человек"))]
	gateway = _JoinRequestsGateway(list(page))
	service = _service(db, gateway)
	group_id = await _community(db)  # _community заводит группу
	report = await _run_join(service, group_id, JoinRequestsParams(restrict_bio_links=True))
	assert (report.approved, report.restricted) == (2, 1)
	assert gateway.decisions == [(1, True), (2, True)]
	assert gateway.restricted == [1]

	gateway.pages = [_requests_page(_request(1, bio="see t.me/spam"))]
	gateway.decisions.clear()
	gateway.restricted.clear()
	async with db.session_factory() as session:  # то же сообщество, но канал
		community = await session.get(Community, group_id)
		assert community is not None
		community.kind = "channel"
		await session.commit()
	report = await _run_join(service, group_id, JoinRequestsParams(restrict_bio_links=True))
	assert (report.approved, report.restricted) == (1, 0)
	assert gateway.restricted == []


async def test_join_requests_personal_channel_costs_one_read_per_applicant(db: Database) -> None:
	"""Канал в профиле проверяется запросом на заявителя — только по флажку."""
	gateway = _JoinRequestsGateway([_requests_page(_request(1), _request(2))])
	gateway.with_channel = {2}
	service = _service(db, gateway)
	community_id = await _community(db)
	report = await _run_join(
		service, community_id, JoinRequestsParams(restrict_personal_channel=True)
	)
	assert gateway.profile_reads == [1, 2]
	assert gateway.restricted == [2]
	assert report.restricted == 1


async def test_join_requests_dry_run_counts_without_touching(db: Database) -> None:
	"""Просмотр считает решения, но ничего не одобряет и не отклоняет."""
	gateway = _JoinRequestsGateway(
		[_requests_page(_request(1, bio="t.me/x"), _request(2, deleted=True), total=9)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	report = await _run_join(
		service, community_id, JoinRequestsParams(restrict_bio_links=True), dry_run=True
	)
	assert (report.found, report.approved, report.restricted, report.declined) == (9, 1, 1, 1)
	assert gateway.decisions == [] and gateway.restricted == []


async def test_join_requests_respect_limit_and_skip_missing(db: Database) -> None:
	"""Потолок за проход соблюдается; исчезнувшая заявка — пропуск, а не сбой."""
	gateway = _JoinRequestsGateway([_requests_page(_request(1), _request(2), _request(3), total=3)])
	gateway.missing = {1}
	service = _service(db, gateway)
	community_id = await _community(db)
	report = await _run_join(service, community_id, JoinRequestsParams(limit=2))
	assert (report.reviewed, report.approved, report.skipped, report.limited) == (2, 1, 1, True)
	assert gateway.decisions == [(2, True)]
	assert gateway.requested == [2]  # читаем не больше потолка


async def test_join_requests_need_invite_right_and_ban_right_for_restriction(db: Database) -> None:
	"""Без права приглашать — отказ; с ограничением нужно ещё право исключать."""
	from sqlalchemy import select

	from pxcontrol.engine.db.models import CommunityExecutor as ExecutorRow

	service = _service(db, _JoinRequestsGateway())
	community_id = await _community(db, can_ban=False, can_invite=False)
	task = await service.task(community_id, TaskKind.JOIN_REQUESTS)
	with pytest.raises(TaskError, match="принимать заявки"):
		await service.run_now(task.id, JoinRequestsParams())
	async with db.session_factory() as session:  # выдаём право приглашать, но не исключать
		row = (
			await session.execute(
				select(ExecutorRow).where(ExecutorRow.community_id == community_id)
			)
		).scalar_one()
		row.rights = ExecutorRights(
			ParticipantStatus.ADMIN, AdminRights(invite_users=True), ALL_MEMBER_RIGHTS
		).to_payload()
		await session.commit()
	await service.run_now(task.id, JoinRequestsParams())  # без ограничения — можно
	await service.settle()
	with pytest.raises(TaskError, match="Некому вести"):
		await service.run_now(task.id, JoinRequestsParams(restrict_bio_links=True))


def test_join_requests_summary_texts() -> None:
	"""Итог приёма заявок одной строкой — для запуска и для просмотра."""
	report = JoinRequestsReport(
		found=4, reviewed=4, approved=3, restricted=1, declined=1, skipped=0
	)
	text = join_requests_summary(report, dry_run=False)
	assert "Принято: 3" in text and "ограничением: 1" in text and "отклонено удалённых: 1" in text
	assert "к приёму 3" in join_requests_summary(report, dry_run=True)
	assert join_requests_summary(JoinRequestsReport(), dry_run=True) == "Заявок на вступление нет."


# --- правила вкладки «Задачи» (спека `screens/tasks.md`) -------------------------------


def _run(
	*,
	outcome: RunOutcome = RunOutcome.DONE,
	summary: str = "",
	error: str | None = None,
	executor: ExecutorRef | None = None,
	dry_run: bool = False,
	at: datetime | None = None,
) -> TaskRunDto:
	"""Запуск задачи для проверки правил показа."""
	moment = at or datetime(2026, 9, 24, 4, 0, tzinfo=UTC)
	return TaskRunDto(
		id=1,
		task_id=1,
		kind=TaskKind.SERVICE_MESSAGES,
		trigger=TaskTrigger.SCHEDULE,
		dry_run=dry_run,
		executor=executor,
		executor_label=None,
		started_at=moment,
		finished_at=moment,
		outcome=outcome,
		summary=summary,
		error=error,
		events=(),
	)


def test_available_kinds_by_community_kind() -> None:
	"""Все четыре задачи — и у канала, и у группы: движок ведёт каждую в обоих."""
	from pxcontrol.ui.pages.tasks import available_kinds

	every = (
		TaskKind.SERVICE_MESSAGES,
		TaskKind.DELETED_ACCOUNTS,
		TaskKind.REACTIONS,
		TaskKind.JOIN_REQUESTS,
	)
	assert available_kinds(CommunityKind.GROUP) == every
	# канал чистит удалённые аккаунты среди подписчиков, заявки принимает как есть
	assert available_kinds(CommunityKind.CHANNEL) == every


def test_last_run_caption_and_when_text() -> None:
	"""Итог на карточке: когда и чем кончилось; ошибка называет причину."""
	from pxcontrol.ui.pages.tasks import last_run_caption, when_text

	# моменты берутся в местном поясе: подписи говорят о местном времени,
	# и от часового пояса машины проверка зависеть не должна
	def local(day: int, hour: int, minute: int) -> datetime:
		return datetime(2026, 9, day, hour, minute).astimezone()

	now = local(24, 12, 0)
	assert last_run_caption(None) == "ещё не запускалась"
	done = last_run_caption(_run(summary="удалено 132", at=local(24, 4, 0)), now)
	assert done == "последний: сегодня 04:00 · удалено 132"
	yesterday = _run(
		outcome=RunOutcome.ERROR,
		error="нет права одобрять заявки",
		at=local(23, 18, 10),
	)
	assert last_run_caption(yesterday, now) == "вчера 18:10 · нет права одобрять заявки"
	assert when_text(local(12, 7, 30), now) == "12.09 07:30"


def test_error_fix_target_points_to_members_only_for_rights() -> None:
	"""Кнопка перехода предлагается там, где ошибку чинят правами и пулом."""
	from pxcontrol.ui.pages.tasks import TaskFixTarget, error_fix_target

	assert error_fix_target(None) is None
	assert error_fix_target(_run(summary="готово")) is None
	rights = _run(outcome=RunOutcome.ERROR, error="У «@ub» нет права одобрять заявки.")
	assert error_fix_target(rights) is TaskFixTarget.MEMBERS
	nobody = _run(outcome=RunOutcome.ERROR, error="В пуле «Чат» некому читать историю.")
	assert error_fix_target(nobody) is TaskFixTarget.MEMBERS
	network = _run(outcome=RunOutcome.ERROR, error="Telegram не ответил: таймаут.")
	assert error_fix_target(network) is None


def test_form_dirty_compares_params_and_schedule() -> None:
	"""Кнопки сохранения оживают, только когда форма отличается от сохранённого."""
	from dataclasses import replace

	from pxcontrol.ui.pages.tasks import TaskForm, form_dirty

	saved = TaskForm(ServiceMessagesParams(), Schedule(), False)
	assert not form_dirty(saved, TaskForm(ServiceMessagesParams(), Schedule(), False))
	# пока задача не прочитана, сравнивать не с чем
	assert not form_dirty(None, TaskForm(ServiceMessagesParams(depth=10), Schedule(), True))
	other_params = TaskForm(replace(ServiceMessagesParams(), depth=999), Schedule(), False)
	assert form_dirty(saved, other_params)
	other_schedule = TaskForm(ServiceMessagesParams(), Schedule(ScheduleKind.INTERVAL), False)
	assert form_dirty(saved, other_schedule)
	assert form_dirty(saved, TaskForm(ServiceMessagesParams(), Schedule(), True))


def test_reactor_matches_filters_search_and_mode() -> None:
	"""Отбор таблицы «Кто ставит»: поиск по имени и три режима."""
	from pxcontrol.ui.pages.tasks import ReactorFilter, ReactorRow, reactor_matches

	rights = ExecutorRights(ParticipantStatus.MEMBER, AdminRights(), ALL_MEMBER_RIGHTS)

	def row(label: str, *, checked: bool = False, paused: bool = False) -> ReactorRow:
		executor = ExecutorDto(
			owner=ExecutorRef(OwnerKind.USER, 1),
			label=label,
			status=ParticipantStatus.MEMBER,
			rights=rights,
			is_default=False,
			paused=paused,
			can_publish=True,
		)
		return ReactorRow(executor, checked, None)

	lara = row("Лара")
	assert reactor_matches(lara, "", ReactorFilter.ALL)
	assert reactor_matches(lara, "лар", ReactorFilter.ALL)
	assert not reactor_matches(lara, "петя", ReactorFilter.ALL)
	assert not reactor_matches(lara, "", ReactorFilter.CHECKED)
	assert reactor_matches(row("Лара", checked=True), "", ReactorFilter.CHECKED)
	# приостановленный проход не поведёт — в отбор «могут ставить» не попадает
	assert not reactor_matches(row("Лара", paused=True), "", ReactorFilter.CAPABLE)
	assert reactor_matches(lara, "", ReactorFilter.CAPABLE)


def test_last_reaction_times_take_real_passes_only() -> None:
	"""Колонка «Последняя реакция» считается по настоящим проходам, не по подбору."""
	from pxcontrol.ui.pages.tasks import last_reaction_times

	owner = ExecutorRef(OwnerKind.USER, 3)
	early = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
	late = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
	runs = [
		_run(executor=owner, at=late, dry_run=True),
		_run(executor=owner, at=early),
		_run(executor=None, at=late),
	]
	assert last_reaction_times(runs) == {owner: early}


def test_confirmations_name_what_will_happen() -> None:
	"""Подтверждения перечисляют, что именно сделает запуск."""
	from pxcontrol.ui.pages.tasks import run_confirmation, schedule_confirmation

	group, channel = CommunityKind.GROUP, CommunityKind.CHANNEL
	schedule = Schedule(ScheduleKind.DAILY, times=("04:00",))
	text = schedule_confirmation(
		TaskKind.SERVICE_MESSAGES, ServiceMessagesParams(), schedule, "Чат", community_kind=group
	)
	assert "ежедневно в 04:00" in text and "Удаление необратимо" in text
	params = DeletedAccountsParams(kick_limit=7)
	kick = schedule_confirmation(
		TaskKind.DELETED_ACCOUNTS, params, schedule, "Чат", community_kind=group
	)
	assert "не больше 7" in kick and "число участников" in kick
	# у канала аудитория — подписчики
	in_channel = run_confirmation(TaskKind.DELETED_ACCOUNTS, params, "Кино", community_kind=channel)
	assert "число подписчиков" in in_channel
	assert "не больше 5" in run_confirmation(
		TaskKind.JOIN_REQUESTS, JoinRequestsParams(limit=5), "Чат", community_kind=group
	)
	# проход реакций обратим руками — вопроса нет
	assert (
		run_confirmation(TaskKind.REACTIONS, ReactionsParams(), "Чат", community_kind=group) == ""
	)


def test_progress_caption_prefers_engine_note() -> None:
	"""Строка хода работы: пометка движка, иначе проценты."""
	from pxcontrol.engine.jobs import JobStatus
	from pxcontrol.engine.services.tasks import TaskJobDto
	from pxcontrol.ui.pages.tasks import progress_caption

	def job(note: str | None, progress: float) -> TaskJobDto:
		return TaskJobDto(
			id=1,
			task_id=1,
			kind=TaskKind.SERVICE_MESSAGES,
			title="Просмотр · Чат",
			community_id=1,
			status=JobStatus.RUNNING,
			progress=progress,
			error=None,
			note=note,
			dry_run=True,
		)

	assert progress_caption(job("1 200 из 2 104", 0.5)) == "1 200 из 2 104"
	assert progress_caption(job(None, 0.42)) == "42 %"
	assert progress_caption(job(None, 0.0)) == "идёт обращение к Telegram"


def test_last_scan_takes_latest_dry_run_report() -> None:
	"""Числа «Служебных записей» — из последнего просмотра, а не из чистки."""
	from dataclasses import replace

	from pxcontrol.ui.pages.tasks import last_scan, scan_caption

	old_scan = ServiceReport(found={ServiceMessageKind.MEMBERS: 7}, scanned=500)
	cleaning = ServiceReport(found={ServiceMessageKind.MEMBERS: 3}, scanned=500, deleted=3)
	early = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)
	late = datetime(2026, 9, 22, 4, 0, tzinfo=UTC)
	# журнал отдаёт запуски новыми сначала: чистка новее просмотра
	runs = [
		replace(_run(at=late), report=cleaning),
		replace(_run(at=early, dry_run=True), report=old_scan),
	]
	assert last_scan(runs) == (old_scan, early)
	assert last_scan([replace(_run(at=late), report=cleaning)]) is None
	assert last_scan([]) is None
	now = datetime(2026, 9, 20, 12, 0).astimezone()
	moment = datetime(2026, 9, 20, 4, 0).astimezone()
	big = ServiceReport(found={}, scanned=5000)
	assert scan_caption(big, moment, now) == (
		"Числа — по последнему просмотру: сегодня 04:00, 5 000 сообщений."
	)
