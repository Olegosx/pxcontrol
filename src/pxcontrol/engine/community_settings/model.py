"""Типы настроек сообщества в Telegram: значения, снимок, изменения (ADR-0043).

Настройка — это пара «ключ → значение». Ключ — строка из каталога
(:mod:`catalog`), значение — одного из немногих видов (:class:`ValueKind`):
переключатель, текст, выбор из списка, разрешения участников, реакции,
фото, связанное сообщество. Новые настройки Telegram почти всегда ложатся
в уже имеющийся вид, поэтому их добавление — запись в каталоге и пара
функций в транспорте, без новых типов, без миграций и без правки экрана.

Снимок (:class:`CommunitySettings`) — значения по ключам плюс контекст:
факты о сообществе, которые не правятся, но от которых зависит,
можно ли править (вид, связь с каналом, число участников, уровень бустов,
пределы сервера). Снимок в базе не хранится: источник истины — Telegram.

Модуль чистый — ни сети, ни базы, ни библиотечных типов.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pxcontrol.engine.telegram.rights import MemberRights
from pxcontrol.engine.telegram.types import ChatReactionsMode, CommunityKind


class ValueKind(StrEnum):
	"""Вид значения настройки — от него зависят проверка и редактор экрана."""

	TOGGLE = "toggle"  # да / нет
	TEXT = "text"  # строка с пределом длины
	CHOICE = "choice"  # одно значение из перечня (секунды, режимы)
	PERMISSIONS = "permissions"  # что разрешено участникам (MemberRights)
	REACTIONS = "reactions"  # какие реакции разрешены (ReactionsValue)
	PHOTO = "photo"  # фото сообщества (PhotoValue)
	LINKED_CHAT = "linked_chat"  # связанное сообщество (LinkedChat)


class SettingSection(StrEnum):
	"""Раздел экрана, в котором показывается настройка."""

	MAIN = "main"  # фото, название, описание
	ACCESS = "access"  # тип, ссылка, вступление, защита содержимого
	MESSAGES = "messages"  # подписи, реакции, автоудаление, обсуждение
	MEMBERS = "members"  # разрешения и режимы группы


#: Подписи разделов — в порядке показа.
SECTION_TITLES: dict[SettingSection, str] = {
	SettingSection.MAIN: "Основное",
	SettingSection.ACCESS: "Тип и доступ",
	SettingSection.MESSAGES: "Сообщения",
	SettingSection.MEMBERS: "Участники",
}


@dataclass(frozen=True)
class Choice:
	"""Вариант значения настройки-выбора: значение и подпись для человека."""

	value: int | str
	label: str


@dataclass(frozen=True)
class ReactionsValue:
	"""Какие реакции разрешены в сообществе.

	Attributes:
		mode: любые · выбранные · никакие.
		emojis: выбранные реакции (только при режиме «выбранные»).
		limit: сколько разных реакций может стоять под одним сообщением;
			None — предел сервера по умолчанию.
	"""

	mode: ChatReactionsMode
	emojis: tuple[str, ...] = ()
	limit: int | None = None


@dataclass(frozen=True)
class PhotoValue:
	"""Фото сообщества.

	В снимке — есть ли фото сейчас (``present``). В изменении —
	``upload``: путь к новой картинке; ``present=False`` без ``upload``
	означает «убрать фото».
	"""

	present: bool
	upload: str | None = None


@dataclass(frozen=True)
class LinkedChat:
	"""Связанное сообщество: у канала — группа обсуждения, у группы — канал.

	``chat_id`` в формате Bot API (-100…); None — связи нет.
	"""

	chat_id: str | None
	title: str | None = None


#: Подписи авторов в канале — три состояния, а не два переключателя:
#: ссылка на профиль без подписи не бывает, сервер сбрасывает её сам
#: (живая проба 25.09.2026).
SIGNATURE_OFF = "off"
SIGNATURE_NAMES = "names"
SIGNATURE_PROFILES = "profiles"


#: Значение любой настройки. None — транспорт значения не сообщил.
SettingValue = bool | str | int | MemberRights | ReactionsValue | PhotoValue | LinkedChat | None


@dataclass(frozen=True)
class SettingsContext:
	"""Факты о сообществе, от которых зависит, можно ли править настройки.

	Сами не правятся на экране. Новые факты добавляются необязательными
	полями: снимок нигде не хранится, миграций это не требует.

	Attributes:
		kind: вид сообщества.
		linked_chat_id: связанное сообщество (у группы — канал, которому
			она служит обсуждением); None — связи нет.
		participants: подписчиков или участников; None — неизвестно.
		boost_level: уровень бустов сообщества; None — неизвестен (0).
		can_set_username: сервер разрешает этому аккаунту менять @имя
			(``channelFull.can_set_username``); None — не сообщил (бот).
		hidden_members_min: с какого числа участников Telegram позволяет
			скрыть их список (``hidden_members_group_size_min``).
		autotranslation_level_min: с какого уровня бустов канал может
			включить автоперевод (``channel_autotranslation_level_min``).
		reactions_max: предел числа разных реакций под сообщением
			(``reactions_uniq_max``).
		reaction_catalog: все стандартные реакции Telegram — из них
			выбираются разрешённые (эмодзи по порядку сервера).
	"""

	kind: CommunityKind
	linked_chat_id: str | None = None
	participants: int | None = None
	boost_level: int | None = None
	can_set_username: bool | None = None
	hidden_members_min: int | None = None
	autotranslation_level_min: int | None = None
	reactions_max: int | None = None
	reaction_catalog: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommunitySettings:
	"""Снимок настроек сообщества глазами одного исполнителя.

	Attributes:
		values: значения по ключам каталога; в снимок входят только
			настройки, которые транспорт умеет читать.
		context: факты о сообществе (см. :class:`SettingsContext`).
	"""

	values: Mapping[str, SettingValue]
	context: SettingsContext

	def value(self, key: str) -> SettingValue:
		"""Значение настройки; None — транспорт её не читал."""
		return self.values.get(key)


@dataclass(frozen=True)
class SettingChange:
	"""Одно изменение: какую настройку и во что превратить."""

	key: str
	value: SettingValue


@dataclass(frozen=True)
class ChangeResult:
	"""Итог одного изменения при сохранении.

	Attributes:
		key: настройка.
		error: текст отказа для человека; None — изменение применено.
	"""

	key: str
	error: str | None = None

	@property
	def applied(self) -> bool:
		return self.error is None
