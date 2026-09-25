"""Настройки сообщества через MTProto (ADR-0043): чтение, запись, отказы — без сети.

Ответы сервера собираются из настоящих типов Telethon там, где разбор
смотрит на имя типа (реакции, фото, ограничения), и из простых объектов
там, где читаются только поля. Клиент — подставной: он записывает запросы
и по заказу бросает ошибки сервера.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
from telethon.errors import rpc_message_to_error
from telethon.tl import functions
from telethon.tl.types import (
	ChatBannedRights,
	ChatPhotoEmpty,
	ChatReactionsAll,
	ChatReactionsNone,
	ChatReactionsSome,
	InputChannelEmpty,
	InputChatPhotoEmpty,
	InputChatUploadedPhoto,
	JsonNumber,
	JsonObject,
	JsonObjectValue,
	JsonString,
	ReactionCustomEmoji,
	ReactionEmoji,
	RpcError,
)

from pxcontrol.engine.community_settings.catalog import CATALOG
from pxcontrol.engine.community_settings.model import (
	SIGNATURE_NAMES,
	SIGNATURE_OFF,
	SIGNATURE_PROFILES,
	LinkedChat,
	PhotoValue,
	ReactionsValue,
	SettingChange,
)
from pxcontrol.engine.telegram.mtproto import (
	MtprotoTransport,
	UserbotFloodError,
	UserbotSettingRefusedError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.mtproto_settings import (
	READERS,
	WRITERS,
	RawSettings,
	WriteTarget,
	discussion_groups,
	json_value,
	read_settings,
	rpc_code,
	standard_reactions,
)
from pxcontrol.engine.telegram.rights import ALL_MEMBER_RIGHTS
from pxcontrol.engine.telegram.types import ChatReactionsMode, CommunityKind

# --- подставной клиент ----------------------------------------------------------------


class FakeClient:
	"""Клиент Telethon: записывает запросы, бросает заказанную ошибку."""

	def __init__(self, error: BaseException | None = None) -> None:
		self.requests: list[Any] = []
		self.error = error

	async def __call__(self, request: Any) -> Any:
		self.requests.append(request)
		if self.error is not None:
			raise self.error
		return True

	async def upload_file(self, path: str) -> str:
		return f"загружено:{path}"


async def _resolve(chat_id: str) -> str:
	return f"сущность:{chat_id}"


def target(client: FakeClient) -> WriteTarget:
	return WriteTarget(client, "сообщество", _resolve)


# --- полнота таблиц -------------------------------------------------------------------


def test_every_catalog_setting_is_read_and_written_by_userbot() -> None:
	"""Новая настройка каталога без строки в таблицах транспорта ловится здесь."""
	keys = {spec.key for spec in CATALOG}
	assert keys == set(READERS), "таблица чтения разошлась с каталогом"
	assert keys == set(WRITERS), "таблица записи разошлась с каталогом"


# --- чтение ---------------------------------------------------------------------------


def raw(**overrides: Any) -> RawSettings:
	"""Ответ сервера по умолчанию: «пустая» группа, всё выключено."""
	channel = SimpleNamespace(
		title="Группа",
		username=None,
		photo=ChatPhotoEmpty(),
		noforwards=False,
		join_request=False,
		join_to_send=False,
		signatures=False,
		signature_profiles=False,
		autotranslation=False,
		forum=False,
		level=None,
		default_banned_rights=ChatBannedRights(until_date=None, edit_rank=True),
	)
	full = SimpleNamespace(
		about=None,
		ttl_period=None,
		slowmode_seconds=None,
		hidden_prehistory=False,
		participants_hidden=False,
		antispam=False,
		linked_chat_id=None,
		participants_count=2,
		can_set_username=True,
		available_reactions=ChatReactionsAll(),
		reactions_limit=None,
	)
	for key, value in overrides.items():
		holder = channel if hasattr(channel, key) else full
		setattr(holder, key, value)
	config = {"hidden_members_group_size_min": 100.0, "reactions_uniq_max": 11.0}
	return RawSettings(channel, full, {}, config, ("👍", "🔥"))


def test_read_settings_values_and_context() -> None:
	"""Значения — по таблице чтения, контекст — из ответа и конфигурации сервера."""
	settings = read_settings(raw(), CommunityKind.GROUP)
	values = settings.values
	assert values["title"] == "Группа" and values["about"] == "" and values["username"] == ""
	assert values["photo"] == PhotoValue(False)
	assert values["ttl"] == 0 and values["slowmode"] == 0
	assert values["reactions"] == ReactionsValue(ChatReactionsMode.ALL)
	assert values["permissions"] == replace(ALL_MEMBER_RIGHTS, edit_rank=False)
	assert values["linked_chat"] == LinkedChat(None)
	context = settings.context
	assert context.kind is CommunityKind.GROUP
	assert context.hidden_members_min == 100 and context.reactions_max == 11
	assert context.participants == 2 and context.can_set_username is True
	assert context.reaction_catalog == ("👍", "🔥")
	assert context.autotranslation_level_min is None, "нет в конфигурации — не выдумываем"


def test_read_signatures_three_states() -> None:
	assert read_settings(raw(), CommunityKind.CHANNEL).values["signatures"] == SIGNATURE_OFF
	names = raw(signatures=True)
	assert read_settings(names, CommunityKind.CHANNEL).values["signatures"] == SIGNATURE_NAMES
	profiles = raw(signatures=True, signature_profiles=True)
	assert read_settings(profiles, CommunityKind.CHANNEL).values["signatures"] == SIGNATURE_PROFILES


def test_read_reactions_modes() -> None:
	"""Выбранные — только стандартные эмодзи; нет поля — реакции запрещены."""
	some = ChatReactionsSome(reactions=[ReactionEmoji("👍"), ReactionCustomEmoji(5)])
	value = read_settings(raw(available_reactions=some, reactions_limit=3), CommunityKind.GROUP)
	assert value.values["reactions"] == ReactionsValue(ChatReactionsMode.SOME, ("👍",), 3)
	none = read_settings(raw(available_reactions=ChatReactionsNone()), CommunityKind.GROUP)
	assert none.values["reactions"] == ReactionsValue(ChatReactionsMode.NONE)
	missing = read_settings(raw(available_reactions=None), CommunityKind.GROUP)
	assert missing.values["reactions"] == ReactionsValue(ChatReactionsMode.NONE)


def test_read_linked_chat_with_title() -> None:
	"""Связанное сообщество — в формате Bot API и с названием из ответа."""
	source = raw(linked_chat_id=4418473186)
	source = replace(source, chats={4418473186: SimpleNamespace(title="Обсуждение")})
	settings = read_settings(source, CommunityKind.CHANNEL)
	assert settings.values["linked_chat"] == LinkedChat("-1004418473186", "Обсуждение")
	assert settings.context.linked_chat_id == "-1004418473186"


# --- запись ---------------------------------------------------------------------------


async def test_toggle_and_simple_writers_build_requests() -> None:
	client = FakeClient()
	await WRITERS["title"](target(client), "Новое")
	await WRITERS["about"](target(client), "")
	await WRITERS["username"](target(client), "@my_group")
	await WRITERS["noforwards"](target(client), True)
	await WRITERS["slowmode"](target(client), 30)
	await WRITERS["ttl"](target(client), 86400)
	await WRITERS["forum"](target(client), True)
	title, about, username, noforwards, slowmode, ttl, forum = client.requests
	assert isinstance(title, functions.channels.EditTitleRequest) and title.title == "Новое"
	assert isinstance(about, functions.messages.EditChatAboutRequest) and about.about == ""
	assert username.username == "my_group", "собака отрезается"
	assert isinstance(noforwards, functions.messages.ToggleNoForwardsRequest) and noforwards.enabled
	assert slowmode.seconds == 30 and ttl.period == 86400
	assert forum.enabled and forum.tabs is False


async def test_signatures_writer_sends_both_flags() -> None:
	client = FakeClient()
	for mode in (SIGNATURE_OFF, SIGNATURE_NAMES, SIGNATURE_PROFILES):
		await WRITERS["signatures"](target(client), mode)
	pairs = [(r.signatures_enabled, r.profiles_enabled) for r in client.requests]
	assert pairs == [(False, False), (True, False), (True, True)]


async def test_reactions_writer_modes() -> None:
	client = FakeClient()
	await WRITERS["reactions"](target(client), ReactionsValue(ChatReactionsMode.ALL, limit=5))
	await WRITERS["reactions"](target(client), ReactionsValue(ChatReactionsMode.SOME, ("👍",)))
	await WRITERS["reactions"](target(client), ReactionsValue(ChatReactionsMode.NONE))
	every, some, none = client.requests
	assert isinstance(every.available_reactions, ChatReactionsAll) and every.reactions_limit == 5
	assert [r.emoticon for r in some.available_reactions.reactions] == ["👍"]
	assert isinstance(none.available_reactions, ChatReactionsNone)


async def test_permissions_writer_sends_granular_bans_only() -> None:
	"""Запреты по видам, без зонтиков: send_media сервер ставит сам (проба)."""
	client = FakeClient()
	allowed = replace(ALL_MEMBER_RIGHTS, send_polls=False)
	await WRITERS["permissions"](target(client), allowed)
	(request,) = client.requests
	banned = request.banned_rights
	assert banned.send_polls and not banned.send_plain
	assert not banned.send_media and not banned.send_messages


async def test_photo_and_linked_chat_writers() -> None:
	client = FakeClient()
	await WRITERS["photo"](target(client), PhotoValue(True, "/x/logo.jpg"))
	await WRITERS["photo"](target(client), PhotoValue(False))
	await WRITERS["linked_chat"](target(client), LinkedChat("-1004418473186"))
	await WRITERS["linked_chat"](target(client), LinkedChat(None))
	upload, remove, link, unlink = client.requests
	assert isinstance(upload.photo, InputChatUploadedPhoto)
	assert upload.photo.file == "загружено:/x/logo.jpg"
	assert isinstance(remove.photo, InputChatPhotoEmpty)
	assert link.group == "сущность:-1004418473186"
	assert isinstance(unlink.group, InputChannelEmpty)


async def test_writer_rejects_wrong_value_kind() -> None:
	with pytest.raises(TypeError):
		await WRITERS["slowmode"](target(FakeClient()), True)


# --- отказы сервера -------------------------------------------------------------------


def test_rpc_code_for_known_and_unknown_errors() -> None:
	"""Код сервера — и у знакомых Telethon ошибок (там message пуст), и у незнакомых."""
	assert rpc_code(errors.ChatNotModifiedError(request=None)) == "CHAT_NOT_MODIFIED"
	assert rpc_code(errors.UsernameOccupiedError(request=None)) == "USERNAME_OCCUPIED"
	unknown = rpc_message_to_error(RpcError(400, "DISCUSSION_CHAT_REQUIRED"), None)
	assert rpc_code(unknown) == "DISCUSSION_CHAT_REQUIRED"
	assert rpc_code(ValueError("не сервер")) == ""


def transport_with(client: FakeClient, monkeypatch: pytest.MonkeyPatch) -> MtprotoTransport:
	transport = MtprotoTransport()

	async def client_and_entity(_chat_id: str) -> tuple[Any, Any]:
		return client, "сообщество"

	monkeypatch.setattr(transport, "_client_and_entity", client_and_entity)
	return transport


async def test_apply_setting_not_modified_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
	client = FakeClient(errors.ChatNotModifiedError(request=None))
	transport = transport_with(client, monkeypatch)
	await transport.apply_setting("-100", SettingChange("title", "То же"))
	assert len(client.requests) == 1


async def test_apply_setting_refusal_has_human_text(monkeypatch: pytest.MonkeyPatch) -> None:
	error = rpc_message_to_error(RpcError(400, "DISCUSSION_CHAT_REQUIRED"), None)
	transport = transport_with(FakeClient(error), monkeypatch)
	with pytest.raises(UserbotSettingRefusedError, match="группе обсуждения"):
		await transport.apply_setting("-100", SettingChange("join_to_send", True))
	occupied = transport_with(FakeClient(errors.UsernameOccupiedError(request=None)), monkeypatch)
	with pytest.raises(UserbotSettingRefusedError, match="занято"):
		await occupied.apply_setting("-100", SettingChange("username", "busy"))


async def test_apply_setting_flood_goes_to_general_translator(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Флуд-лимит — не отказ в правке: его разбирает общий переводчик транспорта."""
	transport = transport_with(
		FakeClient(errors.FloodWaitError(request=None, capture=5)), monkeypatch
	)
	with pytest.raises(UserbotFloodError):
		await transport.apply_setting("-100", SettingChange("ttl", 86400))


