"""Сервис настроек сообщества в Telegram (ADR-0043): исполнитель, доступность, сохранение."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from pxcontrol.engine.community_settings.model import (
	CommunitySettings,
	LinkedChat,
	SettingChange,
	SettingsContext,
	SettingValue,
)
from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, CommunityExecutor, TgAccount
from pxcontrol.engine.services.communities import CommunitiesService
from pxcontrol.engine.services.community_settings import (
	NO_EXECUTOR,
	CommunitySettingsError,
	CommunitySettingsService,
	refusal,
)
from pxcontrol.engine.telegram.bot_api import BotError
from pxcontrol.engine.telegram.mtproto import (
	UserbotFloodError,
	UserbotNotConnectedError,
	UserbotSettingRefusedError,
)
from pxcontrol.engine.telegram.rights import (
	ALL_ADMIN_RIGHTS,
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	ParticipantStatus,
)
from pxcontrol.engine.telegram.types import BotRef, CommunityKind, OwnerKind

OWNER = ExecutorRights(ParticipantStatus.CREATOR, ALL_ADMIN_RIGHTS, ALL_MEMBER_RIGHTS)


@dataclass
class FakeGateway:
	"""Шлюз: отдаёт заданный снимок, записывает изменения, отказывает по заказу."""

	values: dict[str, SettingValue]
	errors: dict[str, Exception] = field(default_factory=dict)
	applied: list[tuple[str, SettingChange]] = field(default_factory=list)

	def _snapshot(self, kind: CommunityKind) -> CommunitySettings:
		return CommunitySettings(dict(self.values), SettingsContext(kind))

	async def userbot_community_settings(
		self, account_id: int, chat_id: str, kind: CommunityKind
	) -> CommunitySettings:
		return self._snapshot(kind)

	async def bot_community_settings(
		self, bot: BotRef, chat_id: str, kind: CommunityKind
	) -> CommunitySettings:
		return self._snapshot(kind)

	async def _apply(self, who: str, change: SettingChange) -> None:
		error = self.errors.get(change.key)
		if error is not None:
			raise error
		self.applied.append((who, change))
		self.values[change.key] = change.value

	async def userbot_apply_setting(
		self, account_id: int, chat_id: str, change: SettingChange
	) -> None:
		await self._apply("user", change)

	async def bot_apply_setting(self, bot: BotRef, chat_id: str, change: SettingChange) -> None:
		await self._apply("bot", change)

	async def userbot_discussion_candidates(self, account_id: int) -> list[LinkedChat]:
		return [LinkedChat("-1005", "Обсуждение")]


async def _community(
	db: Database,
	*,
	user: ExecutorRights | None = OWNER,
	user_paused: bool = False,
	with_bot: bool = False,
	kind: str = "group",
	chat_id: str = "-1001",
) -> int:
	"""Сообщество с публикатором-пользователем и (по желанию) ботом."""
	async with db.session_factory() as session:
		community = Community(title="Старое", tg_chat_id=chat_id, kind=kind)
		session.add(community)
		await session.flush()
		if user is not None:
			account = TgAccount(label="@ub", phone="+7900", session="s", paused=user_paused)
			session.add(account)
			await session.flush()
			community.default_tg_account_id = account.id
			session.add(_row(community.id, user, tg_account_id=account.id))
		if with_bot:
			bot = Bot(label="бот", token="123:abc")
			session.add(bot)
			await session.flush()
			community.default_bot_id = bot.id
			admin = ExecutorRights(ParticipantStatus.ADMIN, ALL_ADMIN_RIGHTS, ALL_MEMBER_RIGHTS)
			session.add(_row(community.id, admin, bot_id=bot.id))
		await session.commit()
		return community.id


def _row(community_id: int, rights: ExecutorRights, **owner: Any) -> CommunityExecutor:
	return CommunityExecutor(
		community_id=community_id,
		status=rights.status,
		rights=rights.to_payload(),
		checked_at=datetime.now(UTC),
		**owner,
	)


def _service(db: Database, gateway: FakeGateway) -> CommunitySettingsService:
	communities = CommunitiesService(db, gateway)  # type: ignore[arg-type]
	return CommunitySettingsService(db, gateway, communities)


GROUP_VALUES: dict[str, SettingValue] = {
	"title": "Старое",
	"about": "",
	"slowmode": 0,
	"forum": False,
	"join_to_send": False,
	"signatures": "off",  # канальная настройка — у группы её быть не должно
}


# --- чтение ---------------------------------------------------------------------------


async def test_open_without_executor_explains(db: Database) -> None:
	service = _service(db, FakeGateway({}))
	community_id = await _community(db, user=None)
	view = await service.open(community_id)
	assert view.reason == NO_EXECUTOR and view.settings is None


async def test_open_by_userbot_filters_kind_and_rates_access(db: Database) -> None:
	"""Показываются настройки этого вида из прочитанных; доступность — по правилам."""
	service = _service(db, FakeGateway(dict(GROUP_VALUES)))
	community_id = await _community(db)
	view = await service.open(community_id)
	assert view.executor_kind is OwnerKind.USER and view.executor_label
	keys = [spec.key for spec in view.specs]
	assert keys == ["title", "about", "join_to_send", "slowmode", "forum"], "порядок каталога"
	assert view.settings is not None and "signatures" not in view.settings.values
	assert view.access is not None
	assert view.access["forum"].editable, "владелец — темы можно"
	assert not view.access["join_to_send"].editable, "не группа обсуждения"


async def test_paused_user_falls_back_to_bot(db: Database) -> None:
	"""Приостановленный пользователь — правит бот, и только то, что умеет бот."""
	service = _service(db, FakeGateway(dict(GROUP_VALUES)))
	community_id = await _community(db, user_paused=True, with_bot=True)
	view = await service.open(community_id)
	assert view.executor_kind is OwnerKind.BOT
	assert view.access is not None
	assert view.access["title"].editable
	assert not view.access["slowmode"].editable, "бот не меняет медленный режим"


async def test_missing_community(db: Database) -> None:
	with pytest.raises(CommunitySettingsError, match="не найдено"):
		await _service(db, FakeGateway({})).open(999)


# --- сохранение --------------------------------------------------------------------------------


async def test_save_applies_in_order_and_updates_record(db: Database) -> None:
	"""Изменения — по порядку каталога; название уходит в запись сообщества."""
	gateway = FakeGateway(dict(GROUP_VALUES))
	service = _service(db, gateway)
	community_id = await _community(db)
	view = await service.open(community_id)
	assert view.settings is not None
	after = {**view.settings.values, "slowmode": 30, "title": "Новое"}
	saved = await service.save(community_id, view.settings, after)
	assert [change.key for _who, change in gateway.applied] == ["title", "slowmode"]
	assert saved.failed == () and saved.view is not None
	async with db.session_factory() as session:
		community = await session.get(Community, community_id)
		assert community is not None and community.title == "Новое"


async def test_refusal_does_not_stop_others(db: Database) -> None:
	gateway = FakeGateway(dict(GROUP_VALUES))
	gateway.errors["title"] = UserbotSettingRefusedError("Название не принято.")
	service = _service(db, gateway)
	community_id = await _community(db)
	view = await service.open(community_id)
	assert view.settings is not None
	saved = await service.save(
		community_id, view.settings, {**view.settings.values, "title": "Х", "slowmode": 10}
	)
	assert [(r.key, r.error) for r in saved.results] == [
		("title", "Название не принято."),
		("slowmode", None),
	]


async def test_flood_stops_the_rest_honestly(db: Database) -> None:
	"""Флуд-лимит останавливает отправку: у остальных — «не отправлено»."""
	gateway = FakeGateway(dict(GROUP_VALUES))
	gateway.errors["title"] = UserbotFloodError("Подождите.", retry_after_s=823)
	service = _service(db, gateway)
	community_id = await _community(db)
	view = await service.open(community_id)
	assert view.settings is not None
	after = {**view.settings.values, "title": "Х", "slowmode": 10, "forum": True}
	saved = await service.save(community_id, view.settings, after)
	assert gateway.applied == []
	errors = {r.key: r.error or "" for r in saved.results}
	assert "823" in errors["title"]
	assert errors["slowmode"].startswith("Не отправлено") and "823" in errors["forum"]


async def test_checks_before_telegram(db: Database) -> None:
	"""Негодное значение и отсутствие права не доходят до Telegram."""
	gateway = FakeGateway(dict(GROUP_VALUES))
	admin = ExecutorRights(
		ParticipantStatus.ADMIN, AdminRights(change_info=True), ALL_MEMBER_RIGHTS
	)
	service = _service(db, gateway)
	community_id = await _community(db, user=admin)
	view = await service.open(community_id)
	assert view.settings is not None
	after = {**view.settings.values, "slowmode": 7, "forum": True, "about": "ок"}
	saved = await service.save(community_id, view.settings, after)
	assert [change.key for _who, change in gateway.applied] == ["about"]
	errors = {r.key: r.error or "" for r in saved.results}
	assert "варианта" in errors["slowmode"] and "владелец" in errors["forum"]


async def test_save_without_executor_is_refused(db: Database) -> None:
	community_id = await _community(db, user=None)
	service = _service(db, FakeGateway({}))
	empty = CommunitySettings({}, SettingsContext(CommunityKind.GROUP))
	with pytest.raises(CommunitySettingsError):
		await service.save(community_id, empty, {})


# --- разбор отказов и обсуждение ------------------------------------------------------------------


def test_refusal_classification() -> None:
	assert refusal(UserbotSettingRefusedError("нет")) == ("нет", False)
	assert refusal(BotError("нет")) == ("нет", False)
	assert refusal(UserbotNotConnectedError("связи нет"))[1] is True
	assert refusal(ConnectionError("сеть"))[1] is True
	with pytest.raises(ValueError):
		refusal(ValueError("неожиданное"))


async def test_discussion_candidates_only_by_userbot(db: Database) -> None:
	gateway = FakeGateway({})
	service = _service(db, gateway)
	by_user = await _community(db, kind="channel")
	assert await service.discussion_candidates(by_user) == [LinkedChat("-1005", "Обсуждение")]
	by_bot = await _community(db, user=None, with_bot=True, kind="channel", chat_id="-1002")
	with pytest.raises(CommunitySettingsError, match="userbot"):
		await service.discussion_candidates(by_bot)


async def test_update_mutable_username_semantics(db: Database) -> None:
	"""@имя: пустая строка — имя сняли (в записи None), None — не трогать."""
	community_id = await _community(db, user=None)
	communities = CommunitiesService(db, FakeGateway({}))  # type: ignore[arg-type]
	assert await communities.update_mutable(community_id, username="public_name")
	assert not await communities.update_mutable(community_id, title=None, username=None)
	assert await communities.update_mutable(community_id, username="")
	async with db.session_factory() as session:
		community = await session.get(Community, community_id)
		assert community is not None and community.username is None
