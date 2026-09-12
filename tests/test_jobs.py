"""Каркас заданий движка (ADR-0025): жизненный цикл, отмена, остановка."""

from __future__ import annotations

import asyncio

import pytest

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.jobs import Job, JobCancelled, JobQueue, JobStatus


class _TestJob(Job):
	"""Задание с меткой — чтобы различать их в проверках порядка."""

	__test__ = False  # не тестовый класс для pytest, а данные тестов

	def __init__(self, job_id: int, label: str) -> None:
		super().__init__(job_id)
		self.label = label


def _queue(
	execute: object, **kwargs: object
) -> JobQueue[_TestJob]:  # pragma: no cover — тонкая обёртка
	"""Очередь заданий с именем по умолчанию (типы — забота вызывающего)."""
	return JobQueue(execute, name="Тест", **kwargs)  # type: ignore[arg-type]


def _put(queue: JobQueue[_TestJob], label: str) -> _TestJob:
	"""Ставит задание в очередь и возвращает его."""
	job = _TestJob(queue.new_id(), label)
	queue.add(job)
	return job


async def test_jobs_run_one_by_one_in_order() -> None:
	"""Задания выполняются по одному, в порядке постановки."""
	order: list[str] = []
	inside = 0
	peak = 0

	async def execute(job: _TestJob) -> None:
		nonlocal inside, peak
		inside += 1
		peak = max(peak, inside)
		await asyncio.sleep(0)
		order.append(job.label)
		inside -= 1

	queue = _queue(execute)
	for label in ("раз", "два", "три"):
		_put(queue, label)
	queue.ensure_worker()
	await queue.wait_idle()
	assert order == ["раз", "два", "три"]
	assert peak == 1
	assert [job.status for job in queue.all()] == [JobStatus.DONE] * 3


async def test_success_fills_progress() -> None:
	"""Успешное задание получает DONE и полную долю выполнения."""

	async def execute(job: _TestJob) -> None:
		job.progress = 0.4

	queue = _queue(execute)
	job = _put(queue, "готовое")
	queue.ensure_worker()
	await queue.wait_idle()
	assert job.status is JobStatus.DONE
	assert job.progress == 1.0


async def test_domain_error_lands_on_card() -> None:
	"""Доменная ошибка показывается человеку как есть; очередь живёт дальше."""

	async def execute(job: _TestJob) -> None:
		if job.label == "битое":
			raise EngineError("Файл не найден — проверьте путь.")

	queue = _queue(execute)
	broken = _put(queue, "битое")
	good = _put(queue, "целое")
	queue.ensure_worker()
	await queue.wait_idle()
	assert broken.status is JobStatus.ERROR
	assert broken.error == "Файл не найден — проверьте путь."
	assert good.status is JobStatus.DONE  # соседа чужая ошибка не трогает


async def test_unexpected_error_is_folded() -> None:
	"""Недоменное исключение сворачивается во «внутреннюю ошибку»."""

	async def execute(job: _TestJob) -> None:
		raise ZeroDivisionError("division by zero")

	queue = _queue(execute)
	job = _put(queue, "сбой")
	queue.ensure_worker()
	await queue.wait_idle()
	assert job.status is JobStatus.ERROR
	assert job.error is not None
	assert "Внутренняя ошибка" in job.error


async def test_cancelled_job_is_not_an_error() -> None:
	"""Отмена — исход, а не сбой: статус CANCELLED и пустая ошибка."""

	async def execute(job: _TestJob) -> None:
		if job.cancel_requested:
			raise JobCancelled
		await asyncio.sleep(0)

	queue = _queue(execute)
	job = _put(queue, "отменяемое")
	job.cancel_requested = True
	queue.ensure_worker()
	await queue.wait_idle()
	assert job.status is JobStatus.CANCELLED
	assert job.error is None


async def test_hard_cancel_interrupts_running_job() -> None:
	"""С ``hard_cancel`` активное задание обрывается отменой его задачи.

	Так гасится сетевая загрузка: ждать её завершения незачем —
	недосланное Telegram не публикует.
	"""
	started = asyncio.Event()

	async def execute(job: _TestJob) -> None:
		started.set()
		try:
			await asyncio.Event().wait()  # «висим на сети»
		except asyncio.CancelledError:
			raise JobCancelled from None

	queue = _queue(execute, hard_cancel=True)
	job = _put(queue, "загрузка")
	queue.ensure_worker()
	await started.wait()
	assert queue.active_id == job.id
	queue.request_cancel(job)
	await queue.wait_idle()
	assert job.status is JobStatus.CANCELLED


