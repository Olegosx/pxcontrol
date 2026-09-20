"""Вкладка «Обзор» страницы сообщества: числа, графики, справка (ADR-0027).

Правило вкладки: **ни одной кнопки и ни одного переключателя** — здесь
только смотрят. Всё действующее живёт в других вкладках.

Данные — один снимок :class:`CommunityOverviewDto` от движка: числа
(участники и изменение за неделю, онлайн или просмотры на пост,
пришли/ушли, удалённые аккаунты), ряды для графиков (участники
за 30 дней, приходы и уходы за 14, профиль по часам) и справка.
Откуда ряды — из статистики Telegram или из локальных снимков, —
снимок называет сам, и сноска внизу это повторяет человеку.

Графики рисуются ``QPainter`` в ``paintEvent`` — столбцы это
прямоугольники, без внешних библиотек. Тексты плиток и справки —
чистые функции, они тестируются без Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
	QAbstractItemView,
	QGridLayout,
	QHBoxLayout,
	QHeaderView,
	QLabel,
	QSizePolicy,
	QTableWidgetItem,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	DotInfoBadge,
	HorizontalSeparator,
	ProgressBar,
	StrongBodyLabel,
	TableWidget,
	isDarkTheme,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_overview import (
	FLOW_DAYS,
	GROWTH_DAYS,
	HOURS_DAYS,
	PERIOD_DAYS,
	CommunityOverviewDto,
	SeriesSource,
)
from pxcontrol.engine.telegram.types import CommunityKind, DayPoint, NamedSeries, Share
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ACCENT_TEXT,
	DIM_TEXT,
	ERROR_TEXT,
	FlowGrid,
	clear_layout,
	elide_text,
	error_reporter,
	font_px,
	format_count,
	format_local,
	plural,
	status_caption,
	tinted,
)

#: Полотна графиков (пиксели): участники и приходы/уходы, часы суток.
_CANVAS_TALL = 84
_CANVAS_SHORT = 56
#: Высота оси подписей под полотном.
_AXIS_HEIGHT = 16

#: Цвета графиков парами «светлая, тёмная тема» (столбцы — от акцента).
_BAR = ("rgba(20,184,166,.55)", "rgba(20,184,166,.5)")
_BAR_FULL = ("#14b8a6", "#14b8a6")
_BAR_DIM = ("rgba(20,184,166,.45)", "rgba(20,184,166,.4)")
_BAR_LEFT = ("#c42b1c", "#ff99a4")
_ZERO_LINE = ("rgba(0,0,0,.18)", "rgba(255,255,255,.14)")
_AXIS_TEXT = ("#8a8a8a", "#7a7a7a")
#: Второй ряд сгруппированных столбцов — серый пресета ``InfoLevel.INFOAMTION``
#: (точка легенды — ``DotInfoBadge.info``), чтобы легенда и полотно совпадали.
_BAR_SECOND = ("#8a8a8a", "#9d9d9d")

#: Потоковая сетка карточек: минимальные ширины и интервал (макет: 10).
_TILE_MIN_WIDTH = 180
_CHART_MIN_WIDTH = 340
_CARD_SPACING = 10
#: Строка долей: ширина подписи и процента; сколько долей показывать.
_SHARE_LABEL_WIDTH = 110
_SHARE_PERCENT_WIDTH = 44
_SHARE_ROWS = 8
#: Сколько недавних постов и активных участников показывать в таблицах.
_TABLE_ROWS = 10
#: Колонка подписей справки: не уже макета (104), шире — по самой длинной подписи.
_REFERENCE_LABEL_WIDTH = 104

#: Имена рядов и долей Telegram — по-русски; незнакомое остаётся как есть.
_SERIES_RU = {
	"views": "Просмотры",
	"shares": "Пересылки",
	"forwards": "Пересылки",
	"reactions": "Реакции",
	"followers": "Подписчики",
	"members": "Участники",
	"joined": "Пришли",
	"left": "Ушли",
	"muted": "Заглушили",
	"unmuted": "Включили звук",
	"messages": "Сообщения",
	"viewers": "Читающие",
	"posters": "Пишущие",
	"search": "Поиск",
	"channels": "Каналы",
	"groups": "Группы",
	"private chats": "Личные чаты",
	"url": "Ссылки",
	"urls": "Ссылки",
	"other": "Прочее",
	"monday": "Пн",
	"tuesday": "Вт",
	"wednesday": "Ср",
	"thursday": "Чт",
	"friday": "Пт",
	"saturday": "Сб",
	"sunday": "Вс",
}


def _qcolor(pair: tuple[str, str]) -> QColor:
	"""QColor из пары «светлая, тёмная» — включая записи ``rgba(r,g,b,a)``.

	Цвета для пера графиков (рисование заказано макетом), не стили.
	"""
	value = pair[1] if isDarkTheme() else pair[0]
	if value.startswith("rgba("):
		parts = [part.strip() for part in value[5:-1].split(",")]
		return QColor(int(parts[0]), int(parts[1]), int(parts[2]), round(float(parts[3]) * 255))
	return QColor(value)


# --- тексты плиток и справки (чистые функции) ------------------------------------------


def delta_caption(delta: int | None, days: int = PERIOD_DAYS) -> tuple[str, tuple[str, str]]:
	"""Подпись изменения «+38 за 7 дней» и её цвет (рост — акцент, убыль — ошибка)."""
	if delta is None:
		return "нет данных за период", DIM_TEXT
	sign = "+" if delta > 0 else ("−" if delta < 0 else "")
	color = ACCENT_TEXT if delta > 0 else (ERROR_TEXT if delta < 0 else DIM_TEXT)
	return (
		f"{sign}{format_count(abs(delta))} за {days} {plural(days, 'день', 'дня', 'дней')}",
		color,
	)


def share_caption(part: int | None, whole: int | None, word: str) -> str:
	"""Доля «4,6% участников»; без чисел — «нет данных»."""
	if part is None or not whole:
		return "нет данных"
	return f"{part / whole * 100:.1f}".replace(".", ",") + f"% {word}"


def flow_caption(joined: int | None, left: int | None) -> str:
	"""«уходит 1 из 2,7 пришедших» — соотношение уходов к приходам."""
	if joined is None or left is None:
		return "нет данных за период"
	if left == 0:
		return "никто не ушёл"
	if joined == 0:
		return "приходов не было"
	ratio = joined / left
	return f"уходит 1 из {ratio:.1f}".replace(".", ",") + " пришедших"


def deleted_caption(
	found: int | None, participants: int | None, checked_at: datetime | None
) -> str:
	"""«2,2% списка · проход 11.09» — доля мёртвых душ и дата прохода."""
	if found is None or checked_at is None:
		return "проход ещё не выполнялся"
	when = checked_at.astimezone().strftime("%d.%m")
	if participants:
		share = f"{found / participants * 100:.1f}".replace(".", ",")
		return f"{share}% списка · проход {when}"
	return f"проход {when}"


def signed(value: int | None) -> str:
	"""Число со знаком: «+61», «−23», «0»; None — «—»."""
	if value is None:
		return "—"
	if value > 0:
		return f"+{format_count(value)}"
	if value < 0:
		return f"−{format_count(-value)}"
	return "0"


def growth_subtitle(points: tuple[DayPoint, ...]) -> str:
	"""«2 066 → 2 104» по краям ряда; пустой ряд — пусто."""
	if len(points) < 2:
		return ""
	return f"{format_count(points[0].value)} → {format_count(points[-1].value)}"


def axis_dates(points: tuple[DayPoint, ...]) -> list[tuple[int, str]]:
	"""Подписи оси только по краям и в середине: (позиция, «15.08»)."""
	if not points:
		return []
	last = len(points) - 1
	positions = sorted({0, last // 2, last})
	return [(index, points[index].day.strftime("%d.%m")) for index in positions]


def period_caption(points: tuple[DayPoint, ...]) -> str:
	"""«31.08 — 13.09» — период ряда по краям."""
	if not points:
		return ""
	return f"{points[0].day.strftime('%d.%m')} — {points[-1].day.strftime('%d.%m')}"


def hours_subtitle(hours: tuple[int, ...] | None, online: bool) -> str:
	"""«в среднем за 7 дней · пик 21:00 — 148» (онлайн) или про активность."""
	if not hours:
		return ""
	peak = max(range(24), key=lambda h: hours[h])
	what = "онлайн" if online else "активность"
	return (
		f"{what} в среднем за {HOURS_DAYS} дней · пик {peak:02d}:00 — {format_count(hours[peak])}"
	)


def kind_text(community: CommunityDto) -> str:
	"""«Канал» / «Супергруппа, форум» / «Супергруппа, не форум»."""
	if community.kind is CommunityKind.CHANNEL:
		return "Канал"
	return "Супергруппа, форум" if community.forum else "Супергруппа, не форум"


def linked_text(overview: CommunityOverviewDto, community: CommunityDto) -> str:
	"""«каналу «Кино»» / «чату обсуждений «…»» / id / «—»."""
	if overview.linked_chat_id is None:
		return "—"
	kind = "чату обсуждений" if community.kind is CommunityKind.CHANNEL else "каналу"
	if overview.linked_title:
		return f"{kind} «{overview.linked_title}»"
	return f"{kind} {overview.linked_chat_id}"


def updated_text(overview: CommunityOverviewDto, community: CommunityDto) -> str:
	"""«сегодня, 14:02 · бот раз в 15 мин, userbot раз в 6 ч»."""
	if overview.fetched_at is None:
		return "ещё не обновлялось"
	# приостановленный публикатор (ADR-0029) опросом пропускается —
	# темп называется только по действующим
	caps = community.capabilities
	cadence = []
	if caps.bot:
		cadence.append("бот раз в 15 мин")
	if caps.userbot:
		cadence.append("userbot раз в 6 ч")
	tail = f" · {', '.join(cadence)}" if cadence else ""
	return f"{format_local(overview.fetched_at)}{tail}"


def reference_rows(
	overview: CommunityOverviewDto, community: CommunityDto
) -> list[tuple[str, str]]:
	"""Справка «подпись — значение» в порядке макета."""
	publisher = "—"
	if community.default_account_label:
		status = community.default_status
		role = f" · {status_caption(status)}" if status else ""
		paused = " · приостановлен" if community.default_account_paused else ""
		publisher = f"{community.default_account_label}{role}{paused}"
	elif community.bot_label:
		paused = " · приостановлен" if community.bot_paused else ""
		publisher = f"бот {community.bot_label}{paused}"
	return [
		("Тип", kind_text(community)),
		("Создана", _date_text(overview.tg_created_at)),
		("Адрес", f"@{community.username}" if community.username else "имя не задано"),
		("ID чата", community.tg_chat_id),
		("Привязана к", linked_text(overview, community)),
		("Публикатор", publisher),
		("Последний пост", format_local(overview.last_post_at) if overview.last_post_at else "—"),
		("Обновлено", updated_text(overview, community)),
	]


def source_note(overview: CommunityOverviewDto, community: CommunityDto) -> str:
	"""Сноска: откуда числа и что вкладка ничего не запускает."""
	if overview.source is SeriesSource.TELEGRAM:
		rows = (
			"Динамика участников, приходы и уходы, активность по часам — встроенная "
			"статистика Telegram (считается сервером, доступна администратору)."
		)
	elif overview.source is SeriesSource.SNAPSHOTS:
		rows = (
			"Динамика — по снимкам опроса за время работы приложения; приходы и уходы — "
			"оценка по разности числа участников (внутри одного интервала приход и уход "
			"взаимно гасятся). Встроенная статистика Telegram этому сообществу недоступна."
		)
	else:
		rows = "Истории пока нет — графики появятся по мере накопления снимков."
	who = "бот раз в 15 минут" if community.bot_id is not None else "userbot раз в 6 часов"
	return (
		f"Участники, онлайн и отложенные — из кэша опроса ({who}). {rows} "
		"Удалённые аккаунты — итог последнего прохода обслуживания. Числа справочные: "
		"ни одна цифра на этой вкладке ничего не запускает."
	)


def series_title(name: str) -> str:
	"""Имя ряда или доли по-русски (словарь известных, иначе как отдал Telegram)."""
	return _SERIES_RU.get(name.strip().casefold(), name)


def pair_caption(pair: tuple[int, int] | None, days: int) -> tuple[str, tuple[str, str]]:
	"""Подпись плитки по паре «сейчас, раньше»: «+2 за 7 дней» и цвет."""
	if pair is None:
		return "нет данных", DIM_TEXT
	return delta_caption(pair[0] - pair[1], days)


def percent_text(part: int | None, total: int | None) -> str:
	"""«38 %» из части и целого; «—» без данных."""
	if part is None or not total:
		return "—"
	return f"{round(part * 100 / total)}\u202f%"


def share_rows(
	shares: tuple[Share, ...], limit: int = _SHARE_ROWS, *, keep_order: bool = False
) -> list[tuple[str, int]]:
	"""Строки карточки долей: имя и процент, по убыванию; хвост — «прочее».

	Проценты считаются от суммы всех долей; при переполнении лимита
	оставшиеся складываются в одну строку. ``keep_order`` — порядок
	Telegram, а не по убыванию (дни недели читаются с понедельника).
	"""
	total = sum(share.value for share in shares)
	if total <= 0:
		return []
	ordered = list(shares) if keep_order else sorted(shares, key=lambda s: s.value, reverse=True)
	head, tail = ordered[:limit], ordered[limit:]
	rows = [(series_title(share.name), round(share.value * 100 / total)) for share in head]
	rest = sum(share.value for share in tail)
	if rest > 0:
		rows.append(("прочее", round(rest * 100 / total)))
	return rows


def series_days(series: tuple[NamedSeries, ...]) -> list[date]:
	"""Все дни, встречающиеся в рядах (объединение), по порядку."""
	return sorted({point.day for item in series for point in item.points})


def _date_text(moment: datetime | None) -> str:
	return moment.astimezone().strftime("%d.%m.%Y") if moment else "—"


# --- графики на QPainter --------------------------------------------------------------


def hours_chart(parent: QWidget, hours: tuple[int, ...]) -> QWidget:
	"""Профиль по часам суток: столбцы от нуля, пик — полным цветом."""
	labels = [(0, "00"), (6, "06"), (12, "12"), (18, "18"), (23, "23")]
	return BarsChart(
		list(hours),
		labels,
		parent,
		canvas=_CANVAS_SHORT,
		gap=3,
		color=_BAR_DIM,
		highlight="max",
		from_zero=True,
	)


def days_chart(
	parent: QWidget, points: tuple[DayPoint, ...], *, from_zero: bool = False
) -> QWidget:
	"""Ряд по дням: подписи дат по краям и в середине, последний день — полным цветом."""
	return BarsChart([p.value for p in points], axis_dates(points), parent, from_zero=from_zero)


@dataclass(frozen=True)
class _Bar:
	"""Столбец: доля высоты (0..1 от базы) и цвет."""

	share: float
	color: QColor


class BarsChart(QWidget):
	"""Столбчатый график с подписями оси по краям.

	``values`` — высоты; база — минимум ряда минус запас (так рост
	виден даже у большого числа), последний столбец — полным цветом.
	"""

	def __init__(
		self,
		values: list[int],
		labels: list[tuple[int, str]],
		parent: QWidget,
		*,
		canvas: int = _CANVAS_TALL,
		gap: int = 2,
		color: tuple[str, str] = _BAR,
		full: tuple[str, str] = _BAR_FULL,
		highlight: str = "last",
		from_zero: bool = False,
	) -> None:
		"""``highlight`` — «last» (последний столбец полным цветом) или
		«max» (наибольший); ``from_zero`` — база в нуле, а не у минимума."""
		super().__init__(parent)
		self._values = values
		self._labels = labels
		self._gap = gap
		self._color = _qcolor(color)
		self._full = _qcolor(full)
		self._highlight = highlight
		self._from_zero = from_zero
		self.setFixedHeight(canvas + (_AXIS_HEIGHT if labels else 0))
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

	def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 — API Qt
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		values = self._values
		if not values:
			return
		canvas = self.height() - (_AXIS_HEIGHT if self._labels else 0)
		count = len(values)
		width = max(2.0, (self.width() - self._gap * (count - 1)) / count)
		low = 0 if self._from_zero else min(values)
		high = max(values)
		if not self._from_zero:
			margin = max(1, (high - low) // 10 or 1)
			low = low - margin
		span = max(1, high - low)
		peak = max(range(count), key=lambda i: values[i])
		for index, value in enumerate(values):
			share = (value - low) / span
			bar_h = max(1.0, share * canvas)
			x = index * (width + self._gap)
			chosen = (self._highlight == "last" and index == count - 1) or (
				self._highlight == "max" and index == peak
			)
			painter.setPen(Qt.PenStyle.NoPen)
			painter.setBrush(self._full if chosen else self._color)
			painter.drawRoundedRect(QRectF(x, canvas - bar_h, width, bar_h), 1, 1)
		_draw_axis(painter, self._labels, width, self._gap, canvas, self.width())


class _GroupedBars(QWidget):
	"""Несколько рядов столбцами рядом на каждый день, база в нуле.

	Первый ряд — акцент, второй — серый пресета библиотеки; больше двух
	рядов Telegram в таких графиках не отдаёт, лишние рисуются серым.
	"""

	def __init__(
		self,
		series: list[list[int]],
		labels: list[tuple[int, str]],
		parent: QWidget,
		*,
		canvas: int = _CANVAS_TALL,
	) -> None:
		super().__init__(parent)
		self._series = series
		self._labels = labels
		self._gap = 3
		self.setFixedHeight(canvas + (_AXIS_HEIGHT if labels else 0))
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

	def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 — API Qt
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		count = max((len(row) for row in self._series), default=0)
		if not count or not self._series:
			return
		canvas = self.height() - (_AXIS_HEIGHT if self._labels else 0)
		peak = max([value for row in self._series for value in row] + [1])
		group_w = max(2.0, (self.width() - self._gap * (count - 1)) / count)
		bar_w = max(1.0, group_w / len(self._series))
		painter.setPen(Qt.PenStyle.NoPen)
		for index in range(count):
			x = index * (group_w + self._gap)
			for position, row in enumerate(self._series):
				value = row[index] if index < len(row) else 0
				if value <= 0:
					continue
				bar_h = max(1.0, value / peak * canvas)
				painter.setBrush(_qcolor(_BAR_FULL if position == 0 else _BAR_SECOND))
				painter.drawRoundedRect(
					QRectF(x + position * bar_w, canvas - bar_h, bar_w, bar_h), 1, 1
				)
		_draw_axis(painter, self._labels, group_w, self._gap, canvas, self.width())


class _FlowChart(QWidget):
	"""Приходы вверх (акцент) и уходы вниз (цвет ошибки) от линии нуля."""

	def __init__(self, joined: list[int], left: list[int], parent: QWidget) -> None:
		super().__init__(parent)
		self._joined = joined
		self._left = left
		self._gap = 3
		self.setFixedHeight(_CANVAS_TALL)
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

	def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 — API Qt
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		count = max(len(self._joined), len(self._left))
		if not count:
			return
		half = (self.height() - 1) / 2
		peak = max([*self._joined, *self._left, 1])
		width = max(2.0, (self.width() - self._gap * (count - 1)) / count)
		painter.setPen(Qt.PenStyle.NoPen)
		for index in range(count):
			x = index * (width + self._gap)
			up = self._joined[index] if index < len(self._joined) else 0
			down = self._left[index] if index < len(self._left) else 0
			if up:
				h = max(1.0, up / peak * half)
				painter.setBrush(_qcolor(_BAR_FULL))
				painter.drawRoundedRect(QRectF(x, half - h, width, h), 1, 1)
			if down:
				h = max(1.0, down / peak * half)
				painter.setBrush(_qcolor(_BAR_LEFT))
				painter.drawRoundedRect(QRectF(x, half + 1, width, h), 1, 1)
		painter.setPen(QPen(_qcolor(_ZERO_LINE), 1))
		painter.drawLine(0, int(half), self.width(), int(half))


def _draw_axis(
	painter: QPainter,
	labels: list[tuple[int, str]],
	width: float,
	gap: int,
	canvas: int,
	total_width: int,
) -> None:
	"""Подписи оси под столбцами: у левого края, в середине, у правого."""
	if not labels:
		return
	painter.setPen(QPen(_qcolor(_AXIS_TEXT)))
	painter.setFont(font_px(11))
	metrics = painter.fontMetrics()
	last_index = labels[-1][0]
	for index, text in labels:
		center = index * (width + gap) + width / 2
		text_w = metrics.horizontalAdvance(text)
		if index == 0:
			x = 0.0
		elif index == last_index:
			x = total_width - text_w
		else:
			x = center - text_w / 2
		painter.drawText(QRectF(x, canvas + 2, text_w + 2, _AXIS_HEIGHT - 2), text)


# --- сборка вкладки ---------------------------------------------------------------------


def card_box(parent: QWidget, margins: tuple[int, int, int, int]) -> tuple[QWidget, QVBoxLayout]:
	card: QWidget = CardWidget(parent)
	layout = QVBoxLayout(card)
	layout.setContentsMargins(*margins)
	layout.setSpacing(10)
	return card, layout


def text_label(
	parent: QWidget,
	text: str,
	size: int,
	color: tuple[str, str] | None = None,
	*,
	bold: bool = False,
) -> QLabel:
	"""Библиотечная надпись нужного кегля: 12 — ``CaptionLabel``, 13–14 —
	``BodyLabel``, крупнее — ``StrongBodyLabel`` с этим кеглем; цвет —
	только её же ``setTextColor`` (акцент, ошибка, приглушённый)."""
	label: QLabel
	if size <= 12:
		label = CaptionLabel(text, parent)
	elif size <= 14:
		label = BodyLabel(text, parent)
	else:
		label = StrongBodyLabel(text, parent)
	if size not in (12, 14) or bold:
		label.setFont(font_px(size, QFont.Weight.DemiBold if bold else QFont.Weight.Normal))
	if color is not None:
		tinted(label, color)
	return label


class OverviewCards(QWidget):
	"""Общие сборки карточек обзора: плитка числа, карточка графика, доли, справка.

	Одна вёрстка на «Обзор» сообщества и страницу аккаунта (ADR-0030):
	сами данные и их порядок — у наследника, здесь только то, как
	выглядят карточки. Только штатные элементы (ADR-0023, п. 5).
	"""

	def _tile_card(self, tile: Tile) -> QWidget:
		return self._tile_widgets(tile).card

	def _tile_widgets(self, tile: Tile) -> TileWidgets:
		"""Плитка с надписями наружу — для обновления чисел на месте."""
		card, layout = card_box(self, (14, 11, 14, 11))
		layout.setSpacing(2)
		layout.addWidget(text_label(card, tile.title, 12))
		values_row = QHBoxLayout()
		values_row.setSpacing(8)
		values: list[QLabel] = []
		for text, size, color in tile.values:
			value = text_label(card, text, size, color, bold=True)
			values_row.addWidget(value, alignment=Qt.AlignmentFlag.AlignBottom)
			values.append(value)
		values_row.addStretch()
		layout.addLayout(values_row)
		caption = text_label(card, tile.caption, 12, tile.caption_color)
		layout.addWidget(caption)
		return TileWidgets(card, values, caption)

	def _chart_card(self, title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
		"""Карточка графика: заголовок слева, подзаголовок приглушённо справа."""
		card, layout = card_box(self, (14, 12, 14, 10))
		head = QHBoxLayout()
		head.addWidget(text_label(card, title, 13))
		head.addStretch()
		if subtitle:
			# «занимай, что дадут»: длинный подзаголовок сокращается, а не
			# распирает карточку — колонки сетки должны остаться равными
			sub = text_label(card, "", 12, DIM_TEXT)
			sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
			sub.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
			elide_text(sub, subtitle)
			head.addWidget(sub, stretch=1)
		layout.addLayout(head)
		return card, layout

	def _rows_card(self, title: str, rows: list[tuple[str, int]]) -> QWidget | None:
		"""Доли строками «имя — полоса — %» на штатном ``ProgressBar``; без строк — нет."""
		if not rows:
			return None
		card, layout = self._chart_card(title)
		for name, percent in rows:
			line = QHBoxLayout()
			line.setSpacing(8)
			label = text_label(card, "", 12)
			label.setFixedWidth(_SHARE_LABEL_WIDTH)
			elide_text(label, name)
			line.addWidget(label)
			bar = ProgressBar(card)
			bar.setRange(0, 100)
			bar.setValue(percent)
			line.addWidget(bar, stretch=1)
			value = text_label(card, f"{percent}\u202f%", 12, DIM_TEXT)
			value.setFixedWidth(_SHARE_PERCENT_WIDTH)
			value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
			line.addWidget(value)
			layout.addLayout(line)
		return card

	def _reference_grid(self, rows: list[tuple[str, str]]) -> QWidget:
		"""Справка: сетка 2 × N «подпись / значение» с хайрлайнами."""
		return self._reference_widgets(rows)[0]

	def _reference_widgets(self, rows: list[tuple[str, str]]) -> tuple[QWidget, list[QLabel]]:
		"""Справка с надписями значений наружу — для обновления на месте.

		Значения обновляются через :func:`elide_text` — так же, как
		при сборке, иначе длинное значение перестало бы сокращаться.
		"""
		values: list[QLabel] = []
		box = QWidget(self)
		# ширина колонки подписей — по самой длинной подписи этих строк:
		# у справки аккаунта подписи длиннее, чем у сообщества
		metrics = self.fontMetrics()
		label_width = max(
			_REFERENCE_LABEL_WIDTH, *(metrics.horizontalAdvance(key) + 6 for key, _v in rows)
		)
		grid = QGridLayout(box)
		grid.setContentsMargins(0, 0, 0, 0)
		grid.setHorizontalSpacing(28)
		grid.setVerticalSpacing(0)
		half = (len(rows) + 1) // 2
		columns = [rows[:half], rows[half:]]
		for column, items in enumerate(columns):
			for position, (label_text, value_text) in enumerate(items):
				cell = QWidget(box)
				cell_layout = QVBoxLayout(cell)
				cell_layout.setContentsMargins(0, 0, 0, 0)
				cell_layout.setSpacing(0)
				line = QHBoxLayout()
				# без внутренних полей: со штатными (по 11 пикселей) от 30
				# оставалось 8, и хвосты букв резались
				line.setContentsMargins(0, 0, 0, 0)
				line.setSpacing(8)
				label = text_label(cell, label_text, 12)
				label.setFixedWidth(label_width)
				line.addWidget(label)
				value = text_label(cell, "", 14)  # BodyLabel штатно, кегль не задаётся
				value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
				elide_text(value, value_text)
				values.append(value)
				line.addWidget(value, stretch=1)
				line_box = QWidget(cell)
				line_box.setLayout(line)
				line_box.setFixedHeight(30)
				cell_layout.addWidget(line_box)
				if position < len(items) - 1:
					cell_layout.addWidget(HorizontalSeparator(cell))
				grid.addWidget(cell, position, column)
			grid.setColumnStretch(column, 1)
		return box, values


class OverviewTab(OverviewCards):
	"""Вкладка «Обзор»: снимок от движка → плитки, графики, справка, сноска."""

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._community = community
		self._show_error = error_reporter(self)
		self._overview: CommunityOverviewDto | None = None
		self._loading = False
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(density.spacing().block_spacing)
		self._layout.addWidget(CaptionLabel("Читаю статистику…", self))

	def update_community(self, community: CommunityDto) -> None:
		"""Свежий снимок сообщества (справка читает его поля)."""
		self._community = community
		if self._overview is not None:
			self._render(self._overview)

	def set_active(self, active: bool) -> None:
		"""Показ вкладки перечитывает снимок из кэша (сети здесь нет)."""
		if active:
			self.reload()

	def reload(self) -> None:
		"""Перечитывает снимок «Обзора» из движка."""
		if self._loading:
			return
		self._loading = True
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.overview(self._community.id),
			self,
			self._on_loaded,
			self._on_failed,
		)

	def _on_failed(self, message: str) -> None:
		self._loading = False
		self._show_error(message)

	def _on_loaded(self, overview: CommunityOverviewDto) -> None:
		self._loading = False
		self._overview = overview
		self._render(overview)

	# --- отрисовка --------------------------------------------------------------------

	def _render(self, overview: CommunityOverviewDto) -> None:
		clear_layout(self._layout)
		# справка — первой: «что за сообщество» читают раньше, чем «что
		# с ним происходит»; числа и графики — следом, потоковой сеткой:
		# на широком окне карточек в ряд больше, на узком меньше
		self._layout.addWidget(self._reference(overview))
		self._layout.addWidget(
			FlowGrid(self._tiles(overview), self, min_width=_TILE_MIN_WIDTH, spacing=_CARD_SPACING)
		)
		cards = self._chart_cards(overview)
		if cards:
			self._layout.addWidget(
				FlowGrid(cards, self, min_width=_CHART_MIN_WIDTH, spacing=_CARD_SPACING)
			)
		else:
			empty = CaptionLabel(
				"Данных пока нет: статистика накопится за неделю наблюдений.", self
			)
			self._layout.addWidget(empty)
		note = CaptionLabel(source_note(overview, self._community), self)
		note.setWordWrap(True)
		self._layout.addWidget(tinted(note, DIM_TEXT))
		self._layout.addStretch()

	# --- плитки чисел ------------------------------------------------------------------

	def _tiles(self, overview: CommunityOverviewDto) -> list[QWidget]:
		"""Плитки чисел: четыре основные и всё, что ещё отдал Telegram."""
		community = self._community
		is_group = community.kind is CommunityKind.GROUP
		audience = "Участников" if is_group else "Подписчиков"
		delta_text, delta_color = delta_caption(overview.participants_delta)
		tiles: list[Tile] = [
			Tile(audience, [(_count(overview.participants), 24, None)], delta_text, delta_color)
		]
		if is_group:
			tiles.append(
				Tile(
					"Онлайн сейчас",
					[(_count(overview.online), 24, None)],
					share_caption(overview.online, overview.participants, "участников"),
					DIM_TEXT,
				)
			)
		else:
			tiles.append(
				Tile(
					"Просмотров на пост",
					[(_count(overview.views_per_post), 24, None)],
					"медиана последних постов"
					if overview.views_per_post is not None
					else "нет данных",
					DIM_TEXT,
				)
			)
		tiles.append(
			Tile(
				f"Пришли · ушли, {PERIOD_DAYS} дней",
				[
					(signed(overview.joined), 24, ACCENT_TEXT if overview.joined else DIM_TEXT),
					(
						signed(-overview.left) if overview.left is not None else "—",
						19,
						ERROR_TEXT if overview.left else DIM_TEXT,
					),
				],
				flow_caption(overview.joined, overview.left),
				DIM_TEXT,
			)
		)
		tiles.append(
			Tile(
				"Удалённых аккаунтов",
				[(_count(overview.deleted_found), 24, None)],
				deleted_caption(
					overview.deleted_found, overview.participants, overview.deleted_checked_at
				),
				DIM_TEXT,
			)
		)
		tiles.extend(self._telegram_tiles(overview))
		return [self._tile_card(tile) for tile in tiles]

	def _telegram_tiles(self, overview: CommunityOverviewDto) -> list[Tile]:
		"""Плитки по парам «сейчас, раньше» из статистики Telegram (только непустые)."""
		days = overview.period_days
		pairs: list[tuple[str, tuple[int, int] | None]] = [
			("Пересылок на пост", overview.shares_per_post),
			("Реакций на пост", overview.reactions_per_post),
			("Просмотров на историю", overview.views_per_story),
			("Пересылок на историю", overview.shares_per_story),
			("Реакций на историю", overview.reactions_per_story),
			(f"Сообщений за {days} дней", overview.messages),
			("Читающих", overview.viewers),
			("Пишущих", overview.posters),
		]
		tiles = []
		for title, pair in pairs:
			if pair is None:
				continue
			text, color = pair_caption(pair, days)
			tiles.append(Tile(title, [(_count(pair[0]), 24, None)], text, color))
		if overview.notifications is not None:
			part, total = overview.notifications
			tiles.append(
				Tile(
					"Уведомления включены",
					[(percent_text(part, total), 24, None)],
					f"{_count(part)} из {_count(total)}",
					DIM_TEXT,
				)
			)
		return tiles

	# --- карточки графиков, долей и таблиц ---------------------------------------------

	def _chart_cards(self, overview: CommunityOverviewDto) -> list[QWidget]:
		"""Все карточки под сетку, в порядке важности; без данных карточки нет."""
		cards: list[QWidget | None] = [
			self._growth_card(overview),
			self._flow_card(overview),
			self._hours_card(overview),
			self._daily_card("Просмотры и пересылки", overview.interactions),
			self._daily_card("Сообщения по дням", overview.messages_daily),
			self._daily_card("Читающие и пишущие", overview.actions),
			self._daily_card("Instant View", overview.iv_interactions),
			self._mute_card(overview),
			self._daily_card("Истории: просмотры и пересылки", overview.story_interactions),
			self._shares_card("Откуда просмотры", overview.views_by_source),
			self._shares_card(
				"Откуда новые участники"
				if self._community.kind is CommunityKind.GROUP
				else "Откуда новые подписчики",
				overview.members_by_source,
			),
			self._shares_card("Языки аудитории", overview.languages),
			self._shares_card("Реакции", overview.reactions_by_emotion),
			self._shares_card("Реакции на истории", overview.story_reactions),
			self._shares_card("По дням недели", overview.weekdays, keep_order=True),
			self._recent_posts_card(overview),
			self._top_card(
				"Самые активные",
				["Участник", "Сообщений", "Символов"],
				[(p.name, p.messages, p.avg_chars) for p in overview.top_posters],
			),
			self._top_card(
				"Администраторы",
				["Админ", "Удалил", "Исключил", "Забанил"],
				[(a.name, a.deleted, a.kicked, a.banned) for a in overview.top_admins],
			),
			self._top_card(
				"Пригласили больше всех",
				["Участник", "Пригласил"],
				[(i.name, i.invitations) for i in overview.top_inviters],
			),
		]
		ready = [card for card in cards if card is not None]
		for card in ready:
			layout = card.layout()
			if isinstance(layout, QVBoxLayout):
				# строка сетки — по самой высокой карточке; лишняя высота
				# остаётся снизу, а не растягивает строки долей и таблицы
				layout.addStretch()
		return ready

	def _growth_card(self, overview: CommunityOverviewDto) -> QWidget | None:
		growth = overview.growth
		if not growth:
			return None
		who = "Участники" if self._community.kind is CommunityKind.GROUP else "Подписчики"
		card, layout = self._chart_card(f"{who}, {GROWTH_DAYS} дней", growth_subtitle(growth))
		layout.addWidget(days_chart(card, growth))
		return card

	def _flow_card(self, overview: CommunityOverviewDto) -> QWidget | None:
		days = series_days(
			(
				NamedSeries("joined", overview.flow_joined),
				NamedSeries("left", overview.flow_left),
			)
		)
		if not days:
			return None
		joined = {p.day: p.value for p in overview.flow_joined}
		left = {p.day: p.value for p in overview.flow_left}
		card, layout = self._chart_card(f"Пришли и ушли, {FLOW_DAYS} дней")
		layout.addWidget(
			_FlowChart([joined.get(d, 0) for d in days], [left.get(d, 0) for d in days], card)
		)
		period = tuple(DayPoint(d, 0) for d in days)
		layout.addLayout(
			self._legend(card, [("пришли", False), ("ушли", True)], period_caption(period))
		)
		return card

	def _mute_card(self, overview: CommunityOverviewDto) -> QWidget | None:
		"""Звук: включили — вверх (акцент), заглушили — вниз (цвет ошибки)."""
		series = overview.mute
		days = series_days(series)
		if not days:
			return None
		by_name = {item.name.casefold(): {p.day: p.value for p in item.points} for item in series}
		muted = next((v for k, v in by_name.items() if "unmute" not in k and "mute" in k), {})
		unmuted = next((v for k, v in by_name.items() if "unmute" in k), {})
		card, layout = self._chart_card("Звук уведомлений")
		layout.addWidget(
			_FlowChart([unmuted.get(d, 0) for d in days], [muted.get(d, 0) for d in days], card)
		)
		period = tuple(DayPoint(d, 0) for d in days)
		layout.addLayout(
			self._legend(card, [("включили", False), ("заглушили", True)], period_caption(period))
		)
		return card

	def _daily_card(self, title: str, series: tuple[NamedSeries, ...]) -> QWidget | None:
		"""Ряды по дням сгруппированными столбцами с легендой по именам Telegram."""
		days = series_days(series)
		if not days:
			return None
		card, layout = self._chart_card(title)
		rows = [[{p.day: p.value for p in item.points}.get(d, 0) for d in days] for item in series]
		period = tuple(DayPoint(d, 0) for d in days)
		layout.addWidget(_GroupedBars(rows, axis_dates(period), card))
		legend = [(series_title(item.name), position > 0) for position, item in enumerate(series)]
		layout.addLayout(self._legend(card, legend, "", grey=True))
		return card

	def _legend(
		self,
		card: QWidget,
		items: list[tuple[str, bool]],
		caption: str,
		*,
		grey: bool = False,
	) -> QHBoxLayout:
		"""Легенда: точки ``DotInfoBadge`` с подписями; справа — период."""
		legend = QHBoxLayout()
		legend.setSpacing(6)
		for position, (text, second) in enumerate(items):
			if position:
				legend.addSpacing(8)
			legend.addWidget(_swatch(card, error=second and not grey, info=second and grey))
			legend.addWidget(text_label(card, text, 11))
		legend.addStretch()
		if caption:
			legend.addWidget(text_label(card, caption, 11, DIM_TEXT))
		return legend

	def _hours_card(self, overview: CommunityOverviewDto) -> QWidget | None:
		"""Профиль по часам суток."""
		hours = overview.hours
		if not hours:
			return None
		title = "Онлайн по часам суток" if overview.hours_online else "Активность по часам суток"
		card, layout = self._chart_card(title, hours_subtitle(hours, overview.hours_online))
		layout.addWidget(hours_chart(card, hours))
		return card

	def _shares_card(
		self, title: str, shares: tuple[Share, ...], *, keep_order: bool = False
	) -> QWidget | None:
		"""Доли Telegram строками (правило строк — ``share_rows``)."""
		return self._rows_card(title, share_rows(shares, keep_order=keep_order))

	def _recent_posts_card(self, overview: CommunityOverviewDto) -> QWidget | None:
		posts = overview.recent_posts[:_TABLE_ROWS]
		if not posts:
			return None
		return self._top_card(
			"Недавние посты",
			["Пост", "Просмотры", "Пересылки", "Реакции"],
			[(f"#{p.msg_id}", p.views, p.forwards, p.reactions) for p in posts],
		)

	def _top_card(
		self, title: str, titles: list[str], rows: list[tuple[object, ...]]
	) -> QWidget | None:
		"""Таблица в карточке: первая колонка — имя, остальные — числа вправо."""
		if not rows:
			return None
		card, layout = self._chart_card(title)
		table = _CardTable(titles, rows[:_TABLE_ROWS], card)
		layout.addWidget(table)
		return card

	def _reference(self, overview: CommunityOverviewDto) -> QWidget:
		"""Справка сообщества сеткой (строки — ``reference_rows``)."""
		return self._reference_grid(reference_rows(overview, self._community))


def _swatch(parent: QWidget, *, error: bool = False, info: bool = False) -> QWidget:
	"""Точка легенды — штатный ``DotInfoBadge``: акцент, цвет ошибки или серый."""
	badge: QWidget
	if error:
		badge = DotInfoBadge.error(parent)
	elif info:
		badge = DotInfoBadge.info(parent)
	else:
		badge = DotInfoBadge.attension(parent)
	return badge


@dataclass(frozen=True)
class TileWidgets:
	"""Собранная плитка: карточка и её надписи (значения, подпись)."""

	card: QWidget
	values: list[QLabel]
	caption: QLabel


@dataclass(frozen=True)
class Tile:
	"""Плитка числа: заголовок, значения (текст, кегль, цвет), подпись и её цвет."""

	title: str
	values: list[tuple[str, int, tuple[str, str] | None]]
	caption: str
	caption_color: tuple[str, str]


class _CardTable(TableWidget):
	"""Штатная таблица внутри карточки: высота по строкам, без своих полос."""

	def __init__(self, titles: list[str], rows: list[tuple[object, ...]], parent: QWidget) -> None:
		super().__init__(parent)
		self.setColumnCount(len(titles))
		self.setHorizontalHeaderLabels(titles)
		self.setRowCount(len(rows))
		self.setBorderVisible(True)
		self.setBorderRadius(6)
		self.setWordWrap(False)
		# ширина — от колонки сетки, а не от содержимого: колонки равные
		self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
		self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
		self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
		vertical = self.verticalHeader()
		if vertical is not None:
			vertical.hide()
		for row_index, row in enumerate(rows):
			for column, value in enumerate(row):
				item = QTableWidgetItem("—" if value is None else str(value))
				if column:
					item.setText("—" if value is None else format_count(int(value)))  # type: ignore[call-overload]
					item.setTextAlignment(
						Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
					)
				self.setItem(row_index, column, item)
		header = self.horizontalHeader()
		if header is not None:
			header.setStretchLastSection(False)
			header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
			for column in range(1, len(titles)):
				header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
		self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		height = header.sizeHint().height() if header is not None else 0
		for index in range(self.rowCount()):
			height += self.rowHeight(index)
		self.setFixedHeight(height + 2 * self.frameWidth())


def _count(value: int | None) -> str:
	"""Число с разделителями или «—»."""
	return format_count(value) if value is not None else "—"
