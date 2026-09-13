"""Дашборд «Каналы и группы»: сводка, разделы по виду, плитка или список.

Страница делится на разделы «Каналы» и «Группы» (пустой раздел
не рисуется), а тело раздела показывается одним из двух видов —
сеткой карточек или таблицей — по переключателю в шапке; положение
переключателя хранится в настройках приложения. Поиск фильтрует
по названию и @имени на клиенте, без обращения к движку.

Клик по карточке или строке открывает страницу сообщества
(:mod:`pxcontrol.ui.pages.community_page`); быстрые действия — кнопки
на карточке и контекстное меню строки — ведут к тем же операциям,
что и страница сообщества, без нового кода в движке.

Правила показа (состояние карточки, набор действий, тексты метрик,
число колонок, сортировка таблицы, фильтр поиска) — чистые функции
без Qt: они тестируются как обычный код.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
	QColor,
	QContextMenuEvent,
	QFont,
	QMouseEvent,
	QPainter,
	QPaintEvent,
	QPen,
	QResizeEvent,
	QShowEvent,
)
from PySide6.QtWidgets import (
	QFrame,
	QGraphicsOpacityEffect,
	QGridLayout,
	QHBoxLayout,
	QLabel,
	QPushButton,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	Action,
	BodyLabel,
	CaptionLabel,
	CardWidget,
	ComboBox,
	FluentIcon,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	RoundMenu,
	ScrollArea,
	SearchLineEdit,
	SegmentedToolWidget,
	StrongBodyLabel,
	SubtitleLabel,
	getFont,
	isDarkTheme,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.publish_queue import QueueItemDto
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, UI_COMMUNITIES_VIEW
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	account_caption,
	bot_caption,
	clear_layout,
	community_logo,
	elide_text,
	error_reporter,
	exec_dialog,
	format_count,
	noop,
	page_layout,
	plural,
	show_info,
	show_success,
)
from pxcontrol.ui.pages.community_page import open_members
from pxcontrol.ui.pages.maintenance import open_maintenance
from pxcontrol.ui.pages.publish_queue_view import QueueFilter, QueueViewDialog
from pxcontrol.ui.theme import ACCENT_COLOR

logger = logging.getLogger(__name__)

#: Значения настройки вида дашборда (``UI_COMMUNITIES_VIEW``).
VIEW_TILES = "tiles"
VIEW_LIST = "list"

#: Сетка карточек: минимальная ширина карточки и интервал (пиксели).
#: Число колонок — сколько таких карточек помещается в ширину области;
#: колонки растягиваются, поэтому пустого поля справа нет.
CARD_MIN_WIDTH = 360
GRID_SPACING = 12

#: Минимальная высота карточки (растёт по содержимому).
_CARD_MIN_HEIGHT = 132

#: Размер логотипа: в шапке карточки и в ячейке таблицы (пиксели).
_CARD_LOGO_SIZE = 40
_ROW_LOGO_SIZE = 28

#: Высоты элементов по макету (пиксели).
_SUMMARY_HEIGHT = 38
_SUMMARY_BADGE_HEIGHT = 26
_BADGE_HEIGHT = 22
_ACTION_HEIGHT = 26
_TABLE_HEADER_HEIGHT = 34
_TABLE_ROW_HEIGHT = 50

#: Ширины числовых колонок таблицы (пиксели); колонка названия тянется.
_COLUMN_WIDTHS = {"participants": 96, "queue": 104, "scheduled": 92, "state": 132}

#: Непрозрачность выключенного сообщества: карточка и строка таблицы.
_DISABLED_CARD_OPACITY = 0.62
_DISABLED_ROW_OPACITY = 0.6

#: Цвета оформления парами «светлая тема, тёмная тема». Тёмные — из макета;
#: светлые — те же роли на светлом фоне (белая полупрозрачность становится
#: чёрной, цвет ошибки — как у ``ErrorLabel``). Библиотечные виджеты
#: этими стилями не красятся (ADR-0023, п. 5): плашки, разделители
#: и кнопки-обводки собраны на чистых виджетах Qt.
_TITLE_COLOR = ("#1b1b1b", "#ffffff")
_TEXT_COLOR = ("#3a3a3a", "#dfdfdf")
_MUTED_COLOR = ("#6f6f6f", "#9d9d9d")
_DIM_COLOR = ("#8a8a8a", "#8a8a8a")
_COUNT_COLOR = ("#8a8a8a", "#6f6f6f")
_HAIRLINE = ("rgba(0,0,0,.08)", "rgba(255,255,255,.08)")
_CARD_HAIRLINE = ("rgba(0,0,0,.07)", "rgba(255,255,255,.07)")
_SUMMARY_BG = ("rgba(0,0,0,.03)", "rgba(255,255,255,.03)")
_SUMMARY_BORDER = ("rgba(0,0,0,.075)", "rgba(255,255,255,.075)")
_DIVIDER = ("rgba(0,0,0,.09)", "rgba(255,255,255,.09)")
_ERROR_TEXT = ("#c42b1c", "#ff99a4")
_ERROR_BORDER = ("rgba(196,43,28,.5)", "rgba(255,153,164,.5)")
_ERROR_HOVER = ("rgba(196,43,28,.08)", "rgba(255,153,164,.08)")
_ERROR_CARD_BORDER = (QColor(196, 43, 28, 102), QColor(255, 153, 164, 102))
_ACCENT_TEXT = (ACCENT_COLOR, ACCENT_COLOR)
_ACCENT_BORDER = ("rgba(20,184,166,.5)", "rgba(20,184,166,.5)")
_ACCENT_BUTTON_BORDER = ("rgba(20,184,166,.55)", "rgba(20,184,166,.55)")
_ACCENT_HOVER = ("rgba(20,184,166,.10)", "rgba(20,184,166,.10)")
_NEUTRAL_BORDER = ("rgba(0,0,0,.22)", "rgba(255,255,255,.22)")
_BUTTON_BORDER = ("rgba(0,0,0,.16)", "rgba(255,255,255,.16)")
_BUTTON_HOVER = ("rgba(0,0,0,.05)", "rgba(255,255,255,.06)")
_TABLE_BORDER = ("rgba(0,0,0,.075)", "rgba(255,255,255,.075)")
_TABLE_HEADER_BG = ("rgba(0,0,0,.035)", "rgba(255,255,255,.035)")
_ROW_BORDER = ("rgba(0,0,0,.06)", "rgba(255,255,255,.06)")

#: Ширина поля поиска в шапке (пиксели).
_SEARCH_WIDTH = 200

#: Числа в тексте метрики — их разметка выделяет жирным.
_DIGITS = re.compile(r"\d+")


# --- правила показа (чистые функции) -----------------------------------------


@dataclass(frozen=True)
class QueueCounts:
	"""Сводка очереди одного сообщества для карточки дашборда.

	Attributes:
		planned: неотправленное без ошибок — включая ждущих слота.
		waiting: из них ждут слота отложек (ADR-0016) — второе число.
		errors: элементы с ошибкой: они ждут повтора и требуют внимания,
			поэтому в «к отправке» не входят.
	"""

	planned: int = 0
	waiting: int = 0
	errors: int = 0


def queue_counts(items: list[QueueItemDto]) -> dict[int, QueueCounts]:
	"""Считает сводку очереди по сообществам (чистая функция).

	Правило показа — предметное (ADR-0016), поэтому живёт отдельно
	от вёрстки и закрыто тестом: в вёрстке его проверить нечем.
	"""
	counts: dict[int, QueueCounts] = {}
	for item in items:
		if item.status.left_queue():
			continue
		current = counts.get(item.community_id, QueueCounts())
		if item.status is JobStatus.ERROR:
			current = replace(current, errors=current.errors + 1)
		else:
			current = replace(
				current,
				planned=current.planned + 1,
				waiting=current.waiting + (item.status is JobStatus.WAITING),
			)
		counts[item.community_id] = current
	return counts


class CardState(StrEnum):
	"""Состояние сообщества на карточке — плашка справа от метрик.

	Ровно одно на карточку; при совпадении причин действует приоритет
	:func:`card_state`: выключено → ошибки → нет публикатора.
	"""

	NORMAL = "normal"  # штатно, плашки нет
	ERRORS = "errors"  # в очереди есть элементы с ошибкой
	NO_PUBLISHER = "no_publisher"  # ни userbot-публикатора, ни бота
	DISABLED = "disabled"  # выключено переключателем активности


def card_state(community: CommunityDto, counts: QueueCounts) -> CardState:
	"""Состояние карточки по приоритету «выключено → ошибки → нет публикатора».

	Выключенное сообщество главнее прочего: пока оно выключено, очередь
	не разбирается и ошибки не чинятся; ошибки главнее отсутствия
	публикатора — они уже случились, а публикатор ещё может вернуться.
	"""
	if not community.enabled:
		return CardState.DISABLED
	if counts.errors > 0:
		return CardState.ERRORS
	caps = community.capabilities
	if not caps.userbot and not caps.bot:
		return CardState.NO_PUBLISHER
	return CardState.NORMAL


class CardAction(StrEnum):
	"""Быстрое действие с карточки (и из контекстного меню строки)."""

	PUBLISH = "publish"  # «Публикация» с этим сообществом
	SCHEDULE = "schedule"  # «Расписание» с фильтром по сообществу
	QUEUE = "queue"  # окно «Вся очередь…» с фильтром по сообществу
	ASSIGN_PUBLISHER = "assign_publisher"  # диалог «Участники…»
	ENABLE = "enable"  # включить сообщество
	MAINTENANCE = "maintenance"  # окно обслуживания (ADR-0026)


#: Подписи действий (кнопка карточки и пункт меню строки — одни и те же).
ACTION_LABELS: dict[CardAction, str] = {
	CardAction.PUBLISH: "Опубликовать",
	CardAction.SCHEDULE: "Расписание",
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
	«Расписания» — «Обслуживание» (уборка нужна именно группам);
	непустая очередь заслуживает кнопки «Очередь» вместо «Расписания».
	"""
	state = card_state(community, counts)
	if state is CardState.DISABLED:
		return (CardAction.ENABLE, CardAction.MAINTENANCE)
	if state is CardState.NO_PUBLISHER:
		return (CardAction.ASSIGN_PUBLISHER,)
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


