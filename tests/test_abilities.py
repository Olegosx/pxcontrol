"""Тесты правила «что исполнитель может» (ADR-0035, этап E).

Одна точка на весь движок и интерфейс: возможности сообщества, подбор
того, кто введёт нового исполнителя, выбор исполнителя обслуживания.
Пока правило жило россыпью предикатов, каждый новый вопрос заводил свой,
и разойтись им было нечем помешать.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.services.abilities import (
	ACTION_WORDS,
	BOT_ADMIN_RIGHTS,
	ExecutorAction,
	can,
)
from pxcontrol.engine.telegram.rights import (
	ALL_ADMIN_RIGHTS,
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	MemberRights,
	ParticipantStatus,
)
from pxcontrol.engine.telegram.types import CommunityKind

CHANNEL = CommunityKind.CHANNEL
GROUP = CommunityKind.GROUP


def admin(**flags: bool) -> ExecutorRights:
	"""Администратор с названными правами (остальные не выданы)."""
	return ExecutorRights(ParticipantStatus.ADMIN, AdminRights(**flags), ALL_MEMBER_RIGHTS)


def member(**allowed: bool) -> ExecutorRights:
	"""Участник с названными разрешениями."""
	return ExecutorRights(ParticipantStatus.MEMBER, AdminRights(), MemberRights(**allowed))


def test_publishing_differs_by_kind_not_by_our_choice() -> None:
	"""В канале публикует админ с правом, в группе — любой неограниченный.

	Различает их Telegram, а не приложение: права ``post_messages``
	в группах не существует вовсе.
	"""
	assert can(admin(post_messages=True), ExecutorAction.PUBLISH, CHANNEL)
	assert not can(admin(), ExecutorAction.PUBLISH, CHANNEL)
	assert not can(member(send_plain=True), ExecutorAction.PUBLISH, CHANNEL)
	# в группе хватает разрешения писать; админу ограничения не мешают
	assert can(member(send_plain=True), ExecutorAction.PUBLISH, GROUP)
	assert can(admin(), ExecutorAction.PUBLISH, GROUP)
	assert not can(member(), ExecutorAction.PUBLISH, GROUP)


def test_admin_rights_are_asked_by_name_not_by_role() -> None:
	"""Роль сама по себе ничего не гарантирует: право спрашивается поимённо."""
	assert can(admin(delete_messages=True), ExecutorAction.DELETE_OTHERS, GROUP)
	assert not can(admin(), ExecutorAction.DELETE_OTHERS, GROUP)
	assert can(admin(ban_users=True), ExecutorAction.BAN, GROUP)
	assert not can(admin(), ExecutorAction.BAN, GROUP)
	assert can(admin(edit_messages=True), ExecutorAction.EDIT_OTHERS, CHANNEL)
	assert can(admin(add_admins=True), ExecutorAction.PROMOTE, CHANNEL)
	# участнику эти действия недоступны, что бы ему ни разрешили
	assert not can(member(send_plain=True), ExecutorAction.DELETE_OTHERS, GROUP)


def test_inviting_is_allowed_to_ordinary_members_too() -> None:
	"""Приглашать может и участник — если сообщество ему это оставило."""
	assert can(member(invite_users=True), ExecutorAction.INVITE, GROUP)
	assert not can(member(), ExecutorAction.INVITE, GROUP)
	assert can(admin(invite_users=True), ExecutorAction.INVITE, GROUP)
	assert not can(admin(), ExecutorAction.INVITE, GROUP), "админу право тоже выдают поимённо"
	# ссылку-приглашение Telegram показывает только администратору
	# (живая проверка 20.09.2026) — участник с правом приглашать её не видит
	assert can(admin(invite_users=True), ExecutorAction.INVITE_LINK, GROUP)
	assert not can(admin(), ExecutorAction.INVITE_LINK, GROUP)
	assert not can(member(invite_users=True), ExecutorAction.INVITE_LINK, GROUP)


def test_reading_needs_only_membership() -> None:
	"""Ленту и участников видит тот, кто состоит: отдельного права нет."""
	assert can(member(), ExecutorAction.READ_HISTORY, GROUP)
	assert not can(ExecutorRights(ParticipantStatus.LEFT), ExecutorAction.READ_HISTORY, GROUP)
	assert not can(
		ExecutorRights(ParticipantStatus.REQUESTED), ExecutorAction.READ_HISTORY, GROUP
	), "заявка участием не считается"


def test_creator_may_everything_everywhere() -> None:
	"""Владельцу можно всё: в снимке ему выданы все права (ADR-0035)."""
	owner = ExecutorRights(ParticipantStatus.CREATOR, ALL_ADMIN_RIGHTS, ALL_MEMBER_RIGHTS)
	for action in ExecutorAction:
		assert can(owner, action, CHANNEL), action
		assert can(owner, action, GROUP), action


def test_outsider_can_do_nothing() -> None:
	"""Не состоит — не может ничего, какое бы действие ни спросили."""
	for status in (ParticipantStatus.LEFT, ParticipantStatus.BANNED):
		rights = ExecutorRights(status)
		for action in ExecutorAction:
			assert not can(rights, action, CHANNEL), (status, action)


def test_every_action_has_a_word_for_the_human() -> None:
	"""У каждого действия есть человеческое имя: его показывают в отказе."""
	assert set(ACTION_WORDS) == set(ExecutorAction)


def test_bot_admin_rights_ask_no_more_than_needed() -> None:
	"""Вводя бота в канал, просим ровно два права — и ни одного лишнего."""
	assert BOT_ADMIN_RIGHTS.post_messages and BOT_ADMIN_RIGHTS.edit_messages
	assert not BOT_ADMIN_RIGHTS.ban_users
	assert not BOT_ADMIN_RIGHTS.add_admins
	assert not BOT_ADMIN_RIGHTS.delete_messages


@pytest.mark.parametrize("action", list(ExecutorAction))
def test_rule_is_total(action: ExecutorAction) -> None:
	"""На любое действие правило отвечает, а не падает: ветка есть у каждого."""
	assert can(admin(), action, CHANNEL) in (True, False)


def test_publishing_as_community_needs_anonymity_in_groups() -> None:
	"""От имени группы публикует только анонимный администратор; в канале — как обычно."""
	action = ExecutorAction.PUBLISH_AS_COMMUNITY
	assert can(admin(post_messages=True), action, CHANNEL)
	assert not can(admin(anonymous=True), action, CHANNEL), "в канале нужно право публиковать"
	assert can(admin(anonymous=True), action, GROUP)
	assert not can(admin(), action, GROUP), "неанонимный администратор пишет от себя"
	assert not can(member(send_plain=True), action, GROUP), "участник от имени группы не пишет"
	# обычная публикация в группе участнику по-прежнему доступна
	assert can(member(send_plain=True), ExecutorAction.PUBLISH, GROUP)


def test_reacting_is_a_member_permission() -> None:
	"""Реакции — разрешение участника (ADR-0039): администратору всегда, участнику по флагу."""
	assert can(admin(), ExecutorAction.REACT, CHANNEL)
	assert can(member(send_reactions=True), ExecutorAction.REACT, GROUP)
	assert not can(member(send_plain=True), ExecutorAction.REACT, GROUP)
	left = ExecutorRights(ParticipantStatus.LEFT, AdminRights(), MemberRights(send_reactions=True))
	assert not can(left, ExecutorAction.REACT, CHANNEL)
	assert ExecutorAction.REACT in ACTION_WORDS
