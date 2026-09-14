"""Дашборд «Пользователи и боты»: сводка, разделы, карточки (ADR-0029).

Раздел первого уровня над «Каналами и группами»: пользователи
(userbot-аккаунты MTProto) и боты Bot API — то, чьими руками приложение
работает в Telegram. Прежде всё это жило категорией «Аккаунты»
в настройках; ключи ИИ остались там своей категорией.

Страница по образцу дашборда сообществ: шапка (поиск, «Добавить»),
строка сводки, разделы «Пользователи» и «Боты» плиткой карточек
(пустой раздел не рисуется). Карточка — информация и действия:
добавление, удаление, приостановка и возобновление, вход, пометка,
диагностика бота. Страницы пользователя пока нет — карточка
не кликается.

Правила показа (состояние, набор действий, подписи, сводка, поиск) —
чистые функции :mod:`user_state`, они тестируются без Qt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
	Action,
	AvatarWidget,
	BodyLabel,
	CaptionLabel,
	CardWidget,
	FluentIcon,
	HorizontalSeparator,
	InfoBadge,
	MessageBox,
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
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	FlowGrid,
	FormDialog,
	WarningLabel,
	clear_layout,
	confirm_delete,
	dim_widget,
	elide_text,
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
)
from pxcontrol.ui.pages.communities import bold_numbers
from pxcontrol.ui.pages.user_state import (
	BOT_ACTION_LABELS,
	USER_ACTION_LABELS,
	BotAction,
	UserAction,
	UserState,
	bot_actions,
	bot_participation_text,
	bot_state,
	bot_subtitle,
	delete_bot_text,
	delete_user_text,
	matches_bot_search,
	matches_user_search,
	participation_text,
	premium_text,
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

#: Цвета подложки аватара-буквы (те же, что у логотипов сообществ).
_AVATAR_COLORS = ("#e17076", "#eda86c", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae")

#: Подсказка, когда вход невозможен без ключа приложения (ADR-0018).
_NO_API_KEY_HINT = (
	"Ключ API Telegram не задан — вход пользователей невозможен. "
	"Получите api_id и api_hash на my.telegram.org и сохраните: Настройки → Общие."
)


def _letter_avatar(parent: QWidget, seed: int, text: str) -> AvatarWidget:
	"""Аватар-буква на подложке: цвет по id, у одной записи всегда один."""
	logo = AvatarWidget(parent)
	logo.setRadius(_CARD_LOGO_SIZE // 2)
	logo.setText(text[:1].upper() or "?")
	color = QColor(_AVATAR_COLORS[seed % len(_AVATAR_COLORS)])
	logo.setBackgroundColor(color, color)
	return logo


def _action_button(parent: QWidget, label: str, primary: bool) -> QPushButton:
	"""Кнопка действия карточки: высота и кегль — по макету."""
	button: QPushButton = PrimaryPushButton(label, parent) if primary else PushButton(label, parent)
	button.setFixedHeight(_ACTION_HEIGHT)
	button.setFont(font_px(_ACTION_FONT_PX))
	return button


class _Card(CardWidget):
	"""Общий каркас карточки: шапка с удалением, разделитель, строка сведений, кнопки.

	Только штатные элементы библиотеки (ADR-0023, п. 5). Ширины у карточки
	нет — её даёт колонка сетки; карточка не кликается: своей страницы
	у пользователя и бота пока нет.
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
	) -> None:
		super().__init__(parent)
		self.setMinimumHeight(_CARD_MIN_HEIGHT)
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
		layout.addWidget(_letter_avatar(box, seed, title))
		column = QVBoxLayout()
		column.setSpacing(2)
		title_label = StrongBodyLabel(box)
		# «занимай, что дадут»: длинное имя не должно распирать карточку
		title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(title_label, title)
		column.addWidget(title_label)
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


