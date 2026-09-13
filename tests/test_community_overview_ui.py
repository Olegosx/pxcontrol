"""Тесты текстов вкладки «Обзор» (чистые функции, без Qt-виджетов)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.community_overview import CommunityOverviewDto, SeriesSource
from pxcontrol.engine.telegram.types import CommunityKind, DayPoint, UserbotRole
from pxcontrol.ui.pages.common import ACCENT_TEXT, DIM_TEXT, ERROR_TEXT
from pxcontrol.ui.pages.community_overview import (
	axis_dates,
	deleted_caption,
	delta_caption,
	flow_caption,
	growth_subtitle,
	hours_subtitle,
	kind_text,
	linked_text,
	period_caption,
	reference_rows,
	share_caption,
	signed,
	source_note,
	updated_text,
)


def _community(**overrides: object) -> CommunityDto:
	base: dict[str, object] = {
		"id": 1,
		"title": "Чат подписчиков",
		"username": "kinohd_chat",
		"tg_chat_id": "-1001874553201",
		"bot_id": None,
		"bot_label": None,
		"enabled": True,
		"default_account_id": 3,
		"default_account_label": "Олег К.",
		"default_role": UserbotRole.MEMBER,
		"kind": CommunityKind.GROUP,
		"forum": False,
	}
	base.update(overrides)
	return CommunityDto(**base)  # type: ignore[arg-type]


def test_delta_caption_sign_and_color() -> None:
	assert delta_caption(38) == ("+38 за 7 дней", ACCENT_TEXT)
	assert delta_caption(-5) == ("−5 за 7 дней", ERROR_TEXT)
	assert delta_caption(0) == ("0 за 7 дней", DIM_TEXT)
	assert delta_caption(None) == ("нет данных за период", DIM_TEXT)
	assert delta_caption(1200, days=1)[0] == "+1 200 за 1 день"


def test_share_flow_and_deleted_captions() -> None:
	assert share_caption(96, 2104, "участников") == "4,6% участников"
	assert share_caption(None, 2104, "участников") == "нет данных"
	assert share_caption(5, 0, "участников") == "нет данных"
	assert flow_caption(61, 23) == "уходит 1 из 2,7 пришедших"
	assert flow_caption(10, 0) == "никто не ушёл"
	assert flow_caption(0, 4) == "приходов не было"
	assert flow_caption(None, None) == "нет данных за период"
	checked = datetime(2026, 9, 11, 12, tzinfo=UTC)
	assert deleted_caption(47, 2104, checked).startswith("2,2% списка · проход ")
	assert deleted_caption(47, None, checked).startswith("проход ")
	assert deleted_caption(None, 2104, None) == "проход ещё не выполнялся"


def test_signed_and_series_captions() -> None:
	assert signed(61) == "+61" and signed(-23) == "−23" and signed(0) == "0" and signed(None) == "—"
	start = date(2026, 8, 15)
	points = tuple(DayPoint(start + timedelta(days=i), 2066 + i) for i in range(30))
	assert growth_subtitle(points) == "2 066 → 2 095"
	assert growth_subtitle(points[:1]) == ""
	assert axis_dates(points) == [(0, "15.08"), (14, "29.08"), (29, "13.09")]
	assert axis_dates(()) == []
	assert period_caption(points[16:]) == "31.08 — 13.09"


def test_hours_subtitle_names_peak() -> None:
	hours = tuple(10 if h != 21 else 148 for h in range(24))
	assert hours_subtitle(hours, online=True) == "онлайн в среднем за 7 дней · пик 21:00 — 148"
	assert hours_subtitle(hours, online=False).startswith("активность")
	assert hours_subtitle(None, online=True) == ""


def test_reference_rows_and_kind() -> None:
	overview = CommunityOverviewDto(
		community_id=1,
		linked_chat_id="-1009",
		linked_title="Кино в HD — премьеры",
		tg_created_at=datetime(2024, 3, 12, tzinfo=UTC),
		fetched_at=datetime(2026, 9, 14, 11, 2, tzinfo=UTC),
	)
	rows = dict(reference_rows(overview, _community()))
	assert rows["Тип"] == "Супергруппа, не форум"
	assert rows["Адрес"] == "@kinohd_chat"
	assert rows["ID чата"] == "-1001874553201"
	assert rows["Привязана к"] == "каналу «Кино в HD — премьеры»"
	assert rows["Публикатор"] == "Олег К. · участник"
	assert rows["Последний пост"] == "—"
	assert rows["Обновлено"].endswith("userbot раз в 6 ч")
	assert kind_text(_community(kind=CommunityKind.CHANNEL)) == "Канал"
	assert kind_text(_community(forum=True)) == "Супергруппа, форум"
	channel = _community(
		kind=CommunityKind.CHANNEL,
		username=None,
		default_account_label=None,
		bot_id=7,
		bot_label="Мой бот",
	)
	rows = dict(reference_rows(overview, channel))
	assert rows["Адрес"] == "имя не задано"
	assert rows["Публикатор"] == "бот Мой бот"
	assert rows["Привязана к"] == "чату обсуждений «Кино в HD — премьеры»"
	assert linked_text(CommunityOverviewDto(community_id=1, linked_chat_id="-1009"), channel) == (
		"чату обсуждений -1009"
	)
	assert linked_text(CommunityOverviewDto(community_id=1), channel) == "—"
	assert updated_text(CommunityOverviewDto(community_id=1), channel) == "ещё не обновлялось"


def test_source_note_names_the_source() -> None:
	community = _community()
	telegram = source_note(
		CommunityOverviewDto(community_id=1, source=SeriesSource.TELEGRAM), community
	)
	assert "встроенная статистика Telegram" in telegram and "userbot раз в 6 часов" in telegram
	snapshots = source_note(
		CommunityOverviewDto(community_id=1, source=SeriesSource.SNAPSHOTS), community
	)
	assert "оценка по разности" in snapshots
	none = source_note(CommunityOverviewDto(community_id=1), _community(bot_id=7))
	assert "бот раз в 15 минут" in none and "Истории пока нет" in none
