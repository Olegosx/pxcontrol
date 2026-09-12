"""Обслуживание сообществ (ADR-0026): просмотр и чистка служебных записей."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, TgAccount
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunitiesService
from pxcontrol.engine.services.maintenance import (
	DEFAULT_DEPTH,
	PAGE_SIZE,
	MaintenanceError,
	MaintenanceService,
	selectable_kinds,
)
from pxcontrol.engine.telegram.mtproto import UserbotFloodError, service_message_kind
from pxcontrol.engine.telegram.types import (
	CommunityInfo,
	CommunityKind,
	ServiceMessageInfo,
	ServiceMessageKind,
	ServiceMessagesPage,
	UserbotRole,
)


class _FakeGateway:
	"""Подставной шлюз: страницы истории и учёт удалений."""

	def __init__(
		self,
		pages: list[ServiceMessagesPage] | None = None,
		*,
		can_delete: bool = True,
	) -> None:
		self.pages = pages or []
		self.can_delete = can_delete
		self.deleted: list[list[int]] = []
		self.requested: list[int] = []  # offset_id каждого запроса
		#: id, которые Telegram отказывается удалять (защищённые им)
		self.undeletable: set[int] = set()

	async def check_community_userbot(self, account_id: int, chat_ref: str) -> CommunityInfo:
		return CommunityInfo(
			chat_id=chat_ref,
			title="Группа",
			username=None,
			kind=CommunityKind.GROUP,
			role=UserbotRole.ADMIN,
			can_delete=self.can_delete,
		)

	async def service_messages_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage:
		self.requested.append(offset_id)
		if not self.pages:
			return ServiceMessagesPage(
				messages=[], scanned=0, next_offset_id=None, oldest_date=None
			)
		return self.pages.pop(0)

	async def delete_messages(self, account_id: int, chat_id: str, message_ids: list[int]) -> int:
		self.deleted.append(list(message_ids))
		return sum(1 for message_id in message_ids if message_id not in self.undeletable)


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


async def _community(db: Database, *, with_account: bool = True) -> int:
	"""Сообщество с userbot-публикатором (или без него)."""
	async with db.session_factory() as session:
		account_id = None
		if with_account:
			account = TgAccount(label="@ub", phone="+7900", session="s")
			session.add(account)
			await session.flush()
			account_id = account.id
		community = Community(
			title="Группа",
			tg_chat_id="-1001",
			kind="group",
			default_tg_account_id=account_id,
		)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


def _service(db: Database, gateway: _FakeGateway) -> MaintenanceService:
	"""Сервис обслуживания поверх подставного шлюза."""
	return MaintenanceService(gateway, CommunitiesService(db, gateway))  # type: ignore[arg-type]


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
	job_id = await service.scan_service_messages(community_id)
	await service.settle()
	item = next(i for i in await service.state() if i.id == job_id)
	assert item.status is JobStatus.DONE
	assert item.scan is not None
	assert item.scan.found == {ServiceMessageKind.MEMBERS: 2, ServiceMessageKind.PINS: 1}
	assert item.scan.scanned == PAGE_SIZE + 10
	assert item.scan.exhausted is True  # история кончилась на второй странице
	assert gateway.deleted == []  # просмотр ничего не трогает


async def test_scan_stops_at_depth(db: Database) -> None:
	"""Проход ограничен глубиной: бесконтрольно историю не читаем."""
	pages = [_page(kinds=[], next_offset_id=index + 1) for index in range(10)]
	gateway = _FakeGateway(pages)
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.scan_service_messages(community_id, depth=PAGE_SIZE * 3)
	await service.settle()
	item = (await service.state())[0]
	assert item.scan is not None
	assert item.scan.scanned == PAGE_SIZE * 3
	assert len(gateway.requested) == 3  # ровно три запроса, не десять
	assert item.scan.exhausted is False  # история не кончилась — просто хватит


async def test_depth_out_of_range_is_rejected(db: Database) -> None:
	"""Негодная глубина отклоняется до постановки, с понятным текстом."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	with pytest.raises(MaintenanceError, match="Глубина просмотра"):
		await service.scan_service_messages(community_id, depth=7)


