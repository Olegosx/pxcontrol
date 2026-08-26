"""Тесты очереди отправки: порядок, отмена, ошибки — без сети."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Channel
from pxcontrol.engine.services.posts import (
	PostDraft,
	PostError,
	PostsService,
	ProgressCallback,
)
from pxcontrol.engine.services.publish_queue import (
	PublishQueue,
	QueueItemDto,
	QueueItemStatus,
)
from pxcontrol.engine.telegram.mtproto import UserbotScheduleFullError
from pxcontrol.engine.telegram.types import TELEGRAM_MAX_SCHEDULED, MediaKind, OutgoingPost


class _SlowGateway:
	"""Подмена шлюза: отправка ждёт отмашки — как долгая загрузка видео."""

	def __init__(self) -> None:
		self.release = asyncio.Event()
		self.published: list[OutgoingPost] = []
		self.fail_texts: set[str] = set()

	def userbot_premium(self) -> bool:
		return False

	async def publish(
		self,
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


async def _add_channel(db: Database) -> int:
	"""Создаёт канал с userbot-админом, возвращает id."""
	async with db.session_factory() as session:
		channel = Channel(title="Канал", tg_chat_id="-1001", userbot_admin=True)
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
		return channel.id


def _queue(db: Database, gateway: _SlowGateway) -> PublishQueue:
	return PublishQueue(PostsService(db, gateway), db)


async def _wait_status(
	queue: PublishQueue, item_id: int, status: QueueItemStatus, tries: int = 500
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


async def test_enqueue_during_send_keeps_order(db: Database) -> None:
	"""Пока первый уходит, второй свободно встаёт в хвост; порядок сохраняется."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	first = await queue.enqueue(PostDraft(channel_id, text="первый"))
	second = await queue.enqueue(PostDraft(channel_id, text="второй"))
	await _wait_status(queue, first, QueueItemStatus.SENDING)
	await _wait_progress(queue, first, 0.5)  # прогресс доехал до состояния
	items = {item.id: item for item in await queue.state()}
	assert items[second].status is QueueItemStatus.PENDING
	gateway.release.set()
	await _wait_status(queue, first, QueueItemStatus.DONE)
	await _wait_status(queue, second, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["первый", "второй"]
	assert not await queue.has_unfinished()


async def test_cancel_pending_skips_send(db: Database) -> None:
	"""Отмена ожидающего: он не отправляется, остальные — по плану."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	first = await queue.enqueue(PostDraft(channel_id, text="первый"))
	second = await queue.enqueue(PostDraft(channel_id, text="второй"))
	await _wait_status(queue, first, QueueItemStatus.SENDING)
	await queue.cancel(second)
	gateway.release.set()
	await _wait_status(queue, first, QueueItemStatus.DONE)
	cancelled = await _wait_status(queue, second, QueueItemStatus.CANCELLED)
	assert cancelled.status is QueueItemStatus.CANCELLED
	assert [post.text for post in gateway.published] == ["первый"]


async def test_cancel_active_moves_to_next(db: Database) -> None:
	"""Отмена отправляющегося обрывает загрузку; очередь идёт дальше."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	first = await queue.enqueue(PostDraft(channel_id, text="первый"))
	second = await queue.enqueue(PostDraft(channel_id, text="второй"))
	await _wait_status(queue, first, QueueItemStatus.SENDING)
	await queue.cancel(first)
	await _wait_status(queue, first, QueueItemStatus.CANCELLED)
	await _wait_status(queue, second, QueueItemStatus.SENDING)
	gateway.release.set()
	await _wait_status(queue, second, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["второй"]


async def test_error_does_not_stop_queue(db: Database) -> None:
	"""Ошибка одного элемента фиксируется в нём и не роняет следующие."""
	gateway = _SlowGateway()
	gateway.release.set()  # отправка без задержки
	gateway.fail_texts = {"сбойный"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	bad = await queue.enqueue(PostDraft(channel_id, text="сбойный"))
	good = await queue.enqueue(PostDraft(channel_id, text="целый"))
	failed = await _wait_status(queue, bad, QueueItemStatus.ERROR)
	assert failed.error is not None and "отклонил" in failed.error
	await _wait_status(queue, good, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["целый"]
	# ошибка висит в списке, пока её не уберут явно
	await queue.dismiss(bad)
	assert [item.id for item in await queue.state()] == [good]


async def test_enqueue_validates_immediately(db: Database) -> None:
	"""Негодный черновик отклоняется при постановке, а не при отправке."""
	queue = _queue(db, _SlowGateway())
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="пуст"):
		await queue.enqueue(PostDraft(channel_id))
	with pytest.raises(PostError, match="Канал не найден"):
		await queue.enqueue(PostDraft(999, text="x"))
	assert await queue.state() == []


async def test_dto_titles_and_flags(db: Database, tmp_path: Path) -> None:
	"""Заголовок — имя файла (учитывая переименование) или начало текста."""
	gateway = _SlowGateway()  # отмашки нет — всё висит, удобно смотреть
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	video = tmp_path / "ролик.mp4"
	video.write_bytes(b"v")
	await queue.enqueue(
		PostDraft(
			channel_id,
			media_path=str(video),
			media_kind=MediaKind.VIDEO,
			rename_to="Новое имя.mp4",
		)
	)
	when = datetime.now(UTC) + timedelta(hours=1)
	await queue.enqueue(PostDraft(channel_id, text="о" * 100, when=when))
	first, second = await queue.state()
	assert first.title == "Новое имя.mp4" and not first.scheduled
	assert first.when is None  # «сейчас» — интерфейс покажет это словом
	assert second.title == "о" * 59 + "…" and second.scheduled
	assert second.when == when  # момент публикации виден в карточке очереди
	assert second.channel_title == "Канал"
	assert await queue.has_unfinished()
	await queue.shutdown()  # гасим воркер с висящей отправкой


async def test_retry_error_sends_again(db: Database) -> None:
	"""Повтор возвращает ошибочный элемент в очередь; вторая попытка уходит."""
	gateway = _SlowGateway()
	gateway.release.set()  # отправка без задержки
	gateway.fail_texts = {"сбойный"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="сбойный"))
	await _wait_status(queue, item, QueueItemStatus.ERROR)
	gateway.fail_texts = set()  # «сеть починилась»
	await queue.retry(item)
	retried = {i.id: i for i in await queue.state()}[item]
	assert retried.error is None  # прежний текст ошибки снят
	await _wait_status(queue, item, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["сбойный"]


async def test_retry_validates_draft_again(db: Database, tmp_path: Path) -> None:
	"""Повтор перепроверяет черновик: исчезнувший файл — ошибка, статус прежний."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"с файлом"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	attachment = tmp_path / "вложение.pdf"
	attachment.write_bytes(b"f")
	item = await queue.enqueue(
		PostDraft(
			channel_id,
			text="с файлом",
			media_path=str(attachment),
			media_kind=MediaKind.DOCUMENT,
		)
	)
	failed = await _wait_status(queue, item, QueueItemStatus.ERROR)
	attachment.unlink()  # файл пропал между попытками
	with pytest.raises(PostError, match="не найден"):
		await queue.retry(item)
	still = {i.id: i for i in await queue.state()}[item]
	assert still.status is QueueItemStatus.ERROR
	assert still.error == failed.error  # прежний текст ошибки сохранён


async def test_retry_after_rename_uses_new_name(db: Database, tmp_path: Path) -> None:
	"""Файл, переименованный неудачной попыткой, при повторе уходит как есть."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"с файлом"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	attachment = tmp_path / "старое.pdf"
	attachment.write_bytes(b"f")
	item = await queue.enqueue(
		PostDraft(
			channel_id,
			text="с файлом",
			media_path=str(attachment),
			media_kind=MediaKind.DOCUMENT,
			rename_to="новое.pdf",
		)
	)
	await _wait_status(queue, item, QueueItemStatus.ERROR)
	assert (tmp_path / "новое.pdf").is_file()  # попытка успела переименовать
	gateway.fail_texts = set()
	await queue.retry(item)
	await _wait_status(queue, item, QueueItemStatus.DONE)
	published = gateway.published[0]
	assert published.media_path == str(tmp_path / "новое.pdf")


async def test_retry_ignores_unfinished(db: Database) -> None:
	"""Повтор действует только на ошибку: живой элемент не трогается."""
	gateway = _SlowGateway()  # отмашки нет — элемент висит в отправке
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="живой"))
	await _wait_status(queue, item, QueueItemStatus.SENDING)
	await queue.retry(item)
	sending = {i.id: i for i in await queue.state()}[item]
	assert sending.status is QueueItemStatus.SENDING
	await queue.shutdown()


async def test_dismiss_ignores_unfinished(db: Database) -> None:
	"""Снять с показа можно только завершённый элемент."""
	queue = _queue(db, _SlowGateway())
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="живой"))
	await queue.dismiss(item)
	assert [i.id for i in await queue.state()] == [item]
	await queue.shutdown()


