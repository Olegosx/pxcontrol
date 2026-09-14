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

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
	QAbstractItemView,
	QGridLayout,
	QHBoxLayout,
	QHeaderView,
	QPushButton,
	QSizePolicy,
	QTableWidgetItem,
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
	HorizontalSeparator,
	InfoBadge,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	PushButton,
	RoundMenu,
	ScrollArea,
	SearchLineEdit,
	SegmentedToolWidget,
	SimpleCardWidget,
	StrongBodyLabel,
	SubtitleLabel,
	TableWidget,
	VerticalSeparator,
	setCustomStyleSheet,
)

from pxcontrol.engine import EngineWorker
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
	QueueCounts,
	account_caption,
	bot_caption,
	clear_layout,
	community_logo,
	dim_widget,
	elide_text,
	error_reporter,
	exec_dialog,
	font_px,
	format_count,
	list_button,
	noop,
	page_layout,
	plural,
	queue_counts,
	section_header,
	show_info,
	show_success,
)
from pxcontrol.ui.pages.community_page import open_members
from pxcontrol.ui.pages.community_state import (
	ACTION_LABELS,
	MAINTENANCE_UNAVAILABLE,
	CardAction,
	CardState,
	action_available,
	card_actions,
	card_state,
	state_badge,
	state_badge_text,
	subtitle_text,
)
from pxcontrol.ui.pages.maintenance import open_maintenance
from pxcontrol.ui.pages.publish_queue_view import QueueFilter, QueueViewDialog

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
_SUMMARY_BADGE_HEIGHT = 26
_ACTION_HEIGHT = 26
_TABLE_HEADER_HEIGHT = 34
_TABLE_ROW_HEIGHT = 50

#: Кегли по макету: кнопки карточки 12.5 → 13, числа таблицы 13.5 → 13,
#: заголовок таблицы и подстрочник строки — 12.
_ACTION_FONT_PX = 13
_TABLE_FONT_PX = 13
_TABLE_HEADER_FONT_PX = 12

#: Непрозрачность выключенного сообщества по макету: карточка и строка.
_DISABLED_CARD_OPACITY = 0.62
_DISABLED_ROW_OPACITY = 0.6

#: Ширины числовых колонок таблицы (пиксели); колонка названия тянется.
_COLUMN_WIDTHS = {"participants": 96, "queue": 104, "scheduled": 92, "state": 132}

#: Ширина поля поиска в шапке (пиксели).
_SEARCH_WIDTH = 200

# --- правила показа (чистые функции) -----------------------------------------


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


_NUMBER_RE = re.compile(r"\d[\d\u202f]*")


def bold_numbers(text: str) -> str:
	"""Размечает числа в тексте полужирным (rich text для ``BodyLabel``).

	Макет карточки: «**5** к отправке · **2** ждут». Числа с узким
	неразрывным пробелом (``format_count``) считаются одним числом.
	"""
	return _NUMBER_RE.sub(lambda m: f"<b>{m.group(0)}</b>", html.escape(text))


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


# --- карточка (вид «плитка») ------------------------------------------------------


