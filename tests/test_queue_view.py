"""Тесты правила показа полного просмотра очереди (ADR-0016, без Qt).

Импортируются только чистые функции (``apply_view``, ``paginate``,
``summary_text``) и перечисления — виджеты диалога не создаются.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.ui.pages.common import queue_signature
from pxcontrol.ui.pages.publish_queue_view import (
	QueueFilter,
	QueueSort,
	apply_view,
	paginate,
	summary_text,
)


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


# --- нарезка на страницы ---------------------------------------------------


def test_paginate_slices_requested_page() -> None:
	"""Страница отдаёт свой срез и номера элементов в общем счёте."""
	items = [_item(i) for i in range(1, 13)]
	view = paginate(items, page=2, per_page=5)
	assert [item.id for item in view.items] == [6, 7, 8, 9, 10]
	assert (view.page, view.pages, view.total) == (2, 3, 12)
	assert (view.first, view.last) == (6, 10)


def test_paginate_last_page_may_be_short() -> None:
	"""Последняя страница короче остальных — счёт номеров это учитывает."""
	view = paginate([_item(i) for i in range(1, 13)], page=3, per_page=5)
	assert [item.id for item in view.items] == [11, 12]
	assert (view.first, view.last) == (11, 12)


def test_paginate_clamps_page_out_of_range() -> None:
	"""Номер за границами зажимается: очередь живая, страница исчезает."""
	items = [_item(i) for i in range(1, 8)]
	assert paginate(items, page=99, per_page=5).page == 2  # ушла под пользователем
	assert paginate(items, page=0, per_page=5).page == 1
	assert paginate(items, page=-3, per_page=5).page == 1


def test_paginate_empty_list_is_single_empty_page() -> None:
	"""Пустой список — одна страница без элементов и без номеров."""
	view = paginate([], page=3, per_page=5)
	assert view.items == []
	assert (view.page, view.pages, view.total) == (1, 1, 0)
	assert (view.first, view.last) == (0, 0)


def test_paginate_per_page_never_below_one() -> None:
	"""Нулевой размер страницы не делит на ноль, а берётся за единицу."""
	view = paginate([_item(1), _item(2)], page=2, per_page=0)
	assert [item.id for item in view.items] == [2]
	assert view.pages == 2


# --- итоговая строка -------------------------------------------------------


def test_summary_text_empty_queue() -> None:
	"""Пустая очередь описывается собой, а не нулями."""
	assert summary_text(paginate([], 1), 0) == "Очередь пуста."


def test_summary_text_filter_hides_everything() -> None:
	"""Фильтр отсеял всё: видно, что элементы в очереди есть."""
	text = summary_text(paginate([], 1), 7)
	assert text == "Ни один из 7 элементов очереди не подходит под фильтр."


def test_summary_text_single_page_has_no_range() -> None:
	"""Одна страница: диапазон номеров не показывается."""
	items = [_item(i) for i in range(1, 5)]
	assert summary_text(paginate(items, 1), 4) == "Показано 4 из 4 элементов очереди."


def test_summary_text_many_pages_shows_range() -> None:
	"""Несколько страниц: видно, какие именно элементы сейчас на экране."""
	items = [_item(i) for i in range(1, 13)]
	text = summary_text(paginate(items, page=2, per_page=5), 12)
	assert text == "Показаны 6–10 из 12 элементов очереди."


def test_summary_text_many_pages_with_filter_names_both_counts() -> None:
	"""С фильтром названы оба числа: подходящих и всего в очереди."""
	items = [_item(i) for i in range(1, 13)]
	text = summary_text(paginate(items, page=1, per_page=5), 40)
	assert text == "Показаны 1–5 из 12 подходящих (в очереди 40)."


# --- отпечаток состава (когда перестраивать карточки) ----------------------


def test_signature_stable_for_same_items() -> None:
	"""Ничего не изменилось — карточки не перестраиваются."""
	items = [_item(1), _item(2)]
	assert queue_signature(items) == queue_signature([_item(1), _item(2)])


def test_signature_notices_edited_title() -> None:
	"""Правка текста меняет заголовок, не трогая статуса, — карточку надо перестроить."""
	before = _item(1)
	after = replace(before, title="новый текст")
	assert queue_signature([before]) != queue_signature([after])


def test_signature_notices_replaced_media() -> None:
	"""Замена вложения меняет путь: кнопка просмотра не должна вести на старый файл."""
	before = replace(_item(1), media_path="/видео/старый.mp4")
	after = replace(before, media_path="/видео/новый.mp4")
	assert queue_signature([before]) != queue_signature([after])


def test_signature_notices_status_and_order() -> None:
	"""Статус и порядок — как раньше: смена любого перестраивает список."""
	first, second = _item(1), _item(2)
	assert queue_signature([first, second]) != queue_signature([second, first])
	assert queue_signature([first]) != queue_signature(
		[replace(first, status=QueueItemStatus.PENDING)]
	)
