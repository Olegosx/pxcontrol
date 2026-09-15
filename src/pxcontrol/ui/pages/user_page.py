"""Страница аккаунта — пользователя или бота (ADR-0030).

Одна страница на оба вида исполнителей, у бота она короче. Открывается
с карточки дашборда «Пользователи и боты» и живёт пунктом его подменю
(главное окно приводит подменю в соответствие по ``users_changed``).

Устройство: шапка (аватар-буква, имя, плашка состояния, подстрочник,
главное действие по состоянию и меню «…»), затем **обзор** без единой
кнопки — по правилу «Обзора» сообщества: справка, плитки окон
час / сутки / неделя, графики (часы суток за 7 дней, занятость
и флуд-лимиты по дням за 30) и виды операций строками — и в конце
список сообществ, где аккаунт состоит (только чтение; клик ведёт
на страницу сообщества).

Снимок активности перечитывается при показе и раз в пять секунд, пока
страница видна (плитки и живая пометка); история для графиков и список
сообществ — при показе и после действий шапки. Вёрстка карточек —
общая с «Обзором» (:class:`OverviewCards`), тексты — чистые функции
:mod:`user_state`.
"""

from __future__ import annotations

import logging
from functools import partial

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	CaptionLabel,
	CardWidget,
	FluentIcon,
	PrimaryPushButton,
	PushButton,
	RoundMenu,
	ScrollArea,
	StrongBodyLabel,
	TitleLabel,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.activity import (
	ActivityHistoryDto,
	OwnerActivityDto,
	WindowStats,
)
from pxcontrol.engine.services.communities import AccountMembershipDto, CommunityDto
from pxcontrol.engine.telegram.lane import LaneOwner, OwnerKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ACCENT_TEXT,
	DIM_TEXT,
	FlowGrid,
	clear_layout,
	community_logo,
	elide_text,
	error_reporter,
	page_layout,
	section_header,
	tinted,
)
from pxcontrol.ui.pages.community_overview import (
	OverviewCards,
	Tile,
	days_chart,
	hours_chart,
	period_caption,
)
from pxcontrol.ui.pages.user_actions import (
	delete_bot,
	delete_user,
	diagnose_bot,
	rename_user,
	set_bot_paused,
	set_user_paused,
	start_login,
)
from pxcontrol.ui.pages.user_state import (
	BOT_ACTION_LABELS,
	USER_ACTION_LABELS,
	BotAction,
	UserAction,
	bot_actions,
	bot_community_caption,
	bot_reference_rows,
	bot_state,
	bot_subtitle,
	busy_days_caption,
	hours_caption,
	kind_rows,
	live_shown,
	live_text,
	membership_caption,
	primary_user_action,
	state_badge,
	user_actions,
	user_reference_rows,
	user_route_key,
	user_state,
	user_subtitle,
	window_tile_caption,
)
from pxcontrol.ui.pages.users import letter_avatar

logger = logging.getLogger(__name__)

#: Размер аватара-буквы в шапке (как у страницы сообщества).
_HEADER_LOGO_SIZE = 56

#: Потоковые сетки: плитки и графики — те же минимумы, что у «Обзора».
_TILE_MIN_WIDTH = 180
_CHART_MIN_WIDTH = 340
_CARD_SPACING = 10

#: Период опроса активности, пока страница видна (ADR-0030).
_ACTIVITY_POLL_MS = 5000

#: Логотип в строке сообщества.
_ROW_LOGO_SIZE = 28

Subject = TgAccountDto | BotDto


def subject_owner(subject: Subject) -> LaneOwner:
	"""Владелец дорожки по снимку: пользователь или бот."""
	kind = OwnerKind.USER if isinstance(subject, TgAccountDto) else OwnerKind.BOT
	return LaneOwner(kind, subject.id)


def subject_title(subject: Subject) -> str:
	"""Заголовок страницы: отображаемое имя пользователя или название бота."""
	return subject.display if isinstance(subject, TgAccountDto) else subject.label


