"""Тесты лестницы выбора маршрута отправки (ADR-0031, п. 2a).

Правило, которое здесь заперто: **умолчание — технически простейший
маршрут из тех, что дают нужный исход**. Пост «сейчас», который бот может
отправить сам, он и отправляет сам — один вызов, никакого состояния,
кнопки с первой секунды; дорисовка поверх чужого поста остаётся
резервным путём для случаев, где иначе нельзя.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.services.publish_route import (
	PublishRoute,
	choose_route,
	markup_blocker,
	poll_blocker,
	polls_are_anonymous_only,
	publish_capabilities,
	route_uses_userbot,
)
from pxcontrol.engine.telegram.types import CommunityKind

BOTH = publish_capabilities(bot_assigned=True, userbot_assigned=True, markup_edit=True)
NO_EDIT = publish_capabilities(bot_assigned=True, userbot_assigned=True)
BOT_ONLY = publish_capabilities(bot_assigned=True, userbot_assigned=False)
USERBOT_ONLY = publish_capabilities(bot_assigned=False, userbot_assigned=True)


def test_post_without_buttons_goes_by_publisher() -> None:
	"""Без кнопок маршрут прежний: публикатор в приоритете (ADR-0011)."""
	assert choose_route(BOTH, with_markup=False, media_over_bot_limit=False) is PublishRoute.USERBOT
	assert choose_route(BOTH, with_markup=False, media_over_bot_limit=True) is PublishRoute.USERBOT
	# публикатора нет — остаётся бот (запасной путь)
	assert choose_route(BOT_ONLY, with_markup=False, media_over_bot_limit=False) is PublishRoute.BOT


def test_buttons_now_go_by_bot_even_when_publisher_exists() -> None:
	"""Главное правило: простейший маршрут — бот отправляет сам.

	Соблазн «пусть всё публикует публикатор ради единообразия» отвергнут
	осознанно: это добавило бы второй вызов, опознание поста и окно
	без кнопок там, где хватает одного вызова.
	"""
	assert choose_route(BOTH, with_markup=True, media_over_bot_limit=False) is PublishRoute.BOT


def test_big_file_with_buttons_falls_back_to_markup_edit() -> None:
	"""Файл не по силам боту — публикатор отправит, бот дорисует."""
	assert (
		choose_route(BOTH, with_markup=True, media_over_bot_limit=True)
		is PublishRoute.USERBOT_MARKUP
	)


def test_limits_follow_the_route_not_capabilities() -> None:
	"""Пределы берутся по маршруту: у бота подписки не бывает."""
	assert route_uses_userbot(PublishRoute.USERBOT)
	assert route_uses_userbot(PublishRoute.USERBOT_MARKUP)
	assert not route_uses_userbot(PublishRoute.BOT)


def _blocker(caps=BOTH, **kwargs: object) -> str | None:  # type: ignore[no-untyped-def]
	"""Препятствие кнопкам с умолчаниями «канал, сейчас, файл мал»."""
	params: dict[str, object] = {
		"title": "Канал",
		"kind": CommunityKind.CHANNEL,
		"scheduled": False,
		"media_over_bot_limit": False,
	}
	params.update(kwargs)
	return markup_blocker(caps, **params)  # type: ignore[arg-type]


def test_no_blocker_in_simple_case() -> None:
	"""Канал с ботом, пост «сейчас», файл по силам боту — кнопки можно."""
	assert _blocker() is None
	assert _blocker(BOT_ONLY) is None  # публикатор кнопкам не нужен


def test_buttons_need_bot() -> None:
	"""Кнопки ставит только бот — без него их не бывает."""
	assert "Кнопки ставит только бот" in (_blocker(USERBOT_ONLY) or "")


def test_scheduled_buttons_go_by_markup_edit() -> None:
	"""Отложенный пост с кнопками: отправит публикатор, бот дорисует.

	Бот отложенных записей создавать не умеет, поэтому простейшего
	маршрута здесь нет — остаётся дорисовка после выхода поста.
	"""
	assert _blocker(scheduled=True) is None
	assert (
		choose_route(BOTH, with_markup=True, media_over_bot_limit=False, scheduled=True)
		is PublishRoute.USERBOT_MARKUP
	)
	# без права изменять сообщения дорисовывать некому — отказ с причиной
	assert "нет права изменять сообщения" in (_blocker(NO_EDIT, scheduled=True) or "")


def test_scheduled_buttons_impossible_in_group() -> None:
	"""В группе бот чужое не правит, а отложку создать не может."""
	reason = _blocker(kind=CommunityKind.GROUP, scheduled=True) or ""
	assert "В группе" in reason and "отложенные" in reason


def test_group_with_big_file_has_no_route() -> None:
	"""В группе бот чужое не правит, а сам такой файл не зальёт."""
	reason = _blocker(kind=CommunityKind.GROUP, media_over_bot_limit=True) or ""
	assert "В группе" in reason


def test_big_file_needs_edit_right() -> None:
	"""Без права изменять сообщения дорисовать кнопки некому."""
	reason = _blocker(NO_EDIT, media_over_bot_limit=True) or ""
	assert "нет права изменять сообщения" in reason


def test_big_file_needs_publisher() -> None:
	"""Крупный файл отправляет публикатор — без него маршрута нет."""
	caps = publish_capabilities(bot_assigned=True, userbot_assigned=False, markup_edit=True)
	reason = _blocker(caps, media_over_bot_limit=True) or ""
	assert "нет" in reason and "публикатор" in reason


@pytest.mark.parametrize("kind", [CommunityKind.CHANNEL, CommunityKind.GROUP])
def test_small_file_allows_buttons_anywhere(kind: CommunityKind) -> None:
	"""Пока файл по силам боту, вид сообщества кнопкам не мешает."""
	assert _blocker(kind=kind) is None


def test_markup_first_sends_by_bot_at_the_minute() -> None:
	"""Режим «кнопки важнее»: отложку не создаём, отправляет бот.

	Право «изменять сообщения» в этом режиме не нужно вовсе: бот ставит
	кнопки своему посту сам — нужно лишь, чтобы он справился с файлом.
	"""
	assert (
		choose_route(
			BOT_ONLY,
			with_markup=True,
			media_over_bot_limit=False,
			scheduled=True,
			markup_first=True,
		)
		is PublishRoute.BOT
	)
	assert _blocker(NO_EDIT, scheduled=True, markup_first=True) is None
	assert _blocker(BOT_ONLY, scheduled=True, markup_first=True) is None


def test_markup_first_needs_bot_sized_file() -> None:
	"""Крупный файл боту не по силам — режим отклоняется с подсказкой."""
	reason = _blocker(scheduled=True, markup_first=True, media_over_bot_limit=True) or ""
	assert "кнопки важнее" in reason and "важнее публикация" in reason


def test_default_mode_still_uses_markup_edit() -> None:
	"""Умолчание не изменилось: публикация важнее, кнопки — после выхода."""
	assert (
		choose_route(BOTH, with_markup=True, media_over_bot_limit=False, scheduled=True)
		is PublishRoute.USERBOT_MARKUP
	)


def test_scheduled_poll_has_no_buttons_unless_bot_sends_it() -> None:
	"""У отложенного опроса кнопки бывают только в режиме «кнопки важнее».

	Отложку создаёт публикатор, а дорисовать клавиатуру к опросу нечем
	(матрица маршрутов, ADR-0031): остаётся путь, где опрос в назначенную
	минуту отправляет сам бот — вместе с кнопками (ADR-0033, C5).
	"""
	assert _blocker(poll=True) is None  # «сейчас» — бот отправляет сам
	reason = _blocker(scheduled=True, poll=True) or ""
	assert "У отложенного опроса кнопок не бывает" in reason
	assert "кнопки важнее" in reason
	assert _blocker(scheduled=True, poll=True, markup_first=True) is None


def test_channel_polls_are_anonymous_only() -> None:
	"""В канале опрос бывает только анонимным — правило Telegram.

	Проверено живьём 17.09.2026: сервер отвечает «You cannot broadcast
	polls where the voters are public». Отказ обязан приходить от нас
	и до отправки, иначе человек узнаёт о правиле из сырой ошибки.
	"""
	assert polls_are_anonymous_only(CommunityKind.CHANNEL)
	assert not polls_are_anonymous_only(CommunityKind.GROUP)
	reason = poll_blocker(False, title="Канал", kind=CommunityKind.CHANNEL) or ""
	assert "только анонимным" in reason
	assert poll_blocker(True, title="Канал", kind=CommunityKind.CHANNEL) is None
	# в группе открытые голоса разрешены — там правило не действует
	assert poll_blocker(False, title="Группа", kind=CommunityKind.GROUP) is None
