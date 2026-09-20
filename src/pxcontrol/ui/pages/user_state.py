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
from pxcontrol.engine.services.activity import (
	HISTORY_DAYS,
	HOURS_DAYS,
	LiveDto,
	OwnerActivityDto,
	WindowStats,
)
from pxcontrol.engine.services.communities import AccountMembershipDto
from pxcontrol.engine.telegram.lane import WorkKind
from pxcontrol.engine.telegram.types import (
	USERBOT_PREMIUM_MAX_FILE_BYTES,
	DayPoint,
	ExecutorRef,
	Share,
	limit_gb,
)
from pxcontrol.ui.pages.common import (
	community_kind_caption,
	format_local,
	plural,
	status_caption,
)


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
}
BOT_ACTION_LABELS: dict[BotAction, str] = {
	BotAction.WHEREABOUTS: "Где состоит?",
	BotAction.PAUSE: "Приостановить",
	BotAction.RESUME: "Возобновить",
}


def user_actions(account: TgAccountDto) -> tuple[UserAction, ...]:
	"""Набор действий карточки пользователя по её состоянию.

	Приостановленному предлагается только возобновление: вход на паузе
	ничего не даёт — аккаунт всё равно не подключится (ADR-0029).
	Не вошедшему — вход первым: это главное действие. Пометка —
	не действие, а правка заголовка на месте (карандаш в шапке).
	"""
	state = user_state(account)
	if state is UserState.PAUSED:
		return (UserAction.RESUME,)
	if state is UserState.NOT_LOGGED_IN:
		return (UserAction.LOGIN, UserAction.PAUSE)
	return (UserAction.PAUSE,)


def bot_actions(bot: BotDto) -> tuple[BotAction, ...]:
	"""Набор действий карточки бота: диагностика нужна только активному."""
	if bot.paused:
		return (BotAction.RESUME,)
	return (BotAction.WHEREABOUTS, BotAction.PAUSE)


def primary_user_action(action: UserAction) -> bool:
	"""Главное действие карточки — акцентная кнопка: вход и возобновление."""
	return action in (UserAction.LOGIN, UserAction.RESUME)


#: Пометка подписки Premium в подстрочнике — звезда перед @именем.
#: Знаком, а не словом: подписка — постоянное свойство аккаунта, и в
#: строке из трёх частей слово «Premium» весило бы столько же, сколько
#: имя и телефон. Что она значит, объясняет страница аккаунта — там
#: подписка названа словами вместе с пределом файла.
PREMIUM_MARK = "★"


def user_subtitle(account: TgAccountDto) -> str:
	"""Подстрочник карточки: «★ @имя · Имя Фамилия · телефон».

	Части, совпадающие с заголовком (отображаемым именем), не повторяются
	строкой ниже; телефон есть всегда — он обязателен при создании.
	Звезда в начале — подписка Premium (:data:`PREMIUM_MARK`); без неё
	строка начинается с @имени.
	"""
	full_name = " ".join(part for part in (account.first_name, account.last_name) if part)
	parts: list[str] = []
	if account.username and f"@{account.username}" != account.display:
		parts.append(f"@{account.username}")
	if full_name and full_name != account.display:
		parts.append(full_name)
	parts.append(account.phone or "без телефона")
	subtitle = " · ".join(parts)
	return f"{PREMIUM_MARK} {subtitle}" if account.premium else subtitle


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


# --- активность (ADR-0030) ------------------------------------------------------------

#: Вид работы словом — то, чем дорожка занята (ADR-0030, ADR-0036).
KIND_WORDS: dict[WorkKind, str] = {
	WorkKind.PUBLISH: "публикация",
	WorkKind.INTERACTIVE: "проверка",
	WorkKind.MAINTENANCE: "обслуживание",
	WorkKind.BACKGROUND: "фоновое чтение",
}


def short_duration(seconds: float) -> str:
	"""«45 с», «2 мин», «1 ч 05 мин» — для остатка заморозки и длительностей."""
	total = max(0, int(round(seconds)))
	if total < 60:
		return f"{total} с"
	minutes, _sec = divmod(total, 60)
	if minutes < 60:
		return f"{minutes} мин"
	hours, minutes = divmod(minutes, 60)
	return f"{hours} ч {minutes:02d} мин"


def busy_percent(stats: WindowStats) -> str:
	"""Доля занятости окна процентами: «12 %», «<1 %», «0 %»."""
	share = stats.busy_share * 100
	if 0 < share < 1:
		return "<1 %"
	return f"{round(share)} %"


def activity_text(stats: WindowStats, window_label: str = "за 24 ч") -> str:
	"""Строка активности карточки: «за 24 ч: 128 операций · занят 12 % · 1 флуд-лимит».

	Без операций и занятости — «операций не было»; ошибки и флуд-лимиты
	добавляются только при ненулевом числе.
	"""
	if stats.operations == 0 and stats.busy_s <= 0:
		return f"{window_label}: операций не было"
	count = stats.operations
	parts = [
		f"{window_label}: {count} {plural(count, 'операция', 'операции', 'операций')}",
		f"занят {busy_percent(stats)}",
	]
	if stats.errors:
		parts.append(f"{stats.errors} {plural(stats.errors, 'ошибка', 'ошибки', 'ошибок')}")
	if stats.floods:
		parts.append(
			f"{stats.floods} {plural(stats.floods, 'флуд-лимит', 'флуд-лимита', 'флуд-лимитов')}"
		)
	return " · ".join(parts)


