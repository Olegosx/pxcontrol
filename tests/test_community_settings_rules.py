"""Каталог и правила настроек сообщества в Telegram (ADR-0043): без сети и базы."""

from __future__ import annotations

import pytest

from pxcontrol.engine.community_settings.catalog import (
	CATALOG,
	SPECS,
	applicable,
	spec_of,
)
from pxcontrol.engine.community_settings.model import (
	SECTION_TITLES,
	SIGNATURE_PROFILES,
	CommunitySettings,
	PhotoValue,
	ReactionsValue,
	SettingChange,
	SettingsContext,
	SettingValue,
	ValueKind,
)
from pxcontrol.engine.community_settings.rules import (
	BOT_CANNOT,
	availability,
	changes,
	value_problem,
)
from pxcontrol.engine.services.abilities import ExecutorAction, can
from pxcontrol.engine.telegram.rights import (
	ALL_ADMIN_RIGHTS,
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	MemberRights,
	ParticipantStatus,
)
from pxcontrol.engine.telegram.types import ChatReactionsMode, CommunityKind

CHANNEL = CommunityKind.CHANNEL
GROUP = CommunityKind.GROUP
ALL_KEYS = frozenset(SPECS)

OWNER = ExecutorRights(ParticipantStatus.CREATOR, ALL_ADMIN_RIGHTS, ALL_MEMBER_RIGHTS)


def admin(**granted: bool) -> ExecutorRights:
	"""Администратор с поимённо выданными правами."""
	return ExecutorRights(ParticipantStatus.ADMIN, AdminRights(**granted), ALL_MEMBER_RIGHTS)


def snapshot(
	kind: CommunityKind = GROUP, values: dict[str, SettingValue] | None = None, **context: object
) -> CommunitySettings:
	return CommunitySettings(values or {}, SettingsContext(kind, **context))  # type: ignore[arg-type]


# --- каталог ---------------------------------------------------------------------------


def test_catalog_is_consistent() -> None:
	"""Ключи уникальны, у выбора есть варианты, раздел подписан."""
	assert len(SPECS) == len(CATALOG)
	for spec in CATALOG:
		assert spec.kinds, spec.key
		assert spec.section in SECTION_TITLES, spec.key
		if spec.kind is ValueKind.CHOICE:
			assert spec.choices, spec.key
			assert len({c.value for c in spec.choices}) == len(spec.choices), spec.key


def test_applicable_by_kind() -> None:
	"""Канальное — только у канала, групповое — только у группы, общее — у обоих."""
	channel = {spec.key for spec in applicable(CHANNEL)}
	group = {spec.key for spec in applicable(GROUP)}
	assert {"signatures", "autotranslation", "linked_chat"} <= channel - group
	assert {"permissions", "slowmode", "forum", "join_to_send"} <= group - channel
	assert {"title", "about", "photo", "username", "noforwards", "reactions"} <= channel & group
	assert spec_of("нет такого") is None


# --- право на настройку ---------------------------------------------------------------------


def test_new_actions_follow_telegram_rights() -> None:
	"""Менять информацию и ограничивать — права поимённо; решения владельца — только ему."""
	assert can(admin(change_info=True), ExecutorAction.CHANGE_INFO, CHANNEL)
	assert not can(admin(ban_users=True), ExecutorAction.CHANGE_INFO, CHANNEL)
	assert can(admin(ban_users=True), ExecutorAction.RESTRICT_MEMBERS, GROUP)
	assert not can(admin(change_info=True), ExecutorAction.RESTRICT_MEMBERS, GROUP)
	full_admin = ExecutorRights(ParticipantStatus.ADMIN, ALL_ADMIN_RIGHTS, ALL_MEMBER_RIGHTS)
	assert not can(full_admin, ExecutorAction.OWN, GROUP), "все права админа — ещё не владелец"
	assert can(OWNER, ExecutorAction.OWN, GROUP)
	# участнику с разрешением «менять информацию» экран не рассчитывает на него
	member = ExecutorRights(ParticipantStatus.MEMBER, allowed=MemberRights(change_info=True))
	assert not can(member, ExecutorAction.CHANGE_INFO, GROUP)


# --- доступность ---------------------------------------------------------------------


def test_availability_order_transport_then_rights_then_telegram() -> None:
	"""Первая же преграда — причина: транспорт, потом право, потом условие."""
	spec = SPECS["forum"]
	linked = snapshot(linked_chat_id="-100500")
	assert availability(spec, linked, OWNER, set()).reason == BOT_CANNOT
	no_right = availability(spec, linked, admin(change_info=True), ALL_KEYS)
	assert not no_right.editable and "владелец" in (no_right.reason or "")
	blocked = availability(spec, linked, OWNER, ALL_KEYS)
	assert not blocked.editable and "обсуждения" in (blocked.reason or "")
	assert availability(spec, snapshot(), OWNER, ALL_KEYS).editable


