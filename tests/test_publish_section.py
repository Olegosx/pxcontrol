"""Тесты словаря стадий раздела «Публикация» (ADR-0032, без виджетов).

Порядок стадий — обещание решения: пункты идут в порядке пути поста,
и человек видит по ним, где сейчас его пост. Порядок легко испортить
перестановкой строк в перечислении, поэтому он под замком теста.
"""

from __future__ import annotations

from pxcontrol.ui.pages.publish_stages import (
	SECTION_ROUTE_KEY,
	PublishStage,
	stage_hint,
	stage_icon,
	stage_title,
)


def test_stage_order_is_post_path() -> None:
	"""Порядок пунктов — порядок пути: создание → наша очередь → сервер."""
	assert list(PublishStage) == [
		PublishStage.NEW_POST,
		PublishStage.QUEUE,
		PublishStage.SCHEDULED,
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
