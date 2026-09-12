"""Тесты правила показа полного просмотра очереди (ADR-0016, без Qt).

Импортируются только чистые функции (``apply_view``, ``paginate``,
``summary_text``) и перечисления — виджеты диалога не создаются.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.publish_queue import QueueItemDto
from pxcontrol.ui.pages.common import SLOT_NOW, card_signature, plan_cards, slot_color, slot_label
from pxcontrol.ui.pages.publish_queue_view import (
	QueueFilter,
	QueueSort,
	apply_view,
	paginate,
	queue_slots,
	summary_text,
)

#: Точка отсчёта времён в тестах: фиксированная, а не «сейчас».
#: Момент публикации входит в отпечаток карточки (он задаёт метку слота),
#: и на текущем времени два одинаковых элемента различались бы микросекундами.
_BASE = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


def _item(
	item_id: int,
	community: str = "Канал",
	community_id: int = 1,
	when_minutes: int | None = 60,
	status: JobStatus = JobStatus.WAITING,
) -> QueueItemDto:
	when = None if when_minutes is None else _BASE + timedelta(minutes=when_minutes)
	return QueueItemDto(
		id=item_id,
		title=f"пост {item_id}",
		community_id=community_id,
		community_title=community,
		when=when,
		status=status,
		progress=0.0,
		error="сбой" if status is JobStatus.ERROR else None,
	)


def test_sort_nearest_puts_now_first() -> None:
	"""«Ближайшие сначала»: посты «сейчас» — раньше любых дат, потом по дате."""
	items = [
		_item(1, when_minutes=120),
		_item(2, when_minutes=None, status=JobStatus.PENDING),
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
		_item(1, community="А", community_id=1, status=JobStatus.WAITING),
		_item(2, community="А", community_id=1, status=JobStatus.PENDING),
		_item(3, community="Б", community_id=2, status=JobStatus.RUNNING),
		_item(4, community="Б", community_id=2, status=JobStatus.ERROR),
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


# --- точечное обновление карточек ------------------------------------------


def test_signature_stable_for_same_item() -> None:
	"""Ничего не изменилось — карточку не трогаем."""
	assert card_signature(_item(1)) == card_signature(_item(1))


def test_signature_notices_edited_title() -> None:
	"""Правка текста меняет заголовок, не трогая статуса, — карточку надо обновить."""
	before = _item(1)
	assert card_signature(before) != card_signature(replace(before, title="новый текст"))


def test_signature_notices_replaced_media() -> None:
	"""Замена вложения меняет путь: кнопка просмотра не должна вести на старый файл."""
	before = replace(_item(1), media_path="/видео/старый.mp4")
	assert card_signature(before) != card_signature(replace(before, media_path="/видео/новый.mp4"))


def test_signature_notices_status() -> None:
	"""Статус меняет состав кнопок карточки — значит и отпечаток."""
	before = _item(1)
	assert card_signature(before) != card_signature(replace(before, status=JobStatus.PENDING))


def _known(items: list[QueueItemDto]) -> dict[int, tuple[object, ...]]:
	"""Отпечатки показанных карточек (состояние панели)."""
	return {item.id: card_signature(item) for item in items}


def test_plan_cards_adds_and_removes() -> None:
	"""Новые карточки добавляются, ушедшие — убираются; остальные не трогаются."""
	known = _known([_item(1), _item(2)])
	plan = plan_cards([_item(2), _item(3)], known)
	assert plan.added == [3]
	assert plan.removed == [1]
	assert plan.changed == []
	assert plan.order == [2, 3]


def test_plan_cards_marks_only_changed() -> None:
	"""Изменился один элемент — обновляется одна карточка, а не весь список."""
	items = [_item(1), _item(2), _item(3)]
	known = _known(items)
	edited = replace(items[1], title="поправленный текст")
	plan = plan_cards([items[0], edited, items[2]], known)
	assert plan.changed == [2]
	assert (plan.added, plan.removed) == ([], [])


def test_plan_cards_reports_order_without_touching_cards() -> None:
	"""Перестановка меняет только порядок: карточки живы, содержимое прежнее.

	Это и позволяет держать открытую форму правки: сортировка очереди
	не должна пересоздавать карточку, в которой набирают текст.
	"""
	items = [_item(1), _item(2)]
	plan = plan_cards([items[1], items[0]], _known(items))
	assert plan.order == [2, 1]
	assert (plan.added, plan.removed, plan.changed) == ([], [], [])


def test_plan_cards_from_empty_state() -> None:
	"""Первый показ: всё новое, убирать нечего."""
	plan = plan_cards([_item(1), _item(2)], {})
	assert plan.added == [1, 2]
	assert (plan.removed, plan.changed) == ([], [])


# --- слоты времени ---------------------------------------------------------


def test_slot_label_is_local_time_or_now() -> None:
	"""Слот — часы и минуты местного времени; пост без времени — «сейчас»."""
	moment = datetime(2026, 9, 12, 15, 30, tzinfo=UTC)
	assert slot_label(moment) == moment.astimezone().strftime("%H:%M")
	assert slot_label(None) == SLOT_NOW


def test_slot_color_is_stable_and_distinct() -> None:
	"""Один слот — один цвет всегда; разные слоты различаются."""
	assert slot_color("18:00") == slot_color("18:00")
	assert slot_color("18:00") != slot_color("09:00")
	# пара «светлая тема, тёмная тема»
	assert len(slot_color("18:00")) == 2


def test_slot_color_of_now_is_neutral() -> None:
	"""У поста «сейчас» слота нет — метка не претендует на цвет расписания."""
	assert slot_color(SLOT_NOW) not in {slot_color(f"{hour:02d}:00") for hour in range(24)}


def test_queue_slots_lists_now_first_then_times() -> None:
	"""Список слотов очереди: «сейчас» первым, времена — по возрастанию."""
	items = [
		_item(1, when_minutes=None, status=JobStatus.PENDING),
		_item(2, when_minutes=600),
		_item(3, when_minutes=60),
		_item(4, when_minutes=60),  # тот же слот, что у 3 — не дублируется
	]
	slots = queue_slots(items)
	assert slots[0] == SLOT_NOW
	assert slots[1:] == sorted(slots[1:])
	assert len(slots) == 3


def test_apply_view_filters_by_slot() -> None:
	"""Фильтр слота оставляет посты только заданного времени публикации."""
	items = [
		_item(1, when_minutes=60),
		_item(2, when_minutes=600),
		_item(3, when_minutes=None, status=JobStatus.PENDING),
	]
	slot = slot_label(items[0].when)
	shown = apply_view(items, QueueSort.ENQUEUED, QueueFilter.ALL, None, slot)
	assert [item.id for item in shown] == [1]
	now_only = apply_view(items, QueueSort.ENQUEUED, QueueFilter.ALL, None, SLOT_NOW)
	assert [item.id for item in now_only] == [3]
	assert len(apply_view(items, QueueSort.ENQUEUED, QueueFilter.ALL, None, None)) == 3


def test_queue_counts_splits_planned_waiting_and_errors() -> None:
	"""Сводка очереди для плитки сообщества считается по правилам ADR-0016.

	Правило предметное: «запланировано» — всё неотправленное без ошибок,
	включая ждущих слота (они же считаются вторым числом); ошибки ждут
	повтора и в план не входят. В вёрстке это было нечем проверить.
	"""
	from pxcontrol.ui.pages.communities import QueueCounts, queue_counts

	items = [
		_item(1, status=JobStatus.PENDING, community_id=10),
		_item(2, status=JobStatus.WAITING, community_id=10),
		_item(3, status=JobStatus.RUNNING, community_id=10),
		_item(4, status=JobStatus.ERROR, community_id=10),
		_item(5, status=JobStatus.DONE, community_id=10),  # покинул очередь
		_item(6, status=JobStatus.CANCELLED, community_id=10),  # тоже
		_item(7, status=JobStatus.PENDING, community_id=20),
	]

	counts = queue_counts(items)

	assert counts[10] == QueueCounts(planned=3, waiting=1, errors=1)
	assert counts[20] == QueueCounts(planned=1, waiting=0, errors=0)
	assert queue_counts([]) == {}