class UserCard(_Card):
	"""Карточка пользователя: профиль, участие, Premium, состояние, действия."""

	def __init__(
		self,
		account: TgAccountDto,
		on_action: Callable[[UserAction, TgAccountDto], None],
		on_delete: Callable[[TgAccountDto], None],
		parent: QWidget,
	) -> None:
		super().__init__(
			parent,
			seed=account.id,
			title=account.display,
			subtitle=user_subtitle(account),
			paused=account.paused,
			on_delete=partial(on_delete, account),
		)
		texts = [participation_text(account)]
		premium = premium_text(account)
		if premium is not None:
			texts.append(premium)
		self.add_info(texts, state_badge(self, user_state(account)))
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
		parent: QWidget,
	) -> None:
		super().__init__(
			parent,
			seed=bot.id,
			title=bot.label,
			subtitle=bot_subtitle(bot),
			paused=bot.paused,
			on_delete=partial(on_delete, bot),
		)
		self.add_info([bot_participation_text(bot)], state_badge(self, bot_state(bot)))
		buttons: list[QPushButton] = []
		for action in bot_actions(bot):
			button = _action_button(self, BOT_ACTION_LABELS[action], action is BotAction.RESUME)
			button.clicked.connect(partial(on_action, action, bot))
			buttons.append(button)
		self.add_actions(buttons)


