"""Тесты общих частей формы поста (чистые функции интерфейса, без Qt).

Перечень типов контента и правила показа тем форума общие для страницы
«Публикация» и окна правки элемента очереди — расходиться им нельзя.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.telegram.types import (
	GENERAL_TOPIC_ID,
	ForumTopicInfo,
	MediaKind,
	UserbotRole,
)
from pxcontrol.ui.pages.common import (
	CONTENT_KINDS,
	counter_text,
	kind_file_filter,
	kind_label,
	topic_label,
	visible_topics,
)

_TOPICS = [
	ForumTopicInfo(GENERAL_TOPIC_ID, "General"),
	ForumTopicInfo(5, "Новости"),
	ForumTopicInfo(9, "Архив", closed=True),
]


def test_visible_topics_drops_general() -> None:
	"""General отдельным пунктом не показывается — он и есть «Общая лента»."""
	shown, _closed = visible_topics(_TOPICS, UserbotRole.ADMIN)
	assert [topic.id for topic in shown] == [5, 9]


def test_visible_topics_keeps_closed_for_admin() -> None:
	"""Админ пишет и в закрытые темы — они остаются в списке."""
	shown, closed = visible_topics(_TOPICS, UserbotRole.ADMIN)
	assert closed == 0
	assert any(topic.closed for topic in shown)


def test_visible_topics_hides_closed_for_member() -> None:
	"""Участнику закрытые недоступны: скрываются, их число — для подписи."""
	shown, closed = visible_topics(_TOPICS, UserbotRole.MEMBER)
	assert [topic.id for topic in shown] == [5]
	assert closed == 1


def test_visible_topics_unknown_role_behaves_as_member() -> None:
	"""Роль неизвестна — считаем участником: показать лишнее хуже, чем скрыть."""
	shown, closed = visible_topics(_TOPICS, None)
	assert [topic.id for topic in shown] == [5]
	assert closed == 1


def test_topic_label_marks_closed() -> None:
	"""Закрытая тема помечена прямо в подписи пункта."""
	assert topic_label(ForumTopicInfo(5, "Новости")) == "Новости"
	assert topic_label(ForumTopicInfo(9, "Архив", closed=True)) == "Архив (закрыта)"


@pytest.mark.parametrize("kind", list(MediaKind))
def test_content_kinds_cover_every_media_kind(kind: MediaKind) -> None:
	"""У каждого типа вложения есть подпись и фильтр файлов.

	Замок контракта: новый тип, забытый в ``CONTENT_KINDS``, уронил бы
	форму в рантайме (подпись и фильтр ищутся поиском по перечню).
	"""
	assert kind_label(kind)
	file_filter = kind_file_filter(kind)
	if kind is MediaKind.NONE:
		assert file_filter == ""  # текстовому посту файл не выбирают
	else:
		assert file_filter


def test_content_kinds_start_with_text() -> None:
	"""Первый сегмент — «Текст»: пост без вложения (у него нет фильтра файлов)."""
	label, kind, file_filter = CONTENT_KINDS[0]
	assert (label, kind, file_filter) == ("Текст", MediaKind.NONE, "")


def test_counter_text_within_limit() -> None:
	"""В пределах лимита — просто «сколько из скольки»."""
	assert counter_text(120, 1024) == "120 / 1024"
	assert counter_text(1024, 1024) == "1024 / 1024"  # ровно предел — ещё не превышение


def test_counter_text_names_the_overflow() -> None:
	"""Превышение названо числом: «сократите» без цифры заставляет считать самому."""
	assert counter_text(1100, 1024) == "1100 / 1024 — на 76 больше предела Telegram"


def test_checked_or_single_treats_lonely_item_as_chosen() -> None:
	"""Один элемент в списке — галочка избыточна, он и есть выбор.

	Правило списков с галочками на странице «Видео»: раньше оно было
	написано дважды (файлы к обработке и готовые видео к публикации),
	вместе с одинаковым комментарием.
	"""
	from pxcontrol.ui.pages.common import checked_or_single

	assert checked_or_single(["один"], []) == ["один"]  # выбирать не из чего
	assert checked_or_single(["a", "b"], []) is None  # выбор не сделан
	assert checked_or_single(["a", "b"], ["b"]) == ["b"]
	assert checked_or_single([], []) is None  # пустой список — выбора нет
