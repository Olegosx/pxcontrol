"""Тесты правил показа дашборда «Пользователи и боты» (без Qt, ADR-0029).

Импортируются только чистые функции и перечисления — виджеты
не создаются. Правила: состояние карточки и его приоритет, набор
действий, подписи, участие, сводка, поиск, тексты удаления.
"""

from __future__ import annotations

from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.activity import HISTORY_DAYS, HOURS_DAYS
from pxcontrol.engine.telegram.types import USERBOT_PREMIUM_MAX_FILE_BYTES, limit_gb
from pxcontrol.ui.pages.user_state import (
	PREMIUM_MARK,
	BotAction,
	BotState,
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
	primary_user_action,
	user_actions,
	user_reference_rows,
	user_state,
	user_subtitle,
	users_summary,
)


def _account(
	account_id: int = 1,
	*,
	label: str | None = None,
	username: str | None = "lara",
	first_name: str | None = "Lara",
	last_name: str | None = "Croft",
	phone: str | None = "+7900",
	logged_in: bool = True,
	connected: bool = True,
	paused: bool = False,
	premium: bool = False,
	memberships: int = 0,
	publisher_of: int = 0,
) -> TgAccountDto:
	full_name = " ".join(part for part in (first_name, last_name) if part)
	display = label or full_name or (f"@{username}" if username else None) or phone or "аккаунт"
	return TgAccountDto(
		id=account_id,
		label=label,
		phone=phone,
		logged_in=logged_in,
		premium=premium,
		username=username,
		first_name=first_name,
		last_name=last_name,
		display=display,
		connected=connected,
		paused=paused,
		memberships=memberships,
		publisher_of=publisher_of,
	)


def _bot(bot_id: int = 1, *, paused: bool = False, publisher_of: int = 0) -> BotDto:
	return BotDto(
		bot_id, "Публикатор", "pub_bot", "1234…cdef", paused=paused, publisher_of=publisher_of
	)


# --- состояние --------------------------------------------------------------------


def test_user_state_priority_paused_over_login_over_connection() -> None:
	assert user_state(_account(paused=True, logged_in=False)) is UserState.PAUSED
	assert user_state(_account(logged_in=False, connected=False)) is UserState.NOT_LOGGED_IN
	assert user_state(_account(connected=False)) is UserState.OFFLINE
	assert user_state(_account()) is UserState.ACTIVE


def test_bot_state_only_pause() -> None:
	assert bot_state(_bot()) is BotState.ACTIVE
	assert bot_state(_bot(paused=True)) is BotState.PAUSED


# --- действия -----------------------------------------------------------------------


def test_user_actions_by_state() -> None:
	# пометка — не действие, а правка заголовка на месте
	assert user_actions(_account(paused=True)) == (UserAction.RESUME,)
	assert user_actions(_account(logged_in=False)) == (UserAction.LOGIN, UserAction.PAUSE)
	assert user_actions(_account()) == (UserAction.PAUSE,)
	assert user_actions(_account(connected=False)) == (UserAction.PAUSE,)
	assert primary_user_action(UserAction.LOGIN) and primary_user_action(UserAction.RESUME)
	assert not primary_user_action(UserAction.PAUSE)


def test_bot_actions_by_state() -> None:
	assert bot_actions(_bot()) == (BotAction.WHEREABOUTS, BotAction.PAUSE)
	assert bot_actions(_bot(paused=True)) == (BotAction.RESUME,)


# --- подписи -----------------------------------------------------------------------


def test_user_subtitle_skips_parts_equal_to_display() -> None:
	# заголовок «Lara Croft» — имя не повторяется, @имя и телефон остаются
	assert user_subtitle(_account()) == "@lara · +7900"
	# заголовок — пометка: и имя, и @имя в подстрочнике
	assert user_subtitle(_account(label="рабочий")) == "@lara · Lara Croft · +7900"
	# заголовок — @имя (имени нет): только телефон
	assert user_subtitle(_account(first_name=None, last_name=None)) == "+7900"
	assert user_subtitle(_account(username=None, phone=None)) == "без телефона"


def test_bot_subtitle() -> None:
	assert bot_subtitle(_bot()) == "@pub_bot · 1234…cdef"
	assert bot_subtitle(BotDto(1, "б", None, "••••")) == "@— · ••••"