def state_badge_text(state: CardState, counts: QueueCounts) -> str | None:
	"""Текст плашки состояния карточки; None — плашка не нужна."""
	if state is CardState.ERRORS:
		return f"{counts.errors} {plural(counts.errors, 'ошибка', 'ошибки', 'ошибок')}"
	if state is CardState.NO_PUBLISHER:
		return "нет публикатора"
	if state is CardState.DISABLED:
		return "выключено"
	return None


@dataclass(frozen=True)
class MetricsText:
	"""Тексты группы метрик карточки.

	Attributes:
		queue: очередь приложения: «5 к отправке · 2 ждут», «очередь
			пуста» или «Очередь не разбирается» (выключено).
		scheduled: отложенные на сервере Telegram: «3 отложено» или
			«нет данных» (кэш статистики пуст); None — не показывать
			(у выключенного сообщества вся группа — одна фраза).
	"""

	queue: str
	scheduled: str | None


def metrics_text(
	community: CommunityDto, counts: QueueCounts, stats: CommunityStatsDto | None
) -> MetricsText:
	"""Тексты метрик карточки (числа словами, без скобок).

	Прежняя запись «5 (2)» не читалась без подсказки — теперь «ждут
	слота» написано словом; «· N ждут» показывается только при
	ненулевом N.
	"""
	if not community.enabled:
		return MetricsText("Очередь не разбирается", None)
	if counts.planned == 0:
		queue = "очередь пуста"
	else:
		queue = f"{counts.planned} к отправке"
		if counts.waiting:
			queue += f" · {counts.waiting} {plural(counts.waiting, 'ждёт', 'ждут', 'ждут')}"
	scheduled_count = stats.scheduled_count if stats is not None else None
	scheduled = "нет данных" if scheduled_count is None else f"{scheduled_count} отложено"
	return MetricsText(queue, scheduled)


def subtitle_text(community: CommunityDto, stats: CommunityStatsDto | None) -> str:
	"""Подстрочник шапки карточки: «@имя · 18 420 подписчиков».

	Без @имени — «имя не задано»; без кэша статистики — только @имя.
	"""
	name = f"@{community.username}" if community.username else "имя не задано"
	participants = stats.participants if stats is not None else None
	if participants is None:
		return name
	return f"{name} · {format_count(participants)} {audience_word(community.kind, participants)}"


def audience_word(kind: CommunityKind, count: int) -> str:
	"""«подписчик(и/ов)» у канала, «участник(а/ов)» у группы — по числу."""
	if kind is CommunityKind.GROUP:
		return plural(count, "участник", "участника", "участников")
	return plural(count, "подписчик", "подписчика", "подписчиков")


