"""Тесты общих частей формы поста (чистые функции интерфейса, без Qt).

Перечень типов контента и правила показа тем форума общие для страницы
«Публикация» и окна правки элемента очереди — расходиться им нельзя.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.services.posts import TextLimits
from pxcontrol.engine.services.publish_route import PublishRoute
from pxcontrol.engine.telegram.rights import ParticipantStatus
from pxcontrol.engine.telegram.types import (
	CAPTION_LENGTH_LIMIT,
	GENERAL_TOPIC_ID,
	TEXT_LENGTH_LIMIT,
	ForumTopicInfo,
	MediaKind,
)
from pxcontrol.ui.pages.common import (
	CONTENT_KINDS,
	READ_ONLY_KIND_LABEL,
	counter_text,
	kind_file_filter,
	kind_label,
	topic_label,
	visible_topics,
)
from pxcontrol.ui.pages.markup_editor import markup_notice

_TOPICS = [
	ForumTopicInfo(GENERAL_TOPIC_ID, "General"),
	ForumTopicInfo(5, "Новости"),
	ForumTopicInfo(9, "Архив", closed=True),
]


def test_visible_topics_drops_general() -> None:
	"""General отдельным пунктом не показывается — он и есть «Общая лента»."""
	shown, _closed = visible_topics(_TOPICS, ParticipantStatus.ADMIN)
	assert [topic.id for topic in shown] == [5, 9]


def test_visible_topics_keeps_closed_for_admin() -> None:
	"""Админ пишет и в закрытые темы — они остаются в списке."""
	shown, closed = visible_topics(_TOPICS, ParticipantStatus.ADMIN)
	assert closed == 0
	assert any(topic.closed for topic in shown)


def test_visible_topics_hides_closed_for_member() -> None:
	"""Участнику закрытые недоступны: скрываются, их число — для подписи."""
	shown, closed = visible_topics(_TOPICS, ParticipantStatus.MEMBER)
	assert [topic.id for topic in shown] == [5]
	assert closed == 1


def test_visible_topics_unknown_status_behaves_as_member() -> None:
	"""Участие неизвестно — считаем участником: показать лишнее хуже, чем скрыть."""
	shown, closed = visible_topics(_TOPICS, None)
	assert [topic.id for topic in shown] == [5]
	assert closed == 1


def test_topic_label_marks_closed() -> None:
	"""Закрытая тема помечена прямо в подписи пункта."""
	assert topic_label(ForumTopicInfo(5, "Новости")) == "Новости"
	assert topic_label(ForumTopicInfo(9, "Архив", closed=True)) == "Архив (закрыта)"


@pytest.mark.parametrize("kind", list(MediaKind))
def test_content_kinds_cover_every_media_kind(kind: MediaKind) -> None:
	"""У каждого типа вложения есть подпись; у создаваемых — и фильтр файлов.

	Замок контракта: новый тип, забытый в ``CONTENT_KINDS``, уронил бы
	форму в рантайме (подпись и фильтр ищутся поиском по перечню).
	Вид, который приложение только читает (``OTHER`` — опрос,
	геопозиция из клиента Telegram), в форме не выбирается: у него
	общая подпись и нет фильтра.
	"""
	assert kind_label(kind)
	if not kind.creatable:
		assert kind_label(kind) == READ_ONLY_KIND_LABEL
		assert kind not in {item_kind for _label, item_kind, _filter in CONTENT_KINDS}
		return
	file_filter = kind_file_filter(kind)
	if kind.needs_file:
		assert file_filter
	else:
		# текст и опрос файла не выбирают: фильтру взяться неоткуда
		assert file_filter == ""


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


# --- блок кнопок под постом (ADR-0031, этап 2б) ---------------------------


def test_limits_follow_route_in_the_form() -> None:
	"""Счётчик символов показывает предел того, кто реально отправит пост.

	Пост с кнопками уходит ботом даже там, где у публикателя Premium, —
	и обещать 4096 знаков подписи было бы обманом: у ботов подписки
	не бывает.
	"""
	premium = TextLimits(text=8192, caption=4096)
	assert premium.on_route(PublishRoute.USERBOT) == premium
	assert premium.on_route(PublishRoute.USERBOT_MARKUP) == premium
	by_bot = premium.on_route(PublishRoute.BOT)
	assert (by_bot.text, by_bot.caption) == (TEXT_LENGTH_LIMIT, CAPTION_LENGTH_LIMIT)


def test_markup_state_is_one_rule_for_all_three_forms() -> None:
	"""Состояние блока кнопок считается правилами движка, а не словами формы.

	Пока каждая форма считала его сама, они разошлись: «Новый пост» знал,
	что у альбома кнопок не бывает, а правка элемента очереди — нет,
	и человек узнавал об этом только отказом при сохранении.
	"""
	from pxcontrol.engine.services.communities import CommunityDto
	from pxcontrol.engine.services.posts import MediaFile
	from pxcontrol.engine.telegram.types import CommunityKind
	from pxcontrol.ui.pages.markup_editor import markup_state

	community = CommunityDto(
		id=1,
		title="Канал",
		username=None,
		tg_chat_id="-1001",
		bot_id=1,
		bot_label="Бот",
		enabled=True,
		default_account_id=7,
		kind=CommunityKind.CHANNEL,
		bot_can_edit=True,
	)
	premium = TextLimits(text=8192, caption=4096)
	video = MediaFile("a.mp4", MediaKind.VIDEO)

	album = markup_state(
		community,
		premium,
		media=(video, MediaFile("b.mp4", MediaKind.VIDEO)),
		scheduled=False,
		has_markup=True,
		markup_first=False,
		over_bot_limit=False,
	)
	assert album.blocked is not None and "альбом" in album.blocked.lower()
	assert album.notice == ""  # запрет назван — объяснять нечего

	single = markup_state(
		community,
		premium,
		media=(video,),
		scheduled=False,
		has_markup=True,
		markup_first=False,
		over_bot_limit=False,
	)
	assert single.blocked is None
	# пост с кнопками уходит ботом — предел подписи базовый, не Premium
	assert single.limits.caption == CAPTION_LENGTH_LIMIT
	assert single.route is PublishRoute.BOT


def test_markup_notice_warns_about_sender_and_delay() -> None:
	"""Форма предупреждает о смене лица поста и о задержке кнопок заранее."""
	by_bot = markup_notice(PublishRoute.BOT, "Паблишер")
	assert "Паблишер" in by_bot and str(CAPTION_LENGTH_LIMIT) in by_bot
	drawn = markup_notice(PublishRoute.USERBOT_MARKUP, "Паблишер")
	assert "публикатор" in drawn and "без них" in drawn
	# пост без кнопок ничего не меняет — и молчит
	assert markup_notice(PublishRoute.USERBOT, "Паблишер") == ""
	# бот без названия — всё равно понятная фраза
	assert "бот" in markup_notice(PublishRoute.BOT, None)