async def test_unexpected_error_shown_collapsed(db: Database) -> None:
	"""Карточка очереди показывает сводку, а не дамп (контракт errors.py).

	Мост интерфейса сворачивает недоменные исключения через user_message;
	очередь пишет текст в карточку сама и обязана делать то же.
	"""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	dump = "Traceback (most recent call last)\n" + "  строка дампа\n" * 40

	async def _boom(*_args: object, **_kwargs: object) -> None:
		raise RuntimeError(dump)

	gateway.publish = _boom  # type: ignore[method-assign]
	item_id = await queue.enqueue(PostDraft(channel_id, text="x"))
	failed = await _wait_status(queue, item_id, QueueItemStatus.ERROR)
	assert failed.error is not None
	assert "строка дампа" not in failed.error  # многострочный дамп не попал
	assert "Внутренняя ошибка" in failed.error


async def test_retry_resets_cancel_flag(db: Database) -> None:
	"""Повтор снимает застрявший флаг отмены.

	Флаг взводится, когда отмена совпала с завершением попытки ошибкой;
	без сброса остановка движка при следующей отправке была бы принята
	за отмену пользователем и подвесила бы shutdown.
	"""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"сбойный"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item_id = await queue.enqueue(PostDraft(channel_id, text="сбойный"))
	await _wait_status(queue, item_id, QueueItemStatus.ERROR)
	internal = next(item for item in queue._items if item.id == item_id)  # noqa: SLF001
	internal.cancel_requested = True  # отмена пришла в момент ошибки
	gateway.fail_texts = set()
	await queue.retry(item_id)
	assert internal.cancel_requested is False
	await _wait_status(queue, item_id, QueueItemStatus.DONE)