class CommunityCard(CardWidget):
	"""Карточка сообщества: шапка, разделитель, метрики и состояние, действия.

	Только штатные элементы библиотеки: надписи, ``HorizontalSeparator``,
	``InfoBadge`` состояния, кнопки. Ширины у карточки нет — её даёт
	колонка сетки (:class:`_TileGrid`); высота — не меньше минимальной.
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
		self.setMinimumHeight(_CARD_MIN_HEIGHT)
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		if not community.enabled:
			dim_widget(self, _DISABLED_CARD_OPACITY)  # макет: карточка 0.62
		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 12, 16, 10)
		layout.setSpacing(9)
		layout.addWidget(self._header(row))
		layout.addWidget(HorizontalSeparator(self))
		layout.addWidget(self._metrics(row))
		layout.addStretch()
		layout.addWidget(self._actions(community, counts, on_action))

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
		elide_text(details, subtitle_text(community, stats.participants if stats else None))
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
			if text is not None:
				label = BodyLabel(group)
				label.setTextFormat(Qt.TextFormat.RichText)
				label.setText(bold_numbers(text))  # макет: числа полужирным
				group_layout.addWidget(label)
		group_layout.addStretch()
		layout.addWidget(group, stretch=1)
		text = state_badge_text(self._state, row.counts)
		if text is not None:
			layout.addWidget(
				state_badge(box, self._state, text), alignment=Qt.AlignmentFlag.AlignRight
			)
		return box

	def _actions(
		self,
		community: CommunityDto,
		counts: QueueCounts,
		on_action: Callable[[CardAction, CommunityDto], None],
	) -> QWidget:
		"""Строка кнопок; набор — по состоянию карточки.

		Кнопки перехватывают свои нажатия сами: клик по кнопке
		не открывает страницу сообщества, клик мимо неё — открывает.
		«Назначить публикатора» — главное действие карточки без
		публикатора, поэтому ``PrimaryPushButton``.
		"""
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(8)
		for action in card_actions(community, counts):
			button: QPushButton
			if action is CardAction.ASSIGN_PUBLISHER:
				button = PrimaryPushButton(ACTION_LABELS[action], box)
			else:
				button = PushButton(ACTION_LABELS[action], box)
			button.setFixedHeight(_ACTION_HEIGHT)  # макет: 26 / 12.5 px
			button.setFont(font_px(_ACTION_FONT_PX))
			if not action_available(action, community):
				button.setEnabled(False)
				button.setToolTip(MAINTENANCE_UNAVAILABLE)
			button.clicked.connect(partial(on_action, action, community))
			layout.addWidget(button)
		layout.addStretch()
		return box


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

#: Колонки таблицы по порядку: заголовок (пустой — по виду раздела) и сортировка.
_TABLE_COLUMNS: tuple[tuple[str, TableColumn], ...] = (
	("Название", TableColumn.TITLE),
	("", TableColumn.PARTICIPANTS),
	("К отправке", TableColumn.QUEUE),
	("Отложено", TableColumn.SCHEDULED),
	("Состояние", TableColumn.STATE),
)

#: Добавка к стилю таблицы (официальный ``setCustomStyleSheet``): радиус
#: рамки 6 и заголовок без вертикальных разделителей — как в макете.
_TABLE_QSS = (
	"QTableView{border-radius: 6px}"
	"QHeaderView::section:horizontal{border: none;"
	" border-bottom: 1px solid rgba(255, 255, 255, 21)}"
)

#: Ширины колонок по порядку (None — колонка названия тянется).
_TABLE_WIDTHS: tuple[int | None, ...] = (
	None,
	_COLUMN_WIDTHS["participants"],
	_COLUMN_WIDTHS["queue"],
	_COLUMN_WIDTHS["scheduled"],
	_COLUMN_WIDTHS["state"],
)


def _number_item(value: int | None) -> QTableWidgetItem:
	"""Числовая ячейка: вправо, кегль макета; нет данных — «—»."""
	item = QTableWidgetItem("—" if value is None else format_count(value))
	item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
	item.setFont(font_px(_TABLE_FONT_PX))
	return item


def _name_cell(row: Row, parent: QWidget) -> QWidget:
	"""Ячейка названия по макету: аватар 28, название, под ним @имя."""
	community, stats = row.community, row.stats
	box = QWidget(parent)
	layout = QHBoxLayout(box)
	layout.setContentsMargins(16, 0, 8, 0)
	layout.setSpacing(11)
	avatar = stats.avatar_path if stats is not None else None
	layout.addWidget(community_logo(box, community.id, community.title, avatar, _ROW_LOGO_SIZE))
	column = QVBoxLayout()
	column.setSpacing(0)
	title = BodyLabel(box)
	title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
	elide_text(title, community.title)
	column.addWidget(title)
	name = CaptionLabel(f"@{community.username}" if community.username else "имя не задано", box)
	column.addWidget(name)
	layout.addLayout(column, stretch=1)
	return box


def _state_cell(row: Row, parent: QWidget) -> QWidget | None:
	"""Ячейка состояния: та же плашка, что на карточке; штатное — «—» текстом."""
	state = card_state(row.community, row.counts)
	text = state_badge_text(state, row.counts)
	if text is None:
		return None
	box = QWidget(parent)
	layout = QHBoxLayout(box)
	layout.setContentsMargins(8, 0, 8, 0)
	layout.addWidget(state_badge(box, state, text))
	layout.addStretch()
	return box


class _Table(TableWidget):
	"""Таблица раздела: штатный ``TableWidget``, строка — сообщество.

	Сортировка — наша чистая функция :func:`sort_rows` с перестройкой
	(правило одно с карточками и закрыто тестами), клик по заголовку
	меняет колонку и направление. Клик по строке открывает страницу
	сообщества, правый клик — меню действий (тот же набор, что кнопки
	карточки). Таблица живёт внутри прокручиваемой страницы, поэтому
	высота — по числу строк, свои полосы прокрутки выключены.
	"""

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
		self._on_sort = on_sort
		self._on_open = on_open
		self._on_action = on_action
		column, descending = sort
		self._rows = sort_rows(rows, column, descending)
		audience = "Участники" if kind is CommunityKind.GROUP else "Подписчики"
		titles = [title or audience for title, _column in _TABLE_COLUMNS]
		self.setColumnCount(len(titles))
		self.setHorizontalHeaderLabels(titles)
		self.setRowCount(len(self._rows))
		self.setBorderVisible(True)
		# радиус и заголовок без вертикальных рамок — официальной добавкой
		# к стилю библиотеки (документация, «Customize style»:
		# setCustomStyleSheet). Радиус здесь же: setBorderRadius библиотеки
		# сам кладёт правило через setCustomStyleSheet, и второй вызов
		# заменил бы первый
		setCustomStyleSheet(self, _TABLE_QSS, _TABLE_QSS)
		self.setShowGrid(False)  # макет: только горизонтальные линии строк
		self.setAlternatingRowColors(False)
		self.setWordWrap(False)
		self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
		self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
		self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
		self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
		self.customContextMenuRequested.connect(self._on_menu)
		self.cellClicked.connect(self._on_cell)
		vertical = self.verticalHeader()
		if vertical is not None:
			vertical.hide()
			vertical.setDefaultSectionSize(_TABLE_ROW_HEIGHT)
		for index, row in enumerate(self._rows):
			self._fill_row(index, row)
		header = self.horizontalHeader()
		if header is not None:
			header.setFixedHeight(_TABLE_HEADER_HEIGHT)
			header.setFont(font_px(_TABLE_HEADER_FONT_PX))
			header.setSortIndicatorShown(True)
			header.setSortIndicator(
				self._column_index(column),
				Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder,
			)
			header.sectionClicked.connect(self._on_header)
			header.setStretchLastSection(False)
			# заголовок названия — влево (как ячейка), числовые — вправо
			first = self.horizontalHeaderItem(0)
			if first is not None:
				first.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
			for index in (1, 2, 3):
				item = self.horizontalHeaderItem(index)
				if item is not None:
					item.setTextAlignment(
						Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
					)
			for index, width in enumerate(_TABLE_WIDTHS):
				if width is None:
					header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
				else:
					header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
					self.setColumnWidth(index, width)
		self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		self._fit_height()

	@staticmethod
	def _column_index(column: TableColumn) -> int:
		"""Номер колонки для индикатора сортировки (у названия — первая)."""
		for index, (_title, candidate) in enumerate(_TABLE_COLUMNS):
			if candidate is column:
				return index
		return 0

	def _fill_row(self, index: int, row: Row) -> None:
		"""Строка по макету: название с аватаром и @именем, числа, плашка."""
		community, counts, stats = row.community, row.counts, row.stats
		self.setItem(index, 0, QTableWidgetItem(""))  # текст рисует виджет ячейки
		name = _name_cell(row, self)
		self.setCellWidget(index, 0, name)
		self.setItem(index, 1, _number_item(stats.participants if stats is not None else None))
		queue = _number_item(counts.planned)
		if counts.waiting:
			queue.setText(f"{counts.planned} +{counts.waiting}")
		self.setItem(index, 2, queue)
		self.setItem(index, 3, _number_item(stats.scheduled_count if stats is not None else None))
		state = _state_cell(row, self)
		if state is None:
			dash = QTableWidgetItem("—")
			dash.setFont(font_px(_TABLE_FONT_PX))
			self.setItem(index, 4, dash)
		else:
			self.setItem(index, 4, QTableWidgetItem(""))
			self.setCellWidget(index, 4, state)
		if not community.enabled:
			# выключенное сообщество приглушено (макет: строка 0.6)
			for widget in (name, state):
				if widget is not None:
					dim_widget(widget, _DISABLED_ROW_OPACITY)

	def _fit_height(self) -> None:
		"""Высота по строкам: таблица внутри прокручиваемой страницы."""
		header = self.horizontalHeader()
		height = header.sizeHint().height() if header is not None else 0
		for index in range(self.rowCount()):
			height += self.rowHeight(index)
		self.setFixedHeight(height + 2 * self.frameWidth())

	def _on_header(self, index: int) -> None:
		self._on_sort(_TABLE_COLUMNS[index][1])

	def _on_cell(self, row: int, _column: int) -> None:
		self._on_open(self._rows[row].community.id)

	def _on_menu(self, pos: QPoint) -> None:
		"""Меню действий — тот же набор, что кнопками на карточке."""
		index = self.indexAt(pos)
		if not index.isValid():
			return
		row = self._rows[index.row()]
		community, counts = row.community, row.counts
		menu = RoundMenu(parent=self)
		for action in card_actions(community, counts):
			item = Action(ACTION_LABELS[action], menu)
			if not action_available(action, community):
				item.setEnabled(False)
				item.setToolTip(MAINTENANCE_UNAVAILABLE)
			item.triggered.connect(partial(self._on_action, action, community))
			menu.addAction(item)
		viewport = self.viewport()
		anchor = viewport.mapToGlobal(pos) if viewport is not None else self.mapToGlobal(pos)
		menu.exec(anchor)


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
		# размер — по двум значкам: политика библиотеки «может расти»,
		# и в строке с растяжкой переключатель занимал половину шапки
		self._view_switch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
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
		"""Кэш статистики получен — рисуем и сообщаем главному окну.

		Сам кэш наполняет периодический опрос движка (ADR-0027) —
		страница его не запускает, только читает при каждом показе.
		"""
		self._stats_cache = {item.community_id: item for item in stats}
		self._render()
		self.communities_changed.emit(list(self._communities))

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
		"""Строка сводки: очередь, активные, ошибки, без публикатора.

		``SimpleCardWidget`` (без реакции на наведение), числа —
		``StrongBodyLabel``, подписи — ``BodyLabel``, между ними
		``VerticalSeparator``; ошибки — кнопка (ведёт в очередь),
		«без публикатора» — ``InfoBadge`` акцентом.
		"""
		clear_layout(self._summary_box)
		if not self._communities:
			return
		totals = summary_counts(self._communities, self._queue_counts)
		bar: QWidget = SimpleCardWidget(self)
		layout = QHBoxLayout(bar)
		layout.setContentsMargins(16, 6, 16, 6)
		layout.setSpacing(12)
		self._summary_segment(bar, layout, totals.queued, "в очереди отправки")
		layout.addWidget(VerticalSeparator(bar))
		self._summary_segment(bar, layout, totals.enabled, f"активных из {totals.total}")
		if totals.errors:
			layout.addWidget(VerticalSeparator(bar))
			text = f"{totals.errors} {plural(totals.errors, 'ошибка', 'ошибки', 'ошибок')}"
			errors = list_button(text, bar, height=_SUMMARY_BADGE_HEIGHT)
			errors.setToolTip("Элементы очереди отправки с ошибкой — открыть очередь")
			errors.clicked.connect(self._open_errors)
			layout.addWidget(errors)
		if totals.without_publisher:
			layout.addWidget(VerticalSeparator(bar))
			badge = InfoBadge.attension(f"{totals.without_publisher} без публикатора", parent=bar)
			badge.setFont(font_px(_ACTION_FONT_PX))
			badge.setFixedHeight(_SUMMARY_BADGE_HEIGHT)
			badge.setContentsMargins(8, 0, 8, 0)
			layout.addWidget(badge)
		layout.addStretch()
		self._summary_box.addWidget(bar)

	@staticmethod
	def _summary_segment(parent: QWidget, layout: QHBoxLayout, number: int, tail: str) -> None:
		"""Сегмент сводки: число жирным и подпись прозой."""
		layout.addWidget(StrongBodyLabel(str(number), parent))
		layout.addWidget(BodyLabel(tail, parent))

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
		return section_header(self, title, count, icon=icon)

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
