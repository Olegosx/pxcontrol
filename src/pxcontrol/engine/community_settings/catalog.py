"""Каталог настроек сообщества в Telegram — единственный их перечень (ADR-0043).

Каждая настройка описана одной записью :class:`SettingSpec`: ключ, раздел
и подпись экрана, вид значения, в каком виде сообщества она есть, какое
действие нужно исполнителю (:class:`ExecutorAction`) и — если Telegram
ставит дополнительное условие — функция-преграда. Экран строит себя
по каталогу, движок по нему проверяет правку до обращения к Telegram,
транспорты по ключам каталога читают и пишут (полноту их таблиц
закрепляют тесты).

**Добавить настройку** = запись здесь + чтение и запись в таблицах
транспорта (``mtproto_settings`` и, если умеет бот, ``bot_settings``).
Миграций не нужно: снимок в базе не хранится.

Источники фактов: права — документация TDLib (права по методам)
и живая проба 25.09.2026 по одному праву за раз; условия и допустимые
значения — та же проба (``_misc/tg_settings_probe.py``, итог —
``_misc/tg_settings_probe_results.txt``). Реакции сервер принял
в группе от администратора с любым правом, но каталог держит
документированное «менять информацию»: для канала шире не проверено,
а экран не должен обещать того, чего сервер может не принять.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pxcontrol.engine.community_settings.model import (
	SIGNATURE_NAMES,
	SIGNATURE_OFF,
	SIGNATURE_PROFILES,
	Choice,
	CommunitySettings,
	SettingSection,
	ValueKind,
)
from pxcontrol.engine.services.abilities import ExecutorAction
from pxcontrol.engine.telegram.types import CommunityKind

#: Оба вида сообщества.
_BOTH = frozenset({CommunityKind.CHANNEL, CommunityKind.GROUP})
_CHANNEL = frozenset({CommunityKind.CHANNEL})
_GROUP = frozenset({CommunityKind.GROUP})

#: Преграда: причина, по которой Telegram не даст править (None — даст).
Blocker = Callable[[CommunitySettings], str | None]


@dataclass(frozen=True)
class SettingSpec:
	"""Описание одной настройки.

	Attributes:
		key: постоянный ключ (им говорят экран, движок и транспорты).
		label: подпись на экране.
		section: раздел экрана.
		kind: вид значения.
		kinds: в каких видах сообщества настройка есть.
		action: что нужно исполнителю, чтобы её менять.
		hint: пояснение под полем (необязательно).
		choices: варианты значения (для ``CHOICE``).
		max_length: предел длины (для ``TEXT``).
		required: текст не бывает пустым (для ``TEXT``).
		multiline: многострочный текст.
		blocker: дополнительное условие Telegram (см. :data:`Blocker`).
	"""

	key: str
	label: str
	section: SettingSection
	kind: ValueKind
	kinds: frozenset[CommunityKind]
	action: ExecutorAction
	hint: str = ""
	choices: tuple[Choice, ...] = ()
	max_length: int | None = None
	required: bool = False
	multiline: bool = False
	blocker: Blocker | None = None


# --- преграды: условия Telegram сверх прав (живая проба 25.09.2026) ---------------


def _username_blocker(settings: CommunitySettings) -> str | None:
	"""@имя: сервер сам говорит, можно ли его менять этому аккаунту."""
	if settings.context.can_set_username is False:
		return "Telegram не разрешает этому аккаунту менять @имя сообщества."
	return None


def _discussion_only(settings: CommunitySettings) -> str | None:
	"""«Вступить, чтобы писать» — только у группы обсуждения канала.

	В обычной группе писать без вступления нельзя и так: сервер отвечает
	``DISCUSSION_CHAT_REQUIRED``.
	"""
	if settings.context.linked_chat_id is None:
		return "Только у группы обсуждения канала: в обычную группу без вступления не пишут."
	return None


def _prehistory_blocker(settings: CommunitySettings) -> str | None:
	"""Историю от новых не скрыть у группы обсуждения и у форума."""
	if settings.context.linked_chat_id is not None:
		return "У группы обсуждения канала история видна всем — так устроен Telegram."
	if settings.value("forum") is True:
		return "У группы с темами история видна всем — выключите темы, чтобы её скрыть."
	return None


def _forum_blocker(settings: CommunitySettings) -> str | None:
	"""Группа обсуждения канала темами не бывает (``CHAT_DISCUSSION_UNALLOWED``)."""
	if settings.context.linked_chat_id is not None:
		return "Группу обсуждения канала нельзя сделать группой с темами."
	return None


def _hidden_members_blocker(settings: CommunitySettings) -> str | None:
	"""Скрыть участников можно с порога сервера (``PARTICIPANTS_TOO_FEW``)."""
	need = settings.context.hidden_members_min
	have = settings.context.participants
	if need is not None and have is not None and have < need:
		return f"Скрыть список можно в группе от {need} участников (сейчас {have})."
	return None


def _autotranslation_blocker(settings: CommunitySettings) -> str | None:
	"""Автоперевод — с уровня бустов канала (``BOOSTS_REQUIRED``)."""
	need = settings.context.autotranslation_level_min
	have = settings.context.boost_level or 0
	if need is not None and have < need:
		return f"Автоперевод доступен с {need}-го уровня бустов канала (сейчас {have})."
	return None


# --- варианты значений ------------------------------------------------------------

#: Медленный режим: сервер принимает только эти значения (SECONDS_INVALID
#: на прочих — проверено 25.09.2026).
SLOWMODE_CHOICES: tuple[Choice, ...] = (
	Choice(0, "выключен"),
	Choice(10, "10 секунд"),
	Choice(30, "30 секунд"),
	Choice(60, "1 минута"),
	Choice(300, "5 минут"),
	Choice(900, "15 минут"),
	Choice(3600, "1 час"),
)

#: Автоудаление сообщений: значения, которые сервер принял 25.09.2026
#: (100 с и 1 час — TTL_PERIOD_INVALID; меньше суток сервер не берёт).
TTL_CHOICES: tuple[Choice, ...] = (
	Choice(0, "выключено"),
	Choice(86400, "1 день"),
	Choice(172800, "2 дня"),
	Choice(604800, "1 неделя"),
	Choice(1209600, "2 недели"),
	Choice(2678400, "1 месяц"),
	Choice(5184000, "2 месяца"),
	Choice(7776000, "3 месяца"),
)

#: Подписи авторов в канале (значения — в модели, SIGNATURE_*).
SIGNATURE_CHOICES: tuple[Choice, ...] = (
	Choice(SIGNATURE_OFF, "не подписывать"),
	Choice(SIGNATURE_NAMES, "подписывать именем автора"),
	Choice(SIGNATURE_PROFILES, "подписывать со ссылкой на профиль"),
)


# --- каталог ------------------------------------------------------------------------

#: Все настройки — в порядке показа на экране.
CATALOG: tuple[SettingSpec, ...] = (
	# основное
	SettingSpec(
		"photo", "Фото", SettingSection.MAIN, ValueKind.PHOTO, _BOTH, ExecutorAction.CHANGE_INFO
	),
	SettingSpec(
		"title",
		"Название",
		SettingSection.MAIN,
		ValueKind.TEXT,
		_BOTH,
		ExecutorAction.CHANGE_INFO,
		max_length=128,
		required=True,
	),
	SettingSpec(
		"about",
		"Описание",
		SettingSection.MAIN,
		ValueKind.TEXT,
		_BOTH,
		ExecutorAction.CHANGE_INFO,
		max_length=255,
		multiline=True,
	),
	# тип и доступ
	SettingSpec(
		"username",
		"Публичная ссылка (@имя)",
		SettingSection.ACCESS,
		ValueKind.TEXT,
		_BOTH,
		ExecutorAction.OWN,
		# предела длины здесь нет: правила @имени проверяет сервер
		# (USERNAME_INVALID), документация их не приводит
		hint="Пусто — сообщество частное, вход по ссылке-приглашению.",
		blocker=_username_blocker,
	),
	SettingSpec(
		"noforwards",
		"Запретить копирование и пересылку",
		SettingSection.ACCESS,
		ValueKind.TOGGLE,
		_BOTH,
		ExecutorAction.OWN,
	),
	SettingSpec(
		"join_request",
		"Вступление по одобрению администратора",
		SettingSection.ACCESS,
		ValueKind.TOGGLE,
		_GROUP,
		ExecutorAction.RESTRICT_MEMBERS,
	),
	SettingSpec(
		"join_to_send",
		"Писать только вступившим",
		SettingSection.ACCESS,
		ValueKind.TOGGLE,
		_GROUP,
		ExecutorAction.RESTRICT_MEMBERS,
		blocker=_discussion_only,
	),
	# сообщения
	SettingSpec(
		"signatures",
		"Подписи авторов",
		SettingSection.MESSAGES,
		ValueKind.CHOICE,
		_CHANNEL,
		ExecutorAction.CHANGE_INFO,
		choices=SIGNATURE_CHOICES,
	),
	SettingSpec(
		"reactions",
		"Реакции",
		SettingSection.MESSAGES,
		ValueKind.REACTIONS,
		_BOTH,
		ExecutorAction.CHANGE_INFO,
	),
	SettingSpec(
		"ttl",
		"Автоудаление сообщений",
		SettingSection.MESSAGES,
		ValueKind.CHOICE,
		_BOTH,
		ExecutorAction.CHANGE_INFO,
		choices=TTL_CHOICES,
	),
	SettingSpec(
		"autotranslation",
		"Автоперевод сообщений",
		SettingSection.MESSAGES,
		ValueKind.TOGGLE,
		_CHANNEL,
		ExecutorAction.CHANGE_INFO,
		blocker=_autotranslation_blocker,
	),
	SettingSpec(
		"linked_chat",
		"Группа обсуждения",
		SettingSection.MESSAGES,
		ValueKind.LINKED_CHAT,
		_CHANNEL,
		ExecutorAction.CHANGE_INFO,
	),
	# участники (группа)
	SettingSpec(
		"permissions",
		"Что разрешено участникам",
		SettingSection.MEMBERS,
		ValueKind.PERMISSIONS,
		_GROUP,
		ExecutorAction.RESTRICT_MEMBERS,
	),
	SettingSpec(
		"slowmode",
		"Медленный режим",
		SettingSection.MEMBERS,
		ValueKind.CHOICE,
		_GROUP,
		ExecutorAction.RESTRICT_MEMBERS,
		choices=SLOWMODE_CHOICES,
	),
	SettingSpec(
		"hidden_prehistory",
		"Скрыть историю от новых участников",
		SettingSection.MEMBERS,
		ValueKind.TOGGLE,
		_GROUP,
		ExecutorAction.CHANGE_INFO,
		blocker=_prehistory_blocker,
	),
	SettingSpec(
		"forum",
		"Темы",
		SettingSection.MEMBERS,
		ValueKind.TOGGLE,
		_GROUP,
		ExecutorAction.OWN,
		blocker=_forum_blocker,
	),
	SettingSpec(
		"participants_hidden",
		"Скрыть список участников",
		SettingSection.MEMBERS,
		ValueKind.TOGGLE,
		_GROUP,
		ExecutorAction.RESTRICT_MEMBERS,
		blocker=_hidden_members_blocker,
	),
	SettingSpec(
		"antispam",
		"Усиленный антиспам",
		SettingSection.MEMBERS,
		ValueKind.TOGGLE,
		_GROUP,
		# живая проба: открывает право «удалять сообщения», и только оно;
		# порог 200 участников из конфигурации сервер не соблюдает
		ExecutorAction.DELETE_OTHERS,
	),
)

#: Каталог по ключам.
SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in CATALOG}


def spec_of(key: str) -> SettingSpec | None:
	"""Описание настройки по ключу; None — ключа нет в каталоге."""
	return SPECS.get(key)


def applicable(kind: CommunityKind) -> tuple[SettingSpec, ...]:
	"""Настройки, которые есть у сообщества такого вида (в порядке каталога)."""
	return tuple(spec for spec in CATALOG if kind in spec.kinds)
