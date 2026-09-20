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
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	CaptionLabel,
	CardWidget,
	FluentIcon,
	PrimaryPushButton,
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
	TitleEditor,
	clear_layout,
	elide_text,
	entity_avatar,
	error_reporter,
	page_layout,
	section_header,
	tinted,
)
from pxcontrol.ui.pages.community_overview import (
	OverviewCards,
	Tile,
	TileWidgets,
	days_chart,
	hours_chart,
	period_caption,
)
from pxcontrol.ui.pages.user_actions import (
	ACTIVITY_POLL_MS,
	delete_bot,
	delete_user,
	run_bot_action,
	run_user_action,
	save_bot_label,
	save_user_label,
)
from pxcontrol.ui.pages.user_state import (
	BOT_ACTION_LABELS,
	BOT_LABEL_PLACEHOLDER,
	USER_ACTION_LABELS,
	USER_LABEL_PLACEHOLDER,
	BotAction,
	UserAction,
	bot_actions,
	bot_reference_rows,
	bot_state,
	bot_subtitle,
	busy_days_caption,
	hours_caption,
	kind_rows,
	live_shown,
	live_text,
	membership_caption,
	state_badge,
	user_actions,
	user_reference_rows,
	user_route_key,
	user_state,
	user_subtitle,
	window_tile_caption,
)

logger = logging.getLogger(__name__)

#: Размер аватара-буквы в шапке (как у страницы сообщества).
_HEADER_LOGO_SIZE = 56

#: Потоковые сетки: плитки и графики — те же минимумы, что у «Обзора».
_TILE_MIN_WIDTH = 180
_CHART_MIN_WIDTH = 340
_CARD_SPACING = 10

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


#: Окна плиток в порядке показа: подпись и поле снимка.
_WINDOWS: tuple[tuple[str, str], ...] = (
	("За час", "last_hour"),
	("За сутки", "last_day"),
	("За неделю", "last_week"),
)


