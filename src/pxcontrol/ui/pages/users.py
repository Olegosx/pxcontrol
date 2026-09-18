"""Дашборд «Пользователи и боты»: сводка, разделы, карточки (ADR-0029).

Раздел первого уровня над «Каналами и группами»: пользователи
(userbot-аккаунты MTProto) и боты Bot API — то, чьими руками приложение
работает в Telegram. Прежде всё это жило категорией «Аккаунты»
в настройках; ключи ИИ остались там своей категорией.

Страница по образцу дашборда сообществ: шапка (поиск, «Добавить»),
строка сводки, разделы «Пользователи» и «Боты» плиткой карточек
(пустой раздел не рисуется). Карточка — информация и действия:
добавление, удаление, приостановка и возобновление, вход, пометка,
диагностика бота. Клик по карточке открывает страницу аккаунта —
пункт живого подменю, которое главное окно приводит в соответствие
по сигналу ``users_changed`` (как у сообществ).

Правила показа (состояние, набор действий, подписи, сводка, поиск) —
чистые функции :mod:`user_state`, они тестируются без Qt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	BodyLabel,
	CaptionLabel,
	CardWidget,
	FluentIcon,
	HorizontalSeparator,
	InfoBadge,
	PrimaryDropDownPushButton,
	PrimaryPushButton,
	PushButton,
	RoundMenu,
	ScrollArea,
	SearchLineEdit,
	SimpleCardWidget,
	StrongBodyLabel,
	SubtitleLabel,
	TransparentToolButton,
	VerticalSeparator,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto, TgApiDto
from pxcontrol.engine.services.activity import OwnerActivityDto
from pxcontrol.engine.telegram.lane import LaneOwner, OwnerKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ACCENT_TEXT,
	FlowGrid,
	FormDialog,
	TitleEditor,
	WarningLabel,
	bold_numbers,
	clear_layout,
	dim_widget,
	elide_text,
	entity_avatar,
	error_reporter,
	exec_dialog,
	font_px,
	noop,
	page_layout,
	plural,
	require_filled,
	section_header,
	show_info,
	show_success,
	tinted,
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
	activity_text,
	bot_actions,
	bot_participation_text,
	bot_state,
	bot_subtitle,
	live_shown,
	live_text,
	matches_bot_search,
	matches_user_search,
	participation_text,
	primary_user_action,
	state_badge,
	user_actions,
	user_state,
	user_subtitle,
	users_summary,
)

logger = logging.getLogger(__name__)

#: Сетка карточек — те же числа, что у дашборда сообществ.
CARD_MIN_WIDTH = 360
GRID_SPACING = 12

#: Минимальная высота карточки (растёт по содержимому).
_CARD_MIN_HEIGHT = 132

#: Размер аватара-буквы в шапке карточки.
_CARD_LOGO_SIZE = 40

#: Высоты и кегли элементов — по макету дашборда сообществ.
_SUMMARY_BADGE_HEIGHT = 26
_ACTION_HEIGHT = 26
_ACTION_FONT_PX = 13

#: Непрозрачность приостановленной карточки — как у выключенного сообщества.
_PAUSED_CARD_OPACITY = 0.62

#: Ширина поля поиска в шапке.
_SEARCH_WIDTH = 200

#: Карандаш правки заголовка — компактный, вровень со строкой текста.
_RENAME_BUTTON_PX = 24

#: Подсказка, когда вход невозможен без ключа приложения (ADR-0018).
_NO_API_KEY_HINT = (
	"Ключ API Telegram не задан — вход пользователей невозможен. "
	"Получите api_id и api_hash на my.telegram.org и сохраните: Настройки → Общие."
)


def _action_button(parent: QWidget, label: str, primary: bool) -> QPushButton:
	"""Кнопка действия карточки: высота и кегль — по макету."""
	button: QPushButton = PrimaryPushButton(label, parent) if primary else PushButton(label, parent)
	button.setFixedHeight(_ACTION_HEIGHT)
	button.setFont(font_px(_ACTION_FONT_PX))
	return button


class _Card(CardWidget):
	"""Общий каркас карточки: шапка с удалением, разделитель, строка сведений, кнопки.

	Только штатные элементы библиотеки (ADR-0023, п. 5). Ширины у карточки
	нет — её даёт колонка сетки; клик по карточке открывает страницу
	аккаунта.
	"""

	def __init__(
		self,
		parent: QWidget,
		*,
		seed: int,
		title: str,
		subtitle: str,
		paused: bool,
		on_delete: Callable[[], None],
		rename_initial: str,
		rename_placeholder: str,
		on_rename: Callable[[str], None],
	) -> None:
		"""``rename_*`` — правка заголовка на месте: начальный текст поля
		(пометка, не отображаемое имя), подсказка пустого поля и колбэк
		с введённым текстом (обрезанным)."""
		super().__init__(parent)
		self.setMinimumHeight(_CARD_MIN_HEIGHT)
		self._rename_initial = rename_initial
		self._rename_placeholder = rename_placeholder
		self._on_rename = on_rename
		# клик мимо кнопок открывает страницу аккаунта (ADR-0030);
		# кнопки перехватывают свои нажатия сами
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		if paused:
			dim_widget(self, _PAUSED_CARD_OPACITY)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 12, 16, 10)
		layout.setSpacing(9)
		layout.addWidget(self._header(seed, title, subtitle, on_delete))
		layout.addWidget(HorizontalSeparator(self))
		self._info = QHBoxLayout()
		self._info.setContentsMargins(0, 0, 0, 0)
		self._info.setSpacing(16)
		layout.addLayout(self._info)
		# активность (ADR-0030): числа за сутки слева, живая пометка справа;
		# заполняется и обновляется отдельно от остальной карточки
		activity_row = QHBoxLayout()
		activity_row.setContentsMargins(0, 0, 0, 0)
		activity_row.setSpacing(16)
		self._activity = CaptionLabel(self)
		self._activity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		activity_row.addWidget(self._activity, stretch=1)
		self._live = tinted(CaptionLabel(self), ACCENT_TEXT)
		activity_row.addWidget(self._live, alignment=Qt.AlignmentFlag.AlignRight)
		layout.addLayout(activity_row)
		self._live_shown = False
		layout.addStretch()
		self._actions = QHBoxLayout()
		self._actions.setContentsMargins(0, 0, 0, 0)
		self._actions.setSpacing(8)
		layout.addLayout(self._actions)

	def _header(
		self, seed: int, title: str, subtitle: str, on_delete: Callable[[], None]
	) -> QWidget:
		"""Шапка: аватар-буква, название, подстрочник, кнопка удаления справа."""
		box = QWidget(self)
		layout = QHBoxLayout(box)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(12)
		layout.addWidget(entity_avatar(box, seed, title, None, _CARD_LOGO_SIZE))
		column = QVBoxLayout()
		column.setSpacing(2)
		title_label = StrongBodyLabel(box)
		# «занимай, что дадут», но не шире своего текста: карандаш встаёт
		# сразу за заголовком, а длинное имя сокращается, не распирая карточку
		title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		self._title_editor = TitleEditor(title_label, box, fit_text=True)
		self._title_editor.set_text(title)
		self._title_editor.submitted.connect(self._on_rename)
		title_row = QHBoxLayout()
		title_row.setSpacing(4)
		title_row.addWidget(self._title_editor, stretch=1)
		rename = TransparentToolButton(FluentIcon.EDIT, box)
		rename.setFixedSize(_RENAME_BUTTON_PX, _RENAME_BUTTON_PX)
		rename.setToolTip("Переименовать (Enter — сохранить, Esc — отмена)")
		rename.clicked.connect(
			lambda: self._title_editor.begin(self._rename_initial, self._rename_placeholder)
		)
		title_row.addWidget(rename)
		title_row.addStretch()
		column.addLayout(title_row)
		details = CaptionLabel(box)
		details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(details, subtitle)
		column.addWidget(details)
		layout.addLayout(column, stretch=1)
		delete = TransparentToolButton(FluentIcon.DELETE, box)
		delete.setToolTip("Удалить")
		delete.clicked.connect(on_delete)
		layout.addWidget(delete, alignment=Qt.AlignmentFlag.AlignTop)
		return box

	def add_info(self, texts: list[str], badge: InfoBadge) -> None:
		"""Строка сведений: тексты слева (сжимаемые), плашка состояния справа."""
		group = QWidget(self)
		# группа сведений уступает место плашке: та говорит о состоянии
		# и обрезаться не должна
		group.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		group_layout = QHBoxLayout(group)
		group_layout.setContentsMargins(0, 0, 0, 0)
		group_layout.setSpacing(16)
		for text in texts:
			label = BodyLabel(group)
			label.setTextFormat(Qt.TextFormat.RichText)
			label.setText(bold_numbers(text))
			group_layout.addWidget(label)
		group_layout.addStretch()
		self._info.addWidget(group, stretch=1)
		badge.setParent(self)
		self._info.addWidget(badge, alignment=Qt.AlignmentFlag.AlignRight)

	def add_actions(self, buttons: list[QPushButton]) -> None:
		"""Строка кнопок действий; лишнее место — справа."""
		for button in buttons:
			self._actions.addWidget(button)
		self._actions.addStretch()

	def set_activity(self, activity: OwnerActivityDto | None) -> None:
		"""Обновляет строку активности на месте — без пересборки карточки.

		None — снимка ещё нет (движок не ответил): строка пустая, а не
		выдуманные нули.
		"""
		if activity is None:
			self._activity.setText("")
			self._live.setText("")
			return
		elide_text(self._activity, activity_text(activity.last_day))
		self._live.setText(live_text(activity.live) if self._live_shown else "")


class UserCard(_Card):
	"""Карточка пользователя: профиль, участие, Premium, состояние, действия."""

	def __init__(
		self,
		account: TgAccountDto,
		on_action: Callable[[UserAction, TgAccountDto], None],
		on_delete: Callable[[TgAccountDto], None],
		on_rename: Callable[[TgAccountDto, str], None],
		parent: QWidget,
	) -> None:
		super().__init__(
			parent,
			seed=account.id,
			title=account.display,
			subtitle=user_subtitle(account),
			paused=account.paused,
			on_delete=partial(on_delete, account),
			rename_initial=account.label or "",
			rename_placeholder=USER_LABEL_PLACEHOLDER,
			on_rename=partial(on_rename, account),
		)
		# подписка Premium здесь не словом: её несёт звезда в подстрочнике
		# (PREMIUM_MARK), а словами она названа на странице аккаунта
		self.add_info([participation_text(account)], state_badge(self, user_state(account)))
		self._live_shown = live_shown(user_state(account))
		buttons: list[QPushButton] = []
		for action in user_actions(account):
			button = _action_button(self, USER_ACTION_LABELS[action], primary_user_action(action))
			button.clicked.connect(partial(on_action, action, account))
			buttons.append(button)
		self.add_actions(buttons)


class BotCard(_Card):
	"""Карточка бота: @имя и токен маской, назначения, состояние, действия."""

	def __init__(
		self,
		bot: BotDto,
		on_action: Callable[[BotAction, BotDto], None],
		on_delete: Callable[[BotDto], None],
		on_rename: Callable[[BotDto, str], None],
		parent: QWidget,
	) -> None:
		super().__init__(
			parent,
			seed=bot.id,
			title=bot.label,
			subtitle=bot_subtitle(bot),
			paused=bot.paused,
			on_delete=partial(on_delete, bot),
			rename_initial=bot.label,
			rename_placeholder=BOT_LABEL_PLACEHOLDER,
			on_rename=partial(on_rename, bot),
		)
		self.add_info([bot_participation_text(bot)], state_badge(self, bot_state(bot)))
		self._live_shown = live_shown(bot_state(bot))
		buttons: list[QPushButton] = []
		for action in bot_actions(bot):
			button = _action_button(self, BOT_ACTION_LABELS[action], action is BotAction.RESUME)
			button.clicked.connect(partial(on_action, action, bot))
			buttons.append(button)
		self.add_actions(buttons)


class UsersPage(ScrollArea):
	"""Дашборд пользователей и ботов: шапка, сводка, два раздела карточек.

	Сигналы для главного окна: ``users_changed`` — свежие списки
	пользователей и ботов (синхронизация подменю), ``open_user`` —
	клик по карточке (переход на страницу аккаунта).
	"""

	users_changed = Signal(list, list)
	open_user = Signal(object)  # LaneOwner

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("users")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._accounts: list[TgAccountDto] = []
		self._bots: list[BotDto] = []
		self._api_key_set = True
		self._query = ""
		self._activity: dict[LaneOwner, OwnerActivityDto] = {}
		self._user_cards: dict[int, UserCard] = {}
		self._bot_cards: dict[int, BotCard] = {}
		self._build()
		# опрос активности — только пока страница видна (см. showEvent)
		self._activity_timer = QTimer(self)
		self._activity_timer.setInterval(ACTIVITY_POLL_MS)
		self._activity_timer.timeout.connect(self._poll_activity)

	def _build(self) -> None:
		"""Собирает шапку, строку сводки, подсказку о ключе и область разделов."""
		layout = page_layout(self)
		header = QHBoxLayout()
		header.setSpacing(12)
		header.addWidget(SubtitleLabel("Пользователи и боты", self))
		header.addStretch()
		self._search = SearchLineEdit(self)
		self._search.setPlaceholderText("Поиск")
		self._search.setFixedWidth(_SEARCH_WIDTH)
		self._search.setToolTip("По имени, @имени, пометке и телефону, регистр не важен")
		self._search.textChanged.connect(self._on_search_changed)
		header.addWidget(self._search)
		add_button = PrimaryDropDownPushButton(FluentIcon.ADD, "Добавить", self)
		menu = RoundMenu(parent=add_button)
		menu.addAction(Action(FluentIcon.PEOPLE, "Пользователя…", triggered=self._on_add_user))
		menu.addAction(Action(FluentIcon.ROBOT, "Бота…", triggered=self._on_add_bot))
		add_button.setMenu(menu)
		header.addWidget(add_button)
		layout.addLayout(header)
		self._summary_box = QVBoxLayout()
		layout.addLayout(self._summary_box)
		self._api_hint = WarningLabel(self)
		self._api_hint.setWordWrap(True)
		layout.addWidget(self._api_hint)
		self._sections = QVBoxLayout()
		self._sections.setSpacing(density.spacing().block_spacing)
		layout.addLayout(self._sections)
		layout.addStretch()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет данные при каждом показе: состояние связи меняется само."""
		super().showEvent(event)
		self.reload()
		self._activity_timer.start()

	def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 — API Qt
		"""Невидимая страница движок не опрашивает."""
		self._activity_timer.stop()
		super().hideEvent(event)

	# --- данные -----------------------------------------------------------------

	def reload(self) -> None:
		"""Перечитывает пользователей, ботов и наличие ключа API из движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_tg_accounts(),
			self,
			self._on_accounts_loaded,
			self._show_error,
		)

	def _on_accounts_loaded(self, accounts: list[TgAccountDto]) -> None:
		self._accounts = accounts
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_bots(),
			self,
			self._on_bots_loaded,
			self._show_error,
		)

	def _on_bots_loaded(self, bots: list[BotDto]) -> None:
		self._bots = bots
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.get_tg_api(),
			self,
			self._on_api_loaded,
			self._show_error,
		)

	def _on_api_loaded(self, credential: TgApiDto | None) -> None:
		"""Ключ приложения прочитан — последним шагом снимок активности."""
		self._api_key_set = credential is not None
		run_in_engine(
			self._worker,
			self._worker.engine.activity.snapshot(),
			self,
			self._on_activity_loaded,
			self._show_error,
		)

	def _on_activity_loaded(self, activity: dict[LaneOwner, OwnerActivityDto]) -> None:
		"""Снимок активности получен — рисуем страницу целиком."""
		self._activity = activity
		self._render()
		self.users_changed.emit(list(self._accounts), list(self._bots))

	def _poll_activity(self) -> None:
		"""Тик таймера: только снимок активности, карточки обновляются на месте."""
		run_in_engine(
			self._worker,
			self._worker.engine.activity.snapshot(),
			self,
			self._apply_activity,
			# опрос фоновый: сбой одного тика не стоит плашки, следующий повторит
			noop,
		)

	def _apply_activity(self, activity: dict[LaneOwner, OwnerActivityDto]) -> None:
		"""Обновляет строки активности у живых карточек."""
		self._activity = activity
		for account_id, card in self._user_cards.items():
			card.set_activity(activity.get(LaneOwner(OwnerKind.USER, account_id)))
		for bot_id, card in self._bot_cards.items():
			card.set_activity(activity.get(LaneOwner(OwnerKind.BOT, bot_id)))

	# --- отрисовка ---------------------------------------------------------------

	def _on_search_changed(self, text: str) -> None:
		"""Поиск фильтрует на клиенте — без обращения к движку."""
		self._query = text
		self._render_sections()

	def _render(self) -> None:
		self._render_summary()
		# подсказка о ключе — когда есть кому входить; без пользователей
		# её несёт пустое состояние
		show_hint = not self._api_key_set and bool(self._accounts)
		self._api_hint.set_note(_NO_API_KEY_HINT if show_hint else "")
		self._render_sections()

	def _render_summary(self) -> None:
		"""Строка сводки: пользователи, боты, приостановленные, без входа."""
		clear_layout(self._summary_box)
		if not self._accounts and not self._bots:
			return
		totals = users_summary(self._accounts, self._bots)
		bar: QWidget = SimpleCardWidget(self)
		layout = QHBoxLayout(bar)
		layout.setContentsMargins(16, 6, 16, 6)
		layout.setSpacing(12)
		layout.addWidget(StrongBodyLabel(str(totals.users), bar))
		layout.addWidget(
			BodyLabel(plural(totals.users, "пользователь", "пользователя", "пользователей"), bar)
		)
		layout.addWidget(VerticalSeparator(bar))
		layout.addWidget(StrongBodyLabel(str(totals.bots), bar))
		layout.addWidget(BodyLabel(plural(totals.bots, "бот", "бота", "ботов"), bar))
		if totals.not_logged_in:
			layout.addWidget(VerticalSeparator(bar))
			layout.addWidget(
				self._summary_badge(bar, InfoBadge.attension, f"{totals.not_logged_in} без входа")
			)
		if totals.paused:
			layout.addWidget(VerticalSeparator(bar))
			layout.addWidget(
				self._summary_badge(bar, InfoBadge.info, f"{totals.paused} приостановлено")
			)
		layout.addStretch()
		self._summary_box.addWidget(bar)

	@staticmethod
	def _summary_badge(parent: QWidget, preset: Callable[..., InfoBadge], text: str) -> InfoBadge:
		"""Плашка сводки штатным пресетом: кегль и высота — по макету."""
		badge = preset(text, parent=parent)
		badge.setFont(font_px(_ACTION_FONT_PX))
		badge.setFixedHeight(_SUMMARY_BADGE_HEIGHT)
		badge.setContentsMargins(8, 0, 8, 0)
		return badge

	def _render_sections(self) -> None:
		"""Разделы «Пользователи» и «Боты» по текущему поиску."""
		clear_layout(self._sections)
		self._user_cards.clear()
		self._bot_cards.clear()
		if not self._accounts and not self._bots:
			self._sections.addWidget(self._empty_state(searched=False))
			return
		accounts = [a for a in self._accounts if matches_user_search(a, self._query)]
		bots = [b for b in self._bots if matches_bot_search(b, self._query)]
		if not accounts and not bots:
			self._sections.addWidget(self._empty_state(searched=True))
			return
		if accounts:
			self._sections.addWidget(
				section_header(self, "Пользователи", len(accounts), icon=FluentIcon.PEOPLE)
			)
			cards: list[QWidget] = []
			for account in accounts:
				card = UserCard(
					account, self._run_user_action, self._delete_user, self._rename_user, self
				)
				card.set_activity(self._activity.get(LaneOwner(OwnerKind.USER, account.id)))
				card.clicked.connect(
					partial(self.open_user.emit, LaneOwner(OwnerKind.USER, account.id))
				)
				self._user_cards[account.id] = card
				cards.append(card)
			self._sections.addWidget(
				FlowGrid(cards, self, min_width=CARD_MIN_WIDTH, spacing=GRID_SPACING)
			)
		if bots:
			self._sections.addWidget(section_header(self, "Боты", len(bots), icon=FluentIcon.ROBOT))
			bot_cards: list[QWidget] = []
			for bot in bots:
				bot_card = BotCard(
					bot, self._run_bot_action, self._delete_bot, self._rename_bot, self
				)
				bot_card.set_activity(self._activity.get(LaneOwner(OwnerKind.BOT, bot.id)))
				bot_card.clicked.connect(
					partial(self.open_user.emit, LaneOwner(OwnerKind.BOT, bot.id))
				)
				self._bot_cards[bot.id] = bot_card
				bot_cards.append(bot_card)
			self._sections.addWidget(
				FlowGrid(bot_cards, self, min_width=CARD_MIN_WIDTH, spacing=GRID_SPACING)
			)

	def _empty_state(self, searched: bool) -> QWidget:
		"""Пустое состояние: никого нет или поиск ничего не нашёл."""
		box = QWidget(self)
		layout = QVBoxLayout(box)
		layout.setContentsMargins(0, 48, 0, 0)
		if searched:
			title = SubtitleLabel("Ничего не найдено", box)
			hint = BodyLabel("Проверьте запрос или добавьте пользователя либо бота.", box)
		else:
			title = SubtitleLabel("Пока нет пользователей и ботов", box)
			hint = BodyLabel(
				"Нажмите «Добавить»: пользователь входит по телефону и коду из Telegram, "
				"бот — по токену от @BotFather."
				+ ("" if self._api_key_set else f"\n{_NO_API_KEY_HINT}"),
				box,
			)
			hint.setWordWrap(True)
		title.setAlignment(Qt.AlignmentFlag.AlignCenter)
		hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title)
		layout.addWidget(hint)
		return box

	# --- действия пользователя (общие с страницей аккаунта — user_actions) ------------

	def _run_user_action(self, action: UserAction, account: TgAccountDto) -> None:
		run_user_action(self._worker, self, action, account, self.reload)

	def _rename_user(self, account: TgAccountDto, label: str) -> None:
		save_user_label(self._worker, self, account, label, self.reload)

	def _delete_user(self, account: TgAccountDto) -> None:
		delete_user(self._worker, self, account, self.reload)

	def _on_add_user(self) -> None:
		"""Новый пользователь: телефон и необязательная пометка.

		Ключ API у пользователя не спрашивается — он один на приложение
		(ADR-0018), задаётся в «Настройки → Общие». Имя и @имя заполнит
		Telegram после входа.
		"""
		dialog = FormDialog(
			"Новый пользователь (userbot-аккаунт)",
			[
				("phone", "Телефон — на него придёт код входа"),
				("label", "Пометка (необязательно, например «рабочий»)"),
			],
			self.window(),
			validator=require_filled("phone", message="Укажите телефон."),
		)
		if not exec_dialog(dialog):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.add_tg_account(
				dialog.value("label"), dialog.value("phone")
			),
			self,
			self._on_user_added,
			self._show_error,
		)

	def _on_user_added(self, account: TgAccountDto) -> None:
		show_success(self, "Пользователь добавлен", f"{account.display} — теперь войдите.")
		self.reload()

	# --- действия бота -----------------------------------------------------------------

	def _run_bot_action(self, action: BotAction, bot: BotDto) -> None:
		run_bot_action(self._worker, self, action, bot, self.reload)

	def _rename_bot(self, bot: BotDto, label: str) -> None:
		save_bot_label(self._worker, self, bot, label, self.reload)

	def _delete_bot(self, bot: BotDto) -> None:
		delete_bot(self._worker, self, bot, self.reload)

	def _on_add_bot(self) -> None:
		dialog = FormDialog(
			"Новый бот",
			[("label", "Название (для себя)"), ("token", "Токен от @BotFather")],
			self.window(),
			validator=require_filled("label", "token", message="Заполните оба поля."),
		)
		if not exec_dialog(dialog):
			return
		show_info(self, "Проверка", "Проверяю токен через Telegram…")
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.add_bot(dialog.value("label"), dialog.value("token")),
			self,
			self._on_bot_added,
			self._show_error,
		)

	def _on_bot_added(self, bot: BotDto) -> None:
		show_success(self, "Бот добавлен", f"@{bot.username}")
		self.reload()