async def test_apply_unknown_setting_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
	transport = transport_with(FakeClient(), monkeypatch)
	with pytest.raises(UserbotUnavailableError, match="не записывает"):
		await transport.apply_setting("-100", SettingChange("нет такого", True))


# --- справочники сервера --------------------------------------------------------------


def test_json_value_and_catalogues() -> None:
	config = JsonObject(
		value=[
			JsonObjectValue("hidden_members_group_size_min", JsonNumber(100.0)),
			JsonObjectValue("translations_auto_enabled", JsonString("enabled")),
		]
	)
	assert json_value(config) == {
		"hidden_members_group_size_min": 100.0,
		"translations_auto_enabled": "enabled",
	}
	catalogue = SimpleNamespace(
		reactions=[
			SimpleNamespace(reaction="👍", inactive=False),
			SimpleNamespace(reaction="🦄", inactive=True),
		]
	)
	assert standard_reactions(catalogue) == ("👍",)
	from telethon.tl.types import Channel, Chat

	group = Channel.__new__(Channel)
	group.id, group.title = 4418473186, "Обсуждение"
	small = Chat.__new__(Chat)
	small.id, small.title = 7, "Малая"
	assert discussion_groups([group, small]) == [LinkedChat("-1004418473186", "Обсуждение")]
