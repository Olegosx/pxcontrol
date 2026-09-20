"""Тесты снимка прав исполнителя (ADR-0035).

Проверяются на **настоящих** объектах обеих библиотек, а не на подделках:
права — это то, что присылает Telegram, и ошибка перевода видна только
на его собственных типах. Тем же приёмом в проекте закреплено правило
про владельца (13.09.2026): библиотека отвечает о нём ровно то, что
прислал сервер, и неполный набор флагов у владельца — не «нельзя».

Отдельный замок — тесты полноты набора: наши права обязаны совпадать
с составом ``chatAdminRights`` и быть подмножеством ``chatBannedRights``.
Пока они зелёные, снимок не молчит о правах, которые Telegram уже знает.
"""

from __future__ import annotations

import inspect

from aiogram.types import (
	ChatMemberAdministrator,
	ChatMemberBanned,
	ChatMemberMember,
	ChatMemberOwner,
	ChatMemberRestricted,
	ChatPermissions,
	User,
)
from telethon.tl.custom.participantpermissions import ParticipantPermissions
from telethon.tl.types import (
	ChannelParticipantAdmin,
	ChannelParticipantBanned,
	ChannelParticipantCreator,
	ChannelParticipantLeft,
	ChannelParticipantSelf,
	ChatAdminRights,
	ChatBannedRights,
	PeerUser,
)

from pxcontrol.engine.telegram.rights import (
	AdminRights,
	ExecutorRights,
	MemberRights,
	ParticipantStatus,
	bot_rights,
	userbot_rights,
)

# --- вспомогательное: объекты библиотек -------------------------------------------


def perms(participant: object) -> ParticipantPermissions:
	"""Права участника супергруппы/канала по его записи участия."""
	return ParticipantPermissions(participant, False)


def admin_rights(**granted: bool) -> ChatAdminRights:
	"""Набор прав администратора MTProto: названное выдано, остальное нет."""
	names = [p for p in inspect.signature(ChatAdminRights.__init__).parameters if p != "self"]
	return ChatAdminRights(**{name: granted.get(name, False) for name in names})


def banned_rights(**forbidden: bool) -> ChatBannedRights:
	"""Набор ограничений MTProto: названное запрещено, остальное нет."""
	return ChatBannedRights(until_date=None, **forbidden)


def user(is_bot: bool = True) -> User:
	"""Учётка для ответов Bot API."""
	return User(id=42, is_bot=is_bot, first_name="Бот")


def chat_permissions(**allowed: bool) -> ChatPermissions:
	"""Общие права сообщества Bot API: названное разрешено, остальное нет."""
	names = list(ChatPermissions.model_fields)
	return ChatPermissions(**{name: allowed.get(name, False) for name in names})


# --- полнота набора ----------------------------------------------------------------


def test_admin_rights_cover_every_telegram_flag() -> None:
	"""Наши права администратора — ровно состав ``chatAdminRights``."""
	theirs = {p for p in inspect.signature(ChatAdminRights.__init__).parameters if p != "self"}
	ours = set(AdminRights.__dataclass_fields__)
	assert ours == theirs


def test_member_rights_are_named_as_telegram_restrictions() -> None:
	"""Разрешения участника названы именами ограничений Telegram.

	Смысл обратный (у нас «можно», у них «нельзя»), поэтому совпадать
	обязаны имена: так снимок читается рядом с документацией. Не в счёт
	только ``view_messages`` (им отличается исключённый от ограниченного)
	и срок ``until_date``.
	"""
	theirs = {p for p in inspect.signature(ChatBannedRights.__init__).parameters if p != "self"}
	ours = set(MemberRights.__dataclass_fields__)
	assert ours <= theirs
	assert theirs - ours == {"until_date", "view_messages", "send_messages", "send_media"}


# --- MTProto: статусы и права -------------------------------------------------------


def test_creator_may_everything_even_with_empty_flags() -> None:
	"""Владельцу можно всё, какие бы флаги ни прислал сервер.

	Тот самый случай 13.09.2026: свойства библиотеки отвечают о владельце
	ровно присланное, и неполный набор выглядел бы запретом.
	"""
	participant = ChannelParticipantCreator(user_id=1, admin_rights=admin_rights())
	assert perms(participant).ban_users is False  # так отвечает сама библиотека
	rights = userbot_rights(perms(participant), banned_rights(send_messages=True))
	assert rights.status is ParticipantStatus.CREATOR
	assert rights.admin.post_messages and rights.admin.delete_messages and rights.admin.ban_users
	assert rights.allowed.send_plain and rights.allowed.send_reactions


