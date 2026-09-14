"""Тесты общих правил показа списков и плана карточек (без Qt).

Импортируются только чистые функции: сортировки и фильтры
:mod:`list_view`, нарезка на страницы, итоговая строка со словами
списка и план точечного обновления :func:`plan_cards` с произвольным
ключом — виджеты не создаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pxcontrol.ui.pages.card_list import plan_cards
from pxcontrol.ui.pages.list_view import (
	ListWords,
	filter_by_community,
	filter_by_slot,
	list_communities,
	list_slots,
	paginate,
	sort_by_community,
	sort_nearest,
	step_page,
	summary_text,
)

_BASE = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Post:
	"""Минимальный элемент списка: сообщество, момент и составной ключ."""

	community_id: int
	message_id: int
	community_title: str = "Канал"
	when: datetime | None = _BASE
	text: str = ""

	@property
	def key(self) -> tuple[int, int]:
		return (self.community_id, self.message_id)


def _post(community_id: int, message_id: int, minutes: int | None = 0, **extra: object) -> _Post:
	when = None if minutes is None else _BASE + timedelta(minutes=minutes)
	return _Post(community_id, message_id, when=when, **extra)  # type: ignore[arg-type]


def test_sort_nearest_uses_order_key_for_ties() -> None:
	"""«Сейчас» первым, одинаковое время разводится ключом порядка."""
	items = [_post(1, 5, 10), _post(1, 2, 10), _post(1, 9, None), _post(1, 1, 5)]
	ordered = sort_nearest(items, lambda item: item.message_id)
	assert [item.message_id for item in ordered] == [9, 1, 2, 5]


def test_sort_by_community_separates_namesakes() -> None:
	"""Тёзки по названию не перемешиваются: в ключе есть id сообщества."""
	items = [
		_post(2, 1, 0, community_title="канал"),
		_post(1, 2, 5, community_title="Канал"),
		_post(1, 3, 0, community_title="Канал"),
		_post(3, 4, 0, community_title="Альфа"),
	]
	ordered = sort_by_community(items, lambda item: item.message_id)
	assert [(item.community_id, item.message_id) for item in ordered] == [
		(3, 4),
		(1, 3),
		(1, 2),
		(2, 1),
	]


def test_filters_by_community_and_slot() -> None:
	items = [_post(1, 1, 0), _post(2, 2, 0), _post(1, 3, 60), _post(1, 4, None)]
	assert [i.message_id for i in filter_by_community(items, 1)] == [1, 3, 4]
	assert filter_by_community(items, None) == items
	slot = list_slots(items)[1]  # первый временной слот после «сейчас»
	assert [i.message_id for i in filter_by_slot(items, slot)] == [1, 2]
	assert list_slots(items)[0] == "сейчас"
	assert list_communities(items) == [(1, "Канал"), (2, "Канал")]


def test_summary_text_uses_list_words() -> None:
	"""Слова итога — у каждого списка свои, форма строки общая."""
	words = ListWords(
		empty="Отложенных записей нет.", of_all="отложенных записей", within="отложено"
	)
	assert summary_text(paginate([], 1), 0, words) == "Отложенных записей нет."
	assert summary_text(paginate([], 1), 7, words) == (
		"Ни один из 7 отложенных записей не подходит под фильтр."
	)
	assert summary_text(paginate([1, 2], 1), 2, words) == "Показано 2 из 2 отложенных записей."
	many = list(range(120))
	assert summary_text(paginate(many, 3), 120, words) == (
		"Показаны 101–120 из 120 отложенных записей."
	)
	assert summary_text(paginate(many, 1), 200, words) == (
		"Показаны 1–50 из 120 подходящих (отложено 200)."
	)


def test_step_page_clamps() -> None:
	assert step_page(1, -1, 3) == 1
	assert step_page(3, 1, 3) == 3
	assert step_page(2, 1, 3) == 3


def test_plan_cards_with_composite_key() -> None:
	"""Ключ карточки — не обязательно id: у отложки он «сообщество, запись»."""
	old = [_post(1, 1, text="а"), _post(2, 1, text="б")]  # один message_id в двух сообществах
	known = {item.key: (item.text,) for item in old}
	new = [_post(2, 1, text="б"), _post(1, 1, text="правлено"), _post(1, 2, text="в")]
	plan = plan_cards(new, known, key=lambda item: item.key, signature=lambda item: (item.text,))
	assert plan.added == [(1, 2)]
	assert plan.changed == [(1, 1)]
	assert plan.removed == []
	assert plan.order == [(2, 1), (1, 1), (1, 2)]
