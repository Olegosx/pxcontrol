"""Состояние сообщества на экране: плашка, набор действий, подписи.

Правила общие для дашборда «Каналы и группы» (карточка и строка таблицы)
и страницы сообщества (шапка): одно состояние на сообщество по приоритету
«выключено → ошибки → нет публикатора», один набор быстрых действий.
Чистые функции без Qt — тестируются как обычный код; единственный
виджет здесь — плашка, штатный ``InfoBadge`` пресетом уровня.
"""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBadge, InfoLevel

from pxcontrol.engine.services.communities import (
	CommunityDto,
)
from pxcontrol.engine.services.publish_queue import QueueItemDto
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui.pages.common import (
	QueueCounts,
	format_count,
	plural,
	queue_counts,
)

#: Подсказка неактивных «Задач» — одна на дашборд и страницу.
TASKS_UNAVAILABLE = "Нужен userbot-публикатор: боту история и участники недоступны"


class CardState(StrEnum):
	"""Состояние сообщества — плашка справа от метрик или в шапке.

	Ровно одно; при совпадении причин действует приоритет
	:func:`card_state`: выключено → ошибки → нет публикатора.
	"""

	NORMAL = "normal"  # штатно
	ERRORS = "errors"  # в очереди есть элементы с ошибкой
	NO_PUBLISHER = "no_publisher"  # ни userbot-публикатора, ни бота
	# публикатор назначен, но приостановлен человеком (ADR-0029):
	# назначать нового не нужно — нужно возобновить прежнего
	PUBLISHER_PAUSED = "publisher_paused"
	# публикатор назначен и не на паузе, но по правам публиковать
	# не может (ADR-0035): права отобрали в Telegram, и звать назначать
	# нового так же неверно, как при паузе
	PUBLISHER_INCAPABLE = "publisher_incapable"
	DISABLED = "disabled"  # выключено переключателем активности


def community_group_title(kind: CommunityKind) -> str:
	"""Название группы вида: «Каналы» или «Группы».

	Одно на всё приложение: заголовок раздела дашборда, пункт навигации
	и строка пути страницы сообщества (ADR-0041) должны звать раздел
	одинаково.
	"""
	return "Каналы" if kind is CommunityKind.CHANNEL else "Группы"


def card_state(community: CommunityDto, counts: QueueCounts) -> CardState:
	"""Состояние по приоритету «выключено → ошибки → публикатор на паузе → нет публикатора».

	Выключенное сообщество главнее прочего: пока оно выключено, очередь
	не разбирается и ошибки не чинятся; ошибки главнее отсутствия
	публикатора — они уже случились, а публикатор ещё может вернуться.
	Пауза публикатора главнее его отсутствия: назначенный есть,
	а «нет публикатора» звало бы назначать нового.
	"""
	if not community.enabled:
		return CardState.DISABLED
	if counts.errors > 0:
		return CardState.ERRORS
	if community.publisher_paused:
		return CardState.PUBLISHER_PAUSED
	caps = community.capabilities
	if not caps.userbot and not caps.bot:
		if community.publisher_incapable:
			return CardState.PUBLISHER_INCAPABLE
		return CardState.NO_PUBLISHER
	return CardState.NORMAL


def state_badge_text(state: CardState, counts: QueueCounts, *, short: bool = False) -> str | None:
	"""Текст плашки состояния; None — плашка не нужна.

	``short`` — вариант для ячейки таблицы: колонка состояния узкая
	(132 по макету), и «публикатор приостановлен» в неё не помещается.
	Полный текст при этом никуда не девается — он идёт подсказкой
	(``screens/communities.md``, раздел 8).
	"""
	if state is CardState.ERRORS:
		return f"{counts.errors} {plural(counts.errors, 'ошибка', 'ошибки', 'ошибок')}"
	if state is CardState.NO_PUBLISHER:
		return "нет публикатора"
	if state is CardState.PUBLISHER_PAUSED:
		return "приостановлен" if short else "публикатор приостановлен"
	if state is CardState.PUBLISHER_INCAPABLE:
		return "без прав" if short else "публикатор без прав"
	if state is CardState.DISABLED:
		return "выключено"
	return None


