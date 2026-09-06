"""Тесты сервиса каналов и чистых функций проверки (без сети)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, TgAccount
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.communities import CommunitiesService, CommunityError
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.bot_api import (
	CommunityCheckError,
	community_kind_from_chat_type,
	ensure_bot_can_post,
	ensure_bot_can_send_in_group,
)
from pxcontrol.engine.telegram.mtproto import (
	UserbotAccessError,
	UserbotNotConnectedError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.refs import ChatRefError, normalize_chat_ref
from pxcontrol.engine.telegram.types import CommunityInfo, CommunityKind, UserbotRole


class _FakeGateway:
	"""Подмена шлюза: токены и каналы проверяются без сети.

	Userbot-проверки адресные (ADR-0019): «админами» считаются аккаунты
	из ``userbot_admins``, остальным Telegram «подтверждает отказ».
	"""

	login = None  # вход userbot в этих тестах не используется

	def __init__(self) -> None:
		self.userbot_admins: set[int] = set()
		self.bot_is_admin = True  # ответ проверки прав бота
		self.kind = CommunityKind.CHANNEL  # вид, который «увидит» проверка
		self.forum = False  # признак форума в ответе проверки
		self.role = UserbotRole.ADMIN  # роль аккаунта в userbot-зонде
		self.title = "Тестовый канал"  # название в ответе проверки
		self.username: str | None = "testchan"  # @имя (None — приватное)

	async def check_bot_token(self, token: str) -> str:
		return "test_bot"

	async def check_community(self, token: str, chat_ref: str) -> CommunityInfo:
		if chat_ref == "@notfound":
			raise CommunityCheckError("Канал не найден — проверьте @имя или ID.")
		if chat_ref == "@noperm" or not self.bot_is_admin:
			raise CommunityCheckError("У бота нет права публиковать сообщения в канале.")
		return CommunityInfo("-1001234", self.title, self.username, self.kind, self.forum)

	async def check_community_userbot(self, account_id: int, chat_ref: str) -> CommunityInfo:
		if account_id not in self.userbot_admins:
			raise UserbotAccessError(
				"Userbot не администратор канала — добавьте аккаунт "
				"администратором с правом публиковать."
			)
		return CommunityInfo(
			"-1001234", self.title, self.username, self.kind, self.forum, role=self.role
		)


async def _make_bot(db: Database) -> int:
	"""Создаёт бота и возвращает его id."""
	accounts = AccountsService(db, _FakeGateway())  # type: ignore[arg-type]
	bot = await accounts.add_bot("Публикатор", "123456:AAAbbb")
	return bot.id


async def _make_account(db: Database, label: str = "@ub") -> int:
	"""Создаёт вошедший userbot-аккаунт, возвращает его id."""
	async with db.session_factory() as session:
		account = TgAccount(label=label, phone="+7900", session="s")
		session.add(account)
		await session.commit()
		await session.refresh(account)
		return account.id


async def test_community_lifecycle(db: Database) -> None:
	"""Канал подключается с проверкой, виден в списке, удаляется."""
	bot_id = await _make_bot(db)
	service = CommunitiesService(db, _FakeGateway())
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.title == "Тестовый канал"
	assert dto.tg_chat_id == "-1001234"
	assert dto.bot_label == "Публикатор"
	listed = await service.list_communities()
	assert [c.title for c in listed] == ["Тестовый канал"]
	assert listed[0].bot_label == "Публикатор"
	await service.delete_community(dto.id)
	assert await service.list_communities() == []


async def test_failed_check_not_saved(db: Database) -> None:
	"""Не прошедший проверку канал не сохраняется."""
	bot_id = await _make_bot(db)
	service = CommunitiesService(db, _FakeGateway())
	with pytest.raises(CommunityCheckError, match="не найден"):
		await service.add_community(bot_id, "@notfound")
	with pytest.raises(CommunityCheckError, match="нет права"):
		await service.add_community(bot_id, "@noperm")
	assert await service.list_communities() == []


async def test_duplicate_community_rejected(db: Database) -> None:
	"""Повторное подключение того же канала — понятная ошибка."""
	bot_id = await _make_bot(db)
	service = CommunitiesService(db, _FakeGateway())
	await service.add_community(bot_id, "@testchan")
	with pytest.raises(CommunityError, match="уже подключено"):
		await service.add_community(bot_id, "@testchan")


async def test_bot_connect_probes_userbot(db: Database) -> None:
	"""Бот-путь попутно привязывает аккаунта-админа; не нашёлся — без него."""
	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.userbot_admins = {account_id}
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.userbot_assigned is True
	assert dto.default_account_id == account_id and dto.default_account_label == "@ub"
	await service.delete_community(dto.id)
	gateway.userbot_admins = set()  # аккаунт есть, но не админ канала
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.userbot_assigned is False and dto.default_account_id is None


async def test_connect_via_userbot(db: Database) -> None:
	"""Через выбранный аккаунт канал подключается без бота; не админ — ошибка."""
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.userbot_admins = {account_id}
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	assert dto.bot_id is None and dto.bot_label is None
	assert dto.default_account_id == account_id and dto.userbot_assigned is True
	listed = await service.list_communities()
	assert listed[0].default_account_label == "@ub"
	with pytest.raises(CommunityError, match="уже подключено"):
		await service.add_community_via_userbot(account_id, "@testchan")
	await service.delete_community(dto.id)
	gateway.userbot_admins = set()
	with pytest.raises(UserbotUnavailableError, match="не администратор"):
		await service.add_community_via_userbot(account_id, "@testchan")
	assert await service.list_communities() == []
	with pytest.raises(CommunityError, match="Аккаунт не найден"):
		await service.add_community_via_userbot(999, "@testchan")


async def test_recheck_updates_binding_both_ways(db: Database) -> None:
	"""Перепроверка привязывает найденного админа и снимает потерявшего права."""
	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")  # аккаунт пока не админ
	assert dto.default_account_id is None
	# аккаунт стал админом (например, добавили после подключения)
	gateway.userbot_admins = {account_id}
	access = await service.recheck_community(dto.id)
	assert access.userbot_ok and access.community.default_account_id == account_id
	assert access.bot_ok is True
	# бота выгнали: бот — только предупреждение, привязка userbot цела
	gateway.bot_is_admin = False
	access = await service.recheck_community(dto.id)
	assert access.bot_ok is False
	assert access.community.bot_id is not None  # бот не отвязан молча
	assert access.community.default_account_id == account_id
	# аккаунт потерял права (подтверждённый отказ) — привязка снимается
	gateway.userbot_admins = set()
	gateway.bot_is_admin = True
	access = await service.recheck_community(dto.id)
	assert access.userbot_ok is False
	assert access.community.default_account_id is None


async def test_recheck_keeps_binding_when_userbot_unreachable(db: Database) -> None:
	"""Сбой связи/подключения аккаунта не сбрасывает привязку.

	Иначе перепроверка при мигнувшей сети молча лишала бы канал
	отложенных постов и больших файлов (маршрутизация идёт по привязке).
	"""

	class _OfflineGateway(_FakeGateway):
		async def check_community_userbot(self, account_id: int, chat_ref: str) -> CommunityInfo:
			raise UserbotNotConnectedError("Userbot не подключён — войдите.")

	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.userbot_admins = {account_id}
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.default_account_id == account_id
	offline = CommunitiesService(db, _OfflineGateway())
	access = await offline.recheck_community(dto.id)
	assert access.userbot_ok is None  # «не удалось проверить», не «не админ»
	assert access.community.default_account_id == account_id  # привязка не тронута


async def test_assign_and_unassign_userbot(db: Database) -> None:
	"""Каналу привязывается аккаунт (с проверкой прав) и отвязывается."""
	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.default_account_id is None
	# без прав — не привязывается
	with pytest.raises(UserbotUnavailableError, match="не администратор"):
		await service.assign_userbot(dto.id, account_id)
	# с правами — привязывается
	gateway.userbot_admins = {account_id}
	updated = await service.assign_userbot(dto.id, account_id)
	assert updated.default_account_id == account_id and updated.default_account_label == "@ub"
	# отвязка: аккаунт исчезает из канала, но остаётся в приложении
	updated = await service.unassign_userbot(dto.id)
	assert updated.default_account_id is None and updated.userbot_assigned is False
	with pytest.raises(CommunityError, match="Аккаунт не найден"):
		await service.assign_userbot(dto.id, 999)


async def test_assign_and_unassign_bot(db: Database) -> None:
	"""Каналу без бота назначается бот (с проверкой прав) и отвязывается."""
	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.userbot_admins = {account_id}
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	assert dto.bot_id is None
	# без прав — не назначается
	gateway.bot_is_admin = False
	with pytest.raises(CommunityCheckError, match="нет права"):
		await service.assign_bot(dto.id, bot_id)
	# с правами — назначается
	gateway.bot_is_admin = True
	updated = await service.assign_bot(dto.id, bot_id)
	assert updated.bot_id == bot_id and updated.bot_label == "Публикатор"
	# отвязка: бот исчезает, привязка userbot не трогается
	updated = await service.unassign_bot(dto.id)
	assert updated.bot_id is None and updated.default_account_id == account_id


async def test_unknown_bot_rejected(db: Database) -> None:
	"""Подключение с несуществующим ботом — понятная ошибка."""
	service = CommunitiesService(db, _FakeGateway())
	with pytest.raises(CommunityError, match="Бот не найден"):
		await service.add_community(999, "@testchan")


def test_normalize_chat_ref() -> None:
	"""Все форматы ввода приводятся к виду для API Telegram."""
	assert normalize_chat_ref("@mychannel") == "@mychannel"
	assert normalize_chat_ref("mychannel") == "@mychannel"
	assert normalize_chat_ref("https://t.me/mychannel") == "@mychannel"
	assert normalize_chat_ref("t.me/mychannel/") == "@mychannel"
	# веб-превью браузерного Telegram: t.me/s/имя — эквивалент t.me/имя
	assert normalize_chat_ref("https://t.me/s/mychannel") == "@mychannel"
	assert normalize_chat_ref("-1001234567") == -1001234567
	with pytest.raises(ChatRefError):
		normalize_chat_ref("   ")


def test_normalize_chat_ref_hardened() -> None:
	"""Пробелы в ID, ссылки t.me/c/… и инвайт-ссылки (правки 2026-07-12)."""
	assert normalize_chat_ref("-100 2233 445 566") == -1002233445566
	assert normalize_chat_ref(" -1002233445566 ") == -1002233445566
	assert normalize_chat_ref("https://t.me/c/2233445566/5") == -1002233445566
	assert normalize_chat_ref("t.me/c/2233445566") == -1002233445566
	with pytest.raises(ChatRefError, match="Инвайт"):
		normalize_chat_ref("https://t.me/+AbCdEfGh123")
	with pytest.raises(ChatRefError, match="Инвайт"):
		normalize_chat_ref("https://t.me/joinchat/AbCdEfGh123")  # старый формат
	with pytest.raises(ChatRefError, match="t.me/c"):
		normalize_chat_ref("t.me/c/abc/5")
	with pytest.raises(ChatRefError, match="Укажите"):
		normalize_chat_ref("--123")  # лишний минус — доменная ошибка, не ValueError


def test_describe_update() -> None:
	"""Описание событий бота: членство, пост в канале, прочее — None."""
	from datetime import datetime

	from pxcontrol.engine.telegram.bot_api import describe_update

	membership = SimpleNamespace(
		date=datetime(2026, 7, 12, 16, 30),
		chat=SimpleNamespace(title="Мой канал", type="channel", id=-1004344346478),
		new_chat_member=SimpleNamespace(status="administrator", can_post_messages=True),
	)
	line = describe_update(SimpleNamespace(my_chat_member=membership, channel_post=None))
	assert line is not None
	assert "Мой канал" in line and "administrator" in line and "есть" in line

	post = SimpleNamespace(
		date=datetime(2026, 7, 12, 16, 31),
		chat=SimpleNamespace(title="Мой канал", id=-1004344346478),
	)
	line = describe_update(SimpleNamespace(my_chat_member=None, channel_post=post))
	assert line is not None and "пост в канале" in line

	assert describe_update(SimpleNamespace(my_chat_member=None, channel_post=None)) is None


async def test_community_enabled_comes_from_settings(db: Database) -> None:
	"""Флаг активности канала в DTO читается из настроек (умолчание — True)."""
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.userbot_admins = {account_id}
	service = CommunitiesService(db, gateway)
	community = await service.add_community_via_userbot(account_id, "@chan")
	assert community.enabled is True
	await SettingsService(db).set_for(COMMUNITY_ENABLED, community.id, False)
	listed = await service.list_communities()
	assert [ch.enabled for ch in listed] == [False]


def test_ensure_bot_can_post() -> None:
	"""Право публиковать: владелец и админ с правом проходят, прочие — нет."""
	ensure_bot_can_post(SimpleNamespace(status="creator"))
	ensure_bot_can_post(SimpleNamespace(status="administrator", can_post_messages=True))
	with pytest.raises(CommunityCheckError, match="не администратор"):
		ensure_bot_can_post(SimpleNamespace(status="member"))
	with pytest.raises(CommunityCheckError, match="нет права"):
		ensure_bot_can_post(SimpleNamespace(status="administrator", can_post_messages=False))


def test_bot_caption_markup_to_html() -> None:
	"""Разметка поля текста доносится бот-путём как HTML.

	Bot API без parse_mode показал бы подписчикам буквальную разметку;
	спецсимволы HTML в значениях полей экранируются. Подмножество —
	как у Telethon (основной путь): жирный, курсив, зачёркнутый, код.
	"""
	from pxcontrol.engine.telegram.bot_api import to_html

	assert to_html("**Название**\nГод: 2026") == "<b>Название</b>\nГод: 2026"
	assert to_html("**Re: Zero <2 сезон> & ещё**") == "<b>Re: Zero &lt;2 сезон&gt; &amp; ещё</b>"
	assert to_html("__курсив__ и ~~зачёркнутый~~") == "<i>курсив</i> и <s>зачёркнутый</s>"
	assert to_html("код: `x = 1`") == "код: <code>x = 1</code>"
	assert to_html("без разметки") == "без разметки"
	assert to_html("непарные 2**3") == "непарные 2**3"


def test_community_kind_from_chat_type() -> None:
	"""Вид по типу чата Bot API; малая группа и личный чат — отказ."""
	assert community_kind_from_chat_type("channel") is CommunityKind.CHANNEL
	assert community_kind_from_chat_type("supergroup") is CommunityKind.GROUP
	with pytest.raises(CommunityCheckError, match="супергруппу"):
		community_kind_from_chat_type("group")
	with pytest.raises(CommunityCheckError, match="личный чат"):
		community_kind_from_chat_type("private")


def test_ensure_bot_can_send_in_group() -> None:
	"""Права бота в группе: участник без ограничений; админа они не касаются."""
	allow = SimpleNamespace(can_send_messages=True)
	deny = SimpleNamespace(can_send_messages=False)
	# админу и создателю общие ограничения группы не мешают
	ensure_bot_can_send_in_group(SimpleNamespace(status="administrator"), deny)
	ensure_bot_can_send_in_group(SimpleNamespace(status="creator"), deny)
	ensure_bot_can_send_in_group(SimpleNamespace(status="member"), allow)
	# Bot API может не отдать права — отсутствие запрета не считается запретом
	ensure_bot_can_send_in_group(SimpleNamespace(status="member"), None)
	ensure_bot_can_send_in_group(
		SimpleNamespace(status="restricted", is_member=True, can_send_messages=True), allow
	)
	with pytest.raises(CommunityCheckError, match="только администраторы"):
		ensure_bot_can_send_in_group(SimpleNamespace(status="member"), deny)
	with pytest.raises(CommunityCheckError, match="не участник"):
		ensure_bot_can_send_in_group(SimpleNamespace(status="left"), allow)
	with pytest.raises(CommunityCheckError, match="не участник"):
		ensure_bot_can_send_in_group(
			SimpleNamespace(status="restricted", is_member=False, can_send_messages=True), allow
		)
	with pytest.raises(CommunityCheckError, match="ограничен в отправке"):
		ensure_bot_can_send_in_group(
			SimpleNamespace(status="restricted", is_member=True, can_send_messages=False), allow
		)


async def _community_row(db: Database, community_id: int) -> Community:
	"""Читает строку сообщества напрямую (проверка хранимых полей)."""
	async with db.session_factory() as session:
		row = await session.get(Community, community_id)
		assert row is not None
		return row


async def test_group_connect_stores_kind_and_forum(db: Database) -> None:
	"""Подключение группы сохраняет вид и признак форума (ADR-0021)."""
	gateway = _FakeGateway()
	gateway.kind = CommunityKind.GROUP
	gateway.forum = True
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	row = await _community_row(db, dto.id)
	assert row.kind == "group"
	assert row.forum is True
	# DTO несёт вид и форум для интерфейса (этап 2)
	assert dto.kind is CommunityKind.GROUP and dto.forum is True
	listed = await service.list_communities()
	assert listed[0].kind is CommunityKind.GROUP and listed[0].forum is True


async def test_recheck_refreshes_forum_keeps_kind(db: Database) -> None:
	"""Перепроверка обновляет признак форума; вид записи не меняется."""
	gateway = _FakeGateway()
	gateway.kind = CommunityKind.GROUP
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	assert (await _community_row(db, dto.id)).forum is False
	gateway.forum = True  # владелец включил темы в группе
	await service.recheck_community(dto.id)
	row = await _community_row(db, dto.id)
	assert row.forum is True
	# вид определяется подключением: даже если Telegram вдруг ответил иначе
	gateway.kind = CommunityKind.CHANNEL
	await service.recheck_community(dto.id)
	assert (await _community_row(db, dto.id)).kind == "group"


async def test_recheck_refreshes_title_and_username(db: Database) -> None:
	"""Перепроверка актуализирует название и @имя (могли смениться в Telegram)."""
	gateway = _FakeGateway()
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	gateway.title = "Новое название"
	gateway.username = "newchan"  # владелец сменил публичную ссылку t.me/newchan
	await service.recheck_community(dto.id)
	row = await _community_row(db, dto.id)
	assert row.title == "Новое название"
	assert row.username == "newchan"
	# имя сняли — сообщество стало приватным: в записи честный None
	gateway.username = None
	await service.recheck_community(dto.id)
	assert (await _community_row(db, dto.id)).username is None


async def test_assign_bot_refreshes_forum(db: Database) -> None:
	"""Назначение бота тоже освежает признак форума (данные уже получены)."""
	gateway = _FakeGateway()
	gateway.kind = CommunityKind.GROUP
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	bot_id = await _make_bot(db)
	gateway.forum = True
	await service.assign_bot(dto.id, bot_id)
	assert (await _community_row(db, dto.id)).forum is True


async def _member_service(db: Database) -> tuple[CommunitiesService, _FakeGateway, int, int]:
	"""Сообщество с одним участником-умолчанием и второй вошедший аккаунт."""
	gateway = _FakeGateway()
	first = await _make_account(db, "@first")
	second = await _make_account(db, "@second")
	gateway.userbot_admins = {first, second}
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(first, "@testchan")
	return service, gateway, dto.id, second


async def test_membership_crud_and_default(db: Database) -> None:
	"""Участники: добавление с ролью из зонда, умолчание, явная смена."""
	service, gateway, community_id, second = await _member_service(db)
	members = await service.list_members(community_id)
	assert [(m.label, m.role, m.is_default) for m in members] == [
		("@first", UserbotRole.ADMIN, True)
	]
	gateway.role = UserbotRole.MEMBER  # второй аккаунт — простой участник
	members = await service.add_member(community_id, second)
	assert [(m.label, m.role, m.is_default) for m in members] == [
		("@first", UserbotRole.ADMIN, True),
		("@second", UserbotRole.MEMBER, False),
	]
	with pytest.raises(CommunityError, match="уже участник"):
		await service.add_member(community_id, second)
	dto = await service.set_default(community_id, second)
	assert dto.default_account_id == second
	assert dto.default_role is UserbotRole.MEMBER
	assert dto.members_count == 2


async def test_remove_default_member_resets_default(db: Database) -> None:
	"""Удаление участника-умолчания сбрасывает умолчание без авто-замены."""
	service, gateway, community_id, second = await _member_service(db)
	members = await service.add_member(community_id, second)
	assert len(members) == 2
	first_id = next(m.account_id for m in members if m.is_default)
	remaining = await service.remove_member(community_id, first_id)
	assert [m.label for m in remaining] == ["@second"]
	dto = next(c for c in await service.list_communities() if c.id == community_id)
	assert dto.default_account_id is None  # авто-выбора нет (ADR-0022)
	assert dto.userbot_assigned is False
	with pytest.raises(CommunityError, match="не участник"):
		await service.remove_member(community_id, first_id)


async def test_set_default_requires_membership(db: Database) -> None:
	"""Умолчанием может стать только участник сообщества."""
	service, _gateway, community_id, second = await _member_service(db)
	with pytest.raises(CommunityError, match="только участник"):
		await service.set_default(community_id, second)


async def test_recheck_updates_roles_and_drops_refused(db: Database) -> None:
	"""Перепроверка: роль обновляется, отказник исключается, умолчание падает."""
	service, gateway, community_id, second = await _member_service(db)
	gateway.role = UserbotRole.MEMBER
	await service.add_member(community_id, second)
	# админа разжаловали в участники — роль обновится по зонду
	await service.recheck_community(community_id)
	members = await service.list_members(community_id)
	assert [(m.label, m.role) for m in members] == [
		("@first", UserbotRole.MEMBER),
		("@second", UserbotRole.MEMBER),
	]
	# умолчание выгнали из сообщества: членство и умолчание снимаются
	gateway.userbot_admins = {second}
	access = await service.recheck_community(community_id)
	assert access.userbot_ok is False
	assert [m.label for m in await service.list_members(community_id)] == ["@second"]
	assert access.community.default_account_id is None