def test_admin_gets_exactly_granted_flags_and_ignores_restrictions() -> None:
	"""Администратору — выданное поимённо; ограничения участников его не трогают."""
	participant = ChannelParticipantAdmin(
		user_id=1,
		promoted_by=2,
		date=None,
		admin_rights=admin_rights(post_messages=True, delete_messages=True),
	)
	rights = userbot_rights(perms(participant), banned_rights(send_messages=True))
	assert rights.status is ParticipantStatus.ADMIN
	assert rights.admin.post_messages and rights.admin.delete_messages
	assert not rights.admin.ban_users and not rights.admin.add_admins
	assert rights.allowed.send_plain  # ограничения на администраторов не действуют


def test_member_obeys_community_restrictions_but_may_react() -> None:
	"""Участник: писать нельзя, а реакции — можно, если их не запрещали.

	Ровно то положение, ради которого заводятся исполнители без прав
	администратора (ADR-0035): в канале постит админ, а реакции ставит
	обычный участник.
	"""
	participant = ChannelParticipantSelf(user_id=1, inviter_id=2, date=None)
	rights = userbot_rights(perms(participant), banned_rights(send_messages=True))
	assert rights.status is ParticipantStatus.MEMBER
	assert not rights.allowed.send_plain and not rights.allowed.send_photos
	assert rights.allowed.send_reactions


def test_media_ban_covers_every_kind_of_attachment() -> None:
	"""Зонтик ``send_media`` закрывает все виды вложений, но не текст."""
	participant = ChannelParticipantSelf(user_id=1, inviter_id=2, date=None)
	rights = userbot_rights(perms(participant), banned_rights(send_media=True))
	assert rights.allowed.send_plain and rights.allowed.send_polls
	assert not rights.allowed.send_photos
	assert not rights.allowed.send_videos
	assert not rights.allowed.send_docs
	assert not rights.allowed.send_stickers


def test_personal_restrictions_add_to_community_ones() -> None:
	"""Ограниченный участник: свои запреты складываются с общими."""
	participant = ChannelParticipantBanned(
		peer=PeerUser(1),
		kicked_by=2,
		date=None,
		banned_rights=banned_rights(send_photos=True),
	)
	rights = userbot_rights(perms(participant), banned_rights(send_polls=True))
	assert rights.status is ParticipantStatus.RESTRICTED
	assert rights.allowed.send_plain
	assert not rights.allowed.send_photos  # личное ограничение
	assert not rights.allowed.send_polls  # общее ограничение


def test_left_and_banned_have_no_rights() -> None:
	"""Не состоит и исключён — прав нет, и это знание, а не пустота."""
	left = userbot_rights(perms(ChannelParticipantLeft(peer=PeerUser(1))))
	assert left.status is ParticipantStatus.LEFT
	assert not left.allowed.send_plain and not left.admin.post_messages
	kicked = userbot_rights(
		perms(
			ChannelParticipantBanned(
				peer=PeerUser(1),
				kicked_by=2,
				date=None,
				banned_rights=banned_rights(view_messages=True),
			)
		)
	)
	assert kicked.status is ParticipantStatus.BANNED
	assert not kicked.allowed.send_reactions


# --- Bot API: статусы и права -------------------------------------------------------


def test_bot_owner_may_everything() -> None:
	"""Бот-владелец сообщества: можно всё (ветка владельца общая)."""
	rights = bot_rights(ChatMemberOwner(user=user(), is_anonymous=False))
	assert rights.status is ParticipantStatus.CREATOR
	assert rights.admin.edit_messages and rights.allowed.send_reactions


def test_bot_admin_maps_every_flag_by_name() -> None:
	"""Права бота-администратора переводятся из имён Bot API в телеграмные."""
	member = ChatMemberAdministrator(
		user=user(),
		can_be_edited=False,
		is_anonymous=False,
		can_manage_chat=True,
		can_delete_messages=True,
		can_manage_video_chats=False,
		can_restrict_members=False,
		can_promote_members=False,
		can_change_info=False,
		can_invite_users=True,
		can_post_stories=False,
		can_edit_stories=False,
		can_delete_stories=False,
		can_post_messages=True,
		can_edit_messages=True,
		can_pin_messages=False,
	)
	rights = bot_rights(member)
	assert rights.status is ParticipantStatus.ADMIN
	assert rights.admin.post_messages and rights.admin.edit_messages
	assert rights.admin.delete_messages and rights.admin.other and rights.admin.invite_users
	assert not rights.admin.ban_users and not rights.admin.manage_call
	assert rights.allowed.send_plain  # администратор ограничениям не подчиняется


def test_bot_member_obeys_community_permissions() -> None:
	"""Бот-участник группы: можно то, что разрешено всем."""
	rights = bot_rights(
		ChatMemberMember(user=user()),
		chat_permissions(can_send_messages=True, can_react_to_messages=True),
	)
	assert rights.status is ParticipantStatus.MEMBER
	assert rights.allowed.send_plain and rights.allowed.send_reactions
	assert not rights.allowed.send_photos and not rights.allowed.send_polls


