"""Тесты очереди обработки видео (ADR-0014, без реального ffmpeg)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.video import (
	BitrateAdvice,
	PresetFields,
	VideoError,
	VideoService,
)
from pxcontrol.engine.services.video_queue import (
	ProcessingQueue,
	ProcessingRequest,
	VideoItemStatus,
)
from pxcontrol.engine.video import ProcessingOptions

FIELDS = PresetFields(name="Тест", subdir="паб")


class _FakeProcessor:
	"""Подмена process(): фиксирует параметры, создаёт файл результата."""

	def __init__(self) -> None:
		self.calls: list[ProcessingOptions] = []

	def __call__(self, options: ProcessingOptions, on_progress: object = None) -> None:
		self.calls.append(options)
		if callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		Path(options.output).parent.mkdir(parents=True, exist_ok=True)
		Path(options.output).write_bytes(b"video")


class _GatedProcessor(_FakeProcessor):
	"""Процессор, ждущий отмашки: пока событие не взведено — «кодирует».

	Колбэк прогресса зовётся в цикле ожидания — отмена (исключение
	из колбэка) прерывает «кодирование», как настоящий run_streaming.
	"""

	def __init__(self) -> None:
		super().__init__()
		self.release = threading.Event()
		self.started = threading.Event()

	def __call__(self, options: ProcessingOptions, on_progress: object = None) -> None:
		self.started.set()
		while not self.release.wait(timeout=0.005):
			if callable(on_progress):
				on_progress(0.1)
		super().__call__(options, on_progress)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
	await database.init()
	yield database
	await database.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	"""Готовое окружение: media в tmp, ffmpeg «найден»; отдаёт исходник."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "исходник.mp4"
	source.write_bytes(b"src")
	return source


async def _wait_status(
	queue: ProcessingQueue, item_id: int, *statuses: VideoItemStatus, timeout: float = 5.0
) -> None:
	"""Ждёт, пока элемент не окажется в одном из статусов."""
	async with asyncio.timeout(timeout):
		while True:
			state = await queue.state()
			item = next(entry for entry in state if entry.id == item_id)
			if item.status in statuses:
				return
			await asyncio.sleep(0.01)


async def _wait_all_finished(queue: ProcessingQueue, timeout: float = 5.0) -> None:
	"""Ждёт завершения всех элементов очереди (в любом исходе)."""
	async with asyncio.timeout(timeout):
		while await queue.has_unfinished():
			await asyncio.sleep(0.01)


async def test_enqueue_many_processes_in_order(db: Database, env: Path, tmp_path: Path) -> None:
	"""Пакет обрабатывается последовательно, результаты — в подпапке пакета."""
	second = tmp_path / "второй.mp4"
	second.write_bytes(b"src2")
	processor = _FakeProcessor()
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=processor))
	ids = await queue.enqueue_many(
		[ProcessingRequest(str(env)), ProcessingRequest(str(second))],
		FIELDS,
		batch_subdir="20260815-1200_пакет",
	)
	await _wait_all_finished(queue)
	state = await queue.state()
	assert [item.status for item in state] == [VideoItemStatus.DONE, VideoItemStatus.DONE]
	assert [item.id for item in state] == ids
	# порядок обработки совпадает с порядком постановки
	assert [Path(call.input).name for call in processor.calls] == ["исходник.mp4", "второй.mp4"]
	# результаты лежат в <processed>/<подпапка пресета>/<подпапка пакета>
	for item in state:
		assert item.output_path is not None
		parent = Path(item.output_path).parent
		assert parent == tmp_path / "media" / "processed" / "паб" / "20260815-1200_пакет"
		assert item.batch == "20260815-1200_пакет"


async def test_single_enqueue_without_batch_subdir(db: Database, env: Path, tmp_path: Path) -> None:
	"""Одиночная обработка кладёт результат прямо в подпапку пресета."""
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=_FakeProcessor()))
	item_id = await queue.enqueue(ProcessingRequest(str(env)), FIELDS)
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	item = (await queue.state())[0]
	assert item.output_path is not None
	assert Path(item.output_path).parent == tmp_path / "media" / "processed" / "паб"


async def test_enqueue_many_validates_before_adding(db: Database, env: Path) -> None:
	"""Битый путь в середине пакета — отказ целиком, очередь пуста."""
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=_FakeProcessor()))
	with pytest.raises(VideoError, match="Файл не найден"):
		await queue.enqueue_many(
			[ProcessingRequest(str(env)), ProcessingRequest("/нет/такого.mp4")],
			FIELDS,
		)
	assert await queue.state() == []
	with pytest.raises(VideoError, match="пуст"):
		await queue.enqueue_many([], FIELDS)


