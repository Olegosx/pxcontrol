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

#: Подсказка неактивного «Обслуживания» — одна на дашборд и страницу.
MAINTENANCE_UNAVAILABLE = "Нужен userbot-публикатор: боту история и участники недоступны"


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


def state_badge_text(state: CardState, counts: QueueCounts) -> str | None:
	"""Текст плашки состояния на карточке; None — плашка не нужна."""
	if state is CardState.ERRORS:
		return f"{counts.errors} {plural(counts.errors, 'ошибка', 'ошибки', 'ошибок')}"
	if state is CardState.NO_PUBLISHER:
		return "нет публикатора"
	if state is CardState.PUBLISHER_PAUSED:
		return "публикатор приостановлен"
	if state is CardState.PUBLISHER_INCAPABLE:
		return "публикатор без прав"
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

	Ошибки — ``ERROR`` (красная), выключено — ``INFOAMTION`` (серая),
	нет публикатора и штатное «активен» — ``ATTENTION`` (акцент темы).
	"""
	levels = {
		CardState.ERRORS: InfoLevel.ERROR,
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
	ASSIGN_PUBLISHER = "assign_publisher"  # диалог «Участники…»
	ENABLE = "enable"  # включить сообщество
	MAINTENANCE = "maintenance"  # окно обслуживания (ADR-0026)


#: Подписи действий (кнопка карточки и пункт меню строки — одни и те же).
ACTION_LABELS: dict[CardAction, str] = {
	CardAction.PUBLISH: "Опубликовать",
	CardAction.SCHEDULE: "Отложено",
	CardAction.QUEUE: "Очередь",
	CardAction.ASSIGN_PUBLISHER: "Назначить публикатора",
	CardAction.ENABLE: "Включить",
	CardAction.MAINTENANCE: "Обслуживание",
}


def card_actions(community: CommunityDto, counts: QueueCounts) -> tuple[CardAction, ...]:
	"""Набор действий карточки по её состоянию.

	Порядок проверок — от самого ограничивающего состояния: выключенному
	сначала нужно включиться, сообществу без публикатора — публикатор
	(остальные действия без него бессмысленны); у группы вместо
	«Отложено» — «Обслуживание» (уборка нужна именно группам);
	непустая очередь заслуживает кнопки «Очередь» вместо «Отложено».
	"""
	state = card_state(community, counts)
	if state is CardState.DISABLED:
		return (CardAction.ENABLE, CardAction.MAINTENANCE)
	if state is CardState.NO_PUBLISHER:
		return (CardAction.ASSIGN_PUBLISHER,)
	if state is CardState.PUBLISHER_PAUSED:
		# действие живёт в разделе «Пользователи и боты» — возобновить
		# аккаунт или бота; с карточки сообщества ничего не предлагается,
		# чтобы не звать назначать нового публикатора вместо возврата прежнего
		return ()
	if community.kind is CommunityKind.GROUP:
		return (CardAction.PUBLISH, CardAction.MAINTENANCE)
	if counts.planned + counts.errors > 0:
		return (CardAction.PUBLISH, CardAction.QUEUE)
	return (CardAction.PUBLISH, CardAction.SCHEDULE)


def action_available(action: CardAction, community: CommunityDto) -> bool:
	"""Доступно ли действие сообществу прямо сейчас.

	Обслуживание умеет только userbot (ADR-0026): без публикатора-userbot
	кнопка показывается, но неактивна — с той же подсказкой, что
	на странице сообщества.
	"""
	if action is CardAction.MAINTENANCE:
		return community.userbot_assigned
	return True


def audience_word(kind: CommunityKind, count: int) -> str:
	"""«подписчик(и/ов)» у канала, «участник(а/ов)» у группы — по числу."""
	if kind is CommunityKind.GROUP:
		return plural(count, "участник", "участника", "участников")
	return plural(count, "подписчик", "подписчика", "подписчиков")


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