def test_bot_restricted_needs_both_layers() -> None:
	"""Ограниченному боту право нужно и лично, и в общих правах сообщества."""
	member = ChatMemberRestricted(
		user=user(),
		is_member=True,
		can_send_messages=True,
		can_send_audios=False,
		can_send_documents=False,
		can_send_photos=True,
		can_send_videos=False,
		can_send_video_notes=False,
		can_send_voice_notes=False,
		can_send_polls=False,
		can_send_other_messages=False,
		can_add_web_page_previews=False,
		can_react_to_messages=True,
		can_edit_tag=False,
		can_change_info=False,
		can_invite_users=False,
		can_pin_messages=False,
		can_manage_topics=False,
		until_date=0,
	)
	rights = bot_rights(member, chat_permissions(can_send_messages=True))
	assert rights.status is ParticipantStatus.RESTRICTED
	assert rights.allowed.send_plain  # разрешено на обоих слоях
	assert not rights.allowed.send_photos  # лично можно, а всем нельзя


def test_bot_kicked_and_left_are_out_of_community() -> None:
	"""Исключённый и вышедший бот — «не состоит», прав нет."""
	kicked = bot_rights(ChatMemberBanned(user=user(), until_date=0))
	assert kicked.status is ParticipantStatus.BANNED
	assert not kicked.status.in_community
	restricted_outside = ChatMemberRestricted(
		user=user(),
		is_member=False,
		can_send_messages=True,
		can_send_audios=False,
		can_send_documents=False,
		can_send_photos=False,
		can_send_videos=False,
		can_send_video_notes=False,
		can_send_voice_notes=False,
		can_send_polls=False,
		can_send_other_messages=False,
		can_add_web_page_previews=False,
		can_react_to_messages=True,
		can_edit_tag=False,
		can_change_info=False,
		can_invite_users=False,
		can_pin_messages=False,
		can_manage_topics=False,
		until_date=0,
	)
	assert bot_rights(restricted_outside).status is ParticipantStatus.LEFT


# --- согласие транспортов -----------------------------------------------------------


def test_both_transports_describe_the_same_position_alike() -> None:
	"""Об одном положении оба транспорта дают один снимок.

	Смысл разделения ADR-0035: выше границы транспорта не видно, кто
	принёс ответ. Если переводы разойдутся, разойдутся и решения о том,
	кому что поручить.
	"""
	by_userbot = userbot_rights(
		perms(ChannelParticipantSelf(user_id=1, inviter_id=2, date=None)),
		banned_rights(send_photos=True, send_polls=True),
	)
	by_bot = bot_rights(
		ChatMemberMember(user=user()),
		chat_permissions(
			can_send_messages=True,
			can_send_audios=True,
			can_send_documents=True,
			can_send_videos=True,
			can_send_video_notes=True,
			can_send_voice_notes=True,
			can_send_other_messages=True,
			can_add_web_page_previews=True,
			can_react_to_messages=True,
			can_change_info=True,
			can_invite_users=True,
			can_pin_messages=True,
			can_manage_topics=True,
			can_edit_tag=True,
		),
	)
	assert by_userbot == by_bot


# --- хранение ------------------------------------------------------------------------


def test_payload_keeps_only_granted_and_returns_them_back() -> None:
	"""Снимок ходит в базу и обратно без потерь; статус едет отдельно."""
	rights = ExecutorRights(
		ParticipantStatus.ADMIN,
		AdminRights(post_messages=True, delete_messages=True),
		MemberRights(send_plain=True, send_reactions=True),
	)
	payload = rights.to_payload()
	assert payload == {
		"admin": ["post_messages", "delete_messages"],
		"allowed": ["send_plain", "send_reactions"],
	}
	assert ExecutorRights.from_payload(ParticipantStatus.ADMIN, payload) == rights


def test_unknown_right_in_payload_is_ignored() -> None:
	"""Запись будущей версии читается, незнакомое право — мимо."""
	payload = {"admin": ["post_messages", "manage_hyperspace"], "allowed": []}
	rights = ExecutorRights.from_payload(ParticipantStatus.ADMIN, payload)
	assert rights.admin.post_messages
	assert not rights.allowed.send_plain


def test_missing_payload_means_snapshot_not_read_yet() -> None:
	"""Пустая колонка прав — «снимок ещё не делался», статус при этом известен."""
	rights = ExecutorRights.from_payload(ParticipantStatus.MEMBER, None)
	assert rights.status is ParticipantStatus.MEMBER
	assert rights.admin == AdminRights() and rights.allowed == MemberRights()
