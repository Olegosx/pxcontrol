"""Сборка границы статистики из ответа Telegram (подставной ответ, без сети)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from pxcontrol.engine.telegram.mtproto import _analytics_from
from pxcontrol.engine.telegram.stats_graph import GraphSeries

_SEP_1 = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
_DAY_MS = 86_400_000


def _pair(current: float, previous: float) -> SimpleNamespace:
	return SimpleNamespace(current=current, previous=previous)


def test_analytics_from_takes_every_field() -> None:
	stats = SimpleNamespace(
		period=SimpleNamespace(
			min_date=datetime(2026, 9, 8, tzinfo=UTC), max_date=datetime(2026, 9, 14, tzinfo=UTC)
		),
		followers=_pair(18420.0, 18382.0),
		views_per_post=_pair(1500.0, 1200.0),
		shares_per_post=_pair(7.4, 5.0),
		reactions_per_post=_pair(12.0, 9.0),
		enabled_notifications=SimpleNamespace(part=38.0, total=100.0),
		recent_posts_interactions=[
			SimpleNamespace(msg_id=41, views=500, forwards=2, reactions=None),
			SimpleNamespace(story_id=7, views=9),  # история — не пост
		],
		top_posters=[SimpleNamespace(user_id=1, messages=12, avg_chars=80)],
		top_admins=[SimpleNamespace(user_id=2, deleted=3, kicked=1, banned=0)],
		top_inviters=[SimpleNamespace(user_id=3, invitations=5)],
		users=[
			SimpleNamespace(id=1, first_name="Олег", last_name="К.", username=None),
			SimpleNamespace(id=2, first_name=None, last_name=None, username="adm"),
		],
	)
	growth = [GraphSeries("Followers", ((_SEP_1, 100.0), (_SEP_1 + _DAY_MS, 102.0)))]
	flow = [GraphSeries("Joined", ((_SEP_1, 5.0),)), GraphSeries("Left", ((_SEP_1, 2.0),))]
	daily_graphs = {
		"interactions": [
			GraphSeries("Views", ((_SEP_1, 50.0),)),
			GraphSeries("Shares", ((_SEP_1, 1.0),)),
		],
		"mute": [],
	}
	share_graphs = {"languages": [GraphSeries("Русский", ((0, 70.0),))], "weekdays": []}
	analytics = _analytics_from(stats, growth, flow, [], daily_graphs, share_graphs)
	assert analytics.period_from == date(2026, 9, 8) and analytics.period_to == date(2026, 9, 14)
	assert analytics.members == (18420, 18382)
	assert analytics.shares_per_post == (7, 5) and analytics.reactions_per_post == (12, 9)
	assert analytics.notifications == (38, 100)
	assert analytics.messages is None  # у канала сообщений нет
	assert analytics.recent_post_views == (500,)
	post = analytics.recent_posts[0]
	assert (post.msg_id, post.views, post.forwards, post.reactions) == (41, 500, 2, None)
	assert analytics.top_posters[0].name == "Олег К."
	assert analytics.top_admins[0].name == "@adm"
	assert analytics.top_inviters[0].name == "3"  # человека в ответе нет — id
	assert [s.name for s in analytics.interactions] == ["Views", "Shares"]
	assert analytics.interactions[0].points[0].value == 50
	assert analytics.mute == ()
	assert analytics.languages[0].name == "Русский" and analytics.languages[0].value == 70
	assert analytics.weekdays == ()
