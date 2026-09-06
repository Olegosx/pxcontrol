"""Тесты правила показа полного просмотра очереди (ADR-0016, без Qt).

Импортируется только чистая функция ``apply_view`` и перечисления —
виджеты диалога не создаются.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.ui.pages.publish_queue_view import QueueFilter, QueueSort, apply_view


def _item(
	item_id: int,
	community: str = "Канал",
	community_id: int = 1,
	when_minutes: int | None = 60,
	status: QueueItemStatus = QueueItemStatus.WAITING,
) -> QueueItemDto:
	when = None if when_minutes is None else datetime.now(UTC) + timedelta(minutes=when_minutes)
	return QueueItemDto(
		id=item_id,
		title=f"пост {item_id}",
		community_id=community_id,
		community_title=community,
		when=when,
		status=status,
		progress=0.0,
		error="сбой" if status is QueueItemStatus.ERROR else None,
	)


def test_sort_nearest_puts_now_first() -> None:
	"""«Ближайшие сначала»: посты «сейчас» — раньше любых дат, потом по дате."""
	items = [
		_item(1, when_minutes=120),
		_item(2, when_minutes=None, status=QueueItemStatus.PENDING),
		_item(3, when_minutes=30),
	]
	shown = apply_view(items, QueueSort.NEAREST, QueueFilter.ALL, None)
	assert [item.id for item in shown] == [2, 3, 1]


def test_sort_enqueued_keeps_id_order() -> None:
	"""«Порядок постановки»: по идентификатору, независимо от дат."""
	items = [_item(2, when_minutes=30), _item(1, when_minutes=999)]
	shown = apply_view(items, QueueSort.ENQUEUED, QueueFilter.ALL, None)
	assert [item.id for item in shown] == [1, 2]


def test_sort_by_community_then_date() -> None:
	"""«По каналам»: каналы по алфавиту без регистра, внутри — по дате."""
	items = [
		_item(1, community="Яблоко", when_minutes=30),
		_item(2, community="арбуз", when_minutes=90),
		_item(3, community="арбуз", when_minutes=30),
	]
	shown = apply_view(items, QueueSort.COMMUNITY, QueueFilter.ALL, None)
	assert [item.id for item in shown] == [3, 2, 1]


def test_status_and_community_filters() -> None:
	"""Фильтры: по статусу («к отправке» включает отправляющийся) и каналу."""
	items = [
		_item(1, community="А", community_id=1, status=QueueItemStatus.WAITING),
		_item(2, community="А", community_id=1, status=QueueItemStatus.PENDING),
		_item(3, community="Б", community_id=2, status=QueueItemStatus.SENDING),
		_item(4, community="Б", community_id=2, status=QueueItemStatus.ERROR),
	]
	sendable = apply_view(items, QueueSort.ENQUEUED, QueueFilter.SENDABLE, None)
	assert [item.id for item in sendable] == [2, 3]
	waiting = apply_view(items, QueueSort.ENQUEUED, QueueFilter.WAITING, None)
	assert [item.id for item in waiting] == [1]
	errors = apply_view(items, QueueSort.ENQUEUED, QueueFilter.ERRORS, None)
	assert [item.id for item in errors] == [4]
	community_b = apply_view(items, QueueSort.ENQUEUED, QueueFilter.ALL, 2)
	assert [item.id for item in community_b] == [3, 4]
	both = apply_view(items, QueueSort.ENQUEUED, QueueFilter.ERRORS, 1)
	assert both == []