def grid_columns(width: int, card_min: int = CARD_MIN_WIDTH, spacing: int = GRID_SPACING) -> int:
	"""Сколько колонок карточек помещается в ширину (не меньше одной).

	Карточка занимает не меньше ``card_min``; между колонками —
	``spacing``. Колонки затем растягиваются на всю ширину.
	"""
	return max(1, (width + spacing) // (card_min + spacing))


def matches_search(community: CommunityDto, query: str) -> bool:
	"""Проходит ли сообщество поиск по названию и @имени (регистр не важен).

	Собака в запросе не мешает: «@kino» находит то же, что «kino».
	"""
	needle = query.strip().casefold().lstrip("@")
	if not needle:
		return True
	return needle in community.title.casefold() or needle in (community.username or "").casefold()


def view_from_setting(value: str) -> str:
	"""Вид дашборда из настройки; незнакомое значение — плитка."""
	return VIEW_LIST if value == VIEW_LIST else VIEW_TILES


@dataclass(frozen=True)
class SummaryCounts:
	"""Числа строки сводки над разделами.

	Attributes:
		queued: элементов в очереди отправки по всем сообществам
			(к отправке и ошибки — всё, что ещё не ушло).
		enabled: включённых сообществ.
		total: подключённых сообществ.
		errors: элементов очереди с ошибкой.
		without_publisher: сообществ без единого способа публикации.
	"""

	queued: int
	enabled: int
	total: int
	errors: int
	without_publisher: int


def summary_counts(
	communities: list[CommunityDto], counts: dict[int, QueueCounts]
) -> SummaryCounts:
	"""Считает строку сводки (фильтр поиска на неё не влияет)."""
	return SummaryCounts(
		queued=sum(item.planned + item.errors for item in counts.values()),
		enabled=sum(1 for c in communities if c.enabled),
		total=len(communities),
		errors=sum(item.errors for item in counts.values()),
		without_publisher=sum(
			1 for c in communities if not c.capabilities.userbot and not c.capabilities.bot
		),
	)


@dataclass(frozen=True)
class Row:
	"""Сообщество со своими данными очереди и статистики — единица показа."""

	community: CommunityDto
	counts: QueueCounts
	stats: CommunityStatsDto | None


class TableColumn(StrEnum):
	"""Колонка таблицы (клик по заголовку сортирует)."""

	TITLE = "title"
	PARTICIPANTS = "participants"
	QUEUE = "queue"
	SCHEDULED = "scheduled"
	STATE = "state"


#: Порядок состояний при сортировке по колонке «Состояние»: требующие
#: внимания — первыми.
_STATE_RANK = {
	CardState.ERRORS: 0,
	CardState.NO_PUBLISHER: 1,
	CardState.DISABLED: 2,
	CardState.NORMAL: 3,
}


def sort_rows(rows: list[Row], column: TableColumn, descending: bool) -> list[Row]:
	"""Сортирует строки таблицы по колонке; ничьи — по названию.

	Отсутствующие данные (кэш пуст) считаются меньше нуля: при
	сортировке по убыванию они уходят в конец, где им и место.
	"""

	def key(row: Row) -> tuple[object, str]:
		community, counts, stats = row.community, row.counts, row.stats
		title = community.title.casefold()
		if column is TableColumn.PARTICIPANTS:
			value = stats.participants if stats is not None else None
			return (-1 if value is None else value, title)
		if column is TableColumn.QUEUE:
			return (counts.planned, title)
		if column is TableColumn.SCHEDULED:
			value = stats.scheduled_count if stats is not None else None
			return (-1 if value is None else value, title)
		if column is TableColumn.STATE:
			return (_STATE_RANK[card_state(community, counts)], title)
		return (title, title)

	return sorted(rows, key=key, reverse=descending)


# --- оформление -----------------------------------------------------------------


def _pick(pair: tuple[str, str]) -> str:
	"""Значение пары «светлая, тёмная» по текущей теме."""
	return pair[1] if isDarkTheme() else pair[0]


def _font(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
	"""Шрифт библиотеки нужного кегля (уважает масштаб из настроек)."""
	font: QFont = getFont(size, weight)
	return font


def _hairline(parent: QWidget, colors: tuple[str, str] = _HAIRLINE) -> QFrame:
	"""Горизонтальная линия в 1 пиксель."""
	line = QFrame(parent)
	line.setFixedHeight(1)
	line.setStyleSheet(f"background: {_pick(colors)};")
	line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
	return line


def _badge(
	parent: QWidget,
	text: str,
	border: tuple[str, str],
	color: tuple[str, str],
	*,
	height: int = _BADGE_HEIGHT,
	padding: int = 8,
) -> QLabel:
	"""Плашка-обводка: состояние карточки, строка таблицы, сводка."""
	label = QLabel(text, parent)
	label.setFixedHeight(height)
	label.setFont(_font(12))
	label.setAlignment(Qt.AlignmentFlag.AlignCenter)
	label.setStyleSheet(
		f"QLabel {{ border: 1px solid {_pick(border)}; border-radius: 4px; "
		f"color: {_pick(color)}; padding: 0 {padding}px; background: transparent; }}"
	)
	return label


def _colored(label: QLabel, color: tuple[str, str]) -> QLabel:
	"""Красит текст надписи по паре цветов (без фона и рамки)."""
	label.setStyleSheet(f"color: {_pick(color)}; background: transparent;")
	return label


def _rich_metric(text: str, number_color: tuple[str, str], tail_color: tuple[str, str]) -> str:
	"""Разметка метрики: числа — жирным цветом заголовка, слова — приглушённо."""
	bold = f'<span style="color:{_pick(number_color)}; font-weight:600">'
	marked = _DIGITS.sub(lambda match: f"{bold}{match.group(0)}</span>", text)
	return f'<span style="color:{_pick(tail_color)}">{marked}</span>'


class _OutlineButton(QPushButton):
	"""Кнопка-обводка карточки (26 пикселей, радиус 4).

	Чистый ``QPushButton``, а не библиотечный ``PushButton``: у того
	свой лист стилей и высота 33 — переопределять его нельзя (ADR-0023,
	п. 5), а 26-пиксельную обводку макета иначе не собрать.
	"""

	def __init__(self, text: str, parent: QWidget, *, accent: bool = False) -> None:
		super().__init__(text, parent)
		self.setFixedHeight(_ACTION_HEIGHT)
		self.setFont(_font(12))
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
		border = _ACCENT_BUTTON_BORDER if accent else _BUTTON_BORDER
		color = _ACCENT_TEXT if accent else _TEXT_COLOR
		hover = _ACCENT_HOVER if accent else _BUTTON_HOVER
		self.setStyleSheet(
			f"QPushButton {{ border: 1px solid {_pick(border)}; border-radius: 4px; "
			f"color: {_pick(color)}; padding: 0 11px; background: transparent; }}"
			f"QPushButton:hover {{ background: {_pick(hover)}; }}"
			f"QPushButton:disabled {{ color: {_pick(_DIM_COLOR)}; "
			f"border-color: {_pick(_ROW_BORDER)}; }}"
		)


# --- карточка (вид «плитка») ------------------------------------------------------


class CommunityCard(CardWidget):
	"""Карточка сообщества: шапка, хайрлайн, метрики и состояние, действия.

	Ширины у карточки нет — её даёт колонка сетки (:class:`_TileGrid`);
	высота — не меньше минимальной, растёт по содержимому.
	"""

	def __init__(
		self,
		row: Row,
		on_action: Callable[[CardAction, CommunityDto], None],
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		community, counts = row.community, row.counts
		self._state = card_state(community, counts)
		self._border = _pick_color(_ERROR_CARD_BORDER) if self._state is CardState.ERRORS else None
		self.setMinimumHeight(_CARD_MIN_HEIGHT)
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 12, 16, 10)
		layout.setSpacing(9)
		layout.addWidget(self._header(row))
		layout.addWidget(_hairline(self, _CARD_HAIRLINE))
		layout.addWidget(self._metrics(row))
		layout.addStretch()
		layout.addWidget(self._actions(community, counts, on_action))
		if self._state is CardState.DISABLED:
			_dim(self, _DISABLED_CARD_OPACITY)

	def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 — API Qt
		"""Рамка карточки с ошибками — цветом ошибки поверх штатной.

		Рисуется своим пером, а не стилем: лист стилей карточки
		принадлежит библиотеке (ADR-0023, п. 5).
		"""
		super().paintEvent(event)
		if self._border is None:
			return
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		painter.setPen(QPen(self._border, 1.0))
		painter.setBrush(Qt.BrushStyle.NoBrush)
		radius = float(self.borderRadius)
		painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

	def _header(self, row: Row) -> QWidget:
		"""Шапка: логотип, название и подстрочник (@имя · подписчики)."""
		community, stats = row.community, row.stats
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)
		avatar_path = stats.avatar_path if stats is not None else None
		layout.addWidget(
			community_logo(box, community.id, community.title, avatar_path, _CARD_LOGO_SIZE)
		)
		column = QVBoxLayout()
		column.setSpacing(2)
		title = StrongBodyLabel(box)
		# «занимай, что дадут»: иначе длинное название требовало бы свою
		# ширину и распирало карточку, а сокращать было бы нечего
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(title, community.title)
		column.addWidget(title)
		details = CaptionLabel(box)
		details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(details, subtitle_text(community, stats))
		column.addWidget(details)
		layout.addLayout(column, stretch=1)
		return box

	def _metrics(self, row: Row) -> QWidget:
		"""Метрики слева (сжимаемые) и плашка состояния справа (нет)."""
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(16)
		group = QWidget(box)
		# группа метрик уступает место плашке: та говорит о проблеме
		# и обрезаться не должна
		group.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		group_layout = QHBoxLayout(group)
		group_layout.setContentsMargins(0, 0, 0, 0)
		group_layout.setSpacing(16)
		texts = metrics_text(row.community, row.counts, row.stats)
		for text in (texts.queue, texts.scheduled):
			if text is None:
				continue
			label = QLabel(_rich_metric(text, _TITLE_COLOR, _MUTED_COLOR), group)
			label.setFont(_font(13))
			label.setStyleSheet("background: transparent;")
			group_layout.addWidget(label)
		group_layout.addStretch()
		layout.addWidget(group, stretch=1)
		badge = self._state_badge(box, row.counts)
		if badge is not None:
			layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignRight)
		return box

	def _state_badge(self, parent: QWidget, counts: QueueCounts) -> QLabel | None:
		"""Плашка состояния (одна из трёх) или ничего в штатном случае."""
		text = state_badge_text(self._state, counts)
		if text is None:
			return None
		return _state_badge(parent, self._state, text)

	def _actions(
		self,
		community: CommunityDto,
		counts: QueueCounts,
		on_action: Callable[[CardAction, CommunityDto], None],
	) -> QWidget:
		"""Строка кнопок-обводок; набор — по состоянию карточки.

		Кнопки перехватывают свои нажатия сами: клик по кнопке
		не открывает страницу сообщества, клик мимо неё — открывает.
		"""
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(8)
		for action in card_actions(community, counts):
			button = _OutlineButton(
				ACTION_LABELS[action], box, accent=action is CardAction.ASSIGN_PUBLISHER
			)
			if not action_available(action, community):
				button.setEnabled(False)
				button.setToolTip(_MAINTENANCE_UNAVAILABLE)
			button.clicked.connect(partial(on_action, action, community))
			layout.addWidget(button)
		layout.addStretch()
		return box