async def test_soft_cancel_only_sets_flag() -> None:
	"""Без ``hard_cancel`` отмена лишь взводит флаг — работу гасит сам исполнитель.

	Так устроена обработка видео: посторонний процесс ffmpeg отменой
	задачи не остановить, его убивает колбэк прогресса.
	"""
	started = asyncio.Event()
	release = asyncio.Event()

	async def execute(job: _TestJob) -> None:
		started.set()
		await release.wait()
		if job.cancel_requested:
			raise JobCancelled

	queue = _queue(execute)
	job = _put(queue, "кодирование")
	queue.ensure_worker()
	await started.wait()
	queue.request_cancel(job)
	assert job.cancel_requested is True
	assert job.status is JobStatus.RUNNING  # задача не отменена — работа идёт
	release.set()
	await queue.wait_idle()
	assert job.status is JobStatus.CANCELLED


async def test_shutdown_cancels_pending_when_asked() -> None:
	"""Очередь без персистентности честно отменяет ожидающих при остановке."""
	release = asyncio.Event()

	async def execute(job: _TestJob) -> None:
		await release.wait()

	queue = _queue(execute, cancel_pending_on_shutdown=True, hard_cancel=True)
	first = _put(queue, "идёт")
	waiting = _put(queue, "ждёт")
	queue.ensure_worker()
	await asyncio.sleep(0)
	shutdown = asyncio.create_task(queue.shutdown())
	await asyncio.sleep(0)
	release.set()
	await shutdown
	assert waiting.status is JobStatus.CANCELLED
	# активному статус в памяти не дописывается: движок останавливается,
	# и запись исхода — дело следующего запуска (у персистентной очереди
	# он уйдёт повторно, у остальных очереди просто не будет)
	assert first.status is JobStatus.RUNNING
	assert queue.stopping is True


async def test_shutdown_keeps_pending_for_persistent_queue() -> None:
	"""Персистентной очереди ожидающие нужны: они переживают выход (ADR-0016)."""

	async def execute(job: _TestJob) -> None:
		return None

	queue = _queue(execute)
	job = _put(queue, "переживёт перезапуск")
	await queue.shutdown()
	assert job.status is JobStatus.PENDING


async def test_stopping_blocks_new_work() -> None:
	"""После остановки воркер новых заданий не берёт."""
	done: list[str] = []

	async def execute(job: _TestJob) -> None:
		done.append(job.label)

	queue = _queue(execute)
	await queue.shutdown()
	_put(queue, "поздний")
	queue.ensure_worker()
	await queue.wait_idle()
	assert done == []


async def test_wait_stop_returns_early_on_shutdown() -> None:
	"""Пауза фоновой задачи прерывается остановкой, а не досиживается."""

	async def execute(job: _TestJob) -> None:
		return None

	queue = _queue(execute)
	waiting = asyncio.create_task(queue.wait_stop(30.0))
	await asyncio.sleep(0)
	await queue.shutdown()
	await asyncio.wait_for(waiting, timeout=1.0)  # не 30 секунд


async def test_queue_bookkeeping() -> None:
	"""Состав очереди: выдача номеров, поиск и снятие задания."""

	async def execute(job: _TestJob) -> None:
		return None

	queue = _queue(execute)
	first = _put(queue, "первое")
	second = _put(queue, "второе")
	assert first.id != second.id
	assert queue.get(second.id) is second
	assert queue.get(9999) is None
	queue.remove(first)
	assert [job.label for job in queue.all()] == ["второе"]
	queue.remove(first)  # повторное снятие безвредно


@pytest.mark.parametrize(
	("status", "finished", "active", "left"),
	[
		(JobStatus.PENDING, False, False, False),
		(JobStatus.WAITING, False, False, False),
		(JobStatus.RUNNING, False, True, False),
		(JobStatus.DONE, True, False, True),
		(JobStatus.ERROR, True, False, False),
		(JobStatus.CANCELLED, True, False, True),
	],
)
def test_status_predicates(status: JobStatus, finished: bool, active: bool, left: bool) -> None:
	"""Признаки статуса: ошибка завершена, но очередь не покинула (ADR-0016)."""
	assert status.finished() is finished
	assert status.active() is active
	assert status.left_queue() is left


async def test_hard_cancel_by_user_lets_queue_continue() -> None:
	"""Отмена человеком — исход задания: очередь берётся за следующее.

	Тем же обрывом задачи гасит работу и остановка движка, поэтому
	каркас различает их по паре «флаг задания + состояние очереди».
	"""
	started: list[str] = []
	done: list[str] = []

	async def execute(job: _TestJob) -> None:
		started.append(job.label)
		if job.label == "первое":
			try:
				await asyncio.Event().wait()
			except asyncio.CancelledError:
				raise
		done.append(job.label)

	queue = _queue(execute, hard_cancel=True)
	first = _put(queue, "первое")
	second = _put(queue, "второе")
	queue.ensure_worker()
	while not started:
		await asyncio.sleep(0)
	queue.request_cancel(first)
	await queue.wait_idle()
	assert first.status is JobStatus.CANCELLED
	assert second.status is JobStatus.DONE  # очередь не остановилась
	assert done == ["второе"]
