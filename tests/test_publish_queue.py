"""Тесты очереди отправки: порядок, отмена, ошибки — без сети."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, PublishQueueItem, TgAccount
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.posts import (
	PostDraft,
	PostError,
	PostsService,
	ProgressCallback,
	PublishPlan,
)
from pxcontrol.engine.services.publish_queue import (
	CATCHUP_INTERVAL_S,
	PublishQueue,
	QueueItemDto,
)
from pxcontrol.engine.telegram.mtproto import UserbotScheduleFullError
from pxcontrol.engine.telegram.types import (
	TELEGRAM_MAX_SCHEDULED,
	MediaKind,
	OutgoingPost,
	TelegramFloodError,
)


class _SlowGateway:
	"""Подмена шлюза: отправка ждёт отмашки — как долгая загрузка видео."""

	def __init__(self) -> None:
		self.release = asyncio.Event()
		self.published: list[OutgoingPost] = []
		self.fail_texts: set[str] = set()

	def userbot_premium(self, account_id: int | None) -> bool:
		return False

	async def publish(
		self,
		account_id: int,
		chat_id: str,
		post: OutgoingPost,
		on_progress: ProgressCallback | None = None,
	) -> None:
		if on_progress is not None:
			on_progress(0.5)
		await self.release.wait()
		if post.text in self.fail_texts:
			raise PostError("Telegram отклонил отправку.")
		self.published.append(post)


async def _add_community(db: Database, tg_chat_id: str = "-1001", title: str = "Канал") -> int:
	"""Создаёт канал с userbot-админом (свой аккаунт), возвращает id."""
	async with db.session_factory() as session:
		account = TgAccount(label=f"@ub{tg_chat_id}", phone="+7900", session="s")
		session.add(account)
		await session.flush()
		community = Community(title=title, tg_chat_id=tg_chat_id, default_tg_account_id=account.id)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


#: Тип фабрики очередей из фикстуры make_queue (для аннотаций тестов).
QueueFactory = Callable[[_SlowGateway], PublishQueue]


@pytest.fixture
async def make_queue(db: Database) -> AsyncIterator[QueueFactory]:
	"""Фабрика очередей с гарантированной остановкой (ADR-0020).

	Тест, бросивший очередь с живыми фоновыми задачами, оставил бы их
	запросы к БД «в полёте» при закрытии цикла событий — поток соединения
	aiosqlite стрелял бы в уже закрытый цикл (см. диагностику в ADR-0020).
	"""
	created: list[PublishQueue] = []

	def factory(gateway: _SlowGateway) -> PublishQueue:
		queue = PublishQueue(PostsService(db, gateway), db)
		created.append(queue)
		return queue

	yield factory
	for queue in created:
		await queue.shutdown()


async def _wait_status(
	queue: PublishQueue, item_id: int, status: JobStatus, tries: int = 500
) -> QueueItemDto:
	"""Ждёт, пока элемент дойдёт до статуса (максимум ~5 секунд).

	Пауза настоящая (не ``sleep(0)``): запросы к SQLite выполняет
	поток aiosqlite, ему нужно реальное время.
	"""
	for _ in range(tries):
		items = {item.id: item for item in await queue.state()}
		if item_id in items and items[item_id].status is status:
			return items[item_id]
		await asyncio.sleep(0.01)
	raise AssertionError(f"элемент {item_id} не достиг статуса {status}")


async def _wait_progress(
	queue: PublishQueue, item_id: int, expected: float, tries: int = 500
) -> None:
	"""Ждёт, пока до элемента доедет доля прогресса загрузки."""
	for _ in range(tries):
		items = {item.id: item for item in await queue.state()}
		if item_id in items and items[item_id].progress == expected:
			return
		await asyncio.sleep(0.01)
	raise AssertionError(f"элемент {item_id} не получил прогресс {expected}")


async def test_enqueue_during_send_keeps_order(db: Database, make_queue: QueueFactory) -> None:
	"""Пока первый уходит, второй свободно встаёт в хвост; порядок сохраняется."""
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	first = await queue.enqueue(PostDraft(community_id, text="первый"))
	second = await queue.enqueue(PostDraft(community_id, text="второй"))
	await _wait_status(queue, first, JobStatus.RUNNING)
	await _wait_progress(queue, first, 0.5)  # прогресс доехал до состояния
	items = {item.id: item for item in await queue.state()}
	assert items[second].status is JobStatus.PENDING
	gateway.release.set()
	await _wait_status(queue, first, JobStatus.DONE)
	await _wait_status(queue, second, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["первый", "второй"]
	assert all(item.status.finished() for item in await queue.state())


async def test_cancel_pending_skips_send(db: Database, make_queue: QueueFactory) -> None:
	"""Отмена ожидающего: он не отправляется, остальные — по плану."""
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	first = await queue.enqueue(PostDraft(community_id, text="первый"))
	second = await queue.enqueue(PostDraft(community_id, text="второй"))
	await _wait_status(queue, first, JobStatus.RUNNING)
	await queue.cancel(second)
	gateway.release.set()
	await _wait_status(queue, first, JobStatus.DONE)
	cancelled = await _wait_status(queue, second, JobStatus.CANCELLED)
	assert cancelled.status is JobStatus.CANCELLED
	assert [post.text for post in gateway.published] == ["первый"]


async def test_cancel_active_moves_to_next(db: Database, make_queue: QueueFactory) -> None:
	"""Отмена отправляющегося обрывает загрузку; очередь идёт дальше."""
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	first = await queue.enqueue(PostDraft(community_id, text="первый"))
	second = await queue.enqueue(PostDraft(community_id, text="второй"))
	await _wait_status(queue, first, JobStatus.RUNNING)
	await queue.cancel(first)
	await _wait_status(queue, first, JobStatus.CANCELLED)
	await _wait_status(queue, second, JobStatus.RUNNING)
	gateway.release.set()
	await _wait_status(queue, second, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["второй"]


async def test_error_does_not_stop_queue(db: Database, make_queue: QueueFactory) -> None:
	"""Ошибка одного элемента фиксируется в нём и не роняет следующие."""
	gateway = _SlowGateway()
	gateway.release.set()  # отправка без задержки
	gateway.fail_texts = {"сбойный"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	bad = await queue.enqueue(PostDraft(community_id, text="сбойный"))
	good = await queue.enqueue(PostDraft(community_id, text="целый"))
	failed = await _wait_status(queue, bad, JobStatus.ERROR)
	assert failed.error is not None and "отклонил" in failed.error
	await _wait_status(queue, good, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["целый"]
	# ошибка висит в списке, пока её не уберут явно
	await queue.dismiss(bad)
	assert [item.id for item in await queue.state()] == [good]


async def test_enqueue_validates_immediately(db: Database, make_queue: QueueFactory) -> None:
	"""Негодный черновик отклоняется при постановке, а не при отправке."""
	queue = make_queue(_SlowGateway())
	community_id = await _add_community(db)
	with pytest.raises(PostError, match="пуст"):
		await queue.enqueue(PostDraft(community_id))
	with pytest.raises(PostError, match="Канал не найден"):
		await queue.enqueue(PostDraft(999, text="x"))
	assert await queue.state() == []


async def test_dto_titles_and_flags(db: Database, make_queue: QueueFactory, tmp_path: Path) -> None:
	"""Заголовок — имя файла (учитывая переименование) или начало текста."""
	gateway = _SlowGateway()  # отмашки нет — всё висит, удобно смотреть
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"v")
	await queue.enqueue(
		PostDraft(
			community_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
			rename_to="Новое имя.mp4",
		)
	)
	when = datetime.now(UTC) + timedelta(hours=1)
	await queue.enqueue(PostDraft(community_id, text="о" * 100, when=when))
	first, second = await queue.state()
	assert first.title == "Новое имя.mp4" and not first.scheduled
	assert first.when is None  # «сейчас» — интерфейс покажет это словом
	assert second.title == "о" * 59 + "…" and second.scheduled
	assert second.when == when  # момент публикации виден в карточке очереди
	assert second.community_title == "Канал"


async def test_retry_error_sends_again(db: Database, make_queue: QueueFactory) -> None:
	"""Повтор возвращает ошибочный элемент в очередь; вторая попытка уходит."""
	gateway = _SlowGateway()
	gateway.release.set()  # отправка без задержки
	gateway.fail_texts = {"сбойный"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="сбойный"))
	await _wait_status(queue, item, JobStatus.ERROR)
	gateway.fail_texts = set()  # «сеть починилась»
	await queue.retry(item)
	retried = {i.id: i for i in await queue.state()}[item]
	assert retried.error is None  # прежний текст ошибки снят
	await _wait_status(queue, item, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["сбойный"]


async def test_retry_validates_draft_again(
	db: Database, make_queue: QueueFactory, tmp_path: Path
) -> None:
	"""Повтор перепроверяет черновик: исчезнувший файл — ошибка, статус прежний."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"с файлом"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	attachment = tmp_path / "вложение.pdf"
	attachment.write_bytes(b"f")
	item = await queue.enqueue(
		PostDraft(
			community_id,
			text="с файлом",
			media_path=str(attachment),
			media_kind=MediaKind.DOCUMENT,
		)
	)
	failed = await _wait_status(queue, item, JobStatus.ERROR)
	attachment.unlink()  # файл пропал между попытками
	with pytest.raises(PostError, match="не найден"):
		await queue.retry(item)
	still = {i.id: i for i in await queue.state()}[item]
	assert still.status is JobStatus.ERROR
	assert still.error == failed.error  # прежний текст ошибки сохранён