class _ActivityOverview(OverviewCards):
	"""Тело страницы: справка, плитки, графики, виды операций — без кнопок."""

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(density.spacing().block_spacing)
		self._tiles_box = QVBoxLayout()
		self._reference_box = QVBoxLayout()
		self._charts_box = QVBoxLayout()
		self._layout.addLayout(self._reference_box)
		self._layout.addLayout(self._tiles_box)
		self._layout.addLayout(self._charts_box)

	def render_reference(self, rows: list[tuple[str, str]]) -> None:
		clear_layout(self._reference_box)
		self._reference_box.addWidget(self._reference_grid(rows))

	def render_tiles(self, activity: OwnerActivityDto | None) -> None:
		"""Плитки окон — обновляются раз в пять секунд, отдельно от графиков."""
		clear_layout(self._tiles_box)
		if activity is None:
			self._tiles_box.addWidget(CaptionLabel("Операций ещё не было.", self))
			return
		windows = (
			("За час", activity.last_hour),
			("За сутки", activity.last_day),
			("За неделю", activity.last_week),
		)
		tiles = [self._window_tile(title, stats) for title, stats in windows]
		self._tiles_box.addWidget(
			FlowGrid(tiles, self, min_width=_TILE_MIN_WIDTH, spacing=_CARD_SPACING)
		)

	def _window_tile(self, title: str, stats: WindowStats) -> QWidget:
		count = stats.operations
		color = ACCENT_TEXT if stats.floods or stats.errors else DIM_TEXT
		return self._tile_card(
			Tile(title, [(str(count), 24, None)], window_tile_caption(stats), color)
		)

	def render_history(self, history: ActivityHistoryDto) -> None:
		"""Графики и виды операций — при показе страницы."""
		clear_layout(self._charts_box)
		cards: list[QWidget] = []
		if sum(history.hours) > 0:
			card, layout = self._chart_card("Операции по часам суток", hours_caption(history.hours))
			layout.addWidget(hours_chart(card, history.hours))
			cards.append(card)
		if any(point.value > 0 for point in history.busy_days):
			card, layout = self._chart_card(
				"Занятость по дням", busy_days_caption(history.busy_days)
			)
			layout.addWidget(days_chart(card, history.busy_days, from_zero=True))
			cards.append(card)
		if any(point.value > 0 for point in history.flood_days):
			card, layout = self._chart_card(
				"Флуд-лимиты по дням", period_caption(history.flood_days)
			)
			layout.addWidget(days_chart(card, history.flood_days, from_zero=True))
			cards.append(card)
		kinds = self._rows_card("Виды операций, 30 дней", kind_rows(history.kinds))
		if kinds is not None:
			cards.append(kinds)
		if not cards:
			self._charts_box.addWidget(
				CaptionLabel("Графики появятся, когда накопятся операции.", self)
			)
			return
		for ready in cards:
			card_layout = ready.layout()
			if isinstance(card_layout, QVBoxLayout):
				card_layout.addStretch()
		self._charts_box.addWidget(
			FlowGrid(cards, self, min_width=_CHART_MIN_WIDTH, spacing=_CARD_SPACING)
		)


