"""Тесты правил показа дашборда «Каналы и группы» (без Qt).

Импортируются только чистые функции и перечисления — виджеты
не создаются. Правила: состояние карточки и его приоритет, набор
действий, тексты метрик и подстрочника, число колонок сетки, фильтр
поиска, сортировка таблицы, строка сводки, склонения.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityAccess, CommunityDto
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.publish_queue import QueueItemDto
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui.pages.common import QueueCounts, bold_numbers, format_count, plural
from pxcontrol.ui.pages.communities import (
	VIEW_LIST,
	VIEW_TILES,
	Row,
	TableColumn,
	grid_columns,
	matches_search,
	metrics_text,
	sort_rows,
	summary_counts,
	view_from_setting,
)
from pxcontrol.ui.pages.community_page import (
	TAB_MEMBERS,
	TAB_OVERVIEW,
	TAB_QUEUE,
	queue_footer_text,
	recheck_summary,
	tab_title,
)
from pxcontrol.ui.pages.community_state import (
	CardAction,
	CardState,
	action_available,
	audience_word,
	bot_member_text,
	card_actions,
	card_state,
	executors_count,
	header_state_text,
	state_badge_text,
	subtitle_text,
)
from pxcontrol.ui.pages.list_view import paginate
from pxcontrol.ui.pages.publish_queue_view import queue_subtitle


def _community(
	community_id: int = 1,
	title: str = "Кино в HD",
	username: str | None = "kinohd",
	*,
	kind: CommunityKind = CommunityKind.CHANNEL,
	enabled: bool = True,
	userbot: bool = True,
	bot: bool = False,
) -> CommunityDto:
	return CommunityDto(
		id=community_id,
		title=title,
		username=username,
		tg_chat_id=f"-100{community_id}",
		bot_id=7 if bot else None,
		bot_label="бот" if bot else None,
		enabled=enabled,
		default_account_id=3 if userbot else None,
		default_account_label="аккаунт" if userbot else None,
		kind=kind,
	)


def _stats(
	community_id: int = 1, participants: int | None = 18420, scheduled: int | None = 3
) -> CommunityStatsDto:
	return CommunityStatsDto(
		community_id=community_id,
		participants=participants,
		online=None,
		scheduled_count=scheduled,
		avatar_path=None,
		fetched_at=datetime(2026, 9, 13, tzinfo=UTC),
	)


# --- склонения и числа ---------------------------------------------------------


def test_plural_follows_russian_rules() -> None:
	forms = ("подписчик", "подписчика", "подписчиков")
	assert [plural(n, *forms) for n in (1, 2, 5, 11, 12, 21, 22, 25, 101, 111)] == [
		"подписчик",
		"подписчика",
		"подписчиков",
		"подписчиков",
		"подписчиков",
		"подписчик",
		"подписчика",
		"подписчиков",
		"подписчик",
		"подписчиков",
	]


def test_format_count_uses_narrow_no_break_space() -> None:
	assert format_count(861) == "861"
	assert format_count(18420) == "18 420"
	assert format_count(1234567) == "1 234 567"


def test_audience_word_by_kind() -> None:
	assert audience_word(CommunityKind.CHANNEL, 3902) == "подписчика"
	assert audience_word(CommunityKind.GROUP, 861) == "участник"
	assert audience_word(CommunityKind.GROUP, 2104) == "участника"


# --- состояние карточки ---------------------------------------------------------


def test_card_state_priority_disabled_over_errors_over_no_publisher() -> None:
	errors = QueueCounts(planned=1, errors=2)
	assert card_state(_community(enabled=False, userbot=False), errors) is CardState.DISABLED
	assert card_state(_community(userbot=False), errors) is CardState.ERRORS
	assert card_state(_community(userbot=False), QueueCounts()) is CardState.NO_PUBLISHER
	assert card_state(_community(), QueueCounts()) is CardState.NORMAL


def test_card_state_bot_counts_as_publisher() -> None:
	assert card_state(_community(userbot=False, bot=True), QueueCounts()) is CardState.NORMAL


def test_state_badge_text_declines_errors() -> None:
	assert state_badge_text(CardState.ERRORS, QueueCounts(errors=1)) == "1 ошибка"
	assert state_badge_text(CardState.ERRORS, QueueCounts(errors=2)) == "2 ошибки"
	assert state_badge_text(CardState.ERRORS, QueueCounts(errors=5)) == "5 ошибок"
	assert state_badge_text(CardState.NO_PUBLISHER, QueueCounts()) == "нет публикатора"
	assert state_badge_text(CardState.DISABLED, QueueCounts()) == "выключено"
	assert state_badge_text(CardState.NORMAL, QueueCounts()) is None


# --- набор действий -------------------------------------------------------------


def test_card_actions_by_state() -> None:
	assert card_actions(_community(), QueueCounts()) == (CardAction.PUBLISH, CardAction.SCHEDULE)
	assert card_actions(_community(), QueueCounts(planned=5)) == (
		CardAction.PUBLISH,
		CardAction.QUEUE,
	)
	# ошибки — тоже непустая очередь: кнопка «Очередь» ведёт к ним
	assert card_actions(_community(), QueueCounts(errors=2)) == (
		CardAction.PUBLISH,
		CardAction.QUEUE,
	)
	assert card_actions(_community(userbot=False), QueueCounts(planned=9)) == (
		CardAction.ASSIGN_PUBLISHER,
	)
	assert card_actions(_community(enabled=False), QueueCounts(planned=9)) == (
		CardAction.ENABLE,
		CardAction.MAINTENANCE,
	)
	assert card_actions(_community(kind=CommunityKind.GROUP), QueueCounts(planned=1)) == (
		CardAction.PUBLISH,
		CardAction.MAINTENANCE,
	)


def test_maintenance_needs_userbot() -> None:
	assert action_available(CardAction.MAINTENANCE, _community(userbot=True))
	assert not action_available(CardAction.MAINTENANCE, _community(userbot=False, bot=True))
	assert action_available(CardAction.PUBLISH, _community(userbot=False, bot=True))


# --- тексты метрик и подстрочника ------------------------------------------------


def test_metrics_text_words_instead_of_brackets() -> None:
	texts = metrics_text(_community(), QueueCounts(planned=5, waiting=2), _stats(scheduled=3))
	assert texts.queue == "5 к отправке · 2 ждут"
	assert texts.scheduled == "3 отложено"
	single = metrics_text(_community(), QueueCounts(planned=1, waiting=1), _stats())
	assert single.queue == "1 к отправке · 1 ждёт"


def test_metrics_text_empty_queue_and_no_cache() -> None:
	texts = metrics_text(_community(), QueueCounts(), None)
	assert texts.queue == "очередь пуста"
	assert texts.scheduled == "нет данных"
	no_scheduled = metrics_text(_community(), QueueCounts(planned=9), _stats(scheduled=None))
	assert no_scheduled.scheduled == "нет данных"


def test_metrics_text_disabled_is_one_phrase() -> None:
	texts = metrics_text(_community(enabled=False), QueueCounts(planned=4), _stats())
	assert texts.queue == "Очередь не разбирается"
	assert texts.scheduled is None


def test_subtitle_text_variants() -> None:
	assert subtitle_text(_community(), 18420) == "@kinohd · 18\u202f420 подписчиков"
	assert subtitle_text(_community(username=None), 18420) == (
		"имя не задано · 18\u202f420 подписчиков"
	)
	assert subtitle_text(_community(), None) == "@kinohd"
	group = _community(kind=CommunityKind.GROUP, username="chat")
	assert subtitle_text(group, 861) == "@chat · 861 участник"


# --- сетка, поиск, вид ------------------------------------------------------------


def test_bold_numbers_marks_numbers_and_escapes_text() -> None:
	# число с узким неразрывным пробелом (format_count) — одно число
	assert bold_numbers(f"{format_count(18420)} подписчиков") == "<b>18\u202f420</b> подписчиков"
	assert bold_numbers("5 к отправке · 2 ждут") == "<b>5</b> к отправке · <b>2</b> ждут"
	# текст экранируется до разметки: скобки из данных не становятся тегами
	assert bold_numbers("<3 к отправке>") == "&lt;<b>3</b> к отправке&gt;"
	assert bold_numbers("очередь пуста") == "очередь пуста"


def test_grid_columns_by_width() -> None:
	assert grid_columns(0) == 1
	assert grid_columns(359) == 1
	assert grid_columns(360) == 1
	assert grid_columns(731) == 1  # две карточки плюс интервал — 732
	assert grid_columns(732) == 2
	assert grid_columns(1104) == 3
	assert grid_columns(1600) == 4


def test_matches_search_by_title_and_username() -> None:
	community = _community(title="Кино в HD — премьеры", username="kinohd_prem")
	assert matches_search(community, "")
	assert matches_search(community, "  ")
	assert matches_search(community, "кино")
	assert matches_search(community, "HD")
	assert matches_search(community, "@KINOHD")
	assert matches_search(community, "prem")
	assert not matches_search(community, "сериал")
	# одна собака — пустой запрос: показывается всё, и без @имени тоже
	assert matches_search(_community(username=None), "@")


def test_view_from_setting_falls_back_to_tiles() -> None:
	assert view_from_setting(VIEW_LIST) == VIEW_LIST
	assert view_from_setting(VIEW_TILES) == VIEW_TILES
	assert view_from_setting("grid") == VIEW_TILES
	assert view_from_setting("") == VIEW_TILES


# --- сводка и сортировка таблицы ---------------------------------------------------


def test_summary_counts() -> None:
	communities = [
		_community(1),
		_community(2, enabled=False),
		_community(3, userbot=False),
		_community(4, userbot=False, bot=True),
	]
	counts = {1: QueueCounts(planned=5, waiting=2, errors=2), 3: QueueCounts(planned=9)}
	totals = summary_counts(communities, counts)
	assert totals.queued == 16
	assert totals.enabled == 3
	assert totals.total == 4
	assert totals.errors == 2
	assert totals.without_publisher == 1


def _rows() -> list[Row]:
	return [
		Row(_community(1, "Кино"), QueueCounts(planned=5, errors=2), _stats(1, 18420, 3)),
		Row(_community(2, "Аниме"), QueueCounts(), _stats(2, 7311, 1)),
		Row(_community(3, "Сериалы", userbot=False), QueueCounts(planned=9), None),
		Row(_community(4, "Док", enabled=False), QueueCounts(planned=2), _stats(4, 3902, 4)),
	]


def test_sort_rows_by_title_ignores_case() -> None:
	ordered = sort_rows(_rows(), TableColumn.TITLE, descending=False)
	assert [row.community.title for row in ordered] == ["Аниме", "Док", "Кино", "Сериалы"]
	reverse = sort_rows(_rows(), TableColumn.TITLE, descending=True)
	assert [row.community.title for row in reverse] == ["Сериалы", "Кино", "Док", "Аниме"]


def test_sort_rows_missing_data_goes_last_when_descending() -> None:
	ordered = sort_rows(_rows(), TableColumn.PARTICIPANTS, descending=True)
	assert [row.community.title for row in ordered] == ["Кино", "Аниме", "Док", "Сериалы"]
	scheduled = sort_rows(_rows(), TableColumn.SCHEDULED, descending=True)
	assert [row.community.title for row in scheduled] == ["Док", "Кино", "Аниме", "Сериалы"]


def test_sort_rows_by_queue_and_state() -> None:
	queue = sort_rows(_rows(), TableColumn.QUEUE, descending=True)
	assert [row.community.title for row in queue] == ["Сериалы", "Кино", "Док", "Аниме"]
	# по состоянию: требующие внимания — первыми (ошибки → нет публикатора → выключено)
	state = sort_rows(_rows(), TableColumn.STATE, descending=False)
	assert [row.community.title for row in state] == ["Кино", "Сериалы", "Док", "Аниме"]


# --- страница сообщества: шапка, вкладки, итоги ----------------------------------------


def test_header_state_text_names_normal_state_by_kind() -> None:
	assert header_state_text(_community(), QueueCounts()) == (CardState.NORMAL, "активен")
	group = _community(kind=CommunityKind.GROUP)
	assert header_state_text(group, QueueCounts()) == (CardState.NORMAL, "активна")
	assert header_state_text(_community(enabled=False), QueueCounts(errors=3)) == (
		CardState.DISABLED,
		"выключено",
	)
	assert header_state_text(_community(), QueueCounts(errors=2)) == (CardState.ERRORS, "2 ошибки")


def test_tab_title_with_and_without_count() -> None:
	assert tab_title(TAB_OVERVIEW) == "Обзор"
	assert tab_title(TAB_QUEUE, 5) == "Очередь 5"
	assert tab_title(TAB_QUEUE, 0) == "Очередь"  # ноль не показывается
	assert tab_title(TAB_MEMBERS, None) == "Участники"


def test_queue_footer_text_single_and_multi_page() -> None:
	assert queue_footer_text(paginate([], 1, 20)) == ""
	items = [_queue_item(i) for i in range(1, 6)]
	assert queue_footer_text(paginate(items, 1, 20)).startswith("В очереди 5 — ближайшие сначала.")
	many = [_queue_item(i) for i in range(1, 46)]
	assert queue_footer_text(paginate(many, 2, 20)).startswith("Показаны 21–40 из 45 — ближайшие")


def _queue_item(item_id: int) -> QueueItemDto:
	return QueueItemDto(
		id=item_id,
		title=f"пост {item_id}",
		community_id=1,
		community_title="Кино",
		when=None,
		status=JobStatus.PENDING,
		progress=0.0,
		error=None,
	)


def test_recheck_summary_distinguishes_unknown_from_lost() -> None:
	community = _community(bot=True)
	ok, text = recheck_summary(CommunityAccess(community, userbot_ok=True, bot_ok=True))
	assert ok and "публикатор — аккаунт" in text and "права на месте" in text
	ok, text = recheck_summary(CommunityAccess(community, userbot_ok=None, bot_ok=None))
	assert not ok and "не удалось проверить" in text and "проверить не удалось" in text
	ok, text = recheck_summary(CommunityAccess(community, userbot_ok=False, bot_ok=False))
	assert not ok and "привязка снята" in text and "права потеряны" in text
	# бота нет — про бота ни слова
	_ok, text = recheck_summary(CommunityAccess(_community(), userbot_ok=True, bot_ok=None))
	assert "бот" not in text


def test_queue_subtitle_without_community_for_community_page() -> None:
	item = QueueItemDto(
		id=1,
		title="пост",
		community_id=1,
		community_title="Кино в HD",
		when=None,
		status=JobStatus.PENDING,
		progress=0.0,
		error=None,
	)
	# состояние несёт светофор в строке действий, подпись его не дублирует
	assert queue_subtitle(item) == "Кино в HD · публикация: сейчас"
	assert queue_subtitle(item, with_community=False) == "публикация: сейчас"


# --- приостановленный публикатор (ADR-0029) ------------------------------------------


def test_card_state_publisher_paused_between_errors_and_no_publisher() -> None:
	"""Пауза публикатора: своя плашка без действий; ошибки главнее, «нет публикатора» — ниже."""
	from dataclasses import replace

	paused = replace(_community(), default_account_paused=True)
	assert card_state(paused, QueueCounts()) is CardState.PUBLISHER_PAUSED
	assert state_badge_text(CardState.PUBLISHER_PAUSED, QueueCounts()) == "публикатор приостановлен"
	assert card_actions(paused, QueueCounts()) == ()
	assert card_state(paused, QueueCounts(errors=1)) is CardState.ERRORS
	# с активным ботом действующий публикатор есть — состояние штатное
	with_bot = replace(_community(bot=True), default_account_paused=True)
	assert card_state(with_bot, QueueCounts()) is CardState.NORMAL
	# оба на паузе — тоже «приостановлен», а не «нет публикатора»
	both = replace(_community(bot=True), default_account_paused=True, bot_paused=True)
	assert card_state(both, QueueCounts()) is CardState.PUBLISHER_PAUSED
	rows = [
		Row(paused, QueueCounts(), None),
		Row(_community(2, "Без", userbot=False), QueueCounts(), None),
		Row(_community(3, "Норма"), QueueCounts(), None),
	]
	ordered = sort_rows(rows, TableColumn.STATE, descending=False)
	assert [row.community.id for row in ordered] == [2, 1, 3]


# --- вкладка «Участники»: исполнители сообщества ----------------------------------


def test_executors_count_adds_bot_to_pool() -> None:
	"""Число на вкладке считает и пул аккаунтов, и бота: список-то общий."""
	from dataclasses import replace

	pool = replace(_community(userbot=True, bot=False), members_count=2)
	assert executors_count(pool) == 2
	assert executors_count(replace(pool, bot_id=7, bot_label="бот")) == 3
	# сообщество без исполнителей вовсе
	assert executors_count(replace(_community(userbot=False), members_count=0)) == 0


def test_bot_member_text_names_assigned_bot() -> None:
	"""Строка раздела «Боты»: назначенный — по названию, иначе честное «не назначен»."""
	assert bot_member_text(_community(bot=True)) == "бот — публикатор"
	assert bot_member_text(_community(bot=False)) == "Бот не назначен"