#: Подсказка неактивного «Обслуживания» — та же, что на странице сообщества.
_MAINTENANCE_UNAVAILABLE = "Нужен userbot-публикатор: боту история и участники недоступны"


def _pick_color(pair: tuple[QColor, QColor]) -> QColor:
	"""Цвет пары «светлая, тёмная» по текущей теме (для рисования пером)."""
	return pair[1] if isDarkTheme() else pair[0]


def _dim(widget: QWidget, opacity: float) -> None:
	"""Приглушает виджет целиком (выключенное сообщество)."""
	effect = QGraphicsOpacityEffect(widget)
	effect.setOpacity(opacity)
	widget.setGraphicsEffect(effect)


def _state_badge(parent: QWidget, state: CardState, text: str) -> QLabel:
	"""Плашка состояния: цвет по виду состояния."""
	if state is CardState.ERRORS:
		return _badge(parent, text, _ERROR_BORDER, _ERROR_TEXT)
	if state is CardState.NO_PUBLISHER:
		return _badge(parent, text, _ACCENT_BORDER, _ACCENT_TEXT)
	return _badge(parent, text, _NEUTRAL_BORDER, _TEXT_COLOR)


class _TileGrid(QWidget):
	"""Сетка карточек с растяжением: число колонок — по своей ширине.

	Пересборка идёт только при смене числа колонок: на каждое движение
	окна перекладывать карточки незачем.
	"""

	def __init__(self, cards: list[QWidget], parent: QWidget) -> None:
		super().__init__(parent)
		self._cards = cards
		self._columns = 0
		# высота — строго по содержимому: лишнее место страницы забирает
		# её растяжка, а не сетка (иначе карточки последнего раздела
		# вытягивались бы вниз вместе с сеткой)
		self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
		self._grid = QGridLayout(self)
		self._grid.setContentsMargins(0, 0, 0, 0)
		self._grid.setHorizontalSpacing(GRID_SPACING)
		self._grid.setVerticalSpacing(GRID_SPACING)
		self._place(1)

	def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — API Qt
		super().resizeEvent(event)
		columns = grid_columns(self.width())
		if columns != self._columns:
			self._place(columns)

	def _place(self, columns: int) -> None:
		"""Раскладывает карточки по колонкам, колонки — на всю ширину."""
		previous = self._columns
		self._columns = columns
		while self._grid.count():
			self._grid.takeAt(0)
		for index, card in enumerate(self._cards):
			self._grid.addWidget(card, index // columns, index % columns)
		for column in range(max(columns, previous)):
			self._grid.setColumnStretch(column, 1 if column < columns else 0)


# --- таблица (вид «список») -------------------------------------------------------


class _HeaderCell(QLabel):
	"""Заголовок колонки: клик сортирует."""

	def __init__(self, text: str, on_click: Callable[[], None], parent: QWidget) -> None:
		super().__init__(text, parent)
		self._on_click = on_click
		self.setFont(_font(12))
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		_colored(self, _MUTED_COLOR)

	def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		inside = self.rect().contains(event.position().toPoint())
		if event.button() is Qt.MouseButton.LeftButton and inside:
			self._on_click()
		super().mouseReleaseEvent(event)


class _TableRow(QFrame):
	"""Строка таблицы: клик открывает страницу, правый клик — меню действий."""

	def __init__(
		self,
		row: Row,
		on_open: Callable[[int], None],
		on_action: Callable[[CardAction, CommunityDto], None],
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self._row = row
		self._on_open = on_open
		self._on_action = on_action
		self.setFixedHeight(_TABLE_ROW_HEIGHT)
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		self.setObjectName("communityRow")
		self.setStyleSheet(
			f"#communityRow {{ border-top: 1px solid {_pick(_ROW_BORDER)}; "
			"background: transparent; }"
		)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(16, 0, 16, 0)
		layout.setSpacing(12)
		layout.addWidget(self._name_cell(), stretch=1)
		community, counts, stats = row.community, row.counts, row.stats
		participants = stats.participants if stats is not None else None
		layout.addWidget(self._number(participants, _COLUMN_WIDTHS["participants"]))
		layout.addWidget(self._queue_cell(counts))
		scheduled = stats.scheduled_count if stats is not None else None
		layout.addWidget(self._number(scheduled, _COLUMN_WIDTHS["scheduled"]))
		layout.addWidget(self._state_cell(community, counts))
		if not community.enabled:
			_dim(self, _DISABLED_ROW_OPACITY)

	def _name_cell(self) -> QWidget:
		"""Логотип, название и @имя."""
		community, stats = self._row.community, self._row.stats
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(10)
		avatar_path = stats.avatar_path if stats is not None else None
		layout.addWidget(
			community_logo(box, community.id, community.title, avatar_path, _ROW_LOGO_SIZE)
		)
		column = QVBoxLayout()
		column.setSpacing(1)
		title = QLabel(box)
		title.setFont(_font(14))
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		_colored(title, _TITLE_COLOR)
		elide_text(title, community.title)
		column.addWidget(title)
		name = QLabel(box)
		name.setFont(_font(12))
		name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		_colored(name, _DIM_COLOR)
		elide_text(name, f"@{community.username}" if community.username else "имя не задано")
		column.addWidget(name)
		layout.addLayout(column, stretch=1)
		return box

	def _number(self, value: int | None, width: int) -> QLabel:
		"""Числовая ячейка, выровненная вправо; ноль и «—» приглушены."""
		label = QLabel("—" if value is None else format_count(value), self)
		label.setFixedWidth(width)
		label.setFont(_font(13))
		label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		_colored(label, _DIM_COLOR if not value else _TEXT_COLOR)
		return label

	def _queue_cell(self, counts: QueueCounts) -> QLabel:
		"""«К отправке»: число и «+N» ждущих слота приглушённым."""
		label = self._number(counts.planned, _COLUMN_WIDTHS["queue"])
		if counts.waiting:
			label.setText(
				f'<span style="color:{_pick(_TEXT_COLOR)}">{counts.planned}</span> '
				f'<span style="color:{_pick(_DIM_COLOR)}">+{counts.waiting}</span>'
			)
		return label

	def _state_cell(self, community: CommunityDto, counts: QueueCounts) -> QWidget:
		"""Состояние: плашка или прочерк."""
		box = QWidget(self)
		box.setFixedWidth(_COLUMN_WIDTHS["state"])
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		state = card_state(community, counts)
		text = state_badge_text(state, counts)
		if text is None:
			dash = QLabel("—", box)
			dash.setFont(_font(13))
			layout.addWidget(_colored(dash, _DIM_COLOR))
		else:
			layout.addWidget(_state_badge(box, state, text))
		layout.addStretch()
		return box

	def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		inside = self.rect().contains(event.position().toPoint())
		if event.button() is Qt.MouseButton.LeftButton and inside:
			self._on_open(self._row.community.id)
		super().mouseReleaseEvent(event)

	def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802 — API Qt
		"""Меню действий — тот же набор, что кнопками на карточке."""
		community, counts = self._row.community, self._row.counts
		menu = RoundMenu(parent=self)
		for action in card_actions(community, counts):
			item = Action(ACTION_LABELS[action], menu)
			if not action_available(action, community):
				item.setEnabled(False)
				item.setToolTip(_MAINTENANCE_UNAVAILABLE)
			item.triggered.connect(partial(self._on_action, action, community))
			menu.addAction(item)
		menu.exec(event.globalPos())


#: Заголовки колонок таблицы; «аудитория» зависит от вида раздела.
_COLUMN_TITLES: dict[TableColumn, str] = {
	TableColumn.TITLE: "Название",
	TableColumn.QUEUE: "К отправке",
	TableColumn.SCHEDULED: "Отложено",
	TableColumn.STATE: "Состояние",
}


class _Table(QFrame):
	"""Таблица раздела: шапка с сортировкой и строки сообществ."""

	def __init__(
		self,
		kind: CommunityKind,
		rows: list[Row],
		sort: tuple[TableColumn, bool],
		on_sort: Callable[[TableColumn], None],
		on_open: Callable[[int], None],
		on_action: Callable[[CardAction, CommunityDto], None],
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self.setObjectName("communityTable")
		# как у сетки карточек: высота по содержимому, не по странице
		self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
		self.setStyleSheet(
			f"#communityTable {{ border: 1px solid {_pick(_TABLE_BORDER)}; "
			"border-radius: 6px; background: transparent; }"
		)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		layout.addWidget(self._header(kind, sort, on_sort))
		column, descending = sort
		for row in sort_rows(rows, column, descending):
			layout.addWidget(_TableRow(row, on_open, on_action, self))

	def _header(
		self,
		kind: CommunityKind,
		sort: tuple[TableColumn, bool],
		on_sort: Callable[[TableColumn], None],
	) -> QWidget:
		"""Шапка таблицы: подписи колонок с признаком сортировки."""
		box = QFrame(self)
		box.setObjectName("communityTableHeader")
		box.setFixedHeight(_TABLE_HEADER_HEIGHT)
		box.setStyleSheet(
			f"#communityTableHeader {{ background: {_pick(_TABLE_HEADER_BG)}; "
			"border-top-left-radius: 6px; border-top-right-radius: 6px; }"
		)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(16, 0, 16, 0)
		layout.setSpacing(12)
		sorted_column, descending = sort
		audience = "Участники" if kind is CommunityKind.GROUP else "Подписчики"
		widths = {
			TableColumn.PARTICIPANTS: _COLUMN_WIDTHS["participants"],
			TableColumn.QUEUE: _COLUMN_WIDTHS["queue"],
			TableColumn.SCHEDULED: _COLUMN_WIDTHS["scheduled"],
			TableColumn.STATE: _COLUMN_WIDTHS["state"],
		}
		for column in TableColumn:
			title = _COLUMN_TITLES.get(column, audience)
			if column is sorted_column:
				title += " ▼" if descending else " ▲"
			cell = _HeaderCell(title, partial(on_sort, column), box)
			if column is TableColumn.TITLE:
				layout.addWidget(cell, stretch=1)
				continue
			cell.setFixedWidth(widths[column])
			right = column in (TableColumn.PARTICIPANTS, TableColumn.QUEUE, TableColumn.SCHEDULED)
			cell.setAlignment(
				(Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft)
				| Qt.AlignmentFlag.AlignVCenter
			)
			layout.addWidget(cell)
		return box


# --- диалог подключения --------------------------------------------------------------


class _ConnectDialog(MessageBoxBase):
	"""Диалог подключения: способ (userbot/бот), исполнитель, ссылка.

	Для userbot-способа аккаунт выбирается явно (ADR-0019): его права
	проверяются, и именно он привязывается к каналу как публикатор.
	"""

	_HINTS = {
		"userbot": (
			"Каналу аккаунт нужен администратором с правом публиковать;\n"
			"группе достаточно участника без ограничений (бот не нужен)."
		),
		"bot": (
			"В канал добавьте бота администратором с правом публиковать;\n"
			"в группу — участником (админство не требуется)."
		),
	}

	def __init__(self, bots: list[BotDto], accounts: list[TgAccountDto], parent: QWidget) -> None:
		"""``accounts`` — вошедшие userbot-аккаунты (кандидаты в админы)."""
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Подключить канал или группу", self))
		self._way = ComboBox(self)
		self._way.addItem("Через userbot (приоритетный способ)")
		self._way.addItem("Через бота")
		self._way.currentIndexChanged.connect(self._on_way_changed)
		self.viewLayout.addWidget(self._way)
		self._hint = BodyLabel("", self)
		self.viewLayout.addWidget(self._hint)
		self._account_combo: DtoComboBox[TgAccountDto] = DtoComboBox(self)
		self._account_combo.set_items(
			accounts, label=lambda acc: account_caption(acc.display, acc.phone)
		)
		self.viewLayout.addWidget(self._account_combo)
		self._combo: DtoComboBox[BotDto] = DtoComboBox(self)
		self._combo.set_items(bots, label=lambda bot: bot_caption(bot.label, bot.username))
		self.viewLayout.addWidget(self._combo)
		self._ref = LineEdit(self)
		self._ref.setPlaceholderText("@имя, ссылка t.me/… или ID -100…")
		self._ref.setClearButtonEnabled(True)
		self.viewLayout.addWidget(self._ref)
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("Подключить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(460)
		self._on_way_changed(0)

	def validate(self) -> bool:
		"""Крючок MessageBoxBase: при ошибке диалог не закрывается —
		введённая ссылка не пропадает."""
		if not self.chat_ref():
			return self._error.fail("Укажите @имя, ссылку или ID канала либо группы.")
		if self.way() == "bot" and self.bot_id() is None:
			return self._error.fail("Сначала добавьте бота: Настройки → Аккаунты.")
		if self.way() == "userbot" and self.account_id() is None:
			return self._error.fail(
				"Нет вошедших userbot-аккаунтов — войдите: Настройки → Аккаунты."
			)
		return self._error.succeed()

	def _on_way_changed(self, index: int) -> None:
		"""Показывает выбор исполнителя своего способа."""
		self._combo.setVisible(index == 1)
		self._account_combo.setVisible(index == 0)
		self._hint.setText(self._HINTS["bot" if index == 1 else "userbot"])

	def way(self) -> str:
		"""Способ подключения: 'userbot' или 'bot'."""
		return "bot" if int(self._way.currentIndex()) == 1 else "userbot"

	def bot_id(self) -> int | None:
		"""Идентификатор выбранного бота (None — ботов нет)."""
		bot = self._combo.selected()
		return bot.id if bot is not None else None

	def account_id(self) -> int | None:
		"""Идентификатор выбранного userbot-аккаунта (None — вошедших нет)."""
		account = self._account_combo.selected()
		return account.id if account is not None else None

	def chat_ref(self) -> str:
		"""Введённая ссылка на сообщество."""
		return str(self._ref.text()).strip()


# --- страница -------------------------------------------------------------------


class CommunitiesPage(ScrollArea):
	"""Дашборд сообществ: шапка, сводка, разделы «Каналы» и «Группы».

	Сигналы для главного окна: ``communities_changed`` — свежий список
	(синхронизация подменю навигации), ``open_community`` — клик
	по карточке или строке (переход на страницу сообщества),
	``publish_requested`` — «Опубликовать» (страница «Публикация»
	с предвыбранным сообществом), ``schedule_requested`` —
	«Расписание» (страница «Расписание» с фильтром по сообществу).
	"""

	communities_changed = Signal(list)
	open_community = Signal(int)
	publish_requested = Signal(int)
	schedule_requested = Signal(int)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("communities")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._communities: list[CommunityDto] = []
		self._queue_counts: dict[int, QueueCounts] = {}
		self._stats_cache: dict[int, CommunityStatsDto] = {}
		self._stats_refreshing = False
		self._view = VIEW_TILES
		self._query = ""
		self._sort: tuple[TableColumn, bool] = (TableColumn.TITLE, False)
		# применение сохранённого вида не должно записывать его обратно
		self._applying_view = False
		self._build()
		run_in_engine(
			worker,
			worker.engine.settings.get(UI_COMMUNITIES_VIEW),
			self,
			self._apply_view_setting,
			# вид — удобство: не прочитался — остаётся плитка
			noop,
		)

	def _build(self) -> None:
		"""Собирает шапку, строку сводки и область разделов."""
		layout = page_layout(self)
		header = QHBoxLayout()
		header.setSpacing(12)
		header.addWidget(SubtitleLabel("Каналы и группы", self))
		header.addStretch()
		self._search = SearchLineEdit(self)
		self._search.setPlaceholderText("Поиск")
		self._search.setFixedWidth(_SEARCH_WIDTH)
		self._search.setToolTip("По названию и @имени, регистр не важен")
		self._search.textChanged.connect(self._on_search_changed)
		header.addWidget(self._search)
		self._view_switch = SegmentedToolWidget(self)
		self._view_switch.addItem(VIEW_TILES, FluentIcon.TILES).setToolTip("Плитка")
		self._view_switch.addItem(VIEW_LIST, FluentIcon.MENU).setToolTip("Список")
		self._view_switch.setCurrentItem(VIEW_TILES)
		self._view_switch.currentItemChanged.connect(self._on_view_changed)
		header.addWidget(self._view_switch)
		connect_button = PrimaryPushButton(FluentIcon.ADD, "Подключить…", self)
		connect_button.clicked.connect(self._on_connect)
		header.addWidget(connect_button)
		layout.addLayout(header)
		self._summary_box = QVBoxLayout()
		layout.addLayout(self._summary_box)
		self._sections = QVBoxLayout()
		# интервал блоков — из плотности (16 обычный, 10 компактный)
		self._sections.setSpacing(density.spacing().block_spacing)
		layout.addLayout(self._sections)
		layout.addStretch()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет данные при каждом показе: страница возвратная
		(с неё уходят на страницы сообществ и приходят обратно)."""
		super().showEvent(event)
		self.reload()

	# --- данные -----------------------------------------------------------------

	def reload(self) -> None:
		"""Перечитывает сообщества и очередь отправки из движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._on_communities_loaded,
			self._show_error,
		)

	def _on_communities_loaded(self, communities: list[CommunityDto]) -> None:
		"""Список получен — вторым шагом состояние очереди отправки."""
		self._communities = communities
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.state(),
			self,
			self._on_queue_loaded,
			self._show_error,
		)

	def _on_queue_loaded(self, items: list[QueueItemDto]) -> None:
		"""Очередь получена — считаем по сообществам план, слоты и ошибки."""
		self._queue_counts = queue_counts(items)
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._on_stats_loaded,
			self._show_error,
		)

	def _on_stats_loaded(self, stats: list[CommunityStatsDto]) -> None:
		"""Кэш статистики получен — рисуем и запускаем фоновое обновление."""
		self._stats_cache = {item.community_id: item for item in stats}
		self._render()
		self.communities_changed.emit(list(self._communities))
		self._refresh_stats_in_background()

	def _refresh_stats_in_background(self) -> None:
		"""Фоновое обновление кэша статистики (не чаще одного за раз).

		Ошибки не показываются: обновление вспомогательное, каждый сбой
		уже залогирован движком — всплывашка при каждом открытии
		страницы без сети только раздражала бы.
		"""
		if self._stats_refreshing:
			return
		self._stats_refreshing = True
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.refresh_stale(),
			self,
			self._on_stats_refreshed,
			self._on_stats_failed,
		)

	def _on_stats_failed(self, message: str) -> None:
		"""Фоновое обновление сводки не удалось — снимаем флаг «идёт».

		Плашкой не тревожим: сводка фоновая, кэш при сбое не затирается,
		и подробности уже записал мост движка. Но флаг снять обязаны,
		иначе следующее обновление не начнётся до перезапуска.
		"""
		logger.debug("Фоновое обновление статистики не удалось: %s", message)
		self._stats_refreshing = False

	def _on_stats_refreshed(self, changed: bool) -> None:
		"""Кэш обновился — перечитываем снимок (без нового обновления)."""
		self._stats_refreshing = False
		if not changed:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._on_fresh_stats,
			self._show_error,
		)

	def _on_fresh_stats(self, stats: list[CommunityStatsDto]) -> None:
		"""Свежий снимок после фонового обновления — только перерисовка."""
		self._stats_cache = {item.community_id: item for item in stats}
		self._render()

	# --- вид и поиск -----------------------------------------------------------

	def _apply_view_setting(self, value: str) -> None:
		"""Ставит переключатель в сохранённое положение (без записи)."""
		view = view_from_setting(value)
		if view == self._view:
			return
		self._applying_view = True
		try:
			self._view_switch.setCurrentItem(view)
		finally:
			self._applying_view = False
		self._view = view
		self._render_sections()

	def _on_view_changed(self, route_key: str) -> None:
		"""Переключатель: перестраиваются только тела разделов."""
		view = view_from_setting(route_key)
		if self._applying_view or view == self._view:
			return
		self._view = view
		self._render_sections()
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(UI_COMMUNITIES_VIEW, view),
			self,
			noop,
			self._show_error,
		)

	def _on_search_changed(self, text: str) -> None:
		"""Поиск фильтрует на клиенте — без обращения к движку."""
		self._query = text
		self._render_sections()

	def _on_sort(self, column: TableColumn) -> None:
		"""Клик по заголовку: та же колонка меняет направление."""
		current, descending = self._sort
		self._sort = (column, not descending if column is current else False)
		self._render_sections()

	# --- отрисовка ---------------------------------------------------------------

	def _render(self) -> None:
		"""Перерисовывает сводку и разделы."""
		self._render_summary()
		self._render_sections()

	def _render_summary(self) -> None:
		"""Строка сводки: очередь, активные, плашки ошибок и без публикатора."""
		clear_layout(self._summary_box)
		if not self._communities:
			return
		totals = summary_counts(self._communities, self._queue_counts)
		bar = QFrame(self)
		bar.setObjectName("communitySummary")
		bar.setFixedHeight(_SUMMARY_HEIGHT)
		bar.setStyleSheet(
			f"#communitySummary {{ background: {_pick(_SUMMARY_BG)}; border-radius: 6px; "
			f"border: 1px solid {_pick(_SUMMARY_BORDER)}; }}"
		)
		layout = QHBoxLayout(bar)
		layout.setContentsMargins(4, 0, 4, 0)
		layout.setSpacing(0)
		layout.addWidget(self._summary_segment(bar, totals.queued, "в очереди отправки"))
		layout.addWidget(self._divider(bar))
		layout.addWidget(self._summary_segment(bar, totals.enabled, f"активных из {totals.total}"))
		if totals.errors:
			layout.addWidget(self._divider(bar))
			layout.addWidget(self._errors_badge(bar, totals.errors))
		if totals.without_publisher:
			layout.addWidget(self._divider(bar))
			layout.addWidget(
				_badge(
					bar,
					f"{totals.without_publisher} без публикатора",
					_ACCENT_BORDER,
					_ACCENT_TEXT,
					height=_SUMMARY_BADGE_HEIGHT,
					padding=14,
				)
			)
		layout.addStretch()
		self._summary_box.addWidget(bar)

	@staticmethod
	def _summary_segment(parent: QWidget, number: int, tail: str) -> QLabel:
		"""Сегмент сводки: число жирным и подпись прозой."""
		label = QLabel(
			f'<span style="color:{_pick(_TITLE_COLOR)}; font-weight:600">{number}</span> '
			f'<span style="color:{_pick(_TEXT_COLOR)}">{tail}</span>',
			parent,
		)
		label.setFont(_font(13))
		label.setContentsMargins(14, 0, 14, 0)
		label.setStyleSheet("background: transparent;")
		return label

	@staticmethod
	def _divider(parent: QWidget) -> QFrame:
		"""Вертикальный разделитель сегментов сводки."""
		line = QFrame(parent)
		line.setFixedSize(1, 18)
		line.setStyleSheet(f"background: {_pick(_DIVIDER)};")
		return line

	def _errors_badge(self, parent: QWidget, errors: int) -> QPushButton:
		"""Плашка ошибок — кнопка: открывает очередь с фильтром «ошибки»."""
		button = QPushButton(f"{errors} {plural(errors, 'ошибка', 'ошибки', 'ошибок')}", parent)
		button.setFixedHeight(_SUMMARY_BADGE_HEIGHT)
		button.setFont(_font(12))
		button.setCursor(Qt.CursorShape.PointingHandCursor)
		button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
		button.setToolTip("Элементы очереди отправки с ошибкой — открыть очередь")
		button.setStyleSheet(
			f"QPushButton {{ border: 1px solid {_pick(_ERROR_BORDER)}; border-radius: 4px; "
			f"color: {_pick(_ERROR_TEXT)}; padding: 0 14px; background: transparent; }}"
			f"QPushButton:hover {{ background: {_pick(_ERROR_HOVER)}; }}"
		)
		button.clicked.connect(self._open_errors)
		return button

	def _render_sections(self) -> None:
		"""Разделы «Каналы» и «Группы» по текущему поиску и виду."""
		clear_layout(self._sections)
		if not self._communities:
			self._sections.addWidget(self._empty_state(searched=False))
			return
		rows = [
			Row(
				community,
				self._queue_counts.get(community.id, QueueCounts()),
				self._stats_cache.get(community.id),
			)
			for community in self._communities
			if matches_search(community, self._query)
		]
		if not rows:
			self._sections.addWidget(self._empty_state(searched=True))
			return
		for kind, title, icon in (
			(CommunityKind.CHANNEL, "Каналы", FluentIcon.CHAT),
			(CommunityKind.GROUP, "Группы", FluentIcon.PEOPLE),
		):
			section_rows = [row for row in rows if row.community.kind is kind]
			if not section_rows:
				continue  # пустой раздел не рисуется вовсе
			self._sections.addWidget(self._section_header(title, icon, len(section_rows)))
			self._sections.addWidget(self._section_body(kind, section_rows))

	def _section_header(self, title: str, icon: FluentIcon, count: int) -> QWidget:
		"""Заголовок раздела: значок вида, подпись, число, хайрлайн."""
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)
		icon_label = QLabel(box)
		icon_label.setFixedSize(14, 14)
		icon_label.setPixmap(icon.icon(color=QColor(_pick(_MUTED_COLOR))).pixmap(14, 14))
		layout.addWidget(icon_label)
		caption = QLabel(title.upper(), box)
		font = _font(13)
		font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
		caption.setFont(font)
		layout.addWidget(_colored(caption, _MUTED_COLOR))
		counter = QLabel(str(count), box)
		counter.setFont(_font(13))
		layout.addWidget(_colored(counter, _COUNT_COLOR))
		layout.addWidget(_hairline(box), stretch=1)
		return box

	def _section_body(self, kind: CommunityKind, rows: list[Row]) -> QWidget:
		"""Тело раздела: сетка карточек или таблица — по переключателю."""
		if self._view == VIEW_LIST:
			return _Table(
				kind,
				rows,
				self._sort,
				self._on_sort,
				self.open_community.emit,
				self._run_action,
				self,
			)
		cards: list[QWidget] = []
		for row in rows:
			card = CommunityCard(row, self._run_action, self)
			card.clicked.connect(partial(self.open_community.emit, row.community.id))
			cards.append(card)
		return _TileGrid(cards, self)  # сетка усыновляет карточки

	def _empty_state(self, searched: bool) -> QWidget:
		"""Пустое состояние: ничего не подключено или поиск ничего не нашёл."""
		box = QWidget(self)
		layout = QVBoxLayout(box)
		layout.setContentsMargins(0, 48, 0, 0)
		if searched:
			title = SubtitleLabel("Ничего не найдено", box)
			hint = BodyLabel("Проверьте запрос или подключите сообщество.", box)
		else:
			title = SubtitleLabel("Пока нет подключённых каналов и групп", box)
			hint = BodyLabel("Нажмите «Подключить…»: через userbot или через бота.", box)
		title.setAlignment(Qt.AlignmentFlag.AlignCenter)
		hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title)
		layout.addWidget(hint)
		return box

	# --- действия -----------------------------------------------------------------

	def _run_action(self, action: CardAction, community: CommunityDto) -> None:
		"""Выполняет быстрое действие карточки или пункт меню строки.

		Переходы на другие страницы — сигналами главному окну; окна
		(очередь, участники, обслуживание) открываются отсюда теми же
		точками входа, что и со страницы сообщества.
		"""
		if action is CardAction.PUBLISH:
			self.publish_requested.emit(community.id)
		elif action is CardAction.SCHEDULE:
			self.schedule_requested.emit(community.id)
		elif action is CardAction.QUEUE:
			exec_dialog(QueueViewDialog(self._worker, self.window(), community_id=community.id))
		elif action is CardAction.ASSIGN_PUBLISHER:
			open_members(self._worker, community, self, self.reload)
		elif action is CardAction.ENABLE:
			run_in_engine(
				self._worker,
				self._worker.engine.settings.set_for(COMMUNITY_ENABLED, community.id, True),
				self,
				self.reload,
				self._show_error,
			)
		elif action is CardAction.MAINTENANCE:
			open_maintenance(self._worker, community, self)

	def _open_errors(self) -> None:
		"""Плашка ошибок сводки: очередь отправки с фильтром «ошибки»."""
		exec_dialog(QueueViewDialog(self._worker, self.window(), status=QueueFilter.ERRORS))

	# --- подключение -----------------------------------------------------------

	def _on_connect(self) -> None:
		"""Загружает ботов и аккаунты, затем открывает диалог подключения."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_bots(),
			self,
			self._on_connect_bots_loaded,
			self._show_error,
		)

	def _on_connect_bots_loaded(self, bots: list[BotDto]) -> None:
		"""Боты получены — вторым шагом список userbot-аккаунтов."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_tg_accounts(),
			self,
			partial(self._open_connect_dialog, bots),
			self._show_error,
		)

	def _open_connect_dialog(self, bots: list[BotDto], accounts: list[TgAccountDto]) -> None:
		logged_in = [account for account in accounts if account.logged_in]
		dialog = _ConnectDialog(bots, logged_in, self.window())
		if not exec_dialog(dialog):
			return
		# пригодность ввода проверил validate() диалога — здесь только сборка
		if dialog.way() == "bot":
			bot_id = dialog.bot_id()
			if bot_id is None:  # недостижимо после validate(), страховка типа
				self._show_error("Сначала добавьте бота: Настройки → Аккаунты.")
				return
			coro = self._worker.engine.communities.add_community(bot_id, dialog.chat_ref())
		else:
			account_id = dialog.account_id()
			if account_id is None:  # недостижимо после validate(), страховка типа
				self._show_error("Войдите в userbot-аккаунт: Настройки → Аккаунты.")
				return
			coro = self._worker.engine.communities.add_community_via_userbot(
				account_id, dialog.chat_ref()
			)
		show_info(self, "Проверка", "Проверяю сообщество и права…")
		run_in_engine(self._worker, coro, self, self._on_connected, self._show_error)

	def _on_connected(self, community: CommunityDto) -> None:
		show_success(self, "Подключено", community.title)
		self.reload()