def header_state_text(community: CommunityDto, counts: QueueCounts) -> tuple[CardState, str]:
	"""Состояние и текст плашки в шапке страницы сообщества.

	В шапке плашка есть всегда: штатное состояние названо словом —
	«активен» у канала, «активна» у группы.
	"""
	state = card_state(community, counts)
	text = state_badge_text(state, counts)
	if text is None:
		text = "активна" if community.kind is CommunityKind.GROUP else "активен"
	return state, text


def state_badge(parent: QWidget, state: CardState, text: str) -> InfoBadge:
	"""Плашка состояния — штатный ``InfoBadge`` пресетом уровня.

	Смысл уровней: акцент (``ATTENTION``) — только штатное состояние
	в шапке страницы («активен»); всё, что мешает публиковать, —
	``WARNING``; ошибки — ``ERROR``; выключено — серая ``INFOAMTION``.
	Акцент на проблеме читался бы как «всё хорошо».
	"""
	levels = {
		CardState.ERRORS: InfoLevel.ERROR,
		CardState.NO_PUBLISHER: InfoLevel.WARNING,
		CardState.PUBLISHER_PAUSED: InfoLevel.WARNING,
		CardState.PUBLISHER_INCAPABLE: InfoLevel.WARNING,
		CardState.DISABLED: InfoLevel.INFOAMTION,
	}
	badge = InfoBadge(text, parent, levels.get(state, InfoLevel.ATTENTION))
	badge.adjustSize()
	return badge


class CardAction(StrEnum):
	"""Быстрое действие с карточки дашборда (и из меню строки таблицы)."""

	PUBLISH = "publish"  # «Публикация» с этим сообществом
	SCHEDULE = "schedule"  # «Публикация» → «Отложено» с фильтром по сообществу
	QUEUE = "queue"  # «Публикация» → «Очередь» с фильтром по сообществу
	# очередь этого сообщества с фильтром «ошибки»: кнопка проблемы
	QUEUE_ERRORS = "queue_errors"
	ASSIGN_PUBLISHER = "assign_publisher"  # диалог «Участники…»
	# возобновить приостановленного публикатора — та же операция,
	# что кнопкой на странице аккаунта (ADR-0029)
	RESUME_PUBLISHER = "resume_publisher"
	ENABLE = "enable"  # включить сообщество
	TASKS = "tasks"  # окно задач сообщества (ADR-0038)


#: Подписи действий (кнопка карточки и пункт меню строки — одни и те же).
ACTION_LABELS: dict[CardAction, str] = {
	CardAction.PUBLISH: "Опубликовать",
	CardAction.SCHEDULE: "Отложено",
	CardAction.QUEUE: "Очередь",
	CardAction.QUEUE_ERRORS: "Ошибки в очереди",
	CardAction.ASSIGN_PUBLISHER: "Назначить публикатора",
	CardAction.RESUME_PUBLISHER: "Возобновить публикатора",
	CardAction.ENABLE: "Включить",
	CardAction.TASKS: "Задачи",
}

#: Предел длины имени в подписи «Возобновить «…»» (макет: кнопка
#: шире прочих, но имя в ней не бесконечное).
PUBLISHER_LABEL_LIMIT = 24


def short_label(text: str) -> str:
	"""Имя не длиннее предела; дальше — многоточие строкой."""
	if len(text) <= PUBLISHER_LABEL_LIMIT:
		return text
	return text[: PUBLISHER_LABEL_LIMIT - 1].rstrip() + "…"


def publisher_label(community: CommunityDto) -> str | None:
	"""Имя назначенного публикатора (userbot или бот); None — не назначен."""
	return community.default_account_label or community.default_bot_label


def action_label(action: CardAction, community: CommunityDto) -> str:
	"""Подпись действия: у «Возобновить» в ней имя публикатора.

	Имя нужно, чтобы человек видел, кого возвращает в работу, — тот же
	исполнитель виден на его странице. Публикатор не назначен (такое
	бывает, когда на паузе кто-то из пула, а умолчания нет) — подпись
	остаётся общей.
	"""
	if action is CardAction.RESUME_PUBLISHER:
		name = publisher_label(community)
		if name:
			return f"Возобновить «{short_label(name)}»"
	return ACTION_LABELS[action]


