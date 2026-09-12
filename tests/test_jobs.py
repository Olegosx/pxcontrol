"""Каркас заданий движка (ADR-0025): жизненный цикл, отмена, остановка."""

from __future__ import annotations

import asyncio

import pytest

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.jobs import Job, JobCancelled, JobDeferred, JobQueue, JobStatus


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


async def test_executor_reports_its_own_cancellation() -> None:
	"""Работу рвёт тот, кто её ведёт; каркасу он сообщает это исходом.

	Так гасится сетевая загрузка: очередь отправки отменяет собственную
	задачу передачи (запрос к БД при этом не рвётся, ADR-0020)
	и сообщает каркасу отмену броском ``JobCancelled``.
	"""
	started = asyncio.Event()
	inner: list[asyncio.Task[None]] = []

	async def execute(job: _TestJob) -> None:
		task: asyncio.Task[None] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]
		inner.append(task)
		started.set()
		try:
			await task
		except asyncio.CancelledError:
			raise JobCancelled from None

	queue = _queue(execute)
	job = _put(queue, "загрузка")
	queue.ensure_worker()
	await started.wait()
	assert queue.active_id == job.id
	queue.request_cancel(job)
	inner[0].cancel()  # очередь рвёт свою сетевую часть сама
	await queue.wait_idle()
	assert job.status is JobStatus.CANCELLED


