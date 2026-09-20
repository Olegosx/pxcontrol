"""Тесты сервиса каналов и чистых функций проверки (без сети)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, TgAccount
from pxcontrol.engine.services.accounts import AccountsService
from pxcontrol.engine.services.communities import (
	CommunitiesService,
	CommunityError,
	JoinOutcome,
)
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.bot_api import (
	BotError,
	community_kind_from_chat_type,
	ensure_bot_can_post,
	ensure_bot_can_send_in_group,
)
from pxcontrol.engine.telegram.lane import LaneOwner, OwnerKind
from pxcontrol.engine.telegram.mtproto import (
	UserbotAccessError,
	UserbotNotConnectedError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.refs import ChatRefError, invite_hash, normalize_chat_ref
from pxcontrol.engine.telegram.rights import (
	ALL_ADMIN_RIGHTS,
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	MemberRights,
	ParticipantStatus,
)
from pxcontrol.engine.telegram.types import BotRef, CommunityInfo, CommunityKind


class _FakeGateway:
	"""Подмена шлюза: токены и каналы проверяются без сети.

	Userbot-проверки адресные (ADR-0019): «админами» считаются аккаунты
	из ``userbot_admins``, остальным Telegram «подтверждает отказ».
	"""

	login = None  # вход userbot в этих тестах не используется

	def __init__(self) -> None:
		self.userbot_admins: set[int] = set()  # кто здесь администратор
		self.invisible_for: set[int] = set()  # кому сообщество не видно вовсе
		self.outsiders: set[int] = set()  # кто в сообществе не состоит (ADR-0035)
		self.bot_is_admin = True  # бот — админ с правом публиковать
		self.bot_visible = True  # сообщество видно боту (иначе Telegram молчит)
		self.bot_inside = True  # бот уже в сообществе
		# ввод исполнителя (ADR-0035): что приложение сделало в Telegram
		self.joined_public: list[tuple[int, str]] = []
		self.joined_by_link: list[tuple[int, str]] = []
		self.invited: list[tuple[int, str]] = []
		self.promoted: list[tuple[int, str, AdminRights]] = []
		self.known_link: str | None = None  # ссылка, которую отдаёт Telegram
		self.approval_needed = False  # приглашение требует одобрения
		self.kind = CommunityKind.CHANNEL  # вид, который «увидит» проверка
		self.forum = False  # признак форума в ответе проверки
		self.status = ParticipantStatus.ADMIN  # участие аккаунта в userbot-зонде
		self.title = "Тестовый канал"  # название в ответе проверки
		self.username: str | None = "testchan"  # @имя (None — приватное)
		self.bot_can_edit = False  # право бота править чужие посты (ADR-0031)

	async def bot_check_token(self, token: str) -> str:
		return "test_bot"

	async def bot_check_community(self, bot: BotRef, chat_ref: str) -> CommunityInfo:
		if chat_ref == "@notfound":
			raise BotError("Канал не найден — проверьте @имя или ID.")
		if chat_ref == "@noperm" or not self.bot_visible or not self.bot_inside:
			raise BotError("Бот не добавлен в сообщество — добавьте его.")
		# нехватка прав больше не отказ, а факт в снимке (ADR-0035, п. 7)
		status = ParticipantStatus.ADMIN if self.bot_is_admin else ParticipantStatus.MEMBER
		admin = (
			AdminRights(post_messages=True, edit_messages=self.bot_can_edit)
			if self.bot_is_admin
			else AdminRights()
		)
		allowed = ALL_MEMBER_RIGHTS if self.bot_is_admin else MemberRights(send_plain=True)
		return CommunityInfo(
			"-1001234",
			self.title,
			self.username,
			self.kind,
			ExecutorRights(status, admin, allowed),
			self.forum,
		)

	async def userbot_join_public(self, account_id: int, username: str) -> None:
		self.joined_public.append((account_id, username))
		self.outsiders.discard(account_id)

	async def userbot_join_by_invite(self, account_id: int, link: str) -> bool:
		self.joined_by_link.append((account_id, link))
		if self.approval_needed:
			return False  # заявка отправлена, участником аккаунт ещё не стал
		self.outsiders.discard(account_id)
		return True

	async def userbot_invite_link(self, account_id: int, chat_id: str) -> str | None:
		return self.known_link

	async def userbot_invite_participant(self, account_id: int, chat_id: str, target: str) -> None:
		self.invited.append((account_id, target))
		self.outsiders.clear()
		self.bot_inside = True

	async def userbot_promote(
		self, account_id: int, chat_id: str, target: str, rights: AdminRights
	) -> None:
		self.promoted.append((account_id, target, rights))
		self.bot_inside = True

	async def userbot_check_community(self, account_id: int, chat_ref: str) -> CommunityInfo:
		if account_id in self.invisible_for:
			raise UserbotAccessError("Сообщество закрыто от этого аккаунта.")
		if account_id in self.outsiders:
			# «не состоит» — факт участия, а не отказ в доступе (ADR-0035)
			return CommunityInfo(
				"-1001234",
				self.title,
				self.username,
				self.kind,
				ExecutorRights(ParticipantStatus.LEFT),
				self.forum,
			)
		# не админ — не отказ, а участие с правами участника (ADR-0035)
		here_admin = account_id in self.userbot_admins
		status = self.status if here_admin else ParticipantStatus.MEMBER
		admin = ALL_ADMIN_RIGHTS if here_admin and status.administers else AdminRights()
		allowed = ALL_MEMBER_RIGHTS if here_admin else MemberRights(send_plain=True)
		return CommunityInfo(
			"-1001234",
			self.title,
			self.username,
			self.kind,
			ExecutorRights(status, admin, allowed),
			self.forum,
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
	assert dto.default_bot_label == "Публикатор"
	listed = await service.list_communities()
	assert [c.title for c in listed] == ["Тестовый канал"]
	assert listed[0].default_bot_label == "Публикатор"
	await service.delete_community(dto.id)
	assert await service.list_communities() == []


async def test_failed_check_not_saved(db: Database) -> None:
	"""Не прошедший проверку канал не сохраняется."""
	bot_id = await _make_bot(db)
	service = CommunitiesService(db, _FakeGateway())
	with pytest.raises(BotError, match="не найден"):
		await service.add_community(bot_id, "@notfound")
	with pytest.raises(BotError, match="не добавлен"):
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
	assert dto.default_bot_id is None and dto.default_bot_label is None
	assert dto.default_account_id == account_id and dto.userbot_assigned is True
	listed = await service.list_communities()
	assert listed[0].default_account_label == "@ub"
	with pytest.raises(CommunityError, match="уже подключено"):
		await service.add_community_via_userbot(account_id, "@testchan")
	await service.delete_community(dto.id)
	# не админ — сообщество всё равно подключается (ADR-0035, п. 7),
	# просто публиковать им нельзя, и снимок это честно показывает
	gateway.userbot_admins = set()
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	assert dto.userbot_assigned is True
	assert not dto.capabilities.userbot
	assert dto.publisher_incapable is True
	await service.delete_community(dto.id)
	# а вот невидимое сообщество не подключается: проверять нечего
	gateway.invisible_for = {account_id}
	with pytest.raises(UserbotUnavailableError, match="закрыто"):
		await service.add_community_via_userbot(account_id, "@testchan")
	assert await service.list_communities() == []
	with pytest.raises(CommunityError, match="Аккаунт не найден"):
		await service.add_community_via_userbot(999, "@testchan")


async def test_recheck_tracks_rights_without_losing_assignments(db: Database) -> None:
	"""Перепроверка обновляет права, но назначения не трогает (ADR-0035).

	Прежде подтверждённый отказ снимал привязку, и вместе с правами
	пропадала настройка: вернули аккаунту права — публикатора всё равно
	нужно назначать заново. Теперь строка живёт, а «может ли он сейчас»
	читается из снимка.
	"""
	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")  # аккаунт пока не админ
	assert dto.default_account_id is None
	# аккаунт стал админом (например, выдали права после подключения)
	gateway.userbot_admins = {account_id}
	access = await service.recheck_community(dto.id)
	assert access.userbot_ok and access.community.default_account_id == account_id
	assert access.bot_ok is True
	# бота разжаловали: предупреждение есть, назначение цело
	gateway.bot_is_admin = False
	access = await service.recheck_community(dto.id)
	assert access.bot_ok is False
	assert access.community.default_bot_id is not None  # бот не отвязан молча
	assert access.community.default_account_id == account_id
	# аккаунт потерял права — назначение остаётся, публиковать нельзя
	gateway.userbot_admins = set()
	gateway.bot_is_admin = True
	access = await service.recheck_community(dto.id)
	assert access.userbot_ok is False
	assert access.community.default_account_id == account_id
	assert not access.community.capabilities.userbot
	# права вернули — и публикация снова доступна без единого назначения
	gateway.userbot_admins = {account_id}
	access = await service.recheck_community(dto.id)
	assert access.userbot_ok is True and access.community.capabilities.userbot


async def test_recheck_keeps_binding_when_userbot_unreachable(db: Database) -> None:
	"""Сбой связи/подключения аккаунта не сбрасывает привязку.

	Иначе перепроверка при мигнувшей сети молча лишала бы канал
	отложенных постов и больших файлов (маршрутизация идёт по привязке).
	"""

	class _OfflineGateway(_FakeGateway):
		async def userbot_check_community(self, account_id: int, chat_ref: str) -> CommunityInfo:
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


async def test_bot_joins_pool_and_leaves_it(db: Database) -> None:
	"""Бот — исполнитель пула: добавляется, становится публикатором, убирается.

	Первый бот сообщества сразу становится публикатором-ботом: иначе
	запасной путь публикации и кнопки остались бы недоступны без второго
	действия человека.
	"""
	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.userbot_admins = {account_id}
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	assert dto.default_bot_id is None
	# бота, который не админ, тоже можно завести — он просто не публикует
	gateway.bot_is_admin = False
	joined = await service.add_executor(dto.id, LaneOwner(OwnerKind.BOT, bot_id))
	assert joined.outcome is JoinOutcome.ALREADY_IN, "бот уже был в сообществе"
	bot_row = next(e for e in joined.executors if e.owner.kind is OwnerKind.BOT)
	assert bot_row.is_default and not bot_row.can_publish
	updated = await service.get_community(dto.id)
	assert updated.default_bot_id == bot_id and not updated.capabilities.bot
	# права выдали — публиковать ботом можно, назначать заново не нужно
	gateway.bot_is_admin = True
	await service.recheck_community(dto.id)
	updated = await service.get_community(dto.id)
	assert updated.capabilities.bot and updated.default_bot_label == "Публикатор"
	# бота убрали: пул без него, публикатор-пользователь не тронут
	await service.remove_executor(dto.id, LaneOwner(OwnerKind.BOT, bot_id))
	updated = await service.get_community(dto.id)
	assert updated.default_bot_id is None and updated.default_account_id == account_id


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


def test_invite_hash_accepts_both_formats_and_bare_hash() -> None:
	"""Хеш приглашения берётся из обоих форматов Telegram и из голого хеша.

	Человек копирует ссылку как придётся: из настроек сообщества,
	из чужого сообщения, иногда — один хеш без адреса.
	"""
	assert invite_hash("https://t.me/+AbCdEf12") == "AbCdEf12"
	assert invite_hash("t.me/joinchat/XyZ") == "XyZ"
	assert invite_hash("https://telegram.me/+QQ") == "QQ"
	assert invite_hash("  AbC  ") == "AbC"


def test_invite_hash_rejects_ordinary_links() -> None:
	"""Обычная ссылка — не приглашение: по ней вступают иначе, и молчать нельзя."""
	with pytest.raises(ChatRefError, match="публичное сообщество"):
		invite_hash("t.me/kino")
	with pytest.raises(ChatRefError, match="публичное сообщество"):
		invite_hash("https://t.me/kino/42")
	for bad in ("@kino", ""):
		with pytest.raises(ChatRefError, match="ссылка-приглашение"):
			invite_hash(bad)


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
	"""Право публиковать: владелец и админ с правом проходят, прочие — нет.

	Правило читает снимок прав (ADR-0035), а не ответ библиотеки: перевод
	ответа в снимок — забота :mod:`rights` и его тестов.
	"""
	ensure_bot_can_post(ExecutorRights(ParticipantStatus.CREATOR, ALL_ADMIN_RIGHTS))
	ensure_bot_can_post(ExecutorRights(ParticipantStatus.ADMIN, AdminRights(post_messages=True)))
	with pytest.raises(BotError, match="не администратор"):
		ensure_bot_can_post(ExecutorRights(ParticipantStatus.MEMBER))
	with pytest.raises(BotError, match="нет права"):
		ensure_bot_can_post(ExecutorRights(ParticipantStatus.ADMIN, AdminRights()))


def test_bot_caption_keeps_separators_literal() -> None:
	"""Бот-путь не разбирает разделители: текст уходит как набран.

	Оформление приезжает сущностями (ADR-0033), а ``**`` и ``__``
	в тексте — обычные символы: пост про ``__init__`` или про степень
	не должен уходить курсивом и жирным. Спецсимволы HTML при этом
	экранируются — иначе чужой тег сломал бы разбор у Telegram.
	"""
	from pxcontrol.engine.telegram.bot_api import post_html

	assert post_html("Метод __init__ и 2**3**4") == "Метод __init__ и 2**3**4"
	assert post_html("Re: Zero <2 сезон> & ещё") == "Re: Zero &lt;2 сезон&gt; &amp; ещё"
	assert post_html("без разметки") == "без разметки"


def test_community_kind_from_chat_type() -> None:
	"""Вид по типу чата Bot API; малая группа и личный чат — отказ."""
	assert community_kind_from_chat_type("channel") is CommunityKind.CHANNEL
	assert community_kind_from_chat_type("supergroup") is CommunityKind.GROUP
	with pytest.raises(BotError, match="супергруппу"):
		community_kind_from_chat_type("group")
	with pytest.raises(BotError, match="личный чат"):
		community_kind_from_chat_type("private")


def test_ensure_bot_can_send_in_group() -> None:
	"""Права бота в группе: участник без ограничений; админа они не касаются.

	Причину отказа называет статус: ограниченному — про его ограничения,
	обычному участнику — про группу, где пишут только администраторы.
	"""
	may_write = MemberRights(send_plain=True)
	# админу и владельцу общие ограничения группы не мешают
	ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.ADMIN, ALL_ADMIN_RIGHTS))
	ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.CREATOR, ALL_ADMIN_RIGHTS))
	ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.MEMBER, AdminRights(), may_write))
	ensure_bot_can_send_in_group(
		ExecutorRights(ParticipantStatus.RESTRICTED, AdminRights(), may_write)
	)
	with pytest.raises(BotError, match="только администраторы"):
		ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.MEMBER))
	with pytest.raises(BotError, match="не участник"):
		ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.LEFT))
	with pytest.raises(BotError, match="не участник"):
		ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.BANNED))
	with pytest.raises(BotError, match="ограничен в отправке"):
		ensure_bot_can_send_in_group(ExecutorRights(ParticipantStatus.RESTRICTED))


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


async def test_get_community_snapshot(db: Database) -> None:
	"""Снимок одного сообщества: свежие данные или понятная ошибка."""
	gateway = _FakeGateway()
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	snapshot = await service.get_community(dto.id)
	assert snapshot.id == dto.id and snapshot.title == dto.title
	with pytest.raises(CommunityError, match="не найден"):
		await service.get_community(999_999)


async def test_confirmed_checks_call_profile_sync_hook(db: Database) -> None:
	"""Живая проверка прав дёргает крючок актуализации профиля аккаунта.

	Подтверждённый отказ крючок не дёргает: связь с аккаунтом
	не подтверждена, актуализировать профиль не по чему.
	"""
	gateway = _FakeGateway()
	admin = await _make_account(db, "@admin")
	other = await _make_account(db, "@other")
	gateway.userbot_admins = {admin, other}
	synced: list[int] = []

	async def hook(account_id: int) -> None:
		synced.append(account_id)

	service = CommunitiesService(db, gateway, profile_sync=hook)
	dto = await service.add_community_via_userbot(admin, "@testchan")
	assert synced == [admin], "подключение подтвердило права — профиль актуализирован"
	synced.clear()
	await service.add_executor(dto.id, LaneOwner(OwnerKind.USER, other))
	assert other in synced, "добавление участника тоже проверяет права живьём"
	# сообщество закрылось от второго аккаунта: зонд отвечает отказом
	gateway.invisible_for = {other}
	synced.clear()
	await service.recheck_community(dto.id)
	assert admin in synced, "перепроверка актуализирует профиль живого участника"
	assert other not in synced, "подтверждённый отказ — без актуализации"


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
	await service.add_executor(dto.id, LaneOwner(OwnerKind.BOT, bot_id))
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
	executors = await service.list_executors(community_id)
	assert [(e.label, e.status, e.is_default) for e in executors] == [
		("@first", ParticipantStatus.ADMIN, True)
	]
	gateway.status = ParticipantStatus.MEMBER  # второй аккаунт — простой участник
	gateway.userbot_admins.discard(second)
	added = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert [(e.label, e.status, e.is_default) for e in added.executors] == [
		("@first", ParticipantStatus.ADMIN, True),
		("@second", ParticipantStatus.MEMBER, False),
	]
	with pytest.raises(CommunityError, match="уже в пуле"):
		await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	dto = await service.set_default_publisher(community_id, LaneOwner(OwnerKind.USER, second))
	assert dto.default_account_id == second
	assert dto.default_status is ParticipantStatus.MEMBER
	assert dto.executors_count == 2


async def _make_private(db: Database, community_id: int) -> None:
	"""Делает сообщество приватным: у записи снимается @имя.

	Вступить по имени в такое нельзя — остаётся ссылка-приглашение.
	"""
	async with db.session_factory() as session:
		community = await session.get(Community, community_id)
		assert community is not None
		community.username = None
		await session.commit()


async def _set_username(db: Database, account_id: int, username: str) -> None:
	"""Проставляет аккаунту @имя: по нему Telegram и находит приглашаемого."""
	async with db.session_factory() as session:
		account = await session.get(TgAccount, account_id)
		assert account is not None
		account.username = username
		await session.commit()


async def _forget_username(db: Database, account_id: int) -> None:
	"""Стирает @имя аккаунта: пригласить такого Telegram не даст."""
	async with db.session_factory() as session:
		account = await session.get(TgAccount, account_id)
		assert account is not None
		account.username = None
		await session.commit()


async def _forget_bot_username(db: Database, bot_id: int) -> None:
	"""Стирает @имя бота (в жизни бывает у записи, созданной до проверки)."""
	from pxcontrol.engine.db.models import Bot as BotRow

	async with db.session_factory() as session:
		bot = await session.get(BotRow, bot_id)
		assert bot is not None
		bot.username = None
		await session.commit()


# --- ввод исполнителя в сообщество (ADR-0035, этап C) -------------------------------


async def test_join_public_community_by_username(db: Database) -> None:
	"""Не состоит, сообщество публичное — аккаунт вступает сам, без чужой помощи.

	Самая дешёвая ступень лестницы: ни ссылки, ни приглашающего, ни чьих-то
	прав. Потому она и первая.
	"""
	service, gateway, community_id, second = await _member_service(db)
	gateway.outsiders = {second}
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert result.outcome is JoinOutcome.JOINED
	assert gateway.joined_public == [(second, "@testchan")]
	assert not gateway.joined_by_link and not gateway.invited
	assert [e.owner.id for e in result.executors] == [1, second]


async def test_already_inside_changes_nothing_in_telegram(db: Database) -> None:
	"""Уже состоит — в Telegram не ходим вовсе, только записываем права."""
	service, gateway, community_id, second = await _member_service(db)
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert result.outcome is JoinOutcome.ALREADY_IN
	assert not gateway.joined_public and not gateway.joined_by_link and not gateway.invited


async def test_private_community_joined_by_existing_link(db: Database) -> None:
	"""Приватное: ссылку **читаем** у Telegram и вступаем по ней (ADR-0035, п. 10).

	Своих ссылок приложение не создаёт: у сообщества она уже есть,
	и Telegram отдаёт её администратору с правом приглашать.
	"""
	service, gateway, community_id, second = await _member_service(db)
	gateway.username = None  # приватное сообщество: вступить по имени нельзя
	await _make_private(db, community_id)
	gateway.outsiders = {second}
	gateway.known_link = "https://t.me/+secretHash"
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert result.outcome is JoinOutcome.JOINED
	assert gateway.joined_by_link == [(second, "https://t.me/+secretHash")]
	assert not gateway.joined_public


async def test_join_request_is_an_outcome_not_a_membership(db: Database) -> None:
	"""Заявка на вступление: строка заводится, но участия ещё нет.

	До одобрения прав у заявителя нет, публиковать им нельзя, и приложение
	говорит это прямо — вместо того чтобы притвориться, что всё готово.
	"""
	service, gateway, community_id, second = await _member_service(db)
	gateway.username = None
	await _make_private(db, community_id)
	gateway.outsiders = {second}
	gateway.known_link = "https://t.me/+needsApproval"
	gateway.approval_needed = True
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert result.outcome is JoinOutcome.REQUESTED
	row = next(e for e in result.executors if e.owner.id == second)
	assert row.status is ParticipantStatus.REQUESTED
	assert not row.can_publish and not row.status.in_community


async def test_private_without_link_asks_the_human(db: Database) -> None:
	"""Ссылки нет и пригласить некому — исход «нужна ссылка», пул не тронут.

	Это не ошибка: в Telegram ничего не делали, операция продолжится
	с той ссылкой, которую даст человек.
	"""
	service, gateway, community_id, second = await _member_service(db)
	gateway.username = None
	await _make_private(db, community_id)
	gateway.outsiders = {second}
	gateway.known_link = None
	await _forget_username(db, second)  # приглашать некого: @имени нет
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert result.outcome is JoinOutcome.NEEDS_LINK
	assert [e.owner.id for e in result.executors] == [1], "пул остался прежним"
	# человек дал ссылку — тем же путём доводим дело до конца
	result = await service.add_executor(
		community_id, LaneOwner(OwnerKind.USER, second), "https://t.me/+fromHuman"
	)
	assert result.outcome is JoinOutcome.JOINED
	assert gateway.joined_by_link == [(second, "https://t.me/+fromHuman")]


async def test_pool_invites_when_there_is_no_link(db: Database) -> None:
	"""Ссылки нет, но у нас есть админ с правом приглашать — приглашаем сами.

	Ступень идёт до вопроса человеку: спрашивать, пока остаются
	автоматические пути, значит звать его зря.
	"""
	service, gateway, community_id, second = await _member_service(db)
	gateway.username = None
	await _make_private(db, community_id)
	await _set_username(db, second, "second")  # без @имени приглашать некого
	gateway.outsiders = {second}
	gateway.known_link = None
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert result.outcome is JoinOutcome.INVITED
	assert gateway.invited == [(1, "@second")], "пригласил администратор из пула"


async def test_bot_is_invited_to_group_and_promoted_in_channel(db: Database) -> None:
	"""Бот сам вступить не может: в группу его приглашают, в канал — назначают.

	В канале участником бот не бывает вовсе, поэтому ввод там — это
	назначение администратором, и права называются заранее.
	"""
	service, gateway, community_id, _second = await _member_service(db)
	bot_id = await _make_bot(db)
	gateway.bot_inside = False
	result = await service.add_executor(community_id, LaneOwner(OwnerKind.BOT, bot_id))
	assert result.outcome is JoinOutcome.PROMOTED
	account_id, target, rights = gateway.promoted[0]
	assert (account_id, target) == (1, "@test_bot")
	assert rights.post_messages and rights.edit_messages
	assert not rights.ban_users, "лишних прав боту не просим"


async def test_bot_without_username_is_refused_honestly(db: Database) -> None:
	"""Без @имени Telegram не найдёт, кого добавлять, — отказ с объяснением."""
	service, gateway, community_id, _second = await _member_service(db)
	bot_id = await _make_bot(db)
	gateway.bot_inside = False
	await _forget_bot_username(db, bot_id)
	with pytest.raises(CommunityError, match="не известно @имя"):
		await service.add_executor(community_id, LaneOwner(OwnerKind.BOT, bot_id))


async def test_remove_default_member_resets_default(db: Database) -> None:
	"""Удаление участника-умолчания сбрасывает умолчание без авто-замены."""
	service, _gateway, community_id, second = await _member_service(db)
	added = await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	assert len(added.executors) == 2
	first = next(e.owner for e in added.executors if e.is_default)
	remaining = await service.remove_executor(community_id, first)
	assert [e.label for e in remaining] == ["@second"]
	dto = next(c for c in await service.list_communities() if c.id == community_id)
	assert dto.default_account_id is None  # авто-выбора нет (ADR-0022)
	assert dto.userbot_assigned is False
	with pytest.raises(CommunityError, match="нет в пуле"):
		await service.remove_executor(community_id, first)


async def test_set_default_requires_membership(db: Database) -> None:
	"""Умолчанием может стать только участник сообщества."""
	service, _gateway, community_id, second = await _member_service(db)
	with pytest.raises(CommunityError, match="только исполнитель пула"):
		await service.set_default_publisher(community_id, LaneOwner(OwnerKind.USER, second))


async def test_recheck_updates_participation_and_keeps_the_pool(db: Database) -> None:
	"""Перепроверка обновляет участие, но пул не редеет (ADR-0035).

	Прежде подтверждённый отказ исключал участника из пула вместе
	с назначением. Теперь членство — факт: меняется участие, а решение
	убрать исполнителя остаётся за человеком.
	"""
	service, gateway, community_id, second = await _member_service(db)
	gateway.status = ParticipantStatus.MEMBER
	await service.add_executor(community_id, LaneOwner(OwnerKind.USER, second))
	# админов разжаловали в участники — участие обновится по зонду
	await service.recheck_community(community_id)
	executors = await service.list_executors(community_id)
	assert [(e.label, e.status) for e in executors] == [
		("@first", ParticipantStatus.MEMBER),
		("@second", ParticipantStatus.MEMBER),
	]
	# в канале участник публиковать не может — это видно, но пул цел
	access = await service.recheck_community(community_id)
	assert access.userbot_ok is False
	assert [e.label for e in await service.list_executors(community_id)] == ["@first", "@second"]
	assert access.community.default_account_id is not None
	assert access.community.publisher_incapable is True


async def test_bot_probe_separates_refusal_from_no_connection(db: Database) -> None:
	"""Обрыв связи не выдаётся за «бот потерял права».

	Подтверждённый отказ Telegram — знание о правах; отсутствие связи —
	отсутствие знания. Прежде обе причины давали одинаковый приговор,
	и человек видел «права потеряны» из-за пропавшей сети.
	"""
	from pxcontrol.engine.telegram.bot_api import BotError

	class _BrokenBotGateway(_FakeGateway):
		"""Бот-проверка падает заданной ошибкой; userbot отвечает как обычно."""

		def __init__(self, failure: Exception) -> None:
			super().__init__()
			self.failure = failure

		async def bot_check_community(self, bot: BotRef, chat_ref: str) -> CommunityInfo:
			raise self.failure

	bot_id = await _make_bot(db)
	refusal = CommunitiesService(db, _BrokenBotGateway(BotError("Бот не админ.")))
	dto = await CommunitiesService(db, _FakeGateway()).add_community(bot_id, "@testchan")
	assert (await refusal.recheck_community(dto.id)).bot_ok is False

	offline = CommunitiesService(db, _BrokenBotGateway(ConnectionError("нет сети")))
	assert (await offline.recheck_community(dto.id)).bot_ok is None


# --- приостановленные публикаторы (ADR-0029) --------------------------------------


async def _pause_account(db: Database, account_id: int, paused: bool = True) -> None:
	async with db.session_factory() as session:
		account = await session.get(TgAccount, account_id)
		assert account is not None
		account.paused = paused
		await session.commit()


async def test_dto_reports_paused_publisher(db: Database) -> None:
	"""Снимок сообщества знает о паузе: возможности без него, плашка — «приостановлен»."""
	gateway = _FakeGateway()
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	assert dto.capabilities.userbot and not dto.publisher_paused
	await _pause_account(db, account_id)
	dto = await service.get_community(dto.id)
	assert dto.default_account_paused is True
	assert dto.userbot_assigned is True, "назначение сохранено — лицо поста то же"
	assert not dto.capabilities.userbot, "но публиковать им сейчас нельзя"
	assert dto.publisher_paused is True, "публиковать некому именно из-за паузы"
	# с активным ботом действующий публикатор есть — паузы «нет»
	bot_id = await _make_bot(db)
	await service.add_executor(dto.id, LaneOwner(OwnerKind.BOT, bot_id))
	dto = await service.get_community(dto.id)
	assert dto.capabilities.bot and not dto.publisher_paused


async def test_recheck_skips_paused_members(db: Database) -> None:
	"""Перепроверка не зондирует приостановленных: их членство и роль не трогаются."""
	gateway = _FakeGateway()
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	dto = await service.add_community_via_userbot(account_id, "@testchan")
	await _pause_account(db, account_id)
	gateway.userbot_admins.clear()  # зонд ответил бы «отказ» и снял членство
	access = await service.recheck_community(dto.id)
	assert access.userbot_ok is None, "не проверяли — не утверждаем"
	assert access.community.default_account_id == account_id
	assert access.community.executors_count == 1


async def test_communities_of_account_and_bot(db: Database) -> None:
	"""Обратная сторона членств: сообщества аккаунта с ролью и умолчанием; сообщества бота."""
	gateway = _FakeGateway()
	account_id = await _make_account(db)
	gateway.userbot_admins.add(account_id)
	service = CommunitiesService(db, gateway)
	first = await service.add_community_via_userbot(account_id, "@testchan")
	bot_id = await _make_bot(db)
	memberships = await service.communities_of_account(account_id)
	assert [(m.community.id, m.status, m.is_default) for m in memberships] == [
		(first.id, ParticipantStatus.ADMIN, True)
	]
	assert await service.communities_of_account(999_999) == []
	await service.add_executor(first.id, LaneOwner(OwnerKind.BOT, bot_id))
	of_bot = await service.communities_of_bot(bot_id)
	assert [(m.community.id, m.is_default) for m in of_bot] == [(first.id, True)]
	assert await service.communities_of_bot(999_999) == []


async def test_bot_edit_right_stored_on_connect(db: Database) -> None:
	"""Право бота править чужие посты сохраняется при подключении (ADR-0031).

	Без него кнопки возможны только у постов, которые бот отправляет сам,
	поэтому признак нужен уже на подключении — форма спрашивает его,
	а не Telegram.
	"""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	gateway.bot_can_edit = True
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.capabilities.markup_edit is True
	executors = await service.list_executors(dto.id)
	bot_row = next(e for e in executors if e.owner.kind is OwnerKind.BOT)
	assert bot_row.rights.admin.edit_messages is True


async def test_bot_edit_right_absent_does_not_block_connect(db: Database) -> None:
	"""Отсутствие права не мешает подключению — только гасит маршрут кнопок."""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()  # право по умолчанию не выдано
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.capabilities.bot is True  # публиковать ботом можно
	assert dto.capabilities.markup_edit is False  # дорисовать кнопки — нельзя


async def test_recheck_updates_bot_edit_right_both_ways(db: Database) -> None:
	"""Право изменчиво: владелец канала выдаёт и отбирает его в любой момент."""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.capabilities.markup_edit is False
	gateway.bot_can_edit = True
	access = await service.recheck_community(dto.id)
	assert access.community.capabilities.markup_edit is True
	gateway.bot_can_edit = False
	access = await service.recheck_community(dto.id)
	assert access.community.capabilities.markup_edit is False


async def test_userbot_probe_does_not_clobber_bot_edit_right(db: Database) -> None:
	"""Права бота принадлежат его строке и чужим зондом не затираются.

	Прежде право жило колонкой сообщества, и userbot-зонд мог бы стереть
	подтверждённое ботом — кнопки «терялись» бы без причины. С ADR-0035
	у каждого исполнителя своя строка, и правило держит сама модель.
	"""

	class _BotUnreachable(_FakeGateway):
		async def bot_check_community(self, bot: BotRef, chat_ref: str) -> CommunityInfo:
			raise UserbotNotConnectedError("Нет связи с Telegram — проверьте сеть.")

	bot_id = await _make_bot(db)
	account_id = await _make_account(db)
	gateway = _FakeGateway()
	gateway.bot_can_edit = True
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.capabilities.markup_edit is True
	# бот недоступен, права принёс только userbot-зонд
	offline = _BotUnreachable()
	offline.userbot_admins = {account_id}
	offline.bot_can_edit = True
	service = CommunitiesService(db, offline)
	access = await service.recheck_community(dto.id)
	assert access.bot_ok is None  # знания о боте нет
	assert access.community.capabilities.markup_edit is True  # право не затёрто


async def test_removing_bot_takes_its_rights_with_it(db: Database) -> None:
	"""Права принадлежат паре «сообщество + этот бот», а не сообществу."""
	bot_id = await _make_bot(db)
	gateway = _FakeGateway()
	gateway.bot_can_edit = True
	service = CommunitiesService(db, gateway)
	dto = await service.add_community(bot_id, "@testchan")
	assert dto.capabilities.markup_edit is True
	await service.remove_executor(dto.id, LaneOwner(OwnerKind.BOT, bot_id))
	dto = await service.get_community(dto.id)
	assert dto.default_bot_id is None
	assert dto.capabilities.markup_edit is False
	assert await service.list_executors(dto.id) == []
	assert dto.capabilities.markup_edit is False
