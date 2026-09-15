"""Тесты хранилища обещанных клавиатур (ADR-0031, этап 1).

Обещание — это клавиатура, которую нельзя применить прямо сейчас: кнопки
ставит бот и только после публикации, а отложенную запись держит сервер
Telegram. Проверяем именно хранение и переходы обещания; применение
появится следующим этапом.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, PromisedMarkup
from pxcontrol.engine.services.markups import MarkupsService
from pxcontrol.engine.telegram.markup import (
	BUTTON_TEXT_LIMIT,
	ButtonKind,
	MarkupError,
	PostButton,
	PostMarkup,
)


def markup(text: str = "Смотреть") -> PostMarkup:
	"""Простая клавиатура из одной кнопки-ссылки."""
	return PostMarkup(((PostButton(ButtonKind.LINK, text, "https://telegram.org"),),))


async def make_community(db: Database, title: str = "Канал", chat_id: str = "-1001") -> int:
	"""Подключённое сообщество: обещание ссылается на него внешним ключом."""
	async with db.session_factory() as session:
		community = Community(title=title, tg_chat_id=chat_id)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def test_promise_and_read_back(db: Database) -> None:
	"""Обещание сохраняется и читается со всеми полями."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	when = datetime.now(UTC) + timedelta(hours=2)
	promise_id = await service.promise(
		community_id, markup(), match_text="Текст поста", when=when, scheduled_message_id=42
	)
	pending = await service.pending()
	assert len(pending) == 1
	promise = pending[0]
	assert promise.id == promise_id
	assert promise.community_id == community_id
	assert promise.scheduled_message_id == 42
	assert promise.match_text == "Текст поста"
	assert promise.markup == markup()
	assert promise.attempts == 0
	assert promise.error is None
	# время возвращается со зоной: SQLite хранит наивное, сравнивать
	# с now(UTC) иначе нельзя
	assert promise.when is not None and promise.when.tzinfo is not None
	assert promise.when == when


async def test_pending_filters_and_orders(db: Database) -> None:
	"""Обещания отдаются по сообществу и в порядке времени публикации."""
	service = MarkupsService(db)
	first = await make_community(db, "Первый", "-1001")
	second = await make_community(db, "Второй", "-1002")
	now = datetime.now(UTC)
	late = await service.promise(first, markup("Позже"), match_text="B", when=now + timedelta(2))
	early = await service.promise(first, markup("Раньше"), match_text="A", when=now + timedelta(1))
	other = await service.promise(second, markup("Чужой"), match_text="C", when=now)
	assert [p.id for p in await service.pending(first)] == [early, late]
	assert [p.id for p in await service.pending(second)] == [other]
	assert len(await service.pending()) == 3


async def test_promise_validates_markup(db: Database) -> None:
	"""В базу не попадает клавиатура, которую Telegram испортит молча."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	with pytest.raises(MarkupError, match="Клавиатура пуста"):
		await service.promise(community_id, PostMarkup(), match_text="Текст")
	with pytest.raises(MarkupError, match="обрежет её молча"):
		await service.promise(
			community_id, markup("Д" * (BUTTON_TEXT_LIMIT + 1)), match_text="Текст"
		)
	assert await service.pending() == []


async def test_reschedule_keeps_promise(db: Database) -> None:
	"""Перенос времени обещание не рвёт — оно привязано к посту, не к дате."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	when = datetime.now(UTC) + timedelta(hours=1)
	promise_id = await service.promise(community_id, markup(), match_text="Текст", when=when)
	moved = when + timedelta(days=3)
	await service.reschedule(promise_id, moved)
	pending = await service.pending()
	assert len(pending) == 1
	assert pending[0].when == moved
	assert pending[0].markup == markup()


async def test_fail_counts_attempts_and_keeps_promise(db: Database) -> None:
	"""Неудачная попытка не снимает обещание: пост вышел, кнопок нет."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	promise_id = await service.promise(community_id, markup(), match_text="Текст")
	await service.fail(promise_id, "у бота нет права править сообщения")
	await service.fail(promise_id, "нет связи с Telegram")
	pending = await service.pending()
	assert len(pending) == 1
	assert pending[0].attempts == 2
	assert pending[0].error == "нет связи с Telegram"


async def test_drop_removes_promise(db: Database) -> None:
	"""Применённое обещание не хранится."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	promise_id = await service.promise(community_id, markup(), match_text="Текст")
	await service.drop(promise_id)
	assert await service.pending() == []


async def test_drop_scheduled_only_named_of_that_community(db: Database) -> None:
	"""Снятие по исчезнувшим отложкам не трогает чужие и ненайденные."""
	service = MarkupsService(db)
	first = await make_community(db, "Первый", "-1001")
	second = await make_community(db, "Второй", "-1002")
	gone = await service.promise(first, markup(), match_text="A", scheduled_message_id=10)
	alive = await service.promise(first, markup(), match_text="B", scheduled_message_id=11)
	foreign = await service.promise(second, markup(), match_text="C", scheduled_message_id=10)
	assert await service.drop_scheduled(first, [10, 999]) == 1
	assert {p.id for p in await service.pending()} == {alive, foreign}
	assert gone not in {p.id for p in await service.pending()}
	assert await service.drop_scheduled(first, []) == 0


async def test_promises_die_with_community(db: Database) -> None:
	"""Удаление сообщества уносит его обещания каскадом."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	await service.promise(community_id, markup(), match_text="Текст")
	async with db.session_factory() as session:
		community = await session.get(Community, community_id)
		assert community is not None
		await session.delete(community)
		await session.commit()
	assert await service.pending() == []


async def test_broken_row_is_skipped(db: Database) -> None:
	"""Запись с неразборчивой клавиатурой не показывается и не применяется."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	good = await service.promise(community_id, markup("Живая"), match_text="A")
	broken = await service.promise(community_id, markup("Битая"), match_text="B")
	async with db.session_factory() as session:
		await session.execute(
			update(PromisedMarkup)
			.where(PromisedMarkup.id == broken)
			.values(markup=[[{"kind": "неизвестный вид", "text": "?", "value": "?"}]])
		)
		await session.commit()
	assert [p.id for p in await service.pending()] == [good]
	# сама строка остаётся: чинить её вслепую нельзя, разбор — в журнале
	async with db.session_factory() as session:
		rows = (await session.execute(select(PromisedMarkup.id))).scalars().all()
	assert set(rows) == {good, broken}
