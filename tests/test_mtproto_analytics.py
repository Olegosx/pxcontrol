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


class _Migrating:
	"""Подставной клиент: домашний дата-центр статистику не отдаёт, шлёт в другой.

	Токен графика принимает только одолженный канал того дата-центра —
	как настоящий Telegram (домашний отвечает GRAPH_INVALID_RELOAD).
	"""

	def __init__(self) -> None:
		from telethon.tl.types import DataJSON, StatsGraph

		self.home_calls: list[str] = []
		self.borrowed: list[int] = []
		self.returned = 0
		self.graph = StatsGraph(json=DataJSON('{"columns": [["x", 1], ["y0", 5]]}'))

	async def __call__(self, request: object) -> object:
		from telethon.errors import GraphInvalidReloadError, StatsMigrateError
		from telethon.tl.functions.stats import LoadAsyncGraphRequest

		self.home_calls.append(type(request).__name__)
		if isinstance(request, LoadAsyncGraphRequest):
			raise GraphInvalidReloadError(request)
		raise StatsMigrateError(request, capture=4)

	async def _borrow_exported_sender(self, dc: int) -> SimpleNamespace:
		self.borrowed.append(dc)

		async def send(request: object) -> object:
			from telethon.tl.functions.stats import LoadAsyncGraphRequest
			from telethon.tl.types import StatsGraphAsync

			if isinstance(request, LoadAsyncGraphRequest):
				assert request.token == "t1"
				return self.graph
			return SimpleNamespace(
				period=None,
				followers=_pair(10.0, 9.0),
				growth_graph=StatsGraphAsync(token="t1"),
			)

		return SimpleNamespace(send=send)

	async def _return_exported_sender(self, sender: object) -> None:
		self.returned += 1


async def test_async_graphs_load_through_stats_dc() -> None:
	from pxcontrol.engine.telegram.mtproto import _fetch_stats, _graph

	client = _Migrating()
	stats, dc = await _fetch_stats(client, "entity")
	assert dc == 4 and client.borrowed == [4] and client.returned == 1
	assert stats.followers.current == 10.0
	# догрузка через канал того же дата-центра — ряд читается
	sender = await client._borrow_exported_sender(dc)
	assert [s.points for s in await _graph(sender.send, stats.growth_graph)] == [((1, 5.0),)]
	# через домашний дата-центр токен отклоняется — пустой ряд, не ошибка
	assert await _graph(client, stats.growth_graph) == []