async def test_community_without_publisher_is_rejected(db: Database) -> None:
	"""Без userbot-публикатора обслуживание недоступно — и объясняет почему."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db, with_account=False)
	with pytest.raises(MaintenanceError, match="userbot-публикатор"):
		await service.scan_service_messages(community_id)


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
	await service.clean_service_messages(community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE
	assert item.clean is not None
	assert item.clean.deleted == 2
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
	await service.clean_service_messages(community_id, [ServiceMessageKind.MEMBERS], delete_limit=3)
	await service.settle()
	item = (await service.state())[0]
	assert item.clean is not None
	assert item.clean.deleted == 3
	assert item.clean.limited is True
	assert gateway.deleted == [[100, 101, 102]]


async def test_clean_counts_refused_as_skipped(db: Database) -> None:
	"""Запись, которую Telegram не даёт удалить, — пропуск, а не сбой."""
	gateway = _FakeGateway([_page(kinds=[ServiceMessageKind.MEMBERS] * 2, scanned=2)])
	gateway.undeletable = {101}
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.clean_service_messages(community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE  # проход не провалился
	assert item.clean is not None
	assert (item.clean.deleted, item.clean.skipped) == (1, 1)


async def test_clean_requires_delete_right(db: Database) -> None:
	"""Без права удалять чистка не начинается — проверка живая, до прохода."""
	gateway = _FakeGateway(can_delete=False)
	service = _service(db, gateway)
	community_id = await _community(db)
	with pytest.raises(MaintenanceError, match="права удалять"):
		await service.clean_service_messages(community_id, [ServiceMessageKind.MEMBERS])
	assert await service.state() == []  # задание даже не поставлено


async def test_clean_without_kinds_is_rejected(db: Database) -> None:
	"""Пустой набор видов (или только защищённые) — отказ до постановки."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	with pytest.raises(MaintenanceError, match="не выбрано|Не выбрано"):
		await service.clean_service_messages(community_id, [ServiceMessageKind.PROTECTED])


async def test_flood_stops_the_pass(db: Database) -> None:
	"""Флуд-лимит прекращает проход: настойчивость удлиняет срок (ADR-0017)."""

	class _FloodingGateway(_FakeGateway):
		async def service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)

	gateway = _FloodingGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.scan_service_messages(community_id)
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.ERROR
	assert item.error is not None and "подождать" in item.error
	assert item.scan is None  # неполный отчёт не сохраняется


async def test_cancel_stops_between_pages(db: Database) -> None:
	"""Отмена прекращает проход между страницами, не рвя обращение."""
	release = asyncio.Event()

	class _SlowGateway(_FakeGateway):
		async def service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			await release.wait()
			return _page(kinds=[ServiceMessageKind.MEMBERS], scanned=1, next_offset_id=90)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	job_id = await service.scan_service_messages(community_id)
	while not gateway.requested:
		await asyncio.sleep(0)
	await service.cancel(job_id)
	release.set()
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.CANCELLED
	assert len(gateway.requested) == 1  # вторая страница не запрашивалась


async def test_scan_and_clean_queue_up(db: Database) -> None:
	"""Задания идут по очереди — одно обращение к аккаунту за раз."""
	gateway = _FakeGateway(
		[
			_page(kinds=[ServiceMessageKind.MEMBERS], scanned=1),
			_page(kinds=[ServiceMessageKind.MEMBERS], scanned=1),
		]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	first = await service.scan_service_messages(community_id, depth=DEFAULT_DEPTH)
	second = await service.clean_service_messages(community_id, [ServiceMessageKind.MEMBERS])
	await service.settle()
	items = {item.id: item for item in await service.state()}
	assert items[first].scan is not None
	assert items[second].clean is not None
	assert all(item.status is JobStatus.DONE for item in items.values())