class _ActivityOverview(OverviewCards):
	"""Тело страницы: справка, плитки, графики, виды операций — без кнопок.

	Справка и плитки собираются один раз, а раз в пять секунд у них
	меняются только тексты: пересборка мигала бы карточками на каждом
	тике. Графики строятся при показе — они не «живые».
	"""

	def __init__(self, parent: QWidget) -> None:
		super().__init__(parent)
		self._layout = QVBoxLayout(self)
		self._layout.setContentsMargins(0, 0, 0, 0)
		self._layout.setSpacing(density.spacing().block_spacing)
		self._reference_box = QVBoxLayout()
		self._tiles_box = QVBoxLayout()
		self._charts_box = QVBoxLayout()
		self._layout.addLayout(self._reference_box)
		self._layout.addLayout(self._tiles_box)
		self._layout.addLayout(self._charts_box)
		self._reference_values: list[QLabel] = []
		self._reference_keys: list[str] = []
		self._tiles: list[TileWidgets] = []
		self._tiles_empty: QWidget | None = None

	def render_reference(self, rows: list[tuple[str, str]]) -> None:
		"""Справка: собирается по составу строк, дальше меняются только значения."""
		keys = [key for key, _value in rows]
		if keys != self._reference_keys:
			clear_layout(self._reference_box)
			box, self._reference_values = self._reference_widgets(rows)
			self._reference_keys = keys
			self._reference_box.addWidget(box)
			return
		for label, (_key, value) in zip(self._reference_values, rows, strict=True):
			elide_text(label, value)

	def render_tiles(self, activity: OwnerActivityDto | None) -> None:
		"""Плитки окон: строятся один раз, дальше обновляются числа и подписи."""
		if activity is None:
			if self._tiles_empty is None:
				clear_layout(self._tiles_box)
				self._tiles = []
				self._tiles_empty = CaptionLabel("Операций ещё не было.", self)
				self._tiles_box.addWidget(self._tiles_empty)
			return
		if not self._tiles:
			clear_layout(self._tiles_box)
			self._tiles_empty = None
			self._tiles = [
				self._tile_widgets(self._window_tile(title, getattr(activity, field)))
				for title, field in _WINDOWS
			]
			self._tiles_box.addWidget(
				FlowGrid(
					[tile.card for tile in self._tiles],
					self,
					min_width=_TILE_MIN_WIDTH,
					spacing=_CARD_SPACING,
				)
			)
			return
		for widgets, (title, field) in zip(self._tiles, _WINDOWS, strict=True):
			tile = self._window_tile(title, getattr(activity, field))
			widgets.values[0].setText(tile.values[0][0])
			widgets.caption.setText(tile.caption)
			tinted(widgets.caption, tile.caption_color)

	@staticmethod
	def _window_tile(title: str, stats: WindowStats) -> Tile:
		color = ACCENT_TEXT if stats.floods or stats.errors else DIM_TEXT
		return Tile(title, [(str(stats.operations), 24, None)], window_tile_caption(stats), color)

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
		self._timer.setInterval(ACTIVITY_POLL_MS)
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
			entity_avatar(box, subject.id, title_text, None, _HEADER_LOGO_SIZE),
			alignment=Qt.AlignmentFlag.AlignTop,
		)
		column = QVBoxLayout()
		column.setSpacing(4)
		title_row = QHBoxLayout()
		title_row.setSpacing(10)
		title = TitleLabel(box)
		# «занимай, что дадут», но не шире своего текста: карандаш и плашка
		# встают сразу за именем (тот же приём, что у страницы сообщества);
		# предел снимает сама правка на месте
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		self._title_editor = TitleEditor(title, box, fit_text=True)
		self._title_editor.set_text(title_text)
		self._title_editor.submitted.connect(self._on_renamed)
		title_row.addWidget(self._title_editor, stretch=1)
		rename = TransparentToolButton(FluentIcon.EDIT, box)
		rename.setToolTip("Переименовать (Enter — сохранить, Esc — отмена)")
		rename.clicked.connect(self._begin_rename)
		title_row.addWidget(rename)
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
		more.setToolTip("Пауза, диагностика, удаление")
		more.clicked.connect(partial(self._show_menu, more))
		row.addWidget(more, alignment=Qt.AlignmentFlag.AlignTop)
		self._header_box.addWidget(box)
		self._apply_live()

	def _begin_rename(self) -> None:
		"""Карандаш: поле ввода на месте заголовка — пометка или название бота."""
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			self._title_editor.begin(subject.label or "", USER_LABEL_PLACEHOLDER)
		else:
			self._title_editor.begin(subject.label, BOT_LABEL_PLACEHOLDER)

	def _on_renamed(self, text: str) -> None:
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			save_user_label(self._worker, self, subject, text, self._after_change)
		else:
			save_bot_label(self._worker, self, subject, text, self._after_change)

	def _main_action(self, parent: QWidget) -> QPushButton | None:
		"""Кнопка шапки — только вход: единственное действие, которое ищут
		глазами. Пауза, возобновление, диагностика и удаление — в меню «…»,
		как редкие и весомые действия."""
		subject = self._subject
		if not isinstance(subject, TgAccountDto) or UserAction.LOGIN not in user_actions(subject):
			return None
		button: QPushButton = PrimaryPushButton(USER_ACTION_LABELS[UserAction.LOGIN], parent)
		button.clicked.connect(partial(self._run_user_action, UserAction.LOGIN, subject))
		return button

	def _show_menu(self, anchor: QWidget) -> None:
		"""Меню «…»: пауза или возобновление, диагностика бота, удаление."""
		menu = RoundMenu(parent=self)
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			for action in user_actions(subject):
				if action is UserAction.LOGIN:
					continue
				item = Action(USER_ACTION_LABELS[action], menu)
				item.triggered.connect(partial(self._run_user_action, action, subject))
				menu.addAction(item)
		else:
			for bot_action in bot_actions(subject):
				item = Action(BOT_ACTION_LABELS[bot_action], menu)
				if bot_action is BotAction.WHEREABOUTS:
					item.setIcon(FluentIcon.SEARCH.icon())
				item.triggered.connect(partial(self._run_bot_action, bot_action, subject))
				menu.addAction(item)
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

	def _render_bot_communities(self, memberships: list[AccountMembershipDto]) -> None:
		"""Сообщества бота — тем же списком, что у пользователя (ADR-0035).

		Пул стал общим, и подстрочник у обоих видов теперь один: участие,
		признак публикатора, выключенность сообщества.
		"""
		rows = [(m.community, membership_caption(m)) for m in memberships]
		self._render_communities(rows, "Не состоит в сообществах.")

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
		layout.addWidget(entity_avatar(card, community.id, community.title, None, _ROW_LOGO_SIZE))
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
		run_user_action(self._worker, self, action, account, self._after_change)

	def _run_bot_action(self, action: BotAction, bot: BotDto) -> None:
		run_bot_action(self._worker, self, action, bot, self._after_change)

	def _on_delete(self) -> None:
		subject = self._subject
		if isinstance(subject, TgAccountDto):
			delete_user(self._worker, self, subject, self._after_change)
		else:
			delete_bot(self._worker, self, subject, self._after_change)
