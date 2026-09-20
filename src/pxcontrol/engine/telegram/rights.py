"""Права исполнителя в сообществе: один снимок поверх обоих транспортов (ADR-0035).

Единица знания — **снимок прав** одного исполнителя (пользователя или бота)
в одном сообществе: как он в нём участвует и что ему там можно. Снимок
складывает транспорт на границе, как он уже складывает вид сообщества
и признак форума; выше границы ответы Telegram не разбирает никто.

Telegram описывает права тремя слоями (https://core.telegram.org/api/rights):

- **права администратора** — что выдано поимённо (17 флагов
  ``chatAdminRights``);
- **личные ограничения участника** — что запрещено лично ему;
- **общие ограничения сообщества** — что запрещено всем.

Наружу отдаётся не эта кухня, а ответ на вопрос потребителя: *может ли
этот исполнитель сделать это*. Поэтому ограничения обоих слоёв
складываются здесь и хранятся **разрешениями** (что можно), а не
запретами (что нельзя): запрет — форма ответа Telegram, разрешение —
форма вопроса приложения.

Оба транспорта сообщают один и тот же состав (сверено 20.09.2026
по установленным библиотекам — Telethon 1.44.0 и aiogram 3.30.0):
семнадцать флагов администратора отображаются один в один, а разрешения
участника Bot API отдаёт целиком, включая реакции и теги. «Неизвестных»
прав поэтому нет, и трёхзначная логика («можно / нельзя / транспорт
не сообщает») не заводится: булево значение означает ровно то, что
сказал Telegram.

Модуль чистый: ни сети, ни базы, ни библиотечных типов — ответы читаются
через ``getattr``, как их и читал прежний разбор прав. Так правило
проверяется тестами целиком и живёт одной точкой, а не шестью функциями
в двух модулях транспортов, как было до ADR-0035.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, TypeVar


class ParticipantStatus(StrEnum):
	"""Как исполнитель участвует в сообществе — снимок из зонда прав.

	Наследник роли из ADR-0022 (``admin``/``member``): двух значений мало,
	потому что «не состоит» и «ограничен» — разные факты, и именно они
	нужны вводу исполнителя в сообщество (ADR-0035).
	"""

	CREATOR = "creator"  # владелец: права урезать невозможно
	ADMIN = "admin"  # администратор: права выданы поимённо
	MEMBER = "member"  # участник: действуют общие ограничения сообщества
	RESTRICTED = "restricted"  # участник с личными ограничениями
	LEFT = "left"  # не состоит
	BANNED = "banned"  # исключён и лишён доступа
	# заявка на вступление отправлена и ждёт одобрения администратора
	# (ADR-0035): исполнитель ещё не участник, но и не «не состоит» —
	# ход уже сделан, и человеку важно видеть разницу
	REQUESTED = "requested"

	@property
	def administers(self) -> bool:
		"""Управляет ли сообществом (владелец или администратор).

		Ограничения участников на них не действуют — правило Telegram,
		а не наше послабление.
		"""
		return self in (ParticipantStatus.CREATOR, ParticipantStatus.ADMIN)

	@property
	def in_community(self) -> bool:
		"""Состоит ли в сообществе сейчас (ограниченный — состоит).

		Отправленная заявка участием не считается: прав у заявителя нет
		до одобрения, и обращаться к сообществу от его имени рано.
		"""
		return self not in (
			ParticipantStatus.LEFT,
			ParticipantStatus.BANNED,
			ParticipantStatus.REQUESTED,
		)


@dataclass(frozen=True)
class AdminRights:
	"""Права администратора — все семнадцать флагов Telegram.

	Имена — телеграмные (``chatAdminRights``): так снимок читается рядом
	с документацией без перевода. У владельца выставлены все: урезать
	права владельца в Telegram невозможно, а библиотека об этом не знает
	и отвечает ровно то, что прислал сервер (проверено 13.09.2026).
	"""

	change_info: bool = False
	post_messages: bool = False
	edit_messages: bool = False
	delete_messages: bool = False
	ban_users: bool = False
	invite_users: bool = False
	pin_messages: bool = False
	add_admins: bool = False
	anonymous: bool = False
	manage_call: bool = False
	other: bool = False
	manage_topics: bool = False
	post_stories: bool = False
	edit_stories: bool = False
	delete_stories: bool = False
	manage_direct_messages: bool = False
	manage_ranks: bool = False


@dataclass(frozen=True)
class MemberRights:
	"""Что исполнителю разрешено как участнику — после сложения слоёв.

	Имена — телеграмные (``chatBannedRights``), но смысл обратный:
	``True`` значит «можно». Часть прав есть и здесь, и у администратора
	(``invite_users``, ``pin_messages``, ``change_info``, ``manage_topics``),
	и это не дубль: там это выданное право администратора, здесь —
	разрешение, оставленное обычному участнику.
	"""

	send_plain: bool = False  # текст
	send_photos: bool = False
	send_videos: bool = False
	send_roundvideos: bool = False  # видеосообщения-кружки
	send_audios: bool = False
	send_voices: bool = False
	send_docs: bool = False
	send_stickers: bool = False
	send_gifs: bool = False
	send_games: bool = False
	send_inline: bool = False  # ответы встроенных ботов
	embed_links: bool = False  # превью ссылок в своих сообщениях
	send_polls: bool = False
	send_reactions: bool = False
	invite_users: bool = False
	pin_messages: bool = False
	change_info: bool = False
	manage_topics: bool = False
	edit_rank: bool = False  # менять свой тег (звание) в сообществе


_Rights = TypeVar("_Rights", AdminRights, MemberRights)


def _all_true(kind: type[_Rights]) -> _Rights:
	"""Набор прав, где выдано всё (владелец и администратор)."""
	return kind(**{field.name: True for field in fields(kind)})


#: Права владельца и разрешения того, на кого ограничения не действуют.
ALL_ADMIN_RIGHTS: AdminRights = _all_true(AdminRights)
ALL_MEMBER_RIGHTS: MemberRights = _all_true(MemberRights)
#: Пустые наборы: исполнитель, которому здесь не можно ничего.
NO_ADMIN_RIGHTS = AdminRights()
NO_MEMBER_RIGHTS = MemberRights()

#: Ограничение ``send_media`` — зонтик над всеми видами вложений: оно
#: старше гранулярных прав 2023 года, и сервер присылает его вместо них.
_MEDIA_RIGHTS = (
	"send_photos",
	"send_videos",
	"send_roundvideos",
	"send_audios",
	"send_voices",
	"send_docs",
	"send_stickers",
	"send_gifs",
	"send_games",
	"send_inline",
)

#: Ограничение ``send_messages`` — зонтик надо всем, что вообще шлют
#: в ленту. Реакции, приглашения, закрепление и оформление под него
#: не попадают: это не отправка сообщений.
_SENDING_RIGHTS = ("send_plain", *_MEDIA_RIGHTS, "send_polls", "embed_links")


@dataclass(frozen=True)
class ExecutorRights:
	"""Снимок прав одного исполнителя в одном сообществе (ADR-0035).

	Attributes:
		status: как участвует.
		admin: выданные права администратора (у не-администратора пусто).
		allowed: что можно как участнику — уже с учётом обоих слоёв
			ограничений; у администратора и владельца разрешено всё.
	"""

	status: ParticipantStatus
	admin: AdminRights = NO_ADMIN_RIGHTS
	allowed: MemberRights = NO_MEMBER_RIGHTS

	def to_payload(self) -> dict[str, list[str]]:
		"""Наборы прав для колонки JSON — списками выданных имён.

		Статус в снимок не входит: он живёт своей колонкой, и дублировать
		его в JSON значило бы завести о нём две правды. Перечисляются
		только выданные права — так строка читается глазами в любом
		обозревателе базы, а невыданное просто отсутствует.
		"""
		return {"admin": _granted(self.admin), "allowed": _granted(self.allowed)}

	@classmethod
	def from_payload(
		cls, status: ParticipantStatus, payload: dict[str, Any] | None
	) -> ExecutorRights:
		"""Восстанавливает снимок из колонки JSON и колонки статуса.

		Незнакомое имя права игнорируется: Telegram добавляет флаги
		(гранулярные права 2023 года, истории, монофорумы), и запись,
		сделанная будущей версией, не должна ломать чтение сегодняшней.
		Пустой ``payload`` (``None``) значит «полный снимок ещё
		не читался» — прав в нём нет, но статус известен.

		Args:
			status: статус участия из одноимённой колонки.
			payload: содержимое колонки прав (или ``None``).
		"""
		payload = payload or {}
		return cls(
			status,
			_restore(AdminRights, payload.get("admin")),
			_restore(MemberRights, payload.get("allowed")),
		)


def admin_flags(rights: AdminRights) -> dict[str, bool]:
	"""Права администратора флагами Telegram — для запроса назначения (ADR-0035).

	Имена наших полей и полей ``chatAdminRights`` совпадают один в один
	(это проверяет тест полноты), поэтому перевода не нужно — нужен
	только словарь той же формы, какую ждёт запрос.
	"""
	return {field.name: bool(getattr(rights, field.name)) for field in fields(AdminRights)}


def _granted(rights: AdminRights | MemberRights) -> list[str]:
	"""Имена выданных прав набора, по порядку объявления."""
	return [field.name for field in fields(rights) if getattr(rights, field.name)]


def _restore(kind: type[Any], names: Any) -> Any:
	"""Собирает набор прав из списка выданных имён (незнакомые — мимо)."""
	granted = set(names or ())
	return kind(**{field.name: field.name in granted for field in fields(kind)})


def _forbids(banned: Any, right: str) -> bool:
	"""Запрещает ли набор ограничений Telegram названное разрешение.

	Учитывает зонтики: ``send_messages`` закрывает всё отправляемое,
	``send_media`` — все виды вложений. Пустой набор (``None``)
	не запрещает ничего.

	Args:
		banned: ``chatBannedRights`` — личные ограничения участника
			или общие ограничения сообщества.
		right: имя разрешения из :class:`MemberRights`.
	"""
	if banned is None:
		return False
	if getattr(banned, right, False):
		return True
	if right in _SENDING_RIGHTS and getattr(banned, "send_messages", False):
		return True
	return right in _MEDIA_RIGHTS and bool(getattr(banned, "send_media", False))


def _allowed_after(*banned: Any) -> MemberRights:
	"""Складывает слои ограничений: разрешено то, что не запрещено нигде."""
	return MemberRights(
		**{
			field.name: not any(_forbids(layer, field.name) for layer in banned)
			for field in fields(MemberRights)
		}
	)


def _mtproto_status(perms: Any) -> ParticipantStatus:
	"""Статус участия по ответу MTProto (``ParticipantPermissions``).

	Владелец проверяется раньше администратора: библиотека считает его
	администратором тоже, а нам нужна отдельная ветка (см.
	:class:`AdminRights`). Исключённый от ограниченного отличается одним
	признаком: у исключённого отнято само право видеть сообщения.
	"""
	if getattr(perms, "is_creator", False):
		return ParticipantStatus.CREATOR
	if getattr(perms, "is_admin", False):
		return ParticipantStatus.ADMIN
	if getattr(perms, "has_left", False):
		return ParticipantStatus.LEFT
	if getattr(perms, "is_banned", False):
		banned = getattr(getattr(perms, "participant", None), "banned_rights", None)
		if getattr(banned, "view_messages", False):
			return ParticipantStatus.BANNED
		return ParticipantStatus.RESTRICTED
	return ParticipantStatus.MEMBER


def userbot_rights(perms: Any, default_banned: Any = None) -> ExecutorRights:
	"""Снимок прав userbot-аккаунта по ответу MTProto.

	Args:
		perms: ``ParticipantPermissions`` самого аккаунта
			(``client.get_permissions(entity, "me")``).
		default_banned: общие ограничения сообщества
			(``entity.default_banned_rights``); ``None`` — их нет.

	Returns:
		Снимок: статус, права администратора и разрешения участника
		после сложения личных и общих ограничений.
	"""
	status = _mtproto_status(perms)
	if status.administers:
		admin = (
			ALL_ADMIN_RIGHTS
			if status is ParticipantStatus.CREATOR
			else _restore(
				AdminRights,
				_granted_by(getattr(getattr(perms, "participant", None), "admin_rights", None)),
			)
		)
		return ExecutorRights(status, admin, ALL_MEMBER_RIGHTS)
	if not status.in_community:
		return ExecutorRights(status)
	personal = getattr(getattr(perms, "participant", None), "banned_rights", None)
	return ExecutorRights(status, NO_ADMIN_RIGHTS, _allowed_after(default_banned, personal))


def _granted_by(admin_rights: Any) -> list[str]:
	"""Имена выданных прав администратора из ответа MTProto.

	Имена полей ``chatAdminRights`` совпадают с нашими один в один,
	поэтому перевода не нужно — нужна только выборка выданных.
	"""
	return [field.name for field in fields(AdminRights) if getattr(admin_rights, field.name, False)]


#: Наше имя права администратора → имя поля Bot API. Составы совпадают,
#: расходятся только имена: Bot API называет права «can_*» и описывает
#: словами то же, что ``chatAdminRights`` — флагами.
_BOT_ADMIN_NAMES = {
	"change_info": "can_change_info",
	"post_messages": "can_post_messages",
	"edit_messages": "can_edit_messages",
	"delete_messages": "can_delete_messages",
	"ban_users": "can_restrict_members",
	"invite_users": "can_invite_users",
	"pin_messages": "can_pin_messages",
	"add_admins": "can_promote_members",
	"anonymous": "is_anonymous",
	"manage_call": "can_manage_video_chats",
	"other": "can_manage_chat",
	"manage_topics": "can_manage_topics",
	"post_stories": "can_post_stories",
	"edit_stories": "can_edit_stories",
	"delete_stories": "can_delete_stories",
	"manage_direct_messages": "can_manage_direct_messages",
	"manage_ranks": "can_manage_tags",
}

#: Наше имя разрешения участника → имя поля Bot API. Четыре наших права
#: приходят одним полем ``can_send_other_messages``: Bot API объединяет
#: стикеры, гифки, игры и встроенных ботов — значение у них общее,
#: и раскладывать его по четырём именам честно.
_BOT_MEMBER_NAMES = {
	"send_plain": "can_send_messages",
	"send_photos": "can_send_photos",
	"send_videos": "can_send_videos",
	"send_roundvideos": "can_send_video_notes",
	"send_audios": "can_send_audios",
	"send_voices": "can_send_voice_notes",
	"send_docs": "can_send_documents",
	"send_stickers": "can_send_other_messages",
	"send_gifs": "can_send_other_messages",
	"send_games": "can_send_other_messages",
	"send_inline": "can_send_other_messages",
	"embed_links": "can_add_web_page_previews",
	"send_polls": "can_send_polls",
	"send_reactions": "can_react_to_messages",
	"invite_users": "can_invite_users",
	"pin_messages": "can_pin_messages",
	"change_info": "can_change_info",
	"manage_topics": "can_manage_topics",
	"edit_rank": "can_edit_tag",
}

#: Статус участника Bot API → наш. ``restricted`` разбирается отдельно:
#: ограниченный может быть и в сообществе, и уже вне его.
_BOT_STATUSES = {
	"creator": ParticipantStatus.CREATOR,
	"administrator": ParticipantStatus.ADMIN,
	"member": ParticipantStatus.MEMBER,
	"left": ParticipantStatus.LEFT,
	"kicked": ParticipantStatus.BANNED,
}


def _bot_status(member: Any) -> ParticipantStatus:
	"""Статус участия по ответу Bot API (``getChatMember`` о самом боте).

	Незнакомый статус считается «не состоит»: приписывать участие
	по неизвестному слову опаснее, чем недосчитаться прав.

	Значение берётся через ``value``: aiogram отдаёт статус членом
	перечисления-строки, и ``str()`` о нём лжёт — возвращает
	``ChatMemberStatus.CREATOR`` вместо ``creator`` (сверено на aiogram
	3.30.0). Сравнение с обычной строкой при этом работает, а поиск
	по словарю строкой из ``str()`` — уже нет.
	"""
	raw = getattr(member, "status", "")
	status = str(getattr(raw, "value", raw))
	if status == "restricted":
		if getattr(member, "is_member", None) is True:
			return ParticipantStatus.RESTRICTED
		return ParticipantStatus.LEFT
	return _BOT_STATUSES.get(status, ParticipantStatus.LEFT)


def _bot_allows(source: Any, name: str) -> bool:
	"""Разрешает ли слой прав Bot API названное действие.

	Два разных «нет» здесь не путаются, и это правило действует
	в приложении с самого бот-пути. Слоя нет вовсе (``source is None`` —
	сообщество не прислало общих прав, так бывает у каналов): запрета
	в нём нет, значит и ограничения нет. Слой есть, а поля в нём нет
	(``None``): право не выдано — Bot API отвечает необязательными
	полями, и приписывать по умолчанию нельзя, недосчитаться безопаснее.
	"""
	if source is None:
		return True
	return getattr(source, name, None) is True


def bot_rights(member: Any, permissions: Any = None) -> ExecutorRights:
	"""Снимок прав бота по ответу Bot API.

	Args:
		member: ответ ``getChatMember`` о самом боте.
		permissions: общие права сообщества (``chat.permissions``);
			``None`` — сообщество их не отдало (так бывает у каналов).

	Returns:
		Снимок: статус, права администратора и разрешения участника
		после сложения личных ограничений с общими правами сообщества.
	"""
	status = _bot_status(member)
	if status.administers:
		admin = (
			ALL_ADMIN_RIGHTS
			if status is ParticipantStatus.CREATOR
			else AdminRights(
				**{ours: _bot_allows(member, theirs) for ours, theirs in _BOT_ADMIN_NAMES.items()}
			)
		)
		return ExecutorRights(status, admin, ALL_MEMBER_RIGHTS)
	if not status.in_community:
		return ExecutorRights(status)
	# у ограниченного свои поля поверх общих прав сообщества; у обычного
	# участника ограничений нет — действуют только общие права
	layers = (permissions, member) if status is ParticipantStatus.RESTRICTED else (permissions,)
	allowed = MemberRights(
		**{
			ours: all(_bot_allows(layer, theirs) for layer in layers)
			for ours, theirs in _BOT_MEMBER_NAMES.items()
		}
	)
	return ExecutorRights(status, NO_ADMIN_RIGHTS, allowed)