async def test_retry_after_rename_uses_new_name(
	db: Database, make_queue: QueueFactory, tmp_path: Path
) -> None:
	"""Файл, переименованный неудачной попыткой, при повторе уходит как есть."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"с файлом"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	attachment = tmp_path / "старое.pdf"
	attachment.write_bytes(b"f")
	item = await queue.enqueue(
		PostDraft(
			community_id,
			text="с файлом",
			media_path=str(attachment),
			media_kind=MediaKind.DOCUMENT,
			rename_to="новое.pdf",
		)
	)
	await _wait_status(queue, item, JobStatus.ERROR)
	assert (tmp_path / "новое.pdf").is_file()  # попытка успела переименовать
	gateway.fail_texts = set()
	await queue.retry(item)
	await _wait_status(queue, item, JobStatus.DONE)
	published = gateway.published[0]
	assert published.media_path == str(tmp_path / "новое.pdf")


async def test_retry_ignores_unfinished(db: Database, make_queue: QueueFactory) -> None:
	"""Повтор действует только на ошибку: живой элемент не трогается."""
	gateway = _SlowGateway()  # отмашки нет — элемент висит в отправке
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="живой"))
	await _wait_status(queue, item, JobStatus.RUNNING)
	await queue.retry(item)
	sending = {i.id: i for i in await queue.state()}[item]
	assert sending.status is JobStatus.RUNNING


async def test_dismiss_ignores_unfinished(db: Database, make_queue: QueueFactory) -> None:
	"""Снять с показа можно только завершённый элемент."""
	queue = make_queue(_SlowGateway())
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="живой"))
	await queue.dismiss(item)
	assert [i.id for i in await queue.state()] == [item]


async def test_unexpected_error_shown_collapsed(db: Database, make_queue: QueueFactory) -> None:
	"""Карточка очереди показывает сводку, а не дамп (контракт errors.py).

	Мост интерфейса сворачивает недоменные исключения через user_message;
	очередь пишет текст в карточку сама и обязана делать то же.
	"""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	dump = "Traceback (most recent call last)\n" + "  строка дампа\n" * 40

	async def _boom(*_args: object, **_kwargs: object) -> None:
		raise RuntimeError(dump)

	gateway.publish = _boom  # type: ignore[method-assign]
	item_id = await queue.enqueue(PostDraft(community_id, text="x"))
	failed = await _wait_status(queue, item_id, JobStatus.ERROR)
	assert failed.error is not None
	assert "строка дампа" not in failed.error  # многострочный дамп не попал
	assert "Внутренняя ошибка" in failed.error


async def test_retry_resets_cancel_flag(db: Database, make_queue: QueueFactory) -> None:
	"""Повтор снимает застрявший флаг отмены.

	Флаг взводится, когда отмена совпала с завершением попытки ошибкой;
	без сброса остановка движка при следующей отправке была бы принята
	за отмену пользователем и подвесила бы shutdown.
	"""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"сбойный"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item_id = await queue.enqueue(PostDraft(community_id, text="сбойный"))
	await _wait_status(queue, item_id, JobStatus.ERROR)
	internal = next(item for item in queue._jobs.all() if item.id == item_id)  # noqa: SLF001
	internal.cancel_requested = True  # отмена пришла в момент ошибки
	gateway.fail_texts = set()
	await queue.retry(item_id)
	assert internal.cancel_requested is False
	await _wait_status(queue, item_id, JobStatus.DONE)


async def test_enqueue_many_keeps_order_and_sends_all(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Пакет черновиков ставится целиком и отправляется по порядку."""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	ids = await queue.enqueue_many(
		[
			PostDraft(community_id, text="первый"),
			PostDraft(community_id, text="второй"),
			PostDraft(community_id, text="третий"),
		]
	)
	assert ids == sorted(ids) and len(ids) == 3
	for item_id in ids:
		await _wait_status(queue, item_id, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["первый", "второй", "третий"]


async def test_enqueue_many_validates_before_adding(db: Database, make_queue: QueueFactory) -> None:
	"""Негодный черновик в середине пакета — отказ целиком, очередь пуста."""
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	with pytest.raises(PostError, match="пуст"):
		await queue.enqueue_many(
			[
				PostDraft(community_id, text="годный"),
				PostDraft(community_id, text=""),  # пустой пост — негодный
			]
		)
	assert await queue.state() == []
	with pytest.raises(PostError, match="Пакет пуст"):
		await queue.enqueue_many([])


# --- слоты отложек и персистентность (ADR-0016) -----------------------------


class _SlotGateway(_SlowGateway):
	"""Шлюз со слотами: get_scheduled отдаёт заданные занятые моменты."""

	def __init__(self) -> None:
		super().__init__()
		self.scheduled: list[datetime] = []
		self.slots_full_once = False  # разовая гонка SCHEDULE_TOO_MUCH

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[object]:
		from types import SimpleNamespace

		return [SimpleNamespace(scheduled_at=moment) for moment in self.scheduled]

	async def publish(
		self,
		account_id: int,
		chat_id: str,
		post: OutgoingPost,
		on_progress: ProgressCallback | None = None,
	) -> None:
		if self.slots_full_once:
			self.slots_full_once = False
			raise UserbotScheduleFullError("Все слоты отложенных сообщений канала заняты.")
		await super().publish(account_id, chat_id, post, on_progress)


def _future(minutes: int) -> datetime:
	return datetime.now(UTC) + timedelta(minutes=minutes)


async def test_scheduled_without_free_slot_waits(db: Database, make_queue: QueueFactory) -> None:
	"""Все 100 слотов заняты — отложенный ждёт; слот освободился — ушёл."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="хвост", when=_future(120)))
	await _wait_status(queue, item, JobStatus.WAITING)
	await queue.settle()  # внеплановая проверка слотов — завершена (ADR-0020)
	assert (await queue.state())[0].status is JobStatus.WAITING
	gateway.scheduled = gateway.scheduled[:-1]  # сервер опубликовал одну отложку
	await queue._release_slots()  # noqa: SLF001 — тик дозора без ожидания N минут
	await _wait_status(queue, item, JobStatus.DONE)
	assert gateway.published[0].when is not None


async def test_release_nearest_date_first(db: Database, make_queue: QueueFactory) -> None:
	"""Свободен один слот — уходит элемент с ближайшей датой, не первый."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED - 1)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	later, sooner = await queue.enqueue_many(
		[
			PostDraft(community_id, text="дальний", when=_future(3 * 24 * 60)),
			PostDraft(community_id, text="ближний", when=_future(24 * 60)),
		]
	)
	await _wait_status(queue, sooner, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["ближний"]
	assert (await _wait_status(queue, later, JobStatus.WAITING)) is not None


async def test_expired_when_publishes_now_without_slot(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Просроченное время — обычное сообщение: слота не ждёт, when=None."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="опоздал", when=_future(120)))
	await _wait_status(queue, item, JobStatus.WAITING)
	target = next(entry for entry in queue._jobs.all() if entry.id == item)  # noqa: SLF001
	# моделируем прошедшее время ожидания (без реального ожидания суток)
	target.draft = replace(target.draft, when=datetime.now(UTC) - timedelta(hours=1))
	await queue._release_slots()  # noqa: SLF001 — слоты по-прежнему заняты
	done = await _wait_status(queue, item, JobStatus.DONE)
	assert gateway.published[0].when is None  # ушёл обычным сообщением
	assert done.when is None  # снимок для интерфейса честен: не «отложка»


async def test_schedule_full_race_returns_to_waiting(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Гонка: слоты заняли руками между проверкой и отправкой — не ошибка."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.slots_full_once = True  # первый publish наткнётся на полный канал
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="гонка", when=_future(120)))
	for _ in range(200):  # первая попытка отправки съедает разовый отказ
		if not gateway.slots_full_once:
			break
		await asyncio.sleep(0.01)
	await _wait_status(queue, item, JobStatus.WAITING)  # не ERROR
	await queue._release_slots()  # noqa: SLF001 — повторная проверка слотов
	await _wait_status(queue, item, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["гонка"]


async def test_topic_persisted_and_restored(db: Database, make_queue: QueueFactory) -> None:
	"""Тема форума элемента очереди переживает перезапуск (ADR-0021)."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(
		PostDraft(community_id, text="в тему", when=_future(120), topic_id=7)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	async with db.session_factory() as session:
		row = await session.get(PublishQueueItem, item)
		assert row is not None and row.topic_id == 7
	await queue.shutdown()

	restarted = make_queue(gateway)  # «перезапуск приложения»
	await restarted.load()
	drafts = {i.id: i.draft for i in restarted._jobs.all()}  # noqa: SLF001 — восстановленный черновик
	assert drafts[item].topic_id == 7


async def test_queue_survives_restart(db: Database, make_queue: QueueFactory) -> None:
	"""Очередь восстанавливается из БД: статусы, ошибки и aware-времена."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.fail_texts = {"сбойный"}
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	bad = await queue.enqueue(PostDraft(community_id, text="сбойный"))
	waiting = await queue.enqueue(PostDraft(community_id, text="ждущий", when=_future(120)))
	await _wait_status(queue, bad, JobStatus.ERROR)
	await _wait_status(queue, waiting, JobStatus.WAITING)
	await queue.shutdown()

	restarted = make_queue(gateway)  # «перезапуск приложения»
	await restarted.load()
	items = {item.id: item for item in await restarted.state()}
	assert items[bad].status is JobStatus.ERROR
	assert items[bad].error is not None and "отклонил" in items[bad].error
	assert items[waiting].status is JobStatus.WAITING
	assert items[waiting].when is not None and items[waiting].when.tzinfo is not None


# --- папка очереди на диске (ADR-0016) --------------------------------------


def _media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	"""Подменяет корень media/ на временный; возвращает папку processed."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	processed = tmp_path / "media" / "processed" / "суб"
	processed.mkdir(parents=True)
	return processed


def _make_video(processed: Path, name: str = "ролик.mp4") -> Path:
	video = processed / name
	video.write_bytes(b"video")
	video.with_suffix(".png").write_bytes(b"png")
	return video


async def test_enqueue_stashes_file_and_cancel_returns_it(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Постановка уводит файл в папку очереди, отмена возвращает обратно."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(
		PostDraft(
			community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120)
		)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	queued = tmp_path / "media" / "queued" / "суб" / "ролик.mp4"
	assert queued.is_file() and queued.with_suffix(".png").is_file()
	assert not video.exists()  # из «Готовых видео» файл ушёл
	await queue.cancel(item)
	await _wait_status(queue, item, JobStatus.CANCELLED)
	assert video.is_file() and video.with_suffix(".png").is_file()
	assert not queued.exists()


async def test_sent_file_moves_from_queued_to_published(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Отправленный файл переезжает из папки очереди в опубликованные."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(
		PostDraft(community_id, media_path=str(video), media_kind=MediaKind.VIDEO)
	)
	await _wait_status(queue, item, JobStatus.DONE)
	published = tmp_path / "media" / "published" / "суб" / "ролик.mp4"
	assert published.is_file()
	assert not (tmp_path / "media" / "queued" / "суб" / "ролик.mp4").exists()
	assert not video.exists()


async def test_stash_collision_rejects_batch(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Файл-тёзка уже ждёт в очереди — постановка отклоняется целиком."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	await queue.enqueue(
		PostDraft(
			community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120)
		)
	)
	twin = _make_video(processed)  # обработали заново под тем же именем
	with pytest.raises(PostError, match="уже есть файл"):
		await queue.enqueue(
			PostDraft(
				community_id, media_path=str(twin), media_kind=MediaKind.VIDEO, when=_future(180)
			)
		)
	assert twin.is_file()  # отклонённый пакет не трогает диск
	assert len(await queue.state()) == 1


async def test_enqueue_rejects_non_video_from_processed(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Не-видео из папки результатов не ставится: конвейер очереди — только для видео."""
	processed = _media(tmp_path, monkeypatch)
	photo = processed / "кадр.png"
	photo.write_bytes(b"png")
	gateway = _SlowGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	with pytest.raises(PostError, match="только видео"):
		await queue.enqueue(
			PostDraft(community_id, media_path=str(photo), media_kind=MediaKind.PHOTO)
		)
	assert photo.is_file()  # файл остался в результатах
	assert await queue.state() == []  # постановка атомарна — очередь пуста


async def test_retry_expired_scheduled_publishes_now(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Повтор просроченного отложенного публикует «сейчас» (ADR-0016, «просрочка → сейчас»)."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"ночной"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="ночной"))
	await _wait_status(queue, item, JobStatus.ERROR)
	# смоделировать «ошибка ночью, повтор утром»: желаемый момент уже прошёл
	internal = next(i for i in queue._jobs.all() if i.id == item)  # noqa: SLF001
	internal.draft = replace(internal.draft, when=datetime.now(UTC) - timedelta(hours=8))
	gateway.fail_texts = set()  # «сеть починилась»
	await queue.retry(item)
	await _wait_status(queue, item, JobStatus.DONE)
	assert gateway.published[-1].when is None  # ушёл «сейчас», а не в прошлое


async def test_drop_community_removes_items_and_returns_files(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Снятие элементов канала: очередь пуста, файл вернулся в результаты."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(
		PostDraft(
			community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120)
		)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	assert not video.exists()  # файл ушёл в папку очереди
	await queue.drop_community(community_id)
	assert await queue.state() == []  # «зомби»-элементов в памяти нет
	assert video.is_file()  # файл вернулся в результаты


class _FloodOnceGateway(_SlowGateway):
	"""Первая отправка упирается во флуд-лимит, вторая проходит."""

	def __init__(self, seconds: int) -> None:
		super().__init__()
		self.seconds = seconds
		self.flooded = False

	async def publish(
		self,
		account_id: int,
		chat_id: str,
		post: OutgoingPost,
		on_progress: ProgressCallback | None = None,
	) -> None:
		if not self.flooded:
			self.flooded = True
			raise TelegramFloodError(
				f"Telegram просит подождать {self.seconds} с.", retry_after_s=self.seconds
			)
		await super().publish(account_id, chat_id, post, on_progress)


async def test_flood_waits_and_retries_instead_of_error(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Флуд-лимит — не исход элемента: очередь ждёт названный срок и повторяет."""
	gateway = _FloodOnceGateway(seconds=17)
	gateway.release.set()
	queue = make_queue(gateway)
	sleeps: list[float] = []

	async def _instant(seconds: float) -> None:
		sleeps.append(seconds)

	queue._sleep = _instant  # noqa: SLF001 — реальная пауза растянула бы тест
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="под флудом"))
	done = await _wait_status(queue, item, JobStatus.DONE)
	assert done.error is None and done.note is None  # ошибки не было
	assert sleeps == [17.0]  # ждали ровно срок, названный сервером
	assert [post.text for post in gateway.published] == ["под флудом"]


async def test_catchup_paces_expired_posts(db: Database, make_queue: QueueFactory) -> None:
	"""Догон щадящий: между просроченными постами — пауза, залпа нет."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	sleeps: list[float] = []

	async def _instant(seconds: float) -> None:
		sleeps.append(seconds)

	queue._sleep = _instant  # noqa: SLF001 — реальная пауза растянула бы тест
	community_id = await _add_community(db)
	first = await queue.enqueue(PostDraft(community_id, text="догон-1", when=_future(120)))
	second = await queue.enqueue(PostDraft(community_id, text="догон-2", when=_future(180)))
	await _wait_status(queue, first, JobStatus.WAITING)
	await _wait_status(queue, second, JobStatus.WAITING)
	past = datetime.now(UTC) - timedelta(hours=8)
	for entry in queue._jobs.all():  # noqa: SLF001 — смоделировать простой недели
		entry.draft = replace(entry.draft, when=past)
	await queue._release_slots()  # noqa: SLF001 — как при старте после простоя
	await _wait_status(queue, first, JobStatus.DONE)
	await _wait_status(queue, second, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["догон-1", "догон-2"]
	assert sleeps == [CATCHUP_INTERVAL_S]  # пауза между постами; после хвоста — нет


class _FloodOnReadGateway(_SlotGateway):
	"""Чтение отложек упирается во флуд-лимит у выбранных аккаунтов.

	Так ведёт себя дорожка аккаунта (ADR-0024): поймав лимит, она
	отказывает всем последующим операциям этого аккаунта — мгновенно
	и не обращаясь к Telegram.
	"""

	def __init__(self, flooded_accounts: set[int] | None = None) -> None:
		super().__init__()
		self.flooded_accounts = flooded_accounts
		self.read_calls = 0

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[object]:
		self.read_calls += 1
		if self.flooded_accounts is None or account_id in self.flooded_accounts:
			raise TelegramFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
		return []


async def test_flood_on_slot_check_isolates_account(db: Database, make_queue: QueueFactory) -> None:
	"""Флуд-лимит одного аккаунта не задевает каналы другого (ADR-0019).

	Дозор своего списка «провинившихся» не ведёт: лимит помнит дорожка
	аккаунта (ADR-0024) и отказывает его операциям сама, не обращаясь
	к Telegram. От дозора требуется другое — пережить отказ: каналы
	лимитированного аккаунта остаются ждать (а не падают в ошибку),
	каналы остальных аккаунтов проверяются и выпускаются.
	"""
	async with db.session_factory() as session:
		flooded_account = TgAccount(label="@ub1", phone="+7900", session="s")
		free_account = TgAccount(label="@ub2", phone="+7901", session="s")
		session.add_all([flooded_account, free_account])
		await session.flush()
		first = Community(
			title="Под лимитом", tg_chat_id="-1001", default_tg_account_id=flooded_account.id
		)
		other = Community(
			title="Свободный", tg_chat_id="-1002", default_tg_account_id=free_account.id
		)
		session.add_all([first, other])
		await session.commit()
		await session.refresh(first)
		await session.refresh(other)
		flooded_id = flooded_account.id
	gateway = _FloodOnReadGateway(flooded_accounts={flooded_id})
	queue = make_queue(gateway)
	a = await queue.enqueue(PostDraft(first.id, text="ждущий А", when=_future(120)))
	b = await queue.enqueue(PostDraft(other.id, text="ждущий Б", when=_future(180)))
	await _wait_status(queue, a, JobStatus.WAITING)
	await queue.settle()  # фоновые проверки постановки — завершены (ADR-0020)
	await queue._release_slots()  # noqa: SLF001 — тик дозора напрямую
	state = {item.id: item for item in await queue.state()}
	assert state[a].status is JobStatus.WAITING  # отказ — не ошибка элемента
	assert state[a].error is None
	# канал свободного аккаунта дозор проверил и выпустил: слоты есть
	assert state[b].status is not JobStatus.WAITING


# --- разделение подготовки и передачи (ADR-0020) -----------------------------


def _gate_prepare(queue: PublishQueue) -> tuple[asyncio.Event, asyncio.Event]:
	"""Задерживает подготовку публикации: (началась, отмашка продолжить).

	Моделирует окно, в котором отправляющийся элемент ещё не дошёл
	до сети: раньше отмена в этом окне рвала запрос к БД (ADR-0020).
	"""
	posts = queue._posts  # noqa: SLF001 — точка подмены, как gateway.publish
	original = posts.prepare_publish
	started = asyncio.Event()
	proceed = asyncio.Event()

	async def slow_prepare(draft: PostDraft) -> PublishPlan:
		started.set()
		await proceed.wait()
		return await original(draft)

	posts.prepare_publish = slow_prepare  # type: ignore[method-assign]
	return started, proceed


async def test_cancel_during_prepare_skips_network(db: Database, make_queue: QueueFactory) -> None:
	"""Отмена, пришедшая на подготовке: сеть не начинается, элемент отменён."""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	started, proceed = _gate_prepare(queue)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="отменят на подготовке"))
	await started.wait()  # воркер вошёл в подготовку, сети ещё нет
	await queue.cancel(item)  # активной задачи нет — сработает только флаг
	proceed.set()
	cancelled = await _wait_status(queue, item, JobStatus.CANCELLED)
	assert cancelled.status is JobStatus.CANCELLED
	assert gateway.published == []  # до шлюза отправка не дошла


async def test_shutdown_during_prepare_leaves_pending_row(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Остановка на подготовке: сети нет, строка pending уйдёт после рестарта."""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	started, proceed = _gate_prepare(queue)
	community_id = await _add_community(db)
	await queue.enqueue(PostDraft(community_id, text="переживёт рестарт"))
	await started.wait()
	shutdown = asyncio.create_task(queue.shutdown())
	await asyncio.sleep(0)  # первый же шаг shutdown взводит событие остановки
	proceed.set()
	await shutdown
	assert gateway.published == []  # сеть не начиналась
	async with db.session_factory() as session:
		rows = (await session.execute(select(PublishQueueItem))).scalars().all()
	assert [row.status for row in rows] == ["pending"]  # уйдёт после перезапуска


async def _wait_queue_empty(queue: PublishQueue, tries: int = 500) -> None:
	"""Ждёт, пока очередь опустеет (снятие по исходу — асинхронное)."""
	for _ in range(tries):
		if await queue.state() == []:
			return
		await asyncio.sleep(0.01)
	raise AssertionError("очередь не опустела")


async def test_drop_community_finishes_active_item(db: Database, make_queue: QueueFactory) -> None:
	"""Удаление канала при активной отправке: элемент доводится до снятия.

	Гарантия «зомби-элементов нет» — уровня движка: карточка исчезает
	по исходу сама, без участия панели интерфейса.
	"""
	gateway = _SlowGateway()  # без release: отправка висит, как долгая загрузка
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="в полёте"))
	await _wait_status(queue, item, JobStatus.RUNNING)
	await queue.drop_community(community_id)
	await _wait_queue_empty(queue)  # исход записан, элемент снят с показа
	assert gateway.published == []  # пост не ушёл
	await queue.shutdown()


async def test_drop_community_during_prepare_cancels_not_errors(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Гонка «канал удалён во время подготовки»: исход — отмена, не ошибка.

	Порядок Engine.delete_community: сначала drop_community, затем удаление
	строки канала; подготовка, упавшая «Канал не найден» на фоне
	взведённой отмены, не должна хоронить элемент в ERROR.
	"""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = make_queue(gateway)
	started, proceed = _gate_prepare(queue)
	community_id = await _add_community(db)
	await queue.enqueue(PostDraft(community_id, text="канал исчезнет"))
	await started.wait()  # воркер в подготовке, активной задачи нет
	await queue.drop_community(community_id)  # взводит флаг и пометку снятия
	async with db.session_factory() as session:  # Engine удаляет строку канала
		await session.execute(delete(Community).where(Community.id == community_id))
		await session.commit()
	proceed.set()  # подготовка продолжится и упадёт «Канал не найден»
	await _wait_queue_empty(queue)  # исход — CANCELLED и снятие, не ERROR
	assert gateway.published == []
	await queue.shutdown()


# --- правка элемента очереди -------------------------------------------------


async def _statuses(queue: PublishQueue) -> dict[int, QueueItemDto]:
	"""Снимок очереди по идентификаторам элементов."""
	return {item.id: item for item in await queue.state()}


async def test_edit_fixes_failed_item_and_sends_it(db: Database, make_queue: QueueFactory) -> None:
	"""Правка поста с ошибкой возвращает его в работу с чистым исходом."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"сбойный"}
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="сбойный"))
	await _wait_status(queue, item, JobStatus.ERROR)
	gateway.fail_texts = set()
	await queue.edit(item, PostDraft(community_id, text="исправленный"))
	await _wait_status(queue, item, JobStatus.DONE)
	assert [post.text for post in gateway.published] == ["исправленный"]
	assert (await _statuses(queue))[item].error is None


async def test_edit_keeps_row_in_sync_with_new_draft(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Правка переписывает строку очереди: перезапуск увидит новый черновик."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="было", when=_future(120)))
	await _wait_status(queue, item, JobStatus.WAITING)
	when = _future(300)
	await queue.edit(item, PostDraft(community_id, text="стало", when=when, topic_id=7))
	async with db.session_factory() as session:
		row = (
			await session.execute(select(PublishQueueItem).where(PublishQueueItem.id == item))
		).scalar_one()
		assert row.text == "стало"
		assert row.topic_id == 7
		assert row.status == JobStatus.WAITING.value
		assert row.when.replace(tzinfo=UTC) == when.replace(microsecond=row.when.microsecond)


async def test_edit_replaces_media_file(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Замена вложения: новый файл уходит в очередь, прежний — в результаты."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	replacement = _make_video(processed, "другой.mp4")
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	when = _future(120)
	item = await queue.enqueue(
		PostDraft(community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=when)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	queued_root = tmp_path / "media" / "queued" / "суб"
	await queue.edit(
		item,
		PostDraft(
			community_id,
			media_path=str(replacement),
			media_kind=MediaKind.VIDEO,
			when=when,
		),
	)
	assert (queued_root / "другой.mp4").is_file()
	assert (queued_root / "другой.png").is_file()  # кадр-превью едет следом
	assert not (queued_root / "ролик.mp4").exists()
	assert video.is_file()  # прежний вернулся в «Готовые видео»


async def test_edit_drops_media_and_returns_file(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Удаление вложения делает пост текстовым, файл возвращается в результаты."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	when = _future(120)
	item = await queue.enqueue(
		PostDraft(community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=when)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	await queue.edit(item, PostDraft(community_id, text="теперь просто текст", when=when))
	assert video.is_file() and video.with_suffix(".png").is_file()
	assert not (tmp_path / "media" / "queued" / "суб" / "ролик.mp4").exists()
	draft = await queue.get_draft(item)
	assert draft.media_path is None and draft.media_kind is MediaKind.NONE


async def test_edit_adds_media_to_text_post(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Текстовому посту можно добавить файл — он уезжает в папку очереди."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	when = _future(120)
	item = await queue.enqueue(PostDraft(community_id, text="подпись", when=when))
	await _wait_status(queue, item, JobStatus.WAITING)
	await queue.edit(
		item,
		PostDraft(
			community_id,
			text="подпись",
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
			when=when,
		),
	)
	assert (tmp_path / "media" / "queued" / "суб" / "ролик.mp4").is_file()
	assert not video.exists()


async def test_edit_switches_between_now_and_scheduled(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Время решает статус: «сейчас» — в отправку, отложенное — ждать слота."""
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	held = await queue.enqueue(PostDraft(community_id, text="держит воркер"))
	await _wait_status(queue, held, JobStatus.RUNNING)
	item = await queue.enqueue(PostDraft(community_id, text="пост"))
	assert (await _statuses(queue))[item].status is JobStatus.PENDING
	await queue.edit(item, PostDraft(community_id, text="пост", when=_future(120)))
	await queue.settle()
	assert (await _statuses(queue))[item].status is JobStatus.WAITING
	await queue.edit(item, PostDraft(community_id, text="пост"))
	assert (await _statuses(queue))[item].status is JobStatus.PENDING
	gateway.release.set()
	await _wait_status(queue, item, JobStatus.DONE)
	assert gateway.published[-1].when is None


async def test_worker_skips_item_while_edit_is_saving(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Пока правка сохраняется, воркер не забирает элемент в отправку.

	Иначе в Telegram уехал бы наполовину применённый черновик: файл уже
	перенесён, а текст и время — ещё прежние.
	"""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	held = await queue.enqueue(PostDraft(community_id, text="держит воркер"))
	await _wait_status(queue, held, JobStatus.RUNNING)
	item = await queue.enqueue(PostDraft(community_id, text="правится"))
	entered = asyncio.Event()
	proceed = asyncio.Event()
	original = queue._posts.stash_for_queue  # noqa: SLF001 — подмена долгого переноса

	async def slow_stash(media_path: str, media_kind: MediaKind) -> str:
		entered.set()
		await proceed.wait()
		return await original(media_path, media_kind)

	monkeypatch.setattr(queue._posts, "stash_for_queue", slow_stash)  # noqa: SLF001
	editing = asyncio.create_task(
		queue.edit(
			item,
			PostDraft(
				community_id,
				text="правится",
				media_path=str(video),
				media_kind=MediaKind.VIDEO,
			),
		)
	)
	await entered.wait()
	gateway.release.set()  # воркер дописывает первый и идёт за следующим
	await _wait_status(queue, held, JobStatus.DONE)
	assert (await _statuses(queue))[item].status is JobStatus.PENDING
	proceed.set()
	await editing
	await _wait_status(queue, item, JobStatus.DONE)
	assert [post.media_path for post in gateway.published][-1] is not None


async def test_edit_rejects_sending_item(db: Database, make_queue: QueueFactory) -> None:
	"""Отправляющийся пост не правится — сначала отмена."""
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="в полёте"))
	await _wait_status(queue, item, JobStatus.RUNNING)
	with pytest.raises(PostError, match="уже отправляется"):
		await queue.edit(item, PostDraft(community_id, text="поздно"))
	with pytest.raises(PostError, match="уже отправляется"):
		await queue.get_draft(item)
	gateway.release.set()


async def test_edit_rejects_community_change(db: Database, make_queue: QueueFactory) -> None:
	"""Канал-получатель правкой не меняется: у другого канала свои правила."""
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	other_id = await _add_community(db, tg_chat_id="-1002", title="Другой")
	item = await queue.enqueue(PostDraft(community_id, text="пост", when=_future(120)))
	await _wait_status(queue, item, JobStatus.WAITING)
	with pytest.raises(PostError, match="не меняется"):
		await queue.edit(item, PostDraft(other_id, text="пост", when=_future(120)))
	assert (await queue.get_draft(item)).community_id == community_id


async def test_edit_rejects_pipeline_file_as_document(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Файл конвейера обработки нельзя переобъявить документом (ADR-0016)."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	when = _future(120)
	item = await queue.enqueue(
		PostDraft(community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=when)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	queued = tmp_path / "media" / "queued" / "суб" / "ролик.mp4"
	with pytest.raises(PostError, match="только видео"):
		await queue.edit(
			item,
			PostDraft(
				community_id, media_path=str(queued), media_kind=MediaKind.DOCUMENT, when=when
			),
		)
	assert queued.is_file()  # отклонённая правка не трогает диск
	assert (await queue.get_draft(item)).media_kind is MediaKind.VIDEO


async def test_edit_rejects_empty_draft_and_keeps_item(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Негодный черновик отклоняется, прежний остаётся в силе."""
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="было", when=_future(120)))
	await _wait_status(queue, item, JobStatus.WAITING)
	with pytest.raises(PostError, match="пуст"):
		await queue.edit(item, PostDraft(community_id, when=_future(120)))
	assert (await queue.get_draft(item)).text == "было"
	assert (await _statuses(queue))[item].status is JobStatus.WAITING


async def test_get_draft_reports_missing_item(db: Database, make_queue: QueueFactory) -> None:
	"""Элемента нет — понятный отказ вместо пустоты."""
	queue = make_queue(_SlowGateway())
	with pytest.raises(PostError, match="не найден"):
		await queue.get_draft(404)


async def test_enqueue_rejects_text_over_community_limit(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Слишком длинный пост не попадает в очередь: отказ на постановке.

	Иначе он ушёл бы в очередь и упал сырой ошибкой Telegram уже при
	отправке — пользователь увидел бы её в карточке, а не в форме.
	"""
	queue = make_queue(_SlowGateway())
	community_id = await _add_community(db)
	with pytest.raises(PostError, match="Текст поста длиннее"):
		await queue.enqueue(PostDraft(community_id, text="я" * 4097))
	assert await queue.state() == []


async def test_edit_rejects_text_over_community_limit(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Правка тоже сверяется с пределом канала, а не только постановка."""
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(PostDraft(community_id, text="было", when=_future(120)))
	await _wait_status(queue, item, JobStatus.WAITING)
	with pytest.raises(PostError, match="Текст поста длиннее"):
		await queue.edit(item, PostDraft(community_id, text="я" * 4097, when=_future(120)))
	assert (await queue.get_draft(item)).text == "было"


async def test_state_carries_media_path_from_queue_folder(
	db: Database, make_queue: QueueFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Снимок очереди несёт путь вложения — карточка даёт по нему просмотр.

	Путь — уже после переезда в папку очереди (ADR-0016): пока пост ждёт
	слота, файла на прежнем месте нет, и смотреть надо тот, что уйдёт.
	"""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item = await queue.enqueue(
		PostDraft(
			community_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120)
		)
	)
	await _wait_status(queue, item, JobStatus.WAITING)
	shown = (await _statuses(queue))[item]
	assert shown.media_path == str(tmp_path / "media" / "queued" / "суб" / "ролик.mp4")
	text_item = await queue.enqueue(PostDraft(community_id, text="без файла", when=_future(180)))
	assert (await _statuses(queue))[text_item].media_path is None


async def test_cancel_during_file_settling_keeps_the_post_published(
	db: Database, make_queue: QueueFactory
) -> None:
	"""Отмена в момент раскладки файлов не объявляет пост отменённым.

	Раскладка идёт после того, как задача передачи отцеплена: пост
	уже в канале, отменять нечего. Раньше этот шаг жил внутри
	отменяемой задачи — отмена возвращала файл опубликованного поста
	в результаты навстречу копирующему потоку, а карточка показывала
	«отменено» вместо «отправлено».
	"""
	gateway = _SlowGateway()
	queue = make_queue(gateway)
	community_id = await _add_community(db)
	item_id = await queue.enqueue(PostDraft(community_id, text="уже в канале"))
	settled = asyncio.Event()

	async def _settle_and_cancel(plan: PublishPlan) -> None:
		# человек жмёт «Отменить» ровно в этот момент
		await queue.cancel(item_id)
		settled.set()

	queue._posts.settle_published = _settle_and_cancel  # type: ignore[method-assign]  # noqa: SLF001
	gateway.release.set()
	await asyncio.wait_for(settled.wait(), timeout=5)
	item = await _wait_status(queue, item_id, JobStatus.DONE)

	assert len(gateway.published) == 1  # пост ушёл
	assert item.error is None  # и засчитан отправленным, а не отменённым
	async with db.session_factory() as session:
		rows = (await session.execute(select(PublishQueueItem))).scalars().all()
	assert rows == []  # строка удалена — повтора после перезапуска не будет
