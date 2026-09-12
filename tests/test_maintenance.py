"""Обслуживание сообществ (ADR-0026): просмотр и чистка служебных записей."""

from __future__ import annotations

import asyncio
import re
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
	MembersReport,
	ServiceReport,
	selectable_kinds,
)
from pxcontrol.engine.telegram.mtproto import UserbotFloodError, service_message_kind
from pxcontrol.engine.telegram.types import (
	CommunityInfo,
	CommunityKind,
	ParticipantsPage,
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
		can_ban: bool = True,
		member_pages: list[ParticipantsPage] | None = None,
	) -> None:
		self.pages = pages or []
		self.member_pages = member_pages or []
		self.can_delete = can_delete
		self.can_ban = can_ban
		self.deleted: list[list[int]] = []
		self.kicked: list[int] = []
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
			can_ban=self.can_ban,
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

	async def participants_page(
		self, account_id: int, chat_id: str, offset: int, limit: int
	) -> ParticipantsPage:
		if not self.member_pages:
			return ParticipantsPage(deleted_ids=[], scanned=0, next_offset=None, total=0)
		return self.member_pages.pop(0)

	async def kick_participant(self, account_id: int, chat_id: str, user_id: int) -> int | None:
		self.kicked.append(user_id)
		# в супергруппе исключение порождает служебную запись
		return 9000 + user_id


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
	assert item.service is not None
	assert item.service.found == {ServiceMessageKind.MEMBERS: 2, ServiceMessageKind.PINS: 1}
	assert item.service.scanned == PAGE_SIZE + 10
	assert item.service.exhausted is True  # история кончилась на второй странице
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
	assert item.service is not None
	assert item.service.scanned == PAGE_SIZE * 3
	assert len(gateway.requested) == 3  # ровно три запроса, не десять
	assert item.service.exhausted is False  # история не кончилась — просто хватит


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
	assert item.service is not None
	assert item.service.deleted == 2
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
	assert item.service is not None
	assert item.service.deleted == 3
	assert item.service.limited is True
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
	assert item.service is not None
	assert (item.service.deleted, item.service.skipped) == (1, 1)


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
	assert item.service is None  # неполный отчёт не сохраняется


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
	assert items[first].service is not None
	assert items[second].service is not None
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
		async def service_messages_page(
			self, account_id: int, chat_id: str, offset_id: int, limit: int
		) -> ServiceMessagesPage:
			self.requested.append(offset_id)
			if len(self.requested) > 1:
				await release.wait()
			return _page(kinds=[ServiceMessageKind.MEMBERS], scanned=1, next_offset_id=90)

	gateway = _SlowGateway()
	service = _service(db, gateway)
	community_id = await _community(db)
	job_id = await service.clean_service_messages(community_id, [ServiceMessageKind.MEMBERS])
	while len(gateway.requested) < 2:
		await asyncio.sleep(0)
	with caplog.at_level("INFO", logger="pxcontrol.engine.services.maintenance"):
		await service.cancel(job_id)
		release.set()
		await service.settle()

	item = (await service.state())[0]
	assert item.status is JobStatus.CANCELLED
	assert item.service is None  # неполный отчёт не сохраняется
	# зато в журнале осталось, сколько записей успели удалить
	summaries = [msg for r in caplog.records if "удалено" in (msg := r.getMessage())]
	assert len(summaries) == 1
	assert re.search(r"удалено ([1-9]\d*)", summaries[0])


# --- тексты интерфейса (чистые функции окна) ------------------------------------


def test_service_summary_tells_what_was_seen() -> None:
	"""Итог просмотра называет число, глубину и границу по дате."""
	from pxcontrol.ui.pages.maintenance import service_summary

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
	from pxcontrol.ui.pages.maintenance import service_summary

	report = ServiceReport(
		found={ServiceMessageKind.PINS: 3}, scanned=42, oldest_date=None, exhausted=True
	)
	assert "целиком" in service_summary(report)


def test_service_summary_for_empty_result() -> None:
	"""Пустой результат не притворяется находкой."""
	from pxcontrol.ui.pages.maintenance import service_summary

	assert "не найдено" in service_summary(ServiceReport(scanned=500))


def test_service_summary_reports_skipped_and_limit() -> None:
	"""Итог чистки честен про пропуски и про упёршийся потолок."""
	from pxcontrol.ui.pages.maintenance import service_summary

	text = service_summary(ServiceReport(deleted=40, skipped=2, scanned=900, limited=True))
	assert "40" in text
	assert "не дал удалить: 2" in text
	assert "повторите" in text  # человеку сказано, что осталось ещё


def test_every_kind_has_human_title() -> None:
	"""У каждого вида есть человеческое название — без «ServiceMessageKind.OTHER»."""
	from pxcontrol.ui.pages.maintenance import kind_title

	for kind in ServiceMessageKind:
		title = kind_title(kind)
		assert title and not title.startswith("ServiceMessageKind")


# --- удалённые аккаунты ---------------------------------------------------------