async def test_enqueue_many_keeps_order_and_sends_all(db: Database) -> None:
	"""Пакет черновиков ставится целиком и отправляется по порядку."""
	gateway = _SlowGateway()
	gateway.release.set()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	ids = await queue.enqueue_many(
		[
			PostDraft(channel_id, text="первый"),
			PostDraft(channel_id, text="второй"),
			PostDraft(channel_id, text="третий"),
		]
	)
	assert ids == sorted(ids) and len(ids) == 3
	for item_id in ids:
		await _wait_status(queue, item_id, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["первый", "второй", "третий"]


async def test_enqueue_many_validates_before_adding(db: Database) -> None:
	"""Негодный черновик в середине пакета — отказ целиком, очередь пуста."""
	gateway = _SlowGateway()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="пуст"):
		await queue.enqueue_many(
			[
				PostDraft(channel_id, text="годный"),
				PostDraft(channel_id, text=""),  # пустой пост — негодный
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

	async def get_scheduled(self, chat_id: str) -> list[object]:
		from types import SimpleNamespace

		return [SimpleNamespace(scheduled_at=moment) for moment in self.scheduled]

	async def publish(
		self,
		chat_id: str,
		post: OutgoingPost,
		on_progress: ProgressCallback | None = None,
	) -> None:
		if self.slots_full_once:
			self.slots_full_once = False
			raise UserbotScheduleFullError("Все слоты отложенных сообщений канала заняты.")
		await super().publish(chat_id, post, on_progress)


def _future(minutes: int) -> datetime:
	return datetime.now(UTC) + timedelta(minutes=minutes)


async def test_scheduled_without_free_slot_waits(db: Database) -> None:
	"""Все 100 слотов заняты — отложенный ждёт; слот освободился — ушёл."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="хвост", when=_future(120)))
	await _wait_status(queue, item, QueueItemStatus.WAITING)
	await asyncio.sleep(0.05)  # внеплановая проверка слотов успевает пройти
	assert (await queue.state())[0].status is QueueItemStatus.WAITING
	gateway.scheduled = gateway.scheduled[:-1]  # сервер опубликовал одну отложку
	await queue._release_slots()  # noqa: SLF001 — тик дозора без ожидания N минут
	await _wait_status(queue, item, QueueItemStatus.DONE)
	assert gateway.published[0].when is not None
	await queue.shutdown()


async def test_release_nearest_date_first(db: Database) -> None:
	"""Свободен один слот — уходит элемент с ближайшей датой, не первый."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED - 1)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	later, sooner = await queue.enqueue_many(
		[
			PostDraft(channel_id, text="дальний", when=_future(3 * 24 * 60)),
			PostDraft(channel_id, text="ближний", when=_future(24 * 60)),
		]
	)
	await _wait_status(queue, sooner, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["ближний"]
	assert (await _wait_status(queue, later, QueueItemStatus.WAITING)) is not None
	await queue.shutdown()


async def test_expired_when_publishes_now_without_slot(db: Database) -> None:
	"""Просроченное время — обычное сообщение: слота не ждёт, when=None."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="опоздал", when=_future(120)))
	await _wait_status(queue, item, QueueItemStatus.WAITING)
	target = next(entry for entry in queue._items if entry.id == item)  # noqa: SLF001
	# моделируем прошедшее время ожидания (без реального ожидания суток)
	target.draft = replace(target.draft, when=datetime.now(UTC) - timedelta(hours=1))
	await queue._release_slots()  # noqa: SLF001 — слоты по-прежнему заняты
	done = await _wait_status(queue, item, QueueItemStatus.DONE)
	assert gateway.published[0].when is None  # ушёл обычным сообщением
	assert done.when is None  # снимок для интерфейса честен: не «отложка»
	await queue.shutdown()


async def test_schedule_full_race_returns_to_waiting(db: Database) -> None:
	"""Гонка: слоты заняли руками между проверкой и отправкой — не ошибка."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.slots_full_once = True  # первый publish наткнётся на полный канал
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="гонка", when=_future(120)))
	for _ in range(200):  # первая попытка отправки съедает разовый отказ
		if not gateway.slots_full_once:
			break
		await asyncio.sleep(0.01)
	await _wait_status(queue, item, QueueItemStatus.WAITING)  # не ERROR
	await queue._release_slots()  # noqa: SLF001 — повторная проверка слотов
	await _wait_status(queue, item, QueueItemStatus.DONE)
	assert [post.text for post in gateway.published] == ["гонка"]
	await queue.shutdown()


