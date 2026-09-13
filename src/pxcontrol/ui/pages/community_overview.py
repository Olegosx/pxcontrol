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
from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	DotInfoBadge,
	HorizontalSeparator,
	StrongBodyLabel,
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
from pxcontrol.engine.telegram.types import CommunityKind, DayPoint
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ACCENT_TEXT,
	DIM_TEXT,
	ERROR_TEXT,
	clear_layout,
	elide_text,
	error_reporter,
	font_px,
	format_count,
	format_local,
	plural,
	role_caption,
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
	cadence = []
	if community.bot_id is not None:
		cadence.append("бот раз в 15 мин")
	if community.userbot_assigned:
		cadence.append("userbot раз в 6 ч")
	tail = f" · {', '.join(cadence)}" if cadence else ""
	return f"{format_local(overview.fetched_at)}{tail}"


def reference_rows(
	overview: CommunityOverviewDto, community: CommunityDto
) -> list[tuple[str, str]]:
	"""Справка «подпись — значение» в порядке макета."""
	publisher = "—"
	if community.default_account_label:
		role = f" · {role_caption(community.default_role)}" if community.default_role else ""
		publisher = f"{community.default_account_label}{role}"
	elif community.bot_label:
		publisher = f"бот {community.bot_label}"
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


def _date_text(moment: datetime | None) -> str:
	return moment.astimezone().strftime("%d.%m.%Y") if moment else "—"


# --- графики на QPainter --------------------------------------------------------------


@dataclass(frozen=True)
class _Bar:
	"""Столбец: доля высоты (0..1 от базы) и цвет."""

	share: float
	color: QColor


class _BarsChart(QWidget):
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


def _card(parent: QWidget, margins: tuple[int, int, int, int]) -> tuple[CardWidget, QVBoxLayout]:
	card = CardWidget(parent)
	layout = QVBoxLayout(card)
	layout.setContentsMargins(*margins)
	layout.setSpacing(10)
	return card, layout