def test_participation_texts() -> None:
	assert participation_text(_account()) == "не состоит в сообществах"
	assert participation_text(_account(memberships=1)) == "в 1 сообществе"
	assert participation_text(_account(memberships=3, publisher_of=2)) == (
		"в 3 сообществах · публикатор в 2"
	)
	assert bot_participation_text(_bot()) == "не назначен публикатором"
	assert bot_participation_text(_bot(publisher_of=1)) == "публикатор в 1 сообществе"
	assert bot_participation_text(_bot(publisher_of=5)) == "публикатор в 5 сообществах"


def test_premium_marked_by_star_before_username() -> None:
	"""Подписка в подстрочнике — звезда перед @именем, а не слово.

	Словами она названа только на странице аккаунта (в справке) — там
	же и объясняется, что значит звезда на карточке.
	"""
	assert user_subtitle(_account(premium=True)) == f"{PREMIUM_MARK} @lara · +7900"
	assert not user_subtitle(_account()).startswith(PREMIUM_MARK)
	# @имени нет — звезда всё равно первая, перед тем, что осталось
	assert user_subtitle(_account(username=None, premium=True)) == f"{PREMIUM_MARK} +7900"
	premium_row = dict(user_reference_rows(_account(premium=True), None))["Premium"]
	assert premium_row == f"да · файлы до {limit_gb(USERBOT_PREMIUM_MAX_FILE_BYTES)} ГБ"


# --- сводка и поиск ------------------------------------------------------------------


def test_users_summary_counts() -> None:
	accounts = [
		_account(1),
		_account(2, logged_in=False),
		_account(3, paused=True, logged_in=False),  # пауза главнее «без входа»
	]
	bots = [_bot(1), _bot(2, paused=True)]
	totals = users_summary(accounts, bots)
	assert (totals.users, totals.bots, totals.paused, totals.not_logged_in) == (3, 2, 2, 1)


def test_search_matches_name_username_label_and_phone() -> None:
	account = _account(label="Рабочий")
	assert matches_user_search(account, "")
	assert matches_user_search(account, "  рабоч ")
	assert matches_user_search(account, "@LARA")
	assert matches_user_search(account, "croft")
	assert matches_user_search(account, "7900")
	assert not matches_user_search(account, "боб")
	assert matches_bot_search(_bot(), "публик") and matches_bot_search(_bot(), "@pub_")
	assert not matches_bot_search(_bot(), "lara")


# --- тексты удаления ---------------------------------------------------------------


def test_delete_texts_name_consequences() -> None:
	plain = delete_user_text(_account(), [])
	assert plain == "Удалить пользователя «Lara Croft»?"
	bound = delete_user_text(_account(), ["Кино", "Чат"])
	assert "«Кино», «Чат»" in bound and "будут ждать" in bound
	assert delete_bot_text(_bot(), []) == "Удалить бота «Публикатор»?"
	assert "«Кино»" in delete_bot_text(_bot(), ["Кино"])


# --- активность (ADR-0030) --------------------------------------------------------------


def test_activity_text_words_and_optional_parts() -> None:
	from pxcontrol.engine.services.activity import WindowStats
	from pxcontrol.ui.pages.user_state import activity_text, busy_percent, short_duration

	assert activity_text(WindowStats()) == "за 24 ч: операций не было"
	stats = WindowStats(operations=128, busy_s=0.12 * 86400, window_s=86400, errors=0, floods=1)
	assert activity_text(stats) == "за 24 ч: 128 операций · занят 12 % · 1 флуд-лимит"
	stats = WindowStats(operations=1, busy_s=3, window_s=3600, errors=2, floods=5)
	assert activity_text(stats, "за час") == (
		"за час: 1 операция · занят <1 % · 2 ошибки · 5 флуд-лимитов"
	)
	assert busy_percent(WindowStats(busy_s=0, window_s=100)) == "0 %"
	assert busy_percent(WindowStats(busy_s=100, window_s=100)) == "100 %"
	assert short_duration(45) == "45 с"
	assert short_duration(125) == "2 мин"
	assert short_duration(3900) == "1 ч 05 мин"


