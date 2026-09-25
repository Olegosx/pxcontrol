"""Настройки сообщества через MTProto: чтение снимка и запись по ключам (ADR-0043).

Две таблицы по ключам каталога (:mod:`pxcontrol.engine.community_settings.catalog`):

- :data:`READERS` — как достать значение из ответа ``GetFullChannelRequest``
  (сущность ``Channel`` и ``ChannelFull`` одним запросом);
- :data:`WRITERS` — каким методом Telegram записать новое значение.

Добавить настройку = запись в каталоге + строка в каждой таблице;
полноту таблиц закрепляет тест. Плюс :data:`REFUSALS` — отказы Telegram
при правке, переведённые в текст для человека (новый отказ — одна строка).

Чтения — чистые функции над ответом (через ``getattr``, как весь разбор
транспорта), записи — короткие корутины над клиентом Telethon. Сама
операция (подключение, дорожка, перевод прочих ошибок) — в
:class:`~pxcontrol.engine.telegram.mtproto.MtprotoTransport`.

Факты — живая проба 25.09.2026 (``_misc/tg_settings_probe.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from pxcontrol.engine.community_settings.model import (
	SIGNATURE_NAMES,
	SIGNATURE_OFF,
	SIGNATURE_PROFILES,
	CommunitySettings,
	LinkedChat,
	PhotoValue,
	ReactionsValue,
	SettingsContext,
	SettingValue,
)
from pxcontrol.engine.telegram.refs import CHANNEL_ID_PREFIX
from pxcontrol.engine.telegram.rights import MemberRights, banned_flags, default_permissions
from pxcontrol.engine.telegram.types import ChatReactionsMode, CommunityKind


@dataclass(frozen=True)
class RawSettings:
	"""Всё, из чего читается снимок: ответ сервера и справочники.

	Attributes:
		channel: сущность ``Channel`` сообщества.
		full: ``ChannelFull`` того же ответа.
		chats: прочие сущности ответа по id (связанное сообщество).
		app_config: конфигурация приложения (``help.getAppConfig``) —
			пределы и условия сервера.
		reaction_catalog: стандартные реакции Telegram по порядку сервера.
	"""

	channel: Any
	full: Any
	chats: Mapping[int, Any]
	app_config: Mapping[str, Any]
	reaction_catalog: tuple[str, ...] = ()


@dataclass(frozen=True)
class WriteTarget:
	"""Куда писать: клиент Telethon, сущность сообщества и поиск чужих сущностей.

	``resolve`` находит сущность другого сообщества по id в формате
	Bot API (связанная группа обсуждения) — тем же способом, каким
	транспорт находит само сообщество.
	"""

	client: Any
	entity: Any
	resolve: Callable[[str], Awaitable[Any]]


Reader = Callable[[RawSettings], SettingValue]
Writer = Callable[[WriteTarget, SettingValue], Awaitable[None]]


# --- чтение ----------------------------------------------------------------------


def _bot_api_id(raw_id: int | None) -> str | None:
	"""Голый id канала Telethon → формат Bot API (-100…)."""
	return f"{CHANNEL_ID_PREFIX}{raw_id}" if raw_id else None


def _photo(raw: RawSettings) -> PhotoValue:
	photo = getattr(raw.channel, "photo", None)
	return PhotoValue(present=photo is not None and type(photo).__name__ != "ChatPhotoEmpty")


def _signatures(raw: RawSettings) -> str:
	if not getattr(raw.channel, "signatures", False):
		return SIGNATURE_OFF
	profiles = getattr(raw.channel, "signature_profiles", False)
	return SIGNATURE_PROFILES if profiles else SIGNATURE_NAMES


def _reactions(raw: RawSettings) -> ReactionsValue:
	"""Реакции сообщества; нет поля или «никаких» — запрещены.

	Пользовательские эмодзи-реакции (``ReactionCustomEmoji``) экран
	не правит: из выбранных берутся только стандартные эмодзи.
	"""
	allowed = getattr(raw.full, "available_reactions", None)
	limit = getattr(raw.full, "reactions_limit", None)
	kind = type(allowed).__name__
	if kind == "ChatReactionsAll":
		return ReactionsValue(ChatReactionsMode.ALL, limit=limit)
	if kind == "ChatReactionsSome":
		emojis = tuple(
			reaction.emoticon
			for reaction in getattr(allowed, "reactions", ())
			if type(reaction).__name__ == "ReactionEmoji"
		)
		return ReactionsValue(ChatReactionsMode.SOME, emojis, limit)
	return ReactionsValue(ChatReactionsMode.NONE, limit=limit)


def _linked(raw: RawSettings) -> LinkedChat:
	linked = getattr(raw.full, "linked_chat_id", None)
	title = getattr(raw.chats.get(linked), "title", None) if linked else None
	return LinkedChat(_bot_api_id(linked), title)


def _flag(source: str, name: str) -> Reader:
	"""Чтение признака сущности (``channel``) или полных сведений (``full``)."""

	def read(raw: RawSettings) -> bool:
		return bool(getattr(getattr(raw, source), name, False))

	return read


READERS: dict[str, Reader] = {
	"photo": _photo,
	"title": lambda raw: str(getattr(raw.channel, "title", "") or ""),
	"about": lambda raw: str(getattr(raw.full, "about", "") or ""),
	"username": lambda raw: str(getattr(raw.channel, "username", "") or ""),
	"noforwards": _flag("channel", "noforwards"),
	"join_request": _flag("channel", "join_request"),
	"join_to_send": _flag("channel", "join_to_send"),
	"signatures": _signatures,
	"reactions": _reactions,
	"ttl": lambda raw: int(getattr(raw.full, "ttl_period", 0) or 0),
	"autotranslation": _flag("channel", "autotranslation"),
	"linked_chat": _linked,
	"permissions": lambda raw: default_permissions(
		getattr(raw.channel, "default_banned_rights", None)
	),
	"slowmode": lambda raw: int(getattr(raw.full, "slowmode_seconds", 0) or 0),
	"hidden_prehistory": _flag("full", "hidden_prehistory"),
	"forum": _flag("channel", "forum"),
	"participants_hidden": _flag("full", "participants_hidden"),
	"antispam": _flag("full", "antispam"),
}


def _config_int(config: Mapping[str, Any], key: str) -> int | None:
	"""Целое из конфигурации приложения (числа там приходят дробными)."""
	value = config.get(key)
	return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def read_settings(raw: RawSettings, kind: CommunityKind) -> CommunitySettings:
	"""Снимок настроек из ответа сервера: значения и контекст сообщества.

	Читается всё, что транспорт умеет; какие из настроек есть у сообщества
	этого вида, решает каталог — отбор делает сервис (транспорт о каталоге
	не знает: он нижний слой).
	"""
	values = {key: read(raw) for key, read in READERS.items()}
	context = SettingsContext(
		kind=kind,
		linked_chat_id=_bot_api_id(getattr(raw.full, "linked_chat_id", None)),
		participants=getattr(raw.full, "participants_count", None),
		boost_level=getattr(raw.channel, "level", None),
		can_set_username=bool(getattr(raw.full, "can_set_username", False)),
		hidden_members_min=_config_int(raw.app_config, "hidden_members_group_size_min"),
		autotranslation_level_min=_config_int(raw.app_config, "channel_autotranslation_level_min"),
		reactions_max=_config_int(raw.app_config, "reactions_uniq_max"),
		reaction_catalog=raw.reaction_catalog,
	)
	return CommunitySettings(values, context)


# --- запись -------------------------------------------------------------------------


def _as_bool(value: SettingValue) -> bool:
	if not isinstance(value, bool):
		raise TypeError(f"ожидался переключатель, пришло {value!r}")
	return value


def _as_int(value: SettingValue) -> int:
	if not isinstance(value, int) or isinstance(value, bool):
		raise TypeError(f"ожидалось число, пришло {value!r}")
	return value


def _as_str(value: SettingValue) -> str:
	if not isinstance(value, str):
		raise TypeError(f"ожидался текст, пришло {value!r}")
	return value


def _toggle(request: Callable[[Any, bool], Any]) -> Writer:
	"""Запись переключателя одним запросом ``request(сущность, значение)``."""

	async def write(target: WriteTarget, value: SettingValue) -> None:
		await target.client(request(target.entity, _as_bool(value)))

	return write


async def _write_photo(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.channels import EditPhotoRequest
	from telethon.tl.types import InputChatPhotoEmpty, InputChatUploadedPhoto

	if not isinstance(value, PhotoValue):
		raise TypeError(f"ожидалось фото, пришло {value!r}")
	if value.upload is None:
		await target.client(EditPhotoRequest(target.entity, InputChatPhotoEmpty()))
		return
	uploaded = await target.client.upload_file(value.upload)
	await target.client(EditPhotoRequest(target.entity, InputChatUploadedPhoto(file=uploaded)))


async def _write_title(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.channels import EditTitleRequest

	await target.client(EditTitleRequest(target.entity, _as_str(value)))


async def _write_about(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.messages import EditChatAboutRequest

	await target.client(EditChatAboutRequest(target.entity, _as_str(value)))


async def _write_username(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.channels import UpdateUsernameRequest

	await target.client(UpdateUsernameRequest(target.entity, _as_str(value).lstrip("@")))


async def _write_signatures(target: WriteTarget, value: SettingValue) -> None:
	"""Подписи и ссылка на профиль — одним запросом (профиль без подписи не бывает)."""
	from telethon.tl.functions.channels import ToggleSignaturesRequest

	mode = _as_str(value)
	await target.client(
		ToggleSignaturesRequest(
			target.entity,
			signatures_enabled=mode != SIGNATURE_OFF,
			profiles_enabled=mode == SIGNATURE_PROFILES,
		)
	)


async def _write_reactions(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.messages import SetChatAvailableReactionsRequest
	from telethon.tl.types import (
		ChatReactionsAll,
		ChatReactionsNone,
		ChatReactionsSome,
		ReactionEmoji,
	)

	if not isinstance(value, ReactionsValue):
		raise TypeError(f"ожидались реакции, пришло {value!r}")
	allowed: Any
	if value.mode is ChatReactionsMode.ALL:
		allowed = ChatReactionsAll()
	elif value.mode is ChatReactionsMode.SOME:
		allowed = ChatReactionsSome(reactions=[ReactionEmoji(emoji) for emoji in value.emojis])
	else:
		allowed = ChatReactionsNone()
	await target.client(
		SetChatAvailableReactionsRequest(target.entity, allowed, reactions_limit=value.limit)
	)


async def _write_ttl(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.messages import SetHistoryTTLRequest

	await target.client(SetHistoryTTLRequest(target.entity, _as_int(value)))


async def _write_linked(target: WriteTarget, value: SettingValue) -> None:
	"""Группа обсуждения канала; ``chat_id=None`` — отвязать."""
	from telethon.tl.functions.channels import SetDiscussionGroupRequest
	from telethon.tl.types import InputChannelEmpty

	if not isinstance(value, LinkedChat):
		raise TypeError(f"ожидалось сообщество, пришло {value!r}")
	group = InputChannelEmpty() if value.chat_id is None else await target.resolve(value.chat_id)
	await target.client(SetDiscussionGroupRequest(broadcast=target.entity, group=group))


async def _write_permissions(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
	from telethon.tl.types import ChatBannedRights

	if not isinstance(value, MemberRights):
		raise TypeError(f"ожидались разрешения, пришло {value!r}")
	rights = ChatBannedRights(until_date=None, **banned_flags(value))
	await target.client(EditChatDefaultBannedRightsRequest(target.entity, rights))


async def _write_slowmode(target: WriteTarget, value: SettingValue) -> None:
	from telethon.tl.functions.channels import ToggleSlowModeRequest

	await target.client(ToggleSlowModeRequest(target.entity, _as_int(value)))


async def _write_forum(target: WriteTarget, value: SettingValue) -> None:
	"""Темы; вид списка тем — прежний (списком): вкладки экран пока не правит."""
	from telethon.tl.functions.channels import ToggleForumRequest

	await target.client(ToggleForumRequest(target.entity, _as_bool(value), tabs=False))


def _request(module: str, name: str) -> Callable[[Any, bool], Any]:
	"""Класс запроса Telethon по имени — импорт на первом вызове.

	Telethon в транспорте импортируется лениво (приложение без userbot
	не платит за загрузку библиотеки); таблица переключателей при этом
	должна строиться при импорте модуля.
	"""

	def build(entity: Any, enabled: bool) -> Any:
		from telethon.tl import functions

		return getattr(getattr(functions, module), name)(entity, enabled)

	return build


WRITERS: dict[str, Writer] = {
	"photo": _write_photo,
	"title": _write_title,
	"about": _write_about,
	"username": _write_username,
	"noforwards": _toggle(_request("messages", "ToggleNoForwardsRequest")),
	"join_request": _toggle(_request("channels", "ToggleJoinRequestRequest")),
	"join_to_send": _toggle(_request("channels", "ToggleJoinToSendRequest")),
	"signatures": _write_signatures,
	"reactions": _write_reactions,
	"ttl": _write_ttl,
	"autotranslation": _toggle(_request("channels", "ToggleAutotranslationRequest")),
	"linked_chat": _write_linked,
	"permissions": _write_permissions,
	"slowmode": _write_slowmode,
	"hidden_prehistory": _toggle(_request("channels", "TogglePreHistoryHiddenRequest")),
	"forum": _write_forum,
	"participants_hidden": _toggle(_request("channels", "ToggleParticipantsHiddenRequest")),
	"antispam": _toggle(_request("channels", "ToggleAntiSpamRequest")),
}


# --- отказы Telegram при правке -----------------------------------------------------------

#: Ответы «ничего не изменилось» — для правки это успех, а не отказ.
NOT_MODIFIED = frozenset({"CHAT_NOT_MODIFIED", "USERNAME_NOT_MODIFIED"})

#: Отказы Telegram при правке настроек → текст для человека. Код — строка
#: ошибки сервера (``RPCError.message``). Флуд-лимит и сессия сюда не входят:
#: их переводит общий переводчик транспорта.
REFUSALS: dict[str, str] = {
	"CHAT_ADMIN_REQUIRED": "Telegram отказал: у исполнителя нет нужного права администратора.",
	"RIGHT_FORBIDDEN": "Telegram отказал: у исполнителя нет нужного права администратора.",
	"CHAT_TITLE_EMPTY": "Название не может быть пустым.",
	"CHAT_ABOUT_TOO_LONG": "Описание длиннее, чем принимает Telegram (255 символов).",
	"USERNAME_INVALID": "Такое @имя Telegram не принимает — проверьте написание.",
	"USERNAME_OCCUPIED": "Это @имя уже занято.",
	"USERNAME_PURCHASE_AVAILABLE": (
		"Это @имя продаётся на fragment.com — занять его бесплатно нельзя."
	),
	"CHANNELS_ADMIN_PUBLIC_TOO_MUCH": (
		"Аккаунт уже администратор предельного числа публичных сообществ — "
		"сделайте какое-то из них частным."
	),
	"PARTICIPANTS_TOO_FEW": "Участников слишком мало, чтобы скрыть их список.",
	"BOOSTS_REQUIRED": "Нужен более высокий уровень бустов сообщества.",
	"DISCUSSION_CHAT_REQUIRED": "Это доступно только группе обсуждения канала.",
	"CHAT_LINK_EXISTS": "У группы обсуждения канала это не меняется.",
	"FORUM_ENABLED": "У группы с темами это не меняется — сначала выключите темы.",
	"CHAT_DISCUSSION_UNALLOWED": "Группу обсуждения канала нельзя сделать группой с темами.",
	"SECONDS_INVALID": "Такого значения медленного режима Telegram не принимает.",
	"TTL_PERIOD_INVALID": "Такого срока автоудаления Telegram не принимает.",
	"CHAT_PUBLIC_REQUIRED": "Это доступно только публичной группе.",
	"BROADCAST_ID_INVALID": "Канал для связи не найден.",
	"MEGAGROUP_ID_INVALID": "Группа обсуждения не найдена.",
	"LINK_NOT_MODIFIED": "Эта группа уже связана с каналом.",
	"REACTION_INVALID": "Telegram не принял одну из выбранных реакций.",
	"PHOTO_CROP_SIZE_SMALL": "Картинка слишком маленькая для фото сообщества.",
	"IMAGE_PROCESS_FAILED": "Telegram не смог обработать картинку.",
	"PHOTO_INVALID": "Эта картинка не годится для фото сообщества.",
}


def refusal_text(code: str) -> str | None:
	"""Текст отказа для человека по коду ошибки Telegram (None — не наш отказ)."""
	return REFUSALS.get(code)


@cache
def _codes_by_class() -> dict[type, str]:
	"""Обратный словарь Telethon: класс исключения → код ошибки сервера."""
	from telethon.errors.rpcerrorlist import rpc_errors_dict

	return {cls: code for code, cls in rpc_errors_dict.items()}


def rpc_code(exc: BaseException) -> str:
	"""Код ошибки сервера («USERNAME_OCCUPIED») у исключения Telethon.

	Telethon кладёт код в ``message`` только у незнакомых ему ошибок;
	у знакомых там общее «BAD_REQUEST», а код определяется самим классом
	(``rpc_errors_dict`` библиотеки, сверено на Telethon 1.44.0). Пустая
	строка — исключение не от сервера.
	"""
	known = _codes_by_class().get(type(exc))
	if known is not None:
		return known
	return str(getattr(exc, "message", "") or "")


# --- справочники сервера ---------------------------------------------------------------


def json_value(value: Any) -> Any:
	"""JSON Telegram (``JsonObject`` и родня) → обычные значения Python.

	Конфигурация приложения (``help.getAppConfig``) приходит деревом
	объектов схемы; разбор — по имени типа, как весь разбор транспорта.
	Незнакомый узел — None.
	"""
	name = type(value).__name__
	if name == "JsonObject":
		return {item.key: json_value(item.value) for item in value.value}
	if name == "JsonArray":
		return [json_value(item) for item in value.value]
	if name in ("JsonString", "JsonNumber", "JsonBool"):
		return value.value
	return None


def standard_reactions(catalogue: Any) -> tuple[str, ...]:
	"""Стандартные реакции Telegram из ``messages.getAvailableReactions``.

	Неактивные (снятые сервером) пропускаются: поставить их нельзя.
	"""
	return tuple(
		item.reaction
		for item in getattr(catalogue, "reactions", ())
		if not getattr(item, "inactive", False)
	)


def discussion_groups(chats: Any) -> list[LinkedChat]:
	"""Группы, пригодные для обсуждения канала (``channels.getGroupsForDiscussion``).

	Сервер сам отбирает годные; малые группы (не ``Channel``) пропускаются —
	приложение их не подключает (ADR-0021).
	"""
	return [
		LinkedChat(_bot_api_id(getattr(chat, "id", None)), getattr(chat, "title", None))
		for chat in chats
		if type(chat).__name__ == "Channel"
	]