@pytest.mark.parametrize(
	("key", "blocking", "free"),
	[
		("username", {"can_set_username": False}, {"can_set_username": True}),
		("join_to_send", {}, {"linked_chat_id": "-100500"}),
		("hidden_prehistory", {"linked_chat_id": "-100500"}, {}),
		(
			"participants_hidden",
			{"participants": 2, "hidden_members_min": 100},
			{"participants": 150, "hidden_members_min": 100},
		),
		(
			"autotranslation",
			{"boost_level": 1, "autotranslation_level_min": 3},
			{"boost_level": 3, "autotranslation_level_min": 3},
		),
	],
)
def test_telegram_conditions(
	key: str, blocking: dict[str, object], free: dict[str, object]
) -> None:
	"""Условия Telegram сверх прав (живая проба 25.09.2026) — преграды каталога."""
	spec = SPECS[key]
	kind = next(iter(spec.kinds))
	assert not availability(spec, snapshot(kind, **blocking), OWNER, ALL_KEYS).editable
	assert availability(spec, snapshot(kind, **free), OWNER, ALL_KEYS).editable


def test_antispam_needs_delete_right() -> None:
	"""Антиспам открывает право «удалять сообщения» (живая проба 25.09.2026)."""
	spec = SPECS["antispam"]
	group = snapshot()
	assert availability(spec, group, admin(delete_messages=True), ALL_KEYS).editable
	assert not availability(spec, group, admin(ban_users=True, change_info=True), ALL_KEYS).editable


def test_prehistory_blocked_by_forum_value() -> None:
	"""Преграда смотрит и на соседнюю настройку снимка: у форума история видна всем."""
	spec = SPECS["hidden_prehistory"]
	forum = snapshot(values={"forum": True})
	result = availability(spec, forum, OWNER, ALL_KEYS)
	assert not result.editable and "тем" in (result.reason or "")


def test_unknown_limits_do_not_block() -> None:
	"""Предел сервера не прочитан — преграды нет: последнее слово за сервером."""
	assert availability(
		SPECS["participants_hidden"], snapshot(participants=2), OWNER, ALL_KEYS
	).editable


# --- проверка значения ----------------------------------------------------------------


def test_value_problem_by_kind() -> None:
	"""Вид, длина, обязательность, вариант, реакции — отсекаются до Telegram."""
	assert value_problem(SPECS["noforwards"], True) is None
	assert value_problem(SPECS["noforwards"], "да") is not None
	assert value_problem(SPECS["slowmode"], True) is not None, "bool не число"
	assert value_problem(SPECS["slowmode"], 10) is None
	assert value_problem(SPECS["slowmode"], 7) is not None
	assert value_problem(SPECS["signatures"], SIGNATURE_PROFILES) is None
	assert value_problem(SPECS["about"], "я" * 255) is None
	assert "255" in (value_problem(SPECS["about"], "я" * 256) or "")
	assert value_problem(SPECS["title"], "  ") is not None
	assert value_problem(SPECS["about"], "") is None
	some = ReactionsValue(ChatReactionsMode.SOME)
	assert value_problem(SPECS["reactions"], some) is not None
	assert (
		value_problem(SPECS["reactions"], ReactionsValue(ChatReactionsMode.SOME, ("👍",))) is None
	)
	assert value_problem(SPECS["permissions"], MemberRights()) is None


# --- изменения ------------------------------------------------------------------------


def test_changes_in_catalog_order_only_differences() -> None:
	"""Только отличия, только прочитанное, порядок — каталога."""
	before = snapshot(values={"title": "A", "about": "", "slowmode": 0, "forum": False})
	after: dict[str, SettingValue] = {
		"forum": True,
		"title": "B",
		"about": "",
		"slowmode": 30,
		"noforwards": True,  # в снимке не было — транспорт не читал
	}
	assert changes(before, after) == [
		SettingChange("title", "B"),
		SettingChange("slowmode", 30),
		SettingChange("forum", True),
	]


def test_new_photo_is_always_a_change() -> None:
	"""Новую картинку сравнить не с чем — загрузка всегда изменение, «оставить» — нет."""
	before = snapshot(values={"photo": PhotoValue(True)})
	assert changes(before, {"photo": PhotoValue(True)}) == []
	upload = PhotoValue(True, "/x/new.jpg")
	assert changes(before, {"photo": upload}) == [SettingChange("photo", upload)]
	assert changes(before, {"photo": PhotoValue(False)}) == [
		SettingChange("photo", PhotoValue(False))
	]