async def test_queue_survives_restart(db: Database) -> None:
	"""Очередь восстанавливается из БД: статусы, ошибки и aware-времена."""
	gateway = _SlotGateway()
	gateway.release.set()
	gateway.fail_texts = {"сбойный"}
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	bad = await queue.enqueue(PostDraft(channel_id, text="сбойный"))
	waiting = await queue.enqueue(PostDraft(channel_id, text="ждущий", when=_future(120)))
	await _wait_status(queue, bad, QueueItemStatus.ERROR)
	await _wait_status(queue, waiting, QueueItemStatus.WAITING)
	await queue.shutdown()

	restarted = _queue(db, gateway)  # «перезапуск приложения»
	await restarted.load()
	items = {item.id: item for item in await restarted.state()}
	assert items[bad].status is QueueItemStatus.ERROR
	assert items[bad].error is not None and "отклонил" in items[bad].error
	assert items[waiting].status is QueueItemStatus.WAITING
	assert items[waiting].when is not None and items[waiting].when.tzinfo is not None
	await restarted.shutdown()


async def test_exit_confirmation_only_for_active_send(db: Database) -> None:
	"""Ждущие выход не задерживают (очередь персистентная) — только отправка."""
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	await queue.enqueue(PostDraft(channel_id, text="ждущий", when=_future(120)))
	assert await queue.has_unfinished() is False  # ждёт слота — не задержка
	sending = await queue.enqueue(PostDraft(channel_id, text="активный"))
	await _wait_status(queue, sending, QueueItemStatus.SENDING)
	assert await queue.has_unfinished() is True  # обрыв загрузки — вопрос
	gateway.release.set()
	await _wait_status(queue, sending, QueueItemStatus.DONE)
	await queue.shutdown()


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
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Постановка уводит файл в папку очереди, отмена возвращает обратно."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(
		PostDraft(channel_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120))
	)
	await _wait_status(queue, item, QueueItemStatus.WAITING)
	queued = tmp_path / "media" / "queued" / "суб" / "ролик.mp4"
	assert queued.is_file() and queued.with_suffix(".png").is_file()
	assert not video.exists()  # из «Готовых видео» файл ушёл
	await queue.cancel(item)
	await _wait_status(queue, item, QueueItemStatus.CANCELLED)
	assert video.is_file() and video.with_suffix(".png").is_file()
	assert not queued.exists()
	await queue.shutdown()


