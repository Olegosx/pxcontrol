"""Тесты правил показа дашборда «Каналы и группы» (без Qt).

Импортируются только чистые функции и перечисления — виджеты
не создаются. Правила: состояние карточки и его приоритет, набор
действий, тексты метрик и подстрочника, число колонок сетки, фильтр
поиска, сортировка таблицы, строка сводки, склонения.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui.pages.common import format_count, plural
from pxcontrol.ui.pages.communities import (
	VIEW_LIST,
	VIEW_TILES,
	CardAction,
	CardState,
	QueueCounts,
	Row,
	TableColumn,
	action_available,
	audience_word,
	card_actions,
	card_state,
	grid_columns,
	matches_search,
	metrics_text,
	sort_rows,
	state_badge_text,
	subtitle_text,
	summary_counts,
	view_from_setting,
)


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
	assert subtitle_text(_community(), _stats()) == "@kinohd · 18 420 подписчиков"
	assert subtitle_text(_community(username=None), _stats()) == (
		"имя не задано · 18 420 подписчиков"
	)
	assert subtitle_text(_community(), None) == "@kinohd"
	assert subtitle_text(_community(), _stats(participants=None)) == "@kinohd"
	group = _community(kind=CommunityKind.GROUP, username="chat")
	assert subtitle_text(group, _stats(participants=861)) == "@chat · 861 участник"


# --- сетка, поиск, вид ------------------------------------------------------------


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
