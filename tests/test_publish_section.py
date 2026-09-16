"""Тесты словаря стадий раздела «Публикация» (ADR-0032, без виджетов).

Порядок стадий — обещание решения: пункты идут в порядке пути поста,
и человек видит по ним, где сейчас его пост. Порядок легко испортить
перестановкой строк в перечислении, поэтому он под замком теста.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pxcontrol.engine.services.posts import PublishedDraft, PublishedPostDto, PublishedRef
from pxcontrol.engine.telegram.markup import PostMarkup
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.pages.publish_batch_page import source_text
from pxcontrol.ui.pages.publish_stages import (
	SECTION_ROUTE_KEY,
	PublishStage,
	stage_hint,
	stage_icon,
	stage_title,
)
from pxcontrol.ui.pages.published_edit import attachment_note, markup_state_note
from pxcontrol.ui.pages.published_view import feed_summary, markup_note, published_subtitle


def test_stage_order_is_post_path() -> None:
	"""Порядок пунктов — порядок пути: создание → наша очередь → сервер.

	«Пакет» — то же создание, только пачкой, поэтому он стоит сразу
	за формой поста, а не между двумя ожиданиями.
	"""
	assert list(PublishStage) == [
		PublishStage.NEW_POST,
		PublishStage.BATCH,
		PublishStage.QUEUE,
		PublishStage.SCHEDULED,
		PublishStage.PUBLISHED,
	]


def test_route_keys_are_unique() -> None:
	"""Маршрутные ключи различны и не совпадают с ключом корня ветки.

	Навигация различает пункты по ключу: совпадение молча потеряло бы
	пункт (``addItem`` игнорирует уже известный ключ).
	"""
	keys = [stage.value for stage in PublishStage]
	assert len(set(keys)) == len(keys)
	assert SECTION_ROUTE_KEY not in keys


def test_every_stage_has_title_hint_and_icon() -> None:
	"""У каждой стадии есть подпись, подсказка и значок — пустых пунктов нет."""
	for stage in PublishStage:
		assert stage_title(stage).strip()
		assert stage_hint(stage).strip()
		assert stage_icon(stage) is not None


def test_hints_tell_the_lists_apart() -> None:
	"""Подсказки называют главную разницу двух ожиданий (ADR-0010/0016).

	Пост в нашей очереди уйдёт только при запущенном приложении,
	отложенную запись сервер Telegram опубликует сам. Пока списки были
	вкладками одной страницы, разница объяснялась над обеими сразу;
	у отдельных экранов каждый обязан сказать это за себя.
	"""
	assert "приложени" in stage_hint(PublishStage.QUEUE)
	assert "Telegram" in stage_hint(PublishStage.SCHEDULED)
	assert stage_hint(PublishStage.QUEUE) != stage_hint(PublishStage.SCHEDULED)


def test_source_text() -> None:
	"""Подпись источника пакета: папка и число файлов с нужным окончанием."""
	assert source_text("", 0) == "Источник не выбран."
	assert source_text("/videos/ready", 1) == "Папка: /videos/ready · 1 файл"
	assert source_text("/videos/ready", 3) == "Папка: /videos/ready · 3 файла"
	assert source_text("/videos/ready", 12) == "Папка: /videos/ready · 12 файлов"


def _post(
	message_id: int = 7,
	*,
	buttons: int = 0,
	views: int | None = None,
	markup_error: str | None = None,
	media_kind: MediaKind = MediaKind.NONE,
) -> PublishedPostDto:
	return PublishedPostDto(
		community_id=1,
		community_title="Канал",
		message_id=message_id,
		text_preview="Вышедший пост",
		published_at=datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
		media_kind=media_kind,
		buttons=buttons,
		views=views,
		markup_error=markup_error,
	)


def test_markup_note_states() -> None:
	"""Три честных состояния кнопок вышедшего поста (ADR-0031).

	Обещание живёт, только пока кнопок нет: как только они встали,
	пометка обязана исчезнуть — иначе она врёт человеку.
	"""
	assert markup_note(_post()) == ""
	assert markup_note(_post(buttons=2)) == "кнопки: 2"
	assert markup_note(_post(markup_error="")) == "кнопки обещаны, ждут бота"
	assert markup_note(_post(markup_error="бот потерял право")) == (
		"кнопки обещаны, не поставлены (бот потерял право)"
	)
	# кнопки стоят — про обещание молчим
	assert markup_note(_post(buttons=1, markup_error="старое")) == "кнопки: 1"


def test_published_subtitle() -> None:
	"""Подпись карточки: когда вышел, что внутри, просмотры и кнопки."""
	subtitle = published_subtitle(_post(views=340, buttons=1, media_kind=MediaKind.VIDEO))
	assert subtitle.startswith("вышел: ")
	assert " · видео · " in subtitle
	assert "340 просмотров" in subtitle
	assert subtitle.endswith("кнопки: 1")
	assert published_subtitle(_post(views=1)).count("1 просмотр") == 1


def test_feed_summary() -> None:
	"""Итог ленты считает прочитанное, а не выдумывает общее число."""
	assert feed_summary(0, False) == "Постов не прочитано."
	assert feed_summary(1, True) == "Прочитано 1 пост · дальше есть"
	assert feed_summary(50, False) == "Прочитано 50 постов · это вся лента"


def _draft(
	*,
	media_kind: MediaKind = MediaKind.NONE,
	buttons: int = 0,
	markup: PostMarkup | None = None,
	blocker: str | None = None,
) -> PublishedDraft:
	return PublishedDraft(
		ref=PublishedRef(1, 77),
		community_title="Канал",
		text="текст",
		media_kind=media_kind,
		topic_id=None,
		buttons=buttons,
		markup=markup,
		text_limit=4096,
		markup_blocker=blocker,
	)


def test_attachment_note_names_what_is_not_editable() -> None:
	"""Вложение подписывается, а не прячется: иначе его ищут глазами."""
	assert attachment_note(_draft()) == ""
	assert attachment_note(_draft(media_kind=MediaKind.VIDEO)) == "вложение: видео"
	assert "опрос" in attachment_note(_draft(media_kind=MediaKind.OTHER))


def test_markup_state_note_states() -> None:
	"""Что сказано над редактором кнопок в каждом из положений."""
	assert "бот" in markup_state_note(_draft())
	assert markup_state_note(_draft(buttons=2, markup=PostMarkup())) == (
		"Пустая клавиатура снимает кнопки под постом."
	)
	# кнопки не нашего вида: правку «как есть» обещать нельзя
	assert "не нашего вида" in markup_state_note(_draft(buttons=3))
	# запрет из движка показывается как есть
	assert markup_state_note(_draft(blocker="В группе нельзя")) == "В группе нельзя"


def test_published_draft_markup_ours() -> None:
	"""Клавиатуру можно показать кнопками, только если она разобрана."""
	assert _draft().markup_ours  # кнопок нет — показывать нечего
	assert _draft(buttons=1, markup=PostMarkup()).markup_ours
	assert not _draft(buttons=1).markup_ours