def _label(
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


class OverviewTab(QWidget):
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

	def set_polling(self, active: bool) -> None:
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
		# с ним происходит»; числа и графики — следом
		self._layout.addWidget(self._reference(overview))
		self._layout.addWidget(self._tiles(overview))
		charts = self._charts(overview)
		if charts is not None:
			self._layout.addWidget(charts)
		hours = self._hours(overview)
		if hours is not None:
			self._layout.addWidget(hours)
		if charts is None and hours is None:
			empty = CaptionLabel(
				"Данных пока нет: статистика накопится за неделю наблюдений.", self
			)
			self._layout.addWidget(empty)
		note = CaptionLabel(source_note(overview, self._community), self)
		note.setWordWrap(True)
		self._layout.addWidget(tinted(note, DIM_TEXT))
		self._layout.addStretch()

	def _tiles(self, overview: CommunityOverviewDto) -> QWidget:
		"""Четыре плитки чисел равной ширины."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(10)
		community = self._community
		is_group = community.kind is CommunityKind.GROUP
		audience = "Участников" if is_group else "Подписчиков"
		delta_text, delta_color = delta_caption(overview.participants_delta)
		tiles: list[
			tuple[str, list[tuple[str, int, tuple[str, str] | None]], str, tuple[str, str]]
		] = [
			(
				audience,
				[(_count(overview.participants), 24, None)],
				delta_text,
				delta_color,
			),
		]
		if is_group:
			tiles.append(
				(
					"Онлайн сейчас",
					[(_count(overview.online), 24, None)],
					share_caption(overview.online, overview.participants, "участников"),
					DIM_TEXT,
				)
			)
		else:
			tiles.append(
				(
					"Просмотров на пост",
					[(_count(overview.views_per_post), 24, None)],
					"медиана последних постов"
					if overview.views_per_post is not None
					else "нет данных",
					DIM_TEXT,
				)
			)
		tiles.append(
			(
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
			(
				"Удалённых аккаунтов",
				[(_count(overview.deleted_found), 24, None)],
				deleted_caption(
					overview.deleted_found, overview.participants, overview.deleted_checked_at
				),
				DIM_TEXT,
			)
		)
		for title, values, caption, caption_color in tiles:
			card, layout = _card(box, (14, 11, 14, 11))
			layout.setSpacing(2)
			layout.addWidget(_label(card, title, 12))
			values_row = QHBoxLayout()
			values_row.setSpacing(8)
			for text, size, color in values:
				values_row.addWidget(
					_label(card, text, size, color, bold=True),
					alignment=Qt.AlignmentFlag.AlignBottom,
				)
			values_row.addStretch()
			layout.addLayout(values_row)
			layout.addWidget(_label(card, caption, 12, caption_color))
			row.addWidget(card, stretch=1)
		return box

	def _charts(self, overview: CommunityOverviewDto) -> QWidget | None:
		"""Два графика в ряд: участники за 30 дней и приходы/уходы за 14."""
		growth = overview.growth
		flow_days = sorted(
			{p.day for p in overview.flow_joined} | {p.day for p in overview.flow_left}
		)
		if not growth and not flow_days:
			return None
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(10)
		if growth:
			card, layout = _card(box, (14, 12, 14, 10))
			head = QHBoxLayout()
			who = "Участники" if self._community.kind is CommunityKind.GROUP else "Подписчики"
			head.addWidget(_label(card, f"{who}, {GROWTH_DAYS} дней", 13))
			head.addStretch()
			head.addWidget(_label(card, growth_subtitle(growth), 12, DIM_TEXT))
			layout.addLayout(head)
			layout.addWidget(_BarsChart([p.value for p in growth], axis_dates(growth), card))
			row.addWidget(card, stretch=145)
		if flow_days:
			joined = {p.day: p.value for p in overview.flow_joined}
			left = {p.day: p.value for p in overview.flow_left}
			card, layout = _card(box, (14, 12, 14, 10))
			layout.addWidget(_label(card, f"Пришли и ушли, {FLOW_DAYS} дней", 13))
			layout.addWidget(
				_FlowChart(
					[joined.get(d, 0) for d in flow_days], [left.get(d, 0) for d in flow_days], card
				)
			)
			legend = QHBoxLayout()
			legend.setSpacing(6)
			legend.addWidget(_swatch(card))
			legend.addWidget(_label(card, "пришли", 11))
			legend.addSpacing(8)
			legend.addWidget(_swatch(card, error=True))
			legend.addWidget(_label(card, "ушли", 11))
			legend.addStretch()
			period = tuple(DayPoint(d, 0) for d in flow_days)
			legend.addWidget(_label(card, period_caption(period), 11, DIM_TEXT))
			layout.addLayout(legend)
			row.addWidget(card, stretch=100)
		return box

	def _hours(self, overview: CommunityOverviewDto) -> QWidget | None:
		"""Профиль по часам суток на всю ширину."""
		hours = overview.hours
		if not hours:
			return None
		card: QWidget
		card, layout = _card(self, (14, 12, 14, 10))
		head = QHBoxLayout()
		title = "Онлайн по часам суток" if overview.hours_online else "Активность по часам суток"
		head.addWidget(_label(card, title, 13))
		head.addStretch()
		head.addWidget(_label(card, hours_subtitle(hours, overview.hours_online), 12, DIM_TEXT))
		layout.addLayout(head)
		labels = [(0, "00"), (6, "06"), (12, "12"), (18, "18"), (23, "23")]
		layout.addWidget(
			_BarsChart(
				list(hours),
				labels,
				card,
				canvas=_CANVAS_SHORT,
				gap=3,
				color=_BAR_DIM,
				highlight="max",
				from_zero=True,
			)
		)
		return card

	def _reference(self, overview: CommunityOverviewDto) -> QWidget:
		"""Справка: сетка 2 × N «подпись / значение» с хайрлайнами."""
		box = QWidget(self)
		grid = QGridLayout(box)
		grid.setContentsMargins(0, 0, 0, 0)
		grid.setHorizontalSpacing(28)
		grid.setVerticalSpacing(0)
		rows = reference_rows(overview, self._community)
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
				label = _label(cell, label_text, 12)
				label.setFixedWidth(104)
				line.addWidget(label)
				value = _label(cell, "", 13)
				value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
				elide_text(value, value_text)
				line.addWidget(value, stretch=1)
				line_box = QWidget(cell)
				line_box.setLayout(line)
				line_box.setFixedHeight(30)
				cell_layout.addWidget(line_box)
				if position < len(items) - 1:
					cell_layout.addWidget(HorizontalSeparator(cell))
				grid.addWidget(cell, position, column)
			grid.setColumnStretch(column, 1)
		return box


def _swatch(parent: QWidget, *, error: bool = False) -> QWidget:
	"""Точка легенды — штатный ``DotInfoBadge``: акцент или цвет ошибки."""
	badge: QWidget = DotInfoBadge.error(parent) if error else DotInfoBadge.attension(parent)
	return badge


def _count(value: int | None) -> str:
	"""Число с разделителями или «—»."""
	return format_count(value) if value is not None else "—"