async def test_error_is_isolated_and_retriable(db: Database, env: Path, tmp_path: Path) -> None:
	"""Ошибка одного файла не трогает остальные; повтор возвращает в очередь."""
	second = tmp_path / "второй.mp4"
	second.write_bytes(b"src2")
	fail_once = {"active": True}

	class _Flaky(_FakeProcessor):
		def __call__(self, options: ProcessingOptions, on_progress: object = None) -> None:
			if "исходник" in options.input and fail_once["active"]:
				fail_once["active"] = False
				raise RuntimeError("ffmpeg завершился с ошибкой: тест")
			super().__call__(options, on_progress)

	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=_Flaky()))
	first_id, second_id = await queue.enqueue_many(
		[ProcessingRequest(str(env)), ProcessingRequest(str(second))], FIELDS
	)
	await _wait_all_finished(queue)
	state = {item.id: item for item in await queue.state()}
	assert state[first_id].status is VideoItemStatus.ERROR
	assert state[first_id].error is not None and "Обработка не удалась" in state[first_id].error
	assert state[second_id].status is VideoItemStatus.DONE

	await queue.retry(first_id)
	await _wait_status(queue, first_id, VideoItemStatus.DONE)


async def test_retry_requires_existing_file(db: Database, env: Path) -> None:
	"""Повтор перепроверяет файл: исчезнувший — отказ, элемент в ошибке."""

	def _boom(_options: ProcessingOptions, _on_progress: object = None) -> None:
		raise RuntimeError("тест")

	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=_boom))
	item_id = await queue.enqueue(ProcessingRequest(str(env)), FIELDS)
	await _wait_status(queue, item_id, VideoItemStatus.ERROR)
	env.unlink()
	with pytest.raises(VideoError, match="Файл не найден"):
		await queue.retry(item_id)
	assert (await queue.state())[0].status is VideoItemStatus.ERROR


async def test_cancel_pending_item(db: Database, env: Path, tmp_path: Path) -> None:
	"""Отмена ожидающего элемента не трогает обрабатывающийся."""
	second = tmp_path / "второй.mp4"
	second.write_bytes(b"src2")
	processor = _GatedProcessor()
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=processor))
	first_id, second_id = await queue.enqueue_many(
		[ProcessingRequest(str(env)), ProcessingRequest(str(second))], FIELDS
	)
	await asyncio.to_thread(processor.started.wait, 5.0)
	await queue.cancel(second_id)
	await _wait_status(queue, second_id, VideoItemStatus.CANCELLED)
	processor.release.set()
	await _wait_status(queue, first_id, VideoItemStatus.DONE)
	assert len(processor.calls) == 1  # отменённый до ffmpeg не дошёл


async def test_cancel_active_item_stops_ffmpeg(db: Database, env: Path, tmp_path: Path) -> None:
	"""Отмена кодирующегося: колбэк прогресса прерывает обработку."""
	processor = _GatedProcessor()
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=processor))
	item_id = await queue.enqueue(ProcessingRequest(str(env)), FIELDS)
	await asyncio.to_thread(processor.started.wait, 5.0)
	await queue.cancel(item_id)
	await _wait_status(queue, item_id, VideoItemStatus.CANCELLED)
	# результат не создан: «кодирование» прервано до записи файла
	processed = tmp_path / "media" / "processed" / "паб"
	assert not processed.is_dir() or not list(processed.glob("*.mp4"))


async def test_auto_bitrate_substitution(
	db: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Исходник больше лимита: рекомендация подставляется, пометка видна."""
	processor = _FakeProcessor()
	service = VideoService(db, "ffmpeg", processor=processor)

	async def _advice(_path: str, _ts: float = 0.0, _te: float = 0.0) -> BitrateAdvice:
		return BitrateAdvice(limit_gb=2, kbps=1234)

	monkeypatch.setattr(service, "bitrate_advice", _advice)
	queue = ProcessingQueue(service)
	item_id = await queue.enqueue(ProcessingRequest(str(env)), FIELDS)
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	assert processor.calls[0].video_bitrate_kbps == 1234
	item = (await queue.state())[0]
	assert item.note is not None and "битрейт снижен" in item.note


async def test_auto_bitrate_keeps_lower_manual_value(
	db: Database, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Заданный вручную битрейт ниже рекомендации не повышается."""
	processor = _FakeProcessor()
	service = VideoService(db, "ffmpeg", processor=processor)

	async def _advice(_path: str, _ts: float = 0.0, _te: float = 0.0) -> BitrateAdvice:
		return BitrateAdvice(limit_gb=2, kbps=1234)

	monkeypatch.setattr(service, "bitrate_advice", _advice)
	queue = ProcessingQueue(service)
	fields = PresetFields(name="Тест", subdir="паб", video_bitrate_kbps=1000)
	item_id = await queue.enqueue(ProcessingRequest(str(env)), fields)
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	assert processor.calls[0].video_bitrate_kbps == 1000
	assert (await queue.state())[0].note is None


async def test_intro_source_override_reaches_processor(db: Database, env: Path) -> None:
	"""Персональный кадр заставки элемента доходит до обработки."""
	processor = _FakeProcessor()
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=processor))
	fields = PresetFields(name="Тест", subdir="паб", intro=True, intro_source="random-choice")
	item_id = await queue.enqueue(
		ProcessingRequest(str(env), intro_source="image:/x/кадр.png"), fields
	)
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	assert processor.calls[0].intro_source == "image:/x/кадр.png"