def card_actions(community: CommunityDto, counts: QueueCounts) -> tuple[CardAction, ...]:
	"""Набор действий карточки по её состоянию.

	Правило одно: **у каждой проблемы своя первая кнопка** — та, что
	эту проблему и решает (``screens/communities.md``, раздел 6.2).
	Порядок проверок — от самого ограничивающего состояния: выключенному
	сначала нужно включиться, дальше идут ошибки, потом публикатор.
	Тому, кто публиковать не может, «Опубликовать» не предлагается
	вовсе. У группы вместо «Отложено» — «Задачи» (уборка нужна именно
	группам); непустая очередь заслуживает кнопки «Очередь».
	"""
	state = card_state(community, counts)
	if state is CardState.DISABLED:
		# «Задачи» выключенному не предлагаются: они доступны с его страницы
		return (CardAction.ENABLE,)
	if state is CardState.ERRORS:
		return (CardAction.QUEUE_ERRORS, CardAction.PUBLISH)
	if state in (CardState.NO_PUBLISHER, CardState.PUBLISHER_INCAPABLE):
		# прав лишили или публикатора нет — обоим нужен публикатор
		return (CardAction.ASSIGN_PUBLISHER,)
	if state is CardState.PUBLISHER_PAUSED:
		# назначать нового незачем: прежний есть, его нужно возобновить
		return (CardAction.RESUME_PUBLISHER,)
	if community.kind is CommunityKind.GROUP:
		return (CardAction.PUBLISH, CardAction.TASKS)
	if counts.planned > 0:
		return (CardAction.PUBLISH, CardAction.QUEUE)
	return (CardAction.PUBLISH, CardAction.SCHEDULE)


def action_available(action: CardAction, community: CommunityDto) -> bool:
	"""Доступно ли действие сообществу прямо сейчас.

	Задачи ведут только пользователи (ADR-0026, ADR-0038): без
	публикатора-userbot кнопка показывается, но неактивна — с той же
	подсказкой, что на странице сообщества.
	"""
	if action is CardAction.TASKS:
		return community.userbot_assigned
	return True


def cannot_publish(community: CommunityDto) -> bool:
	"""Включено, но публиковать некем: нет публикатора, пауза или нет прав.

	Одно правило на сводку дашборда и её фильтр: три состояния
	(``NO_PUBLISHER``, ``PUBLISHER_PAUSED``, ``PUBLISHER_INCAPABLE``)
	различаются причиной, а последствие у них общее — пост будет ждать.
	Выключенное сюда не попадает: у него очередь и так не разбирается.
	"""
	caps = community.capabilities
	return community.enabled and not caps.userbot and not caps.bot


def audience_word(kind: CommunityKind, count: int) -> str:
	"""«подписчик(и/ов)» у канала, «участник(а/ов)» у группы — по числу."""
	if kind is CommunityKind.GROUP:
		return plural(count, "участник", "участника", "участников")
	return plural(count, "подписчик", "подписчика", "подписчиков")


def audience_many(kind: CommunityKind) -> str:
	"""Аудитория вообще, без числа: «подписчиков» у канала, «участников» у группы.

	Для фраз вроде «число участников», «в списке подписчиков» — родительный
	падеж множественного числа, тот же, что у :func:`audience_word` при «5».
	"""
	return audience_word(kind, 5)


def subtitle_text(community: CommunityDto, participants: int | None) -> str:
	"""Подстрочник карточки: «@имя · 18 420 подписчиков».

	Без @имени — «имя не задано»; без кэша статистики — только @имя.
	"""
	name = f"@{community.username}" if community.username else "имя не задано"
	if participants is None:
		return name
	return f"{name} · {format_count(participants)} {audience_word(community.kind, participants)}"


def community_queue_counts(items: list[QueueItemDto], community_id: int) -> QueueCounts:
	"""Сводка очереди одного сообщества (для шапки страницы и вкладки)."""
	return queue_counts(items).get(community_id, QueueCounts())


# --- вкладка «Участники»: исполнители сообщества ----------------------------------


def executors_count(community: CommunityDto) -> int:
	"""Сколько исполнителей у сообщества — число на вкладке «Участники».

	С ADR-0035 пул один на оба вида, и складывать больше нечего: боты
	стоят в нём рядом с людьми, потому что вопрос у человека один —
	«кто работает в этом сообществе».
	"""
	return community.executors_count
