"""Состояние пользователя и бота на экране: плашка, действия, подписи (ADR-0029).

Правила показа дашборда «Пользователи и боты» — по образцу
:mod:`community_state`: одно состояние на карточку по приоритету,
один набор действий, тексты подписей и сводки. Чистые функции без Qt —
тестируются как обычный код; единственный виджет здесь — плашка,
штатный ``InfoBadge`` пресетом уровня.

Термины: «пользователь» — userbot-аккаунт MTProto (глоссарий), «бот» —
бот Bot API. У пользователя есть соединение, поэтому состояний четыре;
у бота соединения нет — только пауза.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBadge, InfoLevel

from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.telegram.types import USERBOT_PREMIUM_MAX_FILE_BYTES, limit_gb
from pxcontrol.ui.pages.common import plural


class UserState(StrEnum):
	"""Состояние пользователя — плашка на карточке.

	Ровно одно; при совпадении причин действует приоритет
	:func:`user_state`: пауза → нет входа → нет связи → подключён.
	"""

	ACTIVE = "active"  # вошёл, соединение живое
	OFFLINE = "offline"  # вошёл, но соединения нет: сеть, ключ API
	NOT_LOGGED_IN = "not_logged_in"  # сессии нет — нужен вход
	PAUSED = "paused"  # приостановлен человеком (ADR-0029)


class BotState(StrEnum):
	"""Состояние бота: соединения у него нет, различима только пауза."""

	ACTIVE = "active"
	PAUSED = "paused"


def user_state(account: TgAccountDto) -> UserState:
	"""Состояние по приоритету «пауза → нет входа → нет связи → подключён».

	Пауза главнее прочего: пока аккаунт приостановлен, ни вход,
	ни связь ничего не меняют; вход главнее связи — без сессии
	соединения не бывает.
	"""
	if account.paused:
		return UserState.PAUSED
	if not account.logged_in:
		return UserState.NOT_LOGGED_IN
	if not account.connected:
		return UserState.OFFLINE
	return UserState.ACTIVE


def bot_state(bot: BotDto) -> BotState:
	"""Состояние бота: приостановлен или активен."""
	return BotState.PAUSED if bot.paused else BotState.ACTIVE


#: Тексты плашек — одно слово на состояние.
USER_STATE_TEXT: dict[UserState, str] = {
	UserState.ACTIVE: "подключён",
	UserState.OFFLINE: "нет связи",
	UserState.NOT_LOGGED_IN: "вход не выполнен",
	UserState.PAUSED: "приостановлен",
}
BOT_STATE_TEXT: dict[BotState, str] = {
	BotState.ACTIVE: "активен",
	BotState.PAUSED: "приостановлен",
}

#: Уровень плашки по состоянию: подключён — зелёная, нет связи —
#: предупреждение, нет входа — акцент (действие нужно), пауза — серая.
_BADGE_LEVELS: dict[UserState | BotState, InfoLevel] = {
	UserState.ACTIVE: InfoLevel.SUCCESS,
	UserState.OFFLINE: InfoLevel.WARNING,
	UserState.NOT_LOGGED_IN: InfoLevel.ATTENTION,
	UserState.PAUSED: InfoLevel.INFOAMTION,
	BotState.ACTIVE: InfoLevel.SUCCESS,
	BotState.PAUSED: InfoLevel.INFOAMTION,
}


def state_badge(parent: QWidget, state: UserState | BotState) -> InfoBadge:
	"""Плашка состояния — штатный ``InfoBadge`` пресетом уровня (ADR-0023, п. 5)."""
	text = USER_STATE_TEXT[state] if isinstance(state, UserState) else BOT_STATE_TEXT[state]
	badge = InfoBadge(text, parent, _BADGE_LEVELS[state])
	badge.adjustSize()
	return badge


class UserAction(StrEnum):
	"""Действие с карточки пользователя."""

	LOGIN = "login"  # пошаговый вход: код и 2FA
	PAUSE = "pause"  # приостановить (ADR-0029)
	RESUME = "resume"  # возобновить
	LABEL = "label"  # своя пометка вместо имени из Telegram


class BotAction(StrEnum):
	"""Действие с карточки бота."""

	WHEREABOUTS = "whereabouts"  # диагностика «где состоит» по событиям Telegram
	PAUSE = "pause"
	RESUME = "resume"


#: Подписи кнопок действий.
USER_ACTION_LABELS: dict[UserAction, str] = {
	UserAction.LOGIN: "Войти",
	UserAction.PAUSE: "Приостановить",
	UserAction.RESUME: "Возобновить",
	UserAction.LABEL: "Пометка…",
}
BOT_ACTION_LABELS: dict[BotAction, str] = {
	BotAction.WHEREABOUTS: "Где состоит?",
	BotAction.PAUSE: "Приостановить",
	BotAction.RESUME: "Возобновить",
}


def user_actions(account: TgAccountDto) -> tuple[UserAction, ...]:
	"""Набор действий карточки пользователя по её состоянию.

	Приостановленному предлагается только возобновление (и пометка):
	вход на паузе ничего не даёт — аккаунт всё равно не подключится
	(ADR-0029). Не вошедшему — вход первым: это главное действие.
	"""
	state = user_state(account)
	if state is UserState.PAUSED:
		return (UserAction.RESUME, UserAction.LABEL)
	if state is UserState.NOT_LOGGED_IN:
		return (UserAction.LOGIN, UserAction.PAUSE, UserAction.LABEL)
	return (UserAction.PAUSE, UserAction.LABEL)


def bot_actions(bot: BotDto) -> tuple[BotAction, ...]:
	"""Набор действий карточки бота: диагностика нужна только активному."""
	if bot.paused:
		return (BotAction.RESUME,)
	return (BotAction.WHEREABOUTS, BotAction.PAUSE)


def primary_user_action(action: UserAction) -> bool:
	"""Главное действие карточки — акцентная кнопка: вход и возобновление."""
	return action in (UserAction.LOGIN, UserAction.RESUME)


def user_subtitle(account: TgAccountDto) -> str:
	"""Подстрочник карточки: «@имя · Имя Фамилия · телефон».

	Части, совпадающие с заголовком (отображаемым именем), не повторяются
	строкой ниже; телефон есть всегда — он обязателен при создании.
	"""
	full_name = " ".join(part for part in (account.first_name, account.last_name) if part)
	parts: list[str] = []
	if account.username and f"@{account.username}" != account.display:
		parts.append(f"@{account.username}")
	if full_name and full_name != account.display:
		parts.append(full_name)
	parts.append(account.phone or "без телефона")
	return " · ".join(parts)


def bot_subtitle(bot: BotDto) -> str:
	"""Подстрочник карточки бота: «@имя · токен маской»."""
	return f"@{bot.username or '—'} · {bot.token_masked}"


def participation_text(account: TgAccountDto) -> str:
	"""Строка участия: «в 3 сообществах · публикатор в 2» (ADR-0022)."""
	if account.memberships == 0:
		return "не состоит в сообществах"
	count = account.memberships
	text = f"в {count} {plural(count, 'сообществе', 'сообществах', 'сообществах')}"
	if account.publisher_of:
		text += f" · публикатор в {account.publisher_of}"
	return text


def bot_participation_text(bot: BotDto) -> str:
	"""Строка назначений бота: «публикатор в 2 сообществах»."""
	if bot.publisher_of == 0:
		return "не назначен публикатором"
	count = bot.publisher_of
	return f"публикатор в {count} {plural(count, 'сообществе', 'сообществах', 'сообществах')}"


def premium_text(account: TgAccountDto) -> str | None:
	"""Пометка Premium с лимитом файла; None — подписки нет (или не подключён)."""
	if not account.premium:
		return None
	return f"Premium · файлы до {limit_gb(USERBOT_PREMIUM_MAX_FILE_BYTES)} ГБ"


@dataclass(frozen=True)
class UsersSummary:
	"""Числа строки сводки над разделами.

	Attributes:
		users: пользователей всего.
		bots: ботов всего.
		paused: приостановленных (пользователи и боты вместе).
		not_logged_in: пользователей без входа (пауза не в счёт).
	"""

	users: int
	bots: int
	paused: int
	not_logged_in: int


def users_summary(accounts: list[TgAccountDto], bots: list[BotDto]) -> UsersSummary:
	"""Считает строку сводки (фильтр поиска на неё не влияет)."""
	return UsersSummary(
		users=len(accounts),
		bots=len(bots),
		paused=sum(1 for a in accounts if a.paused) + sum(1 for b in bots if b.paused),
		not_logged_in=sum(1 for a in accounts if user_state(a) is UserState.NOT_LOGGED_IN),
	)


def _needle(query: str) -> str:
	"""Нормализованный запрос поиска: без краёв, регистра и собаки."""
	return query.strip().casefold().lstrip("@")


def matches_user_search(account: TgAccountDto, query: str) -> bool:
	"""Проходит ли пользователь поиск: имя, @имя, пометка, телефон."""
	needle = _needle(query)
	if not needle:
		return True
	haystack = (
		account.display,
		account.username or "",
		account.first_name or "",
		account.last_name or "",
		account.phone or "",
	)
	return any(needle in part.casefold() for part in haystack)


def matches_bot_search(bot: BotDto, query: str) -> bool:
	"""Проходит ли бот поиск: название и @имя."""
	needle = _needle(query)
	if not needle:
		return True
	return needle in bot.label.casefold() or needle in (bot.username or "").casefold()


def delete_user_text(account: TgAccountDto, bound_titles: list[str]) -> str:
	"""Текст подтверждения удаления пользователя с последствиями (ADR-0019/0016).

	``bound_titles`` — сообщества, где он публикатор по умолчанию:
	они останутся без userbot-публикатора, их посты в очереди отправки
	будут ждать нового.
	"""
	text = f"Удалить пользователя «{account.display}»?"
	if bound_titles:
		names = ", ".join(f"«{title}»" for title in bound_titles)
		text += (
			f"\n\nБез userbot-публикатора останутся сообщества: {names} — "
			"их посты в очереди отправки будут ждать нового публикатора, "
			"отложенные и большие файлы станут недоступны."
		)
	return text


def delete_bot_text(bot: BotDto, bound_titles: list[str]) -> str:
	"""Текст подтверждения удаления бота с перечнем сообществ без бота."""
	text = f"Удалить бота «{bot.label}»?"
	if bound_titles:
		names = ", ".join(f"«{title}»" for title in bound_titles)
		text += f"\n\nБез бота останутся сообщества: {names}."
	return text