def _members(
	*, deleted: list[int], scanned: int, next_offset: int | None, total: int | None
) -> ParticipantsPage:
	"""Страница участников с удалёнными учётками."""
	return ParticipantsPage(
		deleted_ids=deleted, scanned=scanned, next_offset=next_offset, total=total
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
	await service.scan_deleted_accounts(community_id)
	await service.settle()
	item = (await service.state())[0]
	assert item.status is JobStatus.DONE
	assert item.members is not None
	assert (item.members.found, item.members.scanned, item.members.total) == (3, 350, 350)
	assert item.members.exhausted is True
	assert item.members.capped is False  # список отдан целиком
	assert gateway.kicked == []


async def test_scan_members_notices_telegram_cap(db: Database) -> None:
	"""Если Telegram отдал меньше, чем участников, — это видно в отчёте.

	Тот самый предел выдачи, который при проектировании был лишь
	предположением (ADR-0026): теперь он не угадывается, а фиксируется.
	"""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[], scanned=200, next_offset=None, total=10_000)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.scan_deleted_accounts(community_id)
	await service.settle()
	item = (await service.state())[0]
	assert item.members is not None
	assert item.members.capped is True


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
	await service.clean_deleted_accounts(community_id, limit=2)
	await service.settle()
	item = (await service.state())[0]
	assert item.members is not None
	assert item.members.removed == 2
	assert item.members.limited is True
	assert gateway.kicked == [11, 12]


async def test_clean_members_sweeps_its_own_service_notes(db: Database) -> None:
	"""Чистка убирает записи «удалил участника», которые сама породила.

	Иначе уборка мёртвых душ производила бы ровно тот мусор, который
	убирает первый вид обслуживания (ADR-0026).
	"""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11, 12], scanned=10, next_offset=None, total=10)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.clean_deleted_accounts(community_id, limit=10)
	await service.settle()
	item = (await service.state())[0]
	assert item.members is not None
	assert item.members.removed == 2
	assert gateway.deleted == [[9011, 9012]]  # одной пачкой, а не по одной
	assert item.members.service_left == 0


async def test_clean_members_reports_notes_left_without_delete_right(db: Database) -> None:
	"""Без права удалять записи об исключении остаются — и это сказано."""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11], scanned=10, next_offset=None, total=10)],
		can_delete=False,
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.clean_deleted_accounts(community_id, limit=10)
	await service.settle()
	item = (await service.state())[0]
	assert item.members is not None
	assert (item.members.removed, item.members.service_left) == (1, 1)
	assert gateway.deleted == []  # без права даже не пробуем


async def test_clean_members_requires_ban_right(db: Database) -> None:
	"""Без права исключать чистка не начинается."""
	gateway = _FakeGateway(can_ban=False)
	service = _service(db, gateway)
	community_id = await _community(db)
	with pytest.raises(MaintenanceError, match="права исключать"):
		await service.clean_deleted_accounts(community_id)
	assert await service.state() == []


async def test_kick_limit_allows_single_account(db: Database) -> None:
	"""Потолок в одну штуку допустим: убрать один мёртвый аккаунт можно."""
	gateway = _FakeGateway(
		member_pages=[_members(deleted=[11, 12], scanned=10, next_offset=None, total=10)]
	)
	service = _service(db, gateway)
	community_id = await _community(db)
	await service.clean_deleted_accounts(community_id, limit=1)
	await service.settle()
	item = (await service.state())[0]
	assert item.members is not None
	assert item.members.removed == 1


async def test_kick_limit_zero_is_rejected(db: Database) -> None:
	"""Ноль исключений — не проход, а недоразумение: отказ с объяснением."""
	service = _service(db, _FakeGateway())
	community_id = await _community(db)
	with pytest.raises(MaintenanceError, match="Предел исключений"):
		await service.clean_deleted_accounts(community_id, limit=0)


def test_members_summary_mentions_cap_and_leftovers() -> None:
	"""Итог по участникам честен про предел выдачи и оставшиеся записи."""
	from pxcontrol.ui.pages.maintenance import members_summary

	capped = members_summary(MembersReport(found=0, scanned=200, total=10_000, capped=True))
	assert "список не отдаёт" in capped
	left = members_summary(MembersReport(found=3, removed=3, service_left=3))
	assert "осталось в ленте: 3" in left


async def test_deleting_community_stops_its_maintenance(db: Database) -> None:
	"""Удаление сообщества снимает его задания обслуживания.

	Задание держит снимок сообщества и работает по его ``tg_chat_id``,
	от строки в БД не завися: без снятия уборка продолжала бы удалять
	записи в Telegram для сущности, которой в приложении уже нет.
	"""
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
	running = await service.scan_service_messages(community_id)
	waiting = await service.scan_service_messages(community_id)
	while not gateway.requested:
		await asyncio.sleep(0)

	await service.drop_community(community_id)
	release.set()
	await service.settle()

	items = {item.id: item for item in await service.state()}
	assert items[waiting].status is JobStatus.CANCELLED  # ждавшее снято сразу
	assert items[running].status is JobStatus.CANCELLED  # идущее остановлено
	assert len(gateway.requested) == 1  # вторая страница не запрашивалась