async def test_sent_file_moves_from_queued_to_published(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Отправленный файл переезжает из папки очереди в опубликованные."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.release.set()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(
		PostDraft(channel_id, media_path=str(video), media_kind=MediaKind.VIDEO)
	)
	await _wait_status(queue, item, QueueItemStatus.DONE)
	published = tmp_path / "media" / "published" / "суб" / "ролик.mp4"
	assert published.is_file()
	assert not (tmp_path / "media" / "queued" / "суб" / "ролик.mp4").exists()
	assert not video.exists()
	await queue.shutdown()


async def test_stash_collision_rejects_batch(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Файл-тёзка уже ждёт в очереди — постановка отклоняется целиком."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	await queue.enqueue(
		PostDraft(channel_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120))
	)
	twin = _make_video(processed)  # обработали заново под тем же именем
	with pytest.raises(PostError, match="уже есть файл"):
		await queue.enqueue(
			PostDraft(
				channel_id, media_path=str(twin), media_kind=MediaKind.VIDEO, when=_future(180)
			)
		)
	assert twin.is_file()  # отклонённый пакет не трогает диск
	assert len(await queue.state()) == 1
	await queue.shutdown()


async def test_enqueue_rejects_non_video_from_processed(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Не-видео из папки результатов не ставится: конвейер очереди — только для видео."""
	processed = _media(tmp_path, monkeypatch)
	photo = processed / "кадр.png"
	photo.write_bytes(b"png")
	gateway = _SlowGateway()
	gateway.release.set()
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	with pytest.raises(PostError, match="только видео"):
		await queue.enqueue(
			PostDraft(channel_id, media_path=str(photo), media_kind=MediaKind.PHOTO)
		)
	assert photo.is_file()  # файл остался в результатах
	assert await queue.state() == []  # постановка атомарна — очередь пуста
	await queue.shutdown()


async def test_retry_expired_scheduled_publishes_now(db: Database) -> None:
	"""Повтор просроченного отложенного публикует «сейчас» (ADR-0016, «просрочка → сейчас»)."""
	gateway = _SlowGateway()
	gateway.release.set()
	gateway.fail_texts = {"ночной"}
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(PostDraft(channel_id, text="ночной"))
	await _wait_status(queue, item, QueueItemStatus.ERROR)
	# смоделировать «ошибка ночью, повтор утром»: желаемый момент уже прошёл
	internal = next(i for i in queue._items if i.id == item)  # noqa: SLF001
	internal.draft = replace(internal.draft, when=datetime.now(UTC) - timedelta(hours=8))
	gateway.fail_texts = set()  # «сеть починилась»
	await queue.retry(item)
	await _wait_status(queue, item, QueueItemStatus.DONE)
	assert gateway.published[-1].when is None  # ушёл «сейчас», а не в прошлое
	await queue.shutdown()


async def test_drop_channel_removes_items_and_returns_files(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Снятие элементов канала: очередь пуста, файл вернулся в результаты."""
	processed = _media(tmp_path, monkeypatch)
	video = _make_video(processed)
	gateway = _SlotGateway()
	gateway.scheduled = [_future(600 + i) for i in range(TELEGRAM_MAX_SCHEDULED)]
	queue = _queue(db, gateway)
	channel_id = await _add_channel(db)
	item = await queue.enqueue(
		PostDraft(channel_id, media_path=str(video), media_kind=MediaKind.VIDEO, when=_future(120))
	)
	await _wait_status(queue, item, QueueItemStatus.WAITING)
	assert not video.exists()  # файл ушёл в папку очереди
	await queue.drop_channel(channel_id)
	assert await queue.state() == []  # «зомби»-элементов в памяти нет
	assert video.is_file()  # файл вернулся в результаты
	await queue.shutdown()
