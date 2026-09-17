"""Тесты чистых правил показа отложенных записей (без Qt).

Подпись и отпечаток карточки, ключ, слияние перечитанного сообщества,
правило показа, предупреждение о непрочитанных сообществах и сноска
формы правки — всё это функции без виджетов.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pxcontrol.engine.services.posts import (
	ScheduledDraft,
	ScheduledList,
	ScheduledPostDto,
	ScheduledRef,
	UnreadCommunity,
)
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.pages.scheduled_edit import attachment_note
from pxcontrol.ui.pages.scheduled_panel import (
	ScheduledSort,
	apply_scheduled_view,
	merge_community,
	merge_reread,
	scheduled_key,
	scheduled_signature,
	scheduled_subtitle,
)
from pxcontrol.ui.pages.scheduled_view import unread_text

_BASE = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _item(
	community_id: int,
	message_id: int,
	minutes: int = 0,
	*,
	title: str = "Канал",
	text: str = "текст",
	kind: MediaKind = MediaKind.NONE,
) -> ScheduledPostDto:
	return ScheduledPostDto(
		community_id=community_id,
		community_title=title,
		account_id=7,
		message_id=message_id,
		text_preview=text,
		scheduled_at=_BASE + timedelta(minutes=minutes),
		media_kind=kind,
	)


def test_subtitle_names_community_moment_and_kind() -> None:
	item = _item(1, 1, kind=MediaKind.VIDEO)
	subtitle = scheduled_subtitle(item)
	assert subtitle.startswith("Канал · публикация: ")
	assert subtitle.endswith(" · видео")
	assert scheduled_subtitle(_item(1, 2), with_community=False).startswith("публикация: ")
	assert scheduled_subtitle(_item(1, 2)).endswith(" · текст")


def test_key_and_signature() -> None:
	"""Ключ — сообщество и запись; отпечаток не зависит от аккаунта-читателя."""
	item = _item(3, 41)
	assert scheduled_key(item) == (3, 41)
	other_reader = ScheduledPostDto(
		community_id=3,
		community_title="Канал",
		account_id=8,
		message_id=41,
		text_preview="текст",
		scheduled_at=item.scheduled_at,
	)
	assert scheduled_signature(item) == scheduled_signature(other_reader)
	assert scheduled_signature(item) != scheduled_signature(_item(3, 41, text="правлено"))
	assert scheduled_signature(item) != scheduled_signature(_item(3, 41, minutes=5))


def test_merge_community_replaces_only_that_community() -> None:
	items = [_item(1, 1), _item(2, 1), _item(1, 2)]
	merged = merge_community(items, 1, [_item(1, 3)])
	assert [scheduled_key(item) for item in merged] == [(2, 1), (1, 3)]
	assert merge_community(items, 9, []) == items


def test_merge_reread_keeps_records_when_community_unreadable() -> None:
	"""Неудачное перечитывание не стирает записи и называет сообщество.

	Пустой список с непустым unread означает «не спросили», а не
	«отложенных нет» (ADR-0010): подмена им записей выдала бы неполный
	список за полный — человек решил бы, что отложенные исчезли.
	"""
	items = [_item(1, 1), _item(2, 1)]
	failed = ScheduledList(items=[], unread=(UnreadCommunity(1, "Канал"),))

	kept, unread = merge_reread(items, (), 1, failed)
	assert kept == items  # записи на месте
	assert unread == (UnreadCommunity(1, "Канал"),)  # и сообщество названо

	# повторная неудача не удваивает пометку
	kept, unread = merge_reread(kept, unread, 1, failed)
	assert unread == (UnreadCommunity(1, "Канал"),)

	# удачное чтение заменяет записи этого сообщества и снимает пометку
	kept, unread = merge_reread(kept, unread, 1, ScheduledList(items=[_item(1, 5)]))
	assert [scheduled_key(item) for item in kept] == [(2, 1), (1, 5)]
	assert unread == ()


def test_apply_scheduled_view_filters_and_sorts() -> None:
	items = [
		_item(2, 1, 30, title="Бета"),
		_item(1, 1, 10, title="Альфа"),
		_item(1, 2, 10, title="Альфа"),
		_item(1, 3, 90, title="Альфа"),
	]
	nearest = apply_scheduled_view(items, ScheduledSort.NEAREST, None)
	assert [item.message_id for item in nearest] == [1, 2, 1, 3]
	by_community = apply_scheduled_view(items, ScheduledSort.COMMUNITY, None)
	assert [(i.community_id, i.message_id) for i in by_community] == [
		(1, 1),
		(1, 2),
		(1, 3),
		(2, 1),
	]
	assert [i.message_id for i in apply_scheduled_view(items, ScheduledSort.NEAREST, 2)] == [1]
	slot = items[3].when.astimezone().strftime("%H:%M")
	assert [i.message_id for i in apply_scheduled_view(items, ScheduledSort.NEAREST, 1, slot)] == [
		3
	]


def test_unread_text() -> None:
	"""Прочитано всё — строки нет; иначе сообщества названы поимённо."""
	assert unread_text(()) == ""
	unread = (UnreadCommunity(1, "Канал"), UnreadCommunity(2, "Группа"))
	assert unread_text(unread) == "Не удалось прочитать отложенные: «Канал», «Группа»."


def _draft(kind: MediaKind, topic_id: int | None = None) -> ScheduledDraft:
	return ScheduledDraft(
		ref=ScheduledRef(1, 7, 41),
		community_title="Канал",
		text="текст",
		when=_BASE,
		media_kind=kind,
		topic_id=topic_id,
		text_limit=4096,
	)


def test_attachment_note_describes_what_is_not_editable() -> None:
	"""Сноска называет вложение и тему — то, что форма не правит."""
	assert attachment_note(_draft(MediaKind.NONE), None) == ""
	assert attachment_note(_draft(MediaKind.PHOTO), None) == "вложение: фото"
	assert attachment_note(_draft(MediaKind.OTHER), None).startswith("вложение без подписи")
	assert attachment_note(_draft(MediaKind.NONE, 5), "Новости") == "тема форума: Новости"
	assert attachment_note(_draft(MediaKind.VIDEO, 5), None) == "вложение: видео · тема форума: #5"


def test_subtitle_tells_about_promised_buttons() -> None:
	"""Пометка о кнопках: их сейчас нет и быть не может — человек должен знать.

	Иначе владелец решил бы, что кнопки потерялись, и полез бы их
	добавлять заново (ADR-0031).
	"""
	plain = ScheduledPostDto(
		community_id=1,
		community_title="Канал",
		account_id=1,
		message_id=5,
		text_preview="текст",
		scheduled_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
		media_kind=MediaKind.NONE,
	)
	assert "кнопки" not in scheduled_subtitle(plain)
	promised = replace(plain, markup_promised=True)
	assert "кнопки появятся после выхода" in scheduled_subtitle(promised)
	# на странице сообщества название не дублируется, пометка остаётся
	short = scheduled_subtitle(promised, with_community=False)
	assert "Канал" not in short and "кнопки появятся" in short
