"""Тесты обещанных клавиатур: хранение и дозор, который их применяет.

Обещание — это клавиатура, которую нельзя применить прямо сейчас: кнопки
ставит бот и только после публикации, а отложенную запись держит сервер
Telegram (ADR-0031). Проверяются и хранение с переходами обещания,
и осторожность дозора: двусмысленность — не повод угадывать, флуд
прекращает проход, приостановленный бот не стоит попытки.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, PromisedMarkup, TgAccount
from pxcontrol.engine.services.markups import (
	APPLY_MAX_AGE,
	MarkupsService,
	PromisedMarkupDto,
	promise_expired,
)
from pxcontrol.engine.telegram.markup import (
	BUTTON_TEXT_LIMIT,
	ButtonKind,
	MarkupError,
	PostButton,
	PostMarkup,
)
from pxcontrol.engine.telegram.types import BotRef, TelegramFloodError


def _promise_dto(
	*, when: datetime | None, created_at: datetime, attempts: int = 1
) -> PromisedMarkupDto:
	"""Снимок обещания для проверки чистого правила срока."""
	return PromisedMarkupDto(
		id=1,
		community_id=1,
		scheduled_message_id=None,
		message_id=None,
		when=when,
		match_text="текст",
		markup=markup(),
		attempts=attempts,
		error=None,
		created_at=created_at,
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


# --- дозор: применение обещаний (ADR-0031, этап 3) -------------------------


class _FakeGateway:
	"""Подмена шлюза для дозора: поиск поста и правка разметки без сети."""

	def __init__(self) -> None:
		#: какой номер «найдётся» по тексту (None — не опознан)
		self.found: int | None = 777
		self.lookups: list[tuple[int, str, str]] = []
		self.edits: list[tuple[str, int, object]] = []
		self.edit_error: Exception | None = None
		self.lookup_error: Exception | None = None

	async def userbot_find_published(
		self, account_id: int, chat_id: str, text: str, after: datetime, limit: int
	) -> int | None:
		if self.lookup_error is not None:
			raise self.lookup_error
		self.lookups.append((account_id, chat_id, text))
		return self.found

	async def bot_edit_markup(
		self, bot: BotRef, chat_id: str, message_id: int, markup: object
	) -> None:
		if self.edit_error is not None:
			raise self.edit_error
		self.edits.append((chat_id, message_id, markup))


async def make_ready_community(db: Database, *, bot_can_edit: bool = True) -> int:
	"""Сообщество, готовое принять кнопки: бот с правом правки и публикатор."""
	async with db.session_factory() as session:
		bot = Bot(label="Паблишер", token="123:AAA", username="pub_bot")
		account = TgAccount(label="@ub", phone="+7900", session="s")
		session.add_all([bot, account])
		await session.flush()
		community = Community(
			title="Канал",
			tg_chat_id="-1001",
			bot_id=bot.id,
			bot_can_edit=bot_can_edit,
			default_tg_account_id=account.id,
		)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def test_watcher_finds_post_and_sets_buttons(db: Database) -> None:
	"""Отложка вышла — дозор опознал пост, поставил кнопки, снял обещание."""
	gateway = _FakeGateway()
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	when = datetime.now(UTC) - timedelta(minutes=1)  # время публикации прошло
	await service.promise(
		community_id, markup(), match_text="текст поста", when=when, scheduled_message_id=5
	)
	assert await service.apply_due() == 1
	assert gateway.lookups == [(1, "-1001", "текст поста")]
	assert gateway.edits == [("-1001", 777, markup())]
	assert await service.pending() == []  # применённое не хранится


async def test_watcher_waits_until_publication_time(db: Database) -> None:
	"""До назначенного времени поста в канале нет — дозор не тревожит Telegram."""
	gateway = _FakeGateway()
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.promise(community_id, markup(), match_text="текст", when=when)
	assert await service.apply_due() == 0
	assert gateway.lookups == [] and gateway.edits == []
	assert len(await service.pending()) == 1


async def test_watcher_skips_unidentified_post(db: Database) -> None:
	"""Пост не опознан — кнопки не ставятся, попытка записана.

	Промах хуже отсутствия кнопок: клавиатура приклеилась бы к чужому
	посту, поэтому двусмысленность трактуется как «не нашлось».
	"""
	gateway = _FakeGateway()
	gateway.found = None
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	await service.promise(
		community_id, markup(), match_text="текст", when=datetime.now(UTC) - timedelta(minutes=1)
	)
	assert await service.apply_due() == 0
	assert gateway.edits == []
	promises = await service.pending()
	assert len(promises) == 1 and promises[0].attempts == 1
	assert promises[0].error is not None and "не опознан" in promises[0].error


async def test_watcher_waits_for_bot_without_spending_attempt(db: Database) -> None:
	"""Нет права у бота — обещание ждёт, а не тратит попытки.

	Право вернёт человек; наказывать обещание за это нельзя — иначе
	оно исчерпало бы попытки, пока владелец разбирается с правами.
	"""
	gateway = _FakeGateway()
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db, bot_can_edit=False)
	await service.promise(
		community_id, markup(), match_text="текст", when=datetime.now(UTC) - timedelta(minutes=1)
	)
	assert await service.apply_due() == 0
	assert gateway.lookups == [] and gateway.edits == []
	promises = await service.pending()
	assert len(promises) == 1 and promises[0].attempts == 0


async def test_watcher_keeps_trying_within_the_deadline(db: Database) -> None:
	"""Пока срок не вышел, дозор пробует снова: заминка — не приговор.

	Прежнее правило «пять попыток» означало пять минут, и десятиминутный
	обрыв связи хоронил кнопки у вышедшего поста. Теперь мера — время.
	"""
	gateway = _FakeGateway()
	gateway.found = None
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	await service.promise(
		community_id, markup(), match_text="текст", when=datetime.now(UTC) - timedelta(minutes=1)
	)
	for _ in range(8):  # больше прежнего предела в пять попыток
		await service.apply_due()
	assert len(gateway.lookups) == 8
	promises = await service.pending()
	assert len(promises) == 1 and promises[0].attempts == 8


async def test_watcher_releases_promise_after_deadline(db: Database) -> None:
	"""Через сутки дозор отпускает обещание и пишет причину в запись."""
	gateway = _FakeGateway()
	gateway.found = None
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	await service.promise(
		community_id,
		markup(),
		match_text="текст",
		when=datetime.now(UTC) - APPLY_MAX_AGE - timedelta(minutes=1),
	)
	# первый проход даёт обещанию его единственный шанс: приложение
	# могло быть выключено все сутки, и «срок вышел» без попытки —
	# это потеря кнопок молча
	await service.apply_due()
	assert len(gateway.lookups) == 1
	promises = await service.pending()
	assert promises[0].attempts == 1

	# шанс израсходован — теперь срок отпускает обещание, и Telegram
	# больше не тревожим
	await service.apply_due()
	promises = await service.pending()
	assert len(promises) == 1  # запись остаётся: человек увидит причину
	assert promises[0].error is not None and "срок обещания вышел" in promises[0].error
	assert len(gateway.lookups) == 1
	await service.apply_due()  # повторный проход молчит и не тревожит сеть
	assert len(gateway.lookups) == 1


def test_promise_expired_counts_from_publication_or_promise() -> None:
	"""Срок считается от выхода поста, а у поста «сейчас» — от обещания."""
	now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
	born = now - APPLY_MAX_AGE - timedelta(minutes=1)
	scheduled = _promise_dto(when=now - timedelta(hours=1), created_at=born)
	# пост вышел час назад: обещание молодое, хотя запись старая
	assert not promise_expired(scheduled, now)
	assert promise_expired(_promise_dto(when=born, created_at=born), now)
	# пост «сейчас»: срок идёт от самой записи
	assert promise_expired(_promise_dto(when=None, created_at=born), now)
	assert not promise_expired(_promise_dto(when=None, created_at=now), now)


async def test_watcher_drops_promise_when_post_is_gone(db: Database) -> None:
	"""Пост удалили из другого клиента — обещание снимается сразу.

	Раньше отказ «сообщения нет» был неотличим от «нет прав»: дозор
	считал его обычной неудачей и ходил за исчезнувшим постом сутки,
	раз в минуту дёргая бота.
	"""
	from pxcontrol.engine.telegram.bot_api import BotMessageGoneError

	gateway = _FakeGateway()
	gateway.edit_error = BotMessageGoneError("Сообщения уже нет.")
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	await service.promise(community_id, markup(), match_text="текст", message_id=42)

	assert await service.apply_due() == 0
	assert await service.pending() == []  # обещания больше нет


def test_promise_without_attempt_survives_deadline() -> None:
	"""Просрочка без единой попытки обещание не отпускает.

	Срок меряется настенным временем, а дозор работает, только пока
	запущено приложение: посты с кнопками на пятницу и выключенное
	до воскресенья приложение иначе лишились бы кнопок молча, ни разу
	не попробовав.
	"""
	now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
	born = now - APPLY_MAX_AGE - timedelta(days=2)
	assert not promise_expired(_promise_dto(when=born, created_at=born, attempts=0), now)
	assert promise_expired(_promise_dto(when=born, created_at=born, attempts=1), now)


async def test_watcher_uses_known_message_id(db: Database) -> None:
	"""У поста, который уже вышел, номер известен — искать нечего."""
	gateway = _FakeGateway()
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	await service.promise(community_id, markup(), match_text="текст", message_id=42)
	assert await service.apply_due() == 1
	assert gateway.lookups == []  # опознавать не нужно
	assert gateway.edits == [("-1001", 42, markup())]


async def test_flood_stops_the_pass(db: Database) -> None:
	"""Флуд-лимит прекращает проход: остальные обещания дождутся следующего."""
	gateway = _FakeGateway()
	gateway.edit_error = TelegramFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	for number in range(3):
		await service.promise(
			community_id, markup(), match_text=f"текст {number}", message_id=number + 1
		)
	assert await service.apply_due() == 0
	# попытка была одна: дальше дозор не пошёл
	assert len(gateway.edits) == 0
	attempts = [p.attempts for p in await service.pending()]
	assert sorted(attempts) == [0, 0, 0]


async def test_retarget_follows_scheduled_edit(db: Database) -> None:
	"""Правка отложки уводит обещание за собой: время и текст обновляются.

	Без этого дозор искал бы вчерашний текст на вчерашнее время — пост
	вышел бы без кнопок, хотя всё было в порядке.
	"""
	service = MarkupsService(db)
	community_id = await make_community(db)
	when = datetime.now(UTC) + timedelta(hours=1)
	await service.promise(
		community_id, markup(), match_text="было", when=when, scheduled_message_id=7
	)
	moved = when + timedelta(days=1)
	assert await service.retarget(community_id, 7, when=moved, match_text="стало") is True
	promise = (await service.pending())[0]
	assert promise.when == moved and promise.match_text == "стало"
	# у поста без кнопок переносить нечего — и это не ошибка
	assert await service.retarget(community_id, 999, when=moved) is False


async def test_retarget_ignores_empty_change(db: Database) -> None:
	"""Пустая правка ничего не трогает (нечего переносить)."""
	service = MarkupsService(db)
	community_id = await make_community(db)
	await service.promise(community_id, markup(), match_text="текст", scheduled_message_id=3)
	assert await service.retarget(community_id, 3) is False
	assert (await service.pending())[0].match_text == "текст"


async def test_watcher_takes_promise_after_send_now(db: Database) -> None:
	"""«Сейчас» у отложки делает обещание готовым к применению.

	Пост выходит немедленно, его номер меняется — дозор опознаёт пост
	по тексту и ставит кнопки уже на следующем проходе.
	"""
	gateway = _FakeGateway()
	service = MarkupsService(db, gateway)
	community_id = await make_ready_community(db)
	await service.promise(
		community_id,
		markup(),
		match_text="текст",
		when=datetime.now(UTC) + timedelta(hours=5),
		scheduled_message_id=11,
	)
	assert await service.apply_due() == 0  # до времени публикации — не трогаем
	# «Сейчас»: движок переносит обещание на текущий момент
	await service.retarget(community_id, 11, when=datetime.now(UTC))
	assert await service.apply_due() == 1
	assert gateway.edits == [("-1001", 777, markup())]


async def test_promised_ids_lists_scheduled_records(db: Database) -> None:
	"""Номера отложек с обещаниями — для пометки на вкладке «Отложено»."""
	service = MarkupsService(db)
	first = await make_community(db, "Первый", "-1001")
	second = await make_community(db, "Второй", "-1002")
	await service.promise(first, markup(), match_text="A", scheduled_message_id=10)
	await service.promise(first, markup(), match_text="B", scheduled_message_id=11)
	await service.promise(first, markup(), match_text="C", message_id=99)  # уже вышел
	await service.promise(second, markup(), match_text="D", scheduled_message_id=12)
	assert await service.promised_ids(first) == {10, 11}
	assert await service.promised_ids(second) == {12}