async def test_stash_frame_lifecycle(db: Database, env: Path, tmp_path: Path) -> None:
	"""Копия кадра переживает смену партии и удаляется после обработки."""
	frame = tmp_path / "кадр.png"
	frame.write_bytes(b"png")
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=_FakeProcessor()))
	stashed = await queue.stash_frame(str(frame))
	assert Path(stashed).is_file() and stashed != str(frame)
	fields = PresetFields(name="Тест", subdir="паб", intro=True)
	item_id = await queue.enqueue(
		ProcessingRequest(str(env), intro_source=f"image:{stashed}"), fields
	)
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	assert not Path(stashed).exists()  # копия очереди удалена после успеха
	assert frame.exists()  # оригинал пользователя не тронут

	# кадр вне папки очереди (свой PNG из пресета) не удаляется никогда
	item_id = await queue.enqueue(
		ProcessingRequest(str(env), intro_source=f"image:{frame}"), fields
	)
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	assert frame.exists()


async def test_dismiss_removes_finished_and_frame(db: Database, env: Path, tmp_path: Path) -> None:
	"""Снятие с показа убирает завершённый элемент и его копию кадра."""
	frame = tmp_path / "кадр.png"
	frame.write_bytes(b"png")

	def _boom(_options: ProcessingOptions, _on_progress: object = None) -> None:
		raise RuntimeError("тест")

	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=_boom))
	stashed = await queue.stash_frame(str(frame))
	fields = PresetFields(name="Тест", subdir="паб", intro=True)
	item_id = await queue.enqueue(
		ProcessingRequest(str(env), intro_source=f"image:{stashed}"), fields
	)
	await _wait_status(queue, item_id, VideoItemStatus.ERROR)
	assert Path(stashed).exists()  # у ошибки кадр остаётся — нужен повтору
	await queue.dismiss(item_id)
	assert await queue.state() == []
	assert not Path(stashed).exists()


async def test_shutdown_cancels_pending_and_cleans_frames(
	db: Database, env: Path, tmp_path: Path
) -> None:
	"""Остановка движка: ожидающие отменяются, папка кадров удаляется."""
	frame = tmp_path / "кадр.png"
	frame.write_bytes(b"png")
	processor = _GatedProcessor()
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=processor))
	stashed = await queue.stash_frame(str(frame))
	second = tmp_path / "второй.mp4"
	second.write_bytes(b"src2")
	first_id, second_id = await queue.enqueue_many(
		[ProcessingRequest(str(env)), ProcessingRequest(str(second))], FIELDS
	)
	await asyncio.to_thread(processor.started.wait, 5.0)
	start = time.monotonic()
	await queue.shutdown()
	assert time.monotonic() - start < 10.0  # активный ffmpeg погашен флагом
	state = {item.id: item for item in await queue.state()}
	assert state[first_id].status is VideoItemStatus.CANCELLED
	assert state[second_id].status is VideoItemStatus.CANCELLED
	assert not Path(stashed).exists()


async def test_has_unfinished(db: Database, env: Path) -> None:
	"""Занятость очереди видна, пока элементы не завершены."""
	processor = _GatedProcessor()
	queue = ProcessingQueue(VideoService(db, "ffmpeg", processor=processor))
	assert not await queue.has_unfinished()
	item_id = await queue.enqueue(ProcessingRequest(str(env)), FIELDS)
	assert await queue.has_unfinished()
	await asyncio.to_thread(processor.started.wait, 5.0)
	processor.release.set()
	await _wait_status(queue, item_id, VideoItemStatus.DONE)
	assert not await queue.has_unfinished()