class UserPage(ScrollArea):
	"""Страница аккаунта: шапка с действиями, обзор активности, сообщества.

	Сигналы: ``changed`` — данные изменились (пауза, пометка, удаление):
	главное окно перечитывает дашборд, а тот — подменю; ``open_community``
	— клик по строке сообщества.
	"""

	changed = Signal()
	open_community = Signal(int)

	def __init__(
		self, worker: EngineWorker, subject: Subject, parent: QWidget | None = None
	) -> None:
		super().__init__(parent)
		self._worker = worker
		self._subject: Subject = subject
		self._owner = subject_owner(subject)
		self.setObjectName(user_route_key(self._owner))
		self._show_error = error_reporter(self)
		self._activity: OwnerActivityDto | None = None
		self._build()
		self._render_header()
		self._timer = QTimer(self)
		self._timer.setInterval(_ACTIVITY_POLL_MS)
		self._timer.timeout.connect(self._poll_activity)

	@property
	def owner(self) -> LaneOwner:
		"""Владелец дорожки этой страницы."""
		return self._owner

	def update_subject(self, subject: Subject) -> None:
		"""Свежий снимок из дашборда (синхронизация главного окна)."""
		self._subject = subject
		self._render_header()
		self._overview.render_reference(self._reference_rows())

	# --- сборка ------------------------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		self._header_box = QVBoxLayout()
		layout.addLayout(self._header_box)
		self._overview = _ActivityOverview(self)
		layout.addWidget(self._overview)
		self._communities_box = QVBoxLayout()
		self._communities_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._communities_box)
		layout.addStretch()

	def _render_header(self) -> None:
		"""Шапка: аватар, имя с плашкой, подстрочник, главное действие, «…»."""
		clear_layout(self._header_box)
		subject = self._subject
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(14)
		row.setAlignment(Qt.AlignmentFlag.AlignTop)
		title_text = subject_title(subject)
		row.addWidget(
			letter_avatar(box, subject.id, title_text, _HEADER_LOGO_SIZE),
			alignment=Qt.AlignmentFlag.AlignTop,
		)
		column = QVBoxLayout()
		column.setSpacing(4)
		title_row = QHBoxLayout()
		title_row.setSpacing(10)
		title = TitleLabel(box)
		# «занимай, что дадут», но не шире своего текста: плашка встаёт
		# сразу за именем (тот же приём, что у страницы сообщества)
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		title.setMaximumWidth(title.fontMetrics().horizontalAdvance(title_text) + 8)
		elide_text(title, title_text)
		title_row.addWidget(title, stretch=1)
		state = user_state(subject) if isinstance(subject, TgAccountDto) else bot_state(subject)
		title_row.addWidget(state_badge(box, state))
		self._live = tinted(CaptionLabel(box), ACCENT_TEXT)
		title_row.addWidget(self._live)
		title_row.addStretch()
		column.addLayout(title_row)
		subtitle = CaptionLabel(box)
		subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(
			subtitle,
			user_subtitle(subject) if isinstance(subject, TgAccountDto) else bot_subtitle(subject),
		)
		column.addWidget(subtitle)
		row.addLayout(column, stretch=1)
		main = self._main_action(box)
		if main is not None:
			row.addWidget(main, alignment=Qt.AlignmentFlag.AlignTop)
		more = TransparentToolButton(FluentIcon.MORE, box)
		more.setToolTip("Пометка, диагностика, удаление")
		more.clicked.connect(partial(self._show_menu, more))
		row.addWidget(more, alignment=Qt.AlignmentFlag.AlignTop)
		self._header_box.addWidget(box)
		self._apply_live()

	def _main_action(self, parent: QWidget) -> QPushButton | None:
		"""Главное действие шапки — первое из набора по состоянию."""
		subject = self._subject
		button: QPushButton
		if isinstance(subject, TgAccountDto):
			actions = [a for a in user_actions(subject) if a is not UserAction.LABEL]
			if not actions:
				return None
			action = actions[0]
			label = USER_ACTION_LABELS[action]
			button = (
				PrimaryPushButton(label, parent)
				if primary_user_action(action)
				else PushButton(label, parent)
			)
			button.clicked.connect(partial(self._run_user_action, action, subject))
			return button
		bot_action = next((a for a in bot_actions(subject) if a is not BotAction.WHEREABOUTS), None)
		if bot_action is None:
			return None
		label = BOT_ACTION_LABELS[bot_action]
		button = (
			PrimaryPushButton(label, parent)
			if bot_action is BotAction.RESUME
			else PushButton(label, parent)
		)
		button.clicked.connect(partial(self._run_bot_action, bot_action, subject))
		return button

	def _show_menu(self, anchor: QWidget) -> None:
		"""Меню «…»: пометка, диагностика бота, удаление."""
		menu = RoundMenu(parent=self)
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			rename = Action(FluentIcon.TAG, "Пометка…", menu)
			rename.triggered.connect(partial(self._run_user_action, UserAction.LABEL, subject))
			menu.addAction(rename)
		else:
			whereabouts = Action(FluentIcon.SEARCH, "Где состоит?", menu)
			whereabouts.triggered.connect(
				partial(self._run_bot_action, BotAction.WHEREABOUTS, subject)
			)
			menu.addAction(whereabouts)
		menu.addSeparator()
		delete = Action(FluentIcon.DELETE, "Удалить из приложения…", menu)
		delete.triggered.connect(self._on_delete)
		menu.addAction(delete)
		menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

	# --- данные --------------------------------------------------------------------------

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Показ: полный снимок (активность, история, сообщества) и опрос."""
		super().showEvent(event)
		self.reload()
		self._timer.start()

	def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 — API Qt
		"""Невидимая страница движок не опрашивает."""
		self._timer.stop()
		super().hideEvent(event)

	def reload(self) -> None:
		"""Перечитывает всё: снимок активности, историю, сообщества."""
		self._overview.render_reference(self._reference_rows())
		run_in_engine(
			self._worker,
			self._worker.engine.activity.snapshot(),
			self,
			self._on_activity,
			self._show_error,
		)
		run_in_engine(
			self._worker,
			self._worker.engine.activity.history(self._owner),
			self,
			self._overview.render_history,
			self._show_error,
		)
		if self._owner.kind is OwnerKind.USER:
			run_in_engine(
				self._worker,
				self._worker.engine.communities.communities_of_account(self._owner.id),
				self,
				self._render_memberships,
				self._show_error,
			)
		else:
			run_in_engine(
				self._worker,
				self._worker.engine.communities.communities_of_bot(self._owner.id),
				self,
				self._render_bot_communities,
				self._show_error,
			)

	def _poll_activity(self) -> None:
		"""Тик таймера: только снимок активности — плитки и живая пометка."""
		run_in_engine(
			self._worker,
			self._worker.engine.activity.snapshot(),
			self,
			self._on_activity,
			# опрос фоновый: сбой одного тика не стоит плашки
			lambda *_a: None,
		)

	def _on_activity(self, snapshot: dict[LaneOwner, OwnerActivityDto]) -> None:
		self._activity = snapshot.get(self._owner)
		self._overview.render_tiles(self._activity)
		self._overview.render_reference(self._reference_rows())
		self._apply_live()

	def _apply_live(self) -> None:
		"""Живая пометка в шапке — только тем, кто может работать."""
		subject = self._subject
		state = user_state(subject) if isinstance(subject, TgAccountDto) else bot_state(subject)
		if self._activity is None or not live_shown(state):
			self._live.setText("")
			return
		self._live.setText(live_text(self._activity.live))

	def _reference_rows(self) -> list[tuple[str, str]]:
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			return user_reference_rows(subject, self._activity)
		return bot_reference_rows(subject, self._activity)

	# --- сообщества ------------------------------------------------------------------

	def _render_memberships(self, memberships: list[AccountMembershipDto]) -> None:
		rows = [(m.community, membership_caption(m)) for m in memberships]
		self._render_communities(rows, "Не состоит в сообществах.")

	def _render_bot_communities(self, communities: list[CommunityDto]) -> None:
		rows = [(c, bot_community_caption(c)) for c in communities]
		self._render_communities(rows, "Не назначен публикатором ни в одном сообществе.")

	def _render_communities(self, rows: list[tuple[CommunityDto, str]], empty: str) -> None:
		"""Список сообществ: строка с логотипом и подстрочником, клик — на страницу."""
		clear_layout(self._communities_box)
		self._communities_box.addWidget(
			section_header(self, "Сообщества", len(rows), icon=FluentIcon.HOME)
		)
		if not rows:
			self._communities_box.addWidget(CaptionLabel(empty, self))
			return
		for community, caption in rows:
			self._communities_box.addWidget(self._community_row(community, caption))

	def _community_row(self, community: CommunityDto, caption: str) -> QWidget:
		card = CardWidget(self)
		card.setCursor(Qt.CursorShape.PointingHandCursor)
		layout = QHBoxLayout(card)
		layout.setContentsMargins(*density.spacing().card_margins)
		layout.setSpacing(12)
		layout.addWidget(community_logo(card, community.id, community.title, None, _ROW_LOGO_SIZE))
		column = QVBoxLayout()
		column.setSpacing(2)
		title = StrongBodyLabel(card)
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(title, community.title)
		column.addWidget(title)
		details = CaptionLabel(card)
		details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(details, caption)
		column.addWidget(details)
		layout.addLayout(column, stretch=1)
		card.clicked.connect(partial(self.open_community.emit, community.id))
		result: QWidget = card
		return result

	# --- действия --------------------------------------------------------------------

	def _after_change(self) -> None:
		"""После действия: свой снимок и сигнал главному окну."""
		self.changed.emit()

	def _run_user_action(self, action: UserAction, account: TgAccountDto) -> None:
		if action is UserAction.LOGIN:
			start_login(self._worker, self, account, self._after_change)
		elif action is UserAction.PAUSE:
			set_user_paused(self._worker, self, account, True, self._after_change)
		elif action is UserAction.RESUME:
			set_user_paused(self._worker, self, account, False, self._after_change)
		elif action is UserAction.LABEL:
			rename_user(self._worker, self, account, self._after_change)

	def _run_bot_action(self, action: BotAction, bot: BotDto) -> None:
		if action is BotAction.WHEREABOUTS:
			diagnose_bot(self._worker, self, bot)
		elif action is BotAction.PAUSE:
			set_bot_paused(self._worker, self, bot, True, self._after_change)
		elif action is BotAction.RESUME:
			set_bot_paused(self._worker, self, bot, False, self._after_change)

	def _on_delete(self) -> None:
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			delete_user(self._worker, self, subject, self._after_change)
		else:
			delete_bot(self._worker, self, subject, self._after_change)