def live_text(live: LiveDto) -> str:
	"""Живая пометка: «заморожен ещё 2 мин», «сейчас: публикация · ждут 3», «свободен»."""
	if live.frozen_for_s > 0:
		return f"заморожен ещё {short_duration(live.frozen_for_s)}"
	if live.busy_kind is not None:
		text = f"сейчас: {KIND_WORDS.get(live.busy_kind, str(live.busy_kind))}"
		if live.waiting:
			text += f" · ждут {live.waiting}"
		return text
	if live.waiting:
		return f"ждут {live.waiting}"
	return "свободен"


def live_shown(state: UserState | BotState) -> bool:
	"""Показывать ли живую пометку: только тем, кто вообще может работать.

	Приостановленному и не вошедшему «свободен» ничего не сказало бы —
	у них операций не бывает по определению.
	"""
	return state in (UserState.ACTIVE, UserState.OFFLINE, BotState.ACTIVE)


# --- страница аккаунта (ADR-0030) --------------------------------------------------------


def user_route_key(owner: ExecutorRef) -> str:
	"""Ключ маршрута страницы аккаунта в навигации (objectName)."""
	return f"{owner.kind}_{owner.id}"


def kind_rows(kinds: tuple[Share, ...]) -> list[tuple[str, int]]:
	"""Строки «вид операции — %» по убыванию; имена видов — по-русски."""
	total = sum(share.value for share in kinds)
	if total <= 0:
		return []
	words = {kind.value: word for kind, word in KIND_WORDS.items()}
	ordered = sorted(kinds, key=lambda s: s.value, reverse=True)
	return [
		(words.get(share.name, share.name), round(share.value * 100 / total)) for share in ordered
	]


def user_reference_rows(
	account: TgAccountDto, activity: OwnerActivityDto | None
) -> list[tuple[str, str]]:
	"""Справка страницы пользователя: «подпись — значение» в порядке показа."""
	last = activity.last_operation_at if activity is not None else None
	return [
		("Состояние", USER_STATE_TEXT[user_state(account)]),
		("@имя", f"@{account.username}" if account.username else "не задано"),
		("Телефон", account.phone or "не указан"),
		(
			"Premium",
			f"да · файлы до {limit_gb(USERBOT_PREMIUM_MAX_FILE_BYTES)} ГБ"
			if account.premium
			else "нет",
		),
		("Сообщества", participation_text(account)),
		("Последняя операция", format_local(last) if last is not None else "ещё не было"),
	]


def bot_reference_rows(bot: BotDto, activity: OwnerActivityDto | None) -> list[tuple[str, str]]:
	"""Справка страницы бота."""
	last = activity.last_operation_at if activity is not None else None
	return [
		("Состояние", BOT_STATE_TEXT[bot_state(bot)]),
		("@имя", f"@{bot.username}" if bot.username else "не задано"),
		("Токен", bot.token_masked),
		("Сообщества", bot_participation_text(bot)),
		("Последняя операция", format_local(last) if last is not None else "ещё не было"),
	]


def window_tile_caption(stats: WindowStats) -> str:
	"""Подпись плитки окна: «занят 12 % · 1 ошибка · 2 флуд-лимита»."""
	parts = [f"занят {busy_percent(stats)}"]
	if stats.errors:
		parts.append(f"{stats.errors} {plural(stats.errors, 'ошибка', 'ошибки', 'ошибок')}")
	if stats.floods:
		parts.append(
			f"{stats.floods} {plural(stats.floods, 'флуд-лимит', 'флуд-лимита', 'флуд-лимитов')}"
		)
	return " · ".join(parts)


def hours_caption(hours: tuple[int, ...]) -> str:
	"""«за N дней · пик 21:00 — 48 операций»; без операций — пусто.

	Горизонт — из движка (:data:`HOURS_DAYS`): подпись обязана называть
	то окно, по которому график построен.
	"""
	if not hours or sum(hours) == 0:
		return ""
	peak = max(range(24), key=lambda h: hours[h])
	count = hours[peak]
	word = plural(count, "операция", "операции", "операций")
	return f"за {HOURS_DAYS} дней · пик {peak:02d}:00 — {count} {word}"


def busy_days_caption(points: tuple[DayPoint, ...]) -> str:
	"""«N дней · всего 3 ч 12 мин»; без занятости — пусто.

	Горизонт — из движка (:data:`HISTORY_DAYS`).
	"""
	total = sum(point.value for point in points)
	if total <= 0:
		return ""
	return f"{HISTORY_DAYS} дней · всего {short_duration(total)}"


def membership_caption(membership: AccountMembershipDto) -> str:
	"""Подстрочник строки сообщества на странице пользователя."""
	community = membership.community
	parts = [community_kind_caption(community), status_caption(membership.status)]
	if membership.is_default:
		parts.append("публикатор по умолчанию")
	if not community.enabled:
		parts.append("выключено")
	return " · ".join(parts)


#: Подсказка в пустом поле пометки: пустая пометка значит «имя из Telegram».
USER_LABEL_PLACEHOLDER = "имя из Telegram"
#: Подсказка в поле названия бота: пустым оно быть не может.
BOT_LABEL_PLACEHOLDER = "название бота"