async def test_soft_cancel_only_sets_flag() -> None:
	"""Отмена лишь взводит флаг — работу гасит сам исполнитель.

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

	queue = _queue(execute, cancel_pending_on_shutdown=True)
	first = _put(queue, "идёт")
	waiting = _put(queue, "ждёт")
	queue.ensure_worker()
	await asyncio.sleep(0)
	shutdown = asyncio.create_task(queue.shutdown())
	await asyncio.sleep(0)
	release.set()
	await shutdown
	assert waiting.status is JobStatus.CANCELLED
	# активное задание каркас не рвёт, а дожидается — в этом и состоит
	# кооперативная остановка (ADR-0020): прервать работу может только
	# тот, кто её ведёт, и здесь она успела закончиться штатно
	assert first.status is JobStatus.DONE
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


async def test_cancelled_job_does_not_stop_the_queue() -> None:
	"""Отмена одного задания — его исход: очередь берётся за следующее."""
	done: list[str] = []

	async def execute(job: _TestJob) -> None:
		if job.cancel_requested:
			raise JobCancelled
		done.append(job.label)

	queue = _queue(execute)
	first = _put(queue, "первое")
	second = _put(queue, "второе")
	first.cancel_requested = True
	queue.ensure_worker()
	await queue.wait_idle()
	assert first.status is JobStatus.CANCELLED
	assert second.status is JobStatus.DONE
	assert done == ["второе"]


async def test_shutdown_does_not_look_like_user_cancel() -> None:
	"""Остановка движка не взводит флаг отмены человеком.

	Флаг означает «человек передумал», и исход у него другой: задание
	покидает очередь. У остановки исход не записывается вовсе —
	недоделанное уйдёт после перезапуска (ADR-0020).
	"""
	started = asyncio.Event()
	release = asyncio.Event()

	async def execute(job: _TestJob) -> None:
		started.set()
		await release.wait()

	queue = _queue(execute)
	job = _put(queue, "идёт")
	queue.ensure_worker()
	await started.wait()
	shutdown = asyncio.create_task(queue.shutdown())
	await asyncio.sleep(0)
	assert job.cancel_requested is False
	release.set()
	await shutdown


async def test_deferred_job_waits_instead_of_failing() -> None:
	"""Отсрочка — четвёртый исход: задание ждёт, а не падает в ошибку.

	Так очередь отправки поступает, когда кончились слоты отложек
	или Telegram попросил подождать (ADR-0016): ошибкой это не считается,
	на карточке появляется пометка состояния, а пауза не досиживается
	при остановке движка.
	"""
	slept: list[float] = []
	attempts = 0

	async def sleep(seconds: float) -> None:
		slept.append(seconds)

	async def execute(job: _TestJob) -> None:
		nonlocal attempts
		attempts += 1
		if attempts == 1:
			raise JobDeferred(JobStatus.WAITING, note="ждёт слота", delay_s=30)

	queue = _queue(execute, sleep=sleep)
	job = _put(queue, "отложенное")
	queue.ensure_worker()
	await queue.wait_idle()
	assert job.status is JobStatus.WAITING  # не ERROR
	assert job.error is None
	assert slept == [30]
	assert job.note is None  # пометка снята после паузы


async def test_deferred_job_can_return_to_queue() -> None:
	"""Отсрочка с возвратом в очередь: задание повторится само."""
	attempts = 0

	async def execute(job: _TestJob) -> None:
		nonlocal attempts
		attempts += 1
		if attempts == 1:
			raise JobDeferred(JobStatus.PENDING)

	queue = _queue(execute)
	job = _put(queue, "повторимое")
	queue.ensure_worker()
	await queue.wait_idle()
	assert job.status is JobStatus.DONE
	assert attempts == 2  # вернулось в очередь и ушло со второй попытки


# --- крючки очереди: record, ready, cooldown -----------------------------------


async def test_record_runs_before_status_appears_in_memory() -> None:
	"""Исход сначала сохраняется, потом виден в памяти (ADR-0016).

	Порядок именно такой: наблюдатель, увидевший исход в памяти,
	должен быть уверен, что хранилище о нём уже знает.
	"""
	seen: list[tuple[JobStatus, JobStatus]] = []

	async def execute(job: _TestJob) -> None:
		return None

	async def record(job: _TestJob, status: JobStatus, error: str | None) -> None:
		# в момент записи память ещё хранит прежний статус
		seen.append((status, job.status))

	queue = _queue(execute, record=record)
	_put(queue, "раз")
	queue.ensure_worker()
	await queue.wait_idle()

	assert seen == [(JobStatus.DONE, JobStatus.RUNNING)]


async def test_storage_failure_does_not_kill_the_worker() -> None:
	"""Сбой записи исхода не роняет очередь и не прячется.

	Иначе задание навсегда осталось бы «выполняется», а следующие
	не начались бы вовсе: воркер умирал молча.
	"""
	done: list[str] = []

	async def execute(job: _TestJob) -> None:
		done.append(job.label)

	async def record(job: _TestJob, status: JobStatus, error: str | None) -> None:
		if job.label == "первое":
			raise RuntimeError("база заблокирована")

	queue = _queue(execute, record=record)
	broken = _put(queue, "первое")
	following = _put(queue, "второе")
	queue.ensure_worker()
	await queue.wait_idle()

	assert done == ["первое", "второе"]  # очередь не встала
	assert broken.status is JobStatus.DONE  # исход применён
	assert broken.note is not None  # и помечен как несохранённый
	assert following.status is JobStatus.DONE
	assert following.note is None


async def test_ready_holds_a_job_without_blocking_the_rest() -> None:
	"""Неготовое задание пропускается, а не задерживает очередь."""
	done: list[str] = []
	held = True

	async def execute(job: _TestJob) -> None:
		done.append(job.label)

	def ready(job: _TestJob) -> bool:
		return not (held and job.label == "придержанное")

	queue = _queue(execute, ready=ready)
	_put(queue, "придержанное")
	_put(queue, "обычное")
	queue.ensure_worker()
	await queue.wait_idle()

	assert done == ["обычное"]

	held = False  # условие снято — задание берётся следующим заходом
	queue.ensure_worker()
	await queue.wait_idle()
	assert done == ["обычное", "придержанное"]


async def test_cooldown_paces_only_between_jobs() -> None:
	"""Пауза выдерживается после задания, у которого она задана."""
	paused: list[float] = []

	async def execute(job: _TestJob) -> None:
		return None

	def cooldown(job: _TestJob) -> float:
		return 5.0 if job.label == "щадящее" else 0.0

	async def sleep(seconds: float) -> None:
		paused.append(seconds)

	queue = _queue(execute, cooldown=cooldown, sleep=sleep)
	_put(queue, "щадящее")
	_put(queue, "обычное")
	queue.ensure_worker()
	await queue.wait_idle()

	assert paused == [5.0]  # обычное задание паузы не просило

	# одинокое щадящее задание паузу не держит: ждать нечего и некого
	_put(queue, "щадящее")
	queue.ensure_worker()
	await queue.wait_idle()
	assert paused == [5.0]