def test_live_text_and_visibility() -> None:
	from datetime import UTC, datetime

	from pxcontrol.engine.services.activity import LiveDto
	from pxcontrol.engine.telegram.lane import TelegramPriority
	from pxcontrol.ui.pages.user_state import live_shown, live_text

	now = datetime(2026, 9, 15, tzinfo=UTC)
	assert live_text(LiveDto(None, None, 0, 0.0)) == "свободен"
	assert live_text(LiveDto(TelegramPriority.PUBLISH, now, 0, 0.0)) == "сейчас: публикация"
	assert live_text(LiveDto(TelegramPriority.BACKGROUND, now, 3, 0.0)) == (
		"сейчас: фоновое чтение · ждут 3"
	)
	assert live_text(LiveDto(None, None, 2, 0.0)) == "ждут 2"
	# заморозка главнее всего: пока она действует, работы нет
	assert live_text(LiveDto(TelegramPriority.PUBLISH, now, 1, 90.0)) == "заморожен ещё 1 мин"
	assert live_shown(UserState.ACTIVE) and live_shown(UserState.OFFLINE)
	assert live_shown(BotState.ACTIVE)
	assert not live_shown(UserState.PAUSED) and not live_shown(UserState.NOT_LOGGED_IN)
	assert not live_shown(BotState.PAUSED)


# --- страница аккаунта (ADR-0030) --------------------------------------------------------


def test_reference_rows_and_route_key() -> None:
	from datetime import UTC, datetime

	from pxcontrol.engine.services.activity import LiveDto, OwnerActivityDto, WindowStats
	from pxcontrol.engine.telegram.lane import LaneOwner, OwnerKind
	from pxcontrol.ui.pages.user_state import (
		bot_reference_rows,
		user_reference_rows,
		user_route_key,
	)

	assert user_route_key(LaneOwner(OwnerKind.USER, 3)) == "user_3"
	assert user_route_key(LaneOwner(OwnerKind.BOT, 3)) == "bot_3"
	rows = dict(user_reference_rows(_account(memberships=2, publisher_of=1), None))
	assert rows["Состояние"] == "подключён"
	assert rows["@имя"] == "@lara" and rows["Телефон"] == "+7900"
	assert rows["Premium"] == "нет" and rows["Сообщества"] == "в 2 сообществах · публикатор в 1"
	assert rows["Последняя операция"] == "ещё не было"
	last = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
	activity = OwnerActivityDto(
		LaneOwner(OwnerKind.USER, 1),
		LiveDto(None, None, 0, 0.0),
		WindowStats(),
		WindowStats(),
		WindowStats(),
		last,
	)
	assert dict(user_reference_rows(_account(premium=True), activity))["Premium"] == (
		f"да · файлы до {limit_gb(USERBOT_PREMIUM_MAX_FILE_BYTES)} ГБ"
	)
	assert dict(user_reference_rows(_account(), activity))["Последняя операция"] != "ещё не было"
	bot_rows = dict(bot_reference_rows(_bot(paused=True, publisher_of=1), None))
	assert bot_rows["Состояние"] == "приостановлен"
	assert bot_rows["Сообщества"] == "публикатор в 1 сообществе"


def test_kind_rows_captions_and_memberships() -> None:
	from datetime import date

	from pxcontrol.engine.services.activity import WindowStats
	from pxcontrol.engine.services.communities import AccountMembershipDto, CommunityDto
	from pxcontrol.engine.telegram.types import CommunityKind, DayPoint, Share, UserbotRole
	from pxcontrol.ui.pages.user_state import (
		bot_community_caption,
		busy_days_caption,
		hours_caption,
		kind_rows,
		membership_caption,
		window_tile_caption,
	)

	assert kind_rows(()) == []
	assert kind_rows((Share("background", 3), Share("publish", 1))) == [
		("фоновое чтение", 75),
		("публикация", 25),
	]
	assert window_tile_caption(WindowStats(busy_s=36, window_s=3600)) == "занят 1 %"
	assert window_tile_caption(WindowStats(errors=1, floods=2, window_s=10)) == (
		"занят 0 % · 1 ошибка · 2 флуд-лимита"
	)
	assert hours_caption((0,) * 24) == ""
	hours = [0] * 24
	hours[21] = 48
	assert hours_caption(tuple(hours)) == f"за {HOURS_DAYS} дней · пик 21:00 — 48 операций"
	assert busy_days_caption(()) == ""
	points = (DayPoint(date(2026, 9, 14), 3600), DayPoint(date(2026, 9, 15), 720))
	assert busy_days_caption(points) == f"{HISTORY_DAYS} дней · всего 1 ч 12 мин"
	community = CommunityDto(
		id=1,
		title="Чат",
		username=None,
		tg_chat_id="-1001",
		bot_id=7,
		bot_label="бот",
		enabled=False,
		default_account_id=3,
		kind=CommunityKind.GROUP,
	)
	membership = AccountMembershipDto(community, UserbotRole.ADMIN, True)
	assert membership_caption(membership) == (
		"Группа · админ · публикатор по умолчанию · выключено"
	)
	assert bot_community_caption(community) == "Группа · бот-публикатор · выключено"