class UsersPage(ScrollArea):
	"""Дашборд пользователей и ботов: шапка, сводка, два раздела карточек."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("users")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._accounts: list[TgAccountDto] = []
		self._bots: list[BotDto] = []
		self._api_key_set = True
		self._query = ""
		self._build()

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
		"""Ключ приложения прочитан — рисуем страницу целиком."""
		self._api_key_set = credential is not None
		self._render()

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
			cards: list[QWidget] = [
				UserCard(account, self._run_user_action, self._delete_user, self)
				for account in accounts
			]
			self._sections.addWidget(
				FlowGrid(cards, self, min_width=CARD_MIN_WIDTH, spacing=GRID_SPACING)
			)
		if bots:
			self._sections.addWidget(section_header(self, "Боты", len(bots), icon=FluentIcon.ROBOT))
			bot_cards: list[QWidget] = [
				BotCard(bot, self._run_bot_action, self._delete_bot, self) for bot in bots
			]
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

	# --- действия пользователя -----------------------------------------------------

	def _run_user_action(self, action: UserAction, account: TgAccountDto) -> None:
		if action is UserAction.LOGIN:
			self._start_login(account)
		elif action is UserAction.PAUSE:
			self._set_user_paused(account, True)
		elif action is UserAction.RESUME:
			self._set_user_paused(account, False)
		elif action is UserAction.LABEL:
			self._rename_user(account)

	def _set_user_paused(self, account: TgAccountDto, paused: bool) -> None:
		"""Пауза или возобновление (ADR-0029); подтверждения нет — обратимо."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.set_tg_account_paused(account.id, paused),
			self,
			partial(self._on_user_paused, paused),
			self._show_error,
		)

	def _on_user_paused(self, paused: bool, account: TgAccountDto) -> None:
		if paused:
			show_info(
				self,
				"Приостановлен",
				f"{account.display}: посты его сообществ ждут возобновления.",
			)
		elif user_state(account) is UserState.OFFLINE:
			show_info(self, "Возобновлён", f"{account.display}: соединение появится при связи.")
		else:
			show_success(self, "Возобновлён", account.display)
		self.reload()

	def _rename_user(self, account: TgAccountDto) -> None:
		"""Переназначение ручной пометки (пусто — снять)."""
		dialog = FormDialog(
			"Пометка пользователя",
			[("label", "Пометка (пусто — имя из Telegram)")],
			self.window(),
			accept_text="Сохранить",
			initial={"label": account.label or ""},
		)
		if not exec_dialog(dialog):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.set_account_label(account.id, dialog.value("label")),
			self,
			lambda *_a: self.reload(),
			self._show_error,
		)

	def _delete_user(self, account: TgAccountDto) -> None:
		"""Удаление: сначала — какие сообщества останутся без публикатора."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			partial(self._confirm_delete_user, account),
			self._show_error,
		)

	def _confirm_delete_user(self, account: TgAccountDto, communities: list[CommunityDto]) -> None:
		bound = [c.title for c in communities if c.default_account_id == account.id]
		if not confirm_delete(self, delete_user_text(account, bound)):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.delete_tg_account(account.id),
			self,
			self.reload,
			self._show_error,
		)

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

	# --- вход: телефон → код → (пароль 2FA) ---------------------------------------

	def _start_login(self, account: TgAccountDto) -> None:
		"""Шаг 1: просим Telegram отправить код на телефон."""
		show_info(self, "Вход", f"Отправляю код на {account.phone or 'номер аккаунта'}…")
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.start_login(account.id),
			self,
			lambda *_a: self._ask_code(account),
			self._show_error,
		)

	def _ask_code(self, account: TgAccountDto) -> None:
		"""Шаг 2: код, присланный Telegram."""
		dialog = FormDialog(
			f"Код отправлен ({account.phone})",
			[("code", "Код из Telegram")],
			self.window(),
			accept_text="Подтвердить",
		)
		if not exec_dialog(dialog):
			self._cancel_login(account)
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.confirm_login_code(account.id, dialog.value("code")),
			self,
			partial(self._after_code, account),
			self._show_error,
		)

	def _after_code(self, account: TgAccountDto, done: bool) -> None:
		"""После кода: вход завершён или нужен пароль 2FA."""
		if done:
			self._on_logged_in(account)
			return
		self._ask_password(account)

	def _ask_password(self, account: TgAccountDto) -> None:
		"""Шаг 3 (если включён): пароль двухфакторной защиты."""
		dialog = FormDialog(
			"Двухфакторная защита",
			[("password", "Пароль 2FA")],
			self.window(),
			accept_text="Войти",
			password_fields=("password",),
		)
		if not exec_dialog(dialog):
			self._cancel_login(account)
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.confirm_login_password(
				account.id, dialog.value("password")
			),
			self,
			lambda *_a: self._on_logged_in(account),
			self._show_error,
		)

	def _on_logged_in(self, account: TgAccountDto) -> None:
		show_success(self, "Вход выполнен", account.display)
		self.reload()

	def _cancel_login(self, account: TgAccountDto) -> None:
		"""Диалог закрыт — прерываем незавершённый вход."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.cancel_login(account.id),
			self,
			noop,
			self._show_error,
		)

	# --- действия бота -----------------------------------------------------------------

	def _run_bot_action(self, action: BotAction, bot: BotDto) -> None:
		if action is BotAction.WHEREABOUTS:
			self._diagnose_bot(bot)
		elif action is BotAction.PAUSE:
			self._set_bot_paused(bot, True)
		elif action is BotAction.RESUME:
			self._set_bot_paused(bot, False)

	def _set_bot_paused(self, bot: BotDto, paused: bool) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.set_bot_paused(bot.id, paused),
			self,
			partial(self._on_bot_paused, paused),
			self._show_error,
		)

	def _on_bot_paused(self, paused: bool, bot: BotDto) -> None:
		if paused:
			show_info(self, "Приостановлен", f"Бот «{bot.label}» больше не используется.")
		else:
			show_success(self, "Возобновлён", f"Бот «{bot.label}»")
		self.reload()

	def _diagnose_bot(self, bot: BotDto) -> None:
		"""Диагностика «где состоит бот» по событиям Telegram за сутки."""
		show_info(self, "Диагностика", "Читаю события бота…")
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.bot_whereabouts(bot.id),
			self,
			partial(self._show_diagnosis, bot),
			self._show_error,
		)

	def _show_diagnosis(self, bot: BotDto, lines: list[str]) -> None:
		text = "\n".join(lines) or (
			"Событий за последние 24 часа нет — Telegram хранит их сутки.\n"
			"Добавьте бота администратором сообщества и проверьте снова."
		)
		box = MessageBox(f"Где состоит @{bot.username or bot.label}", text, self.window())
		box.yesButton.setText("Понятно")
		box.cancelButton.hide()
		exec_dialog(box)

	def _delete_bot(self, bot: BotDto) -> None:
		"""Удаление: сначала — какие сообщества останутся без бота."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			partial(self._confirm_delete_bot, bot),
			self._show_error,
		)

	def _confirm_delete_bot(self, bot: BotDto, communities: list[CommunityDto]) -> None:
		bound = [c.title for c in communities if c.bot_id == bot.id]
		if not confirm_delete(self, delete_bot_text(bot, bound)):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.delete_bot(bot.id),
			self,
			self.reload,
			self._show_error,
		)

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
