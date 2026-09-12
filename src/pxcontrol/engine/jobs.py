"""Каркас заданий движка: жизненный цикл, прогресс, отмена, повтор (ADR-0025).

В движке есть работы, которые идут минутами и за которыми человек
наблюдает: кодирование видео, отправка постов, обслуживание сообществ.
Все они устроены одинаково — очередь заданий, строго по одному за раз,
у каждого статус, доля выполнения, возможность отменить, повторить
после ошибки и убрать с показа. Различается только **что именно делает
шаг** и **каким ресурсом он ограничен**: кодирование упирается
в процессор, обращения к Telegram — в дорожку аккаунта (ADR-0024).

Этот модуль выражает общую часть — жизненный цикл, — и ничего не знает
ни про ffmpeg, ни про Telegram. Конкретная очередь заводит свой подкласс
:class:`Job` со своими полями, отдаёт каркасу исполнителя одного задания
и строит из заданий свои снимки для интерфейса.

Чего каркас сознательно **не** делает: не трактует предметные исключения
(их переводит сам исполнитель), не хранит задания между запусками
(персистентность — дело конкретной очереди, ADR-0016) и не решает,
что делать с файлами на диске. Попытка обобщить и это превратила бы
каркас в свалку частных случаев — ровно то, от чего предостерегал
ADR-0014, откладывая общий базовый класс до третьего потребителя.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pxcontrol.engine.errors import user_message

logger = logging.getLogger(__name__)

#: Сколько ждать завершения активного задания при остановке движка.
#: Кооперативная отмена (ADR-0020) штатно срабатывает за секунды;
#: предел страхует от фазы, в которой отмену подхватить нечем.
DEFAULT_SHUTDOWN_TIMEOUT_S = 30.0


class JobStatus(StrEnum):
	"""Состояние задания — общее для всех очередей движка.

	Одно имя на одно состояние: до появления каркаса активная работа
	называлась ``SENDING`` у отправки и ``PROCESSING`` у обработки
	видео, и панель интерфейса вынуждена была знать оба.
	"""

	PENDING = "pending"  # ждёт своей очереди
	WAITING = "waiting"  # ждёт внешнего условия (слот отложек, ADR-0016)
	RUNNING = "running"  # выполняется прямо сейчас
	DONE = "done"  # завершено успешно
	ERROR = "error"  # не удалось (текст — в error)
	CANCELLED = "cancelled"  # отменено человеком или остановкой движка

	def finished(self) -> bool:
		"""Завершено ли задание (в любом исходе)."""
		return self in (self.DONE, self.ERROR, self.CANCELLED)

	def active(self) -> bool:
		"""Идёт ли работа прямо сейчас (ждущие — не в счёт)."""
		return self is self.RUNNING

	def left_queue(self) -> bool:
		"""Покинуло ли задание очередь насовсем.

		Ошибки здесь нет намеренно: элемент с ошибкой остаётся живым —
		его повторяют, правят или убирают руками (ADR-0016).
		"""
		return self in (self.DONE, self.CANCELLED)


class JobCancelled(Exception):  # noqa: N818 — сигнал исхода, а не ошибка
	"""Задание прервано по требованию — это исход, а не сбой.

	Исполнитель бросает его, увидев ``job.cancel_requested`` (или свой
	предметный сигнал отмены, например убитый ffmpeg). Наследовать
	``EngineError`` не должен: человеку показывать нечего, отмену он
	запросил сам.
	"""


class Job:
	"""Задание очереди: общее состояние выполнения.

	Конкретная очередь заводит подкласс со своими полями (заявка,
	путь результата, черновик поста) — каркас о них не знает.
	"""

	def __init__(self, job_id: int) -> None:
		self.id = job_id
		self.status = JobStatus.PENDING
		self.progress = 0.0
		self.error: str | None = None
		#: пометка состояния для карточки (автоснижение битрейта,
		#: пауза после флуд-лимита); None — нечего сказать
		self.note: str | None = None
		#: отмену запросил человек — отличает её от остановки движка
		self.cancel_requested = False


_J = TypeVar("_J", bound=Job)


class JobQueue(Generic[_J]):
	"""Последовательный исполнитель заданий одной очереди.

	Задания выполняются строго по одному: и кодирование, и обращения
	к Telegram — работы, которые от параллельности не выигрывают
	(x264 сам занимает все ядра; темп запросов к аккаунту держит
	дорожка шлюза, ADR-0024).

	Все методы вызываются в цикле событий движка, поэтому состояние
	не требует блокировок. Исключение — ``job.progress``
	и ``job.cancel_requested``: их читает и пишет колбэк прогресса
	из рабочего потока, но это атомарные операции над простыми полями.
	"""

	def __init__(
		self,
		execute: Callable[[_J], Coroutine[Any, Any, None]],
		*,
		name: str,
		hard_cancel: bool = False,
		cancel_pending_on_shutdown: bool = False,
		shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
	) -> None:
		"""Args:
		execute: исполнитель одного задания. Ошибку переводит сам
			(её текст попадёт на карточку), отмену сообщает броском
			:class:`JobCancelled`.
		name: имя очереди для сообщений в логе («обработка», «отправка»).
		hard_cancel: отменять ли активное задание отменой его задачи.
			Нужно там, где работа висит на сетевом вызове (отправка);
			там, где работу ведёт посторонний процесс (ffmpeg), отмена
			задачи его не остановит — такая очередь полагается только
			на флаг ``cancel_requested``.
		cancel_pending_on_shutdown: помечать ли ожидающие задания
			отменёнными при остановке движка. Для очереди без
			персистентности это честно (после перезапуска её нет),
			для персистентной — нет: там задания переживают выход.
		shutdown_timeout_s: сколько ждать активное задание при остановке.
		"""
		self._execute = execute
		self._name = name
		self._hard_cancel = hard_cancel
		self._cancel_pending_on_shutdown = cancel_pending_on_shutdown
		self._shutdown_timeout_s = shutdown_timeout_s
		self._jobs: list[_J] = []
		self._next_id = 1
		self._worker: asyncio.Task[None] | None = None
		self._active: tuple[int, asyncio.Task[None]] | None = None
		# кооперативная остановка (ADR-0020): задачи выходят в безопасных
		# точках, запросы к БД не обрываются посреди работы
		self._stop = asyncio.Event()

	# --- состав очереди -------------------------------------------------------

	def new_id(self) -> int:
		"""Выдаёт следующий номер задания (для очередей без своих id)."""
		job_id = self._next_id
		self._next_id += 1
		return job_id

	def add(self, job: _J) -> None:
		"""Ставит готовое задание в хвост очереди (не запуская воркера)."""
		self._jobs.append(job)

	def all(self) -> list[_J]:
		"""Задания в порядке постановки (снимок списка)."""
		return list(self._jobs)

	def get(self, job_id: int) -> _J | None:
		"""Задание по номеру (None — такого нет)."""
		for job in self._jobs:
			if job.id == job_id:
				return job
		return None

	def remove(self, job: _J) -> None:
		"""Убирает задание из очереди (снятие с показа, удаление канала)."""
		with suppress(ValueError):
			self._jobs.remove(job)

	@property
	def stopping(self) -> bool:
		"""Движок останавливается — новую работу начинать нельзя."""
		return self._stop.is_set()

	@property
	def active_id(self) -> int | None:
		"""Номер задания, выполняющегося прямо сейчас (None — нет такого)."""
		return self._active[0] if self._active is not None else None

	# --- выполнение -----------------------------------------------------------

	def request_cancel(self, job: _J) -> None:
		"""Взводит отмену задания; исход запишет само выполнение.

		Ожидающее задание отменяет вызывающая очередь (у неё свои
		побочные действия — вернуть файл, удалить строку); здесь —
		только активное: флаг всегда, отмена задачи — если очередь
		заведена с ``hard_cancel``.
		"""
		job.cancel_requested = True
		if self._hard_cancel and self._active is not None and self._active[0] == job.id:
			self._active[1].cancel()

	def ensure_worker(self) -> None:
		"""Запускает фоновую задачу выполнения, если она не крутится."""
		if self._worker is None or self._worker.done():
			self._worker = asyncio.create_task(self._run())

	async def wait_idle(self) -> None:
		"""Дожидается простоя очереди (детерминированная точка для тестов).

		Без неё тестам пришлось бы синхронизироваться сном настенного
		времени — гонка по построению.
		"""
		while (worker := self._worker) is not None and not worker.done():
			with suppress(asyncio.CancelledError):
				await worker

	async def shutdown(self) -> None:
		"""Гасит очередь при остановке движка (ADR-0020).

		Взводится событие остановки: воркер выходит между заданиями,
		активное задание получает запрос отмены (и отмену задачи, если
		очередь так заведена). Задание, не завершившееся за отведённый
		срок, отменяется жёстко — последнее средство.
		"""
		self._stop.set()
		if self._cancel_pending_on_shutdown:
			for job in self._jobs:
				if job.status is JobStatus.PENDING:
					job.status = JobStatus.CANCELLED
		if self._active is not None:
			active = self.get(self._active[0])
			if active is not None:
				active.cancel_requested = True
			if self._hard_cancel:
				self._active[1].cancel()
		if self._worker is not None:
			with suppress(TimeoutError, asyncio.CancelledError):
				await asyncio.wait_for(self._worker, timeout=self._shutdown_timeout_s)
			self._worker = None

	async def wait_stop(self, seconds: float) -> None:
		"""Ждёт срок или остановку движка — что наступит раньше.

		Паузы фоновых задач идут через это ожидание: остановка прерывает
		их немедленно, не заставляя `shutdown` ждать полную паузу.
		"""
		with suppress(TimeoutError):
			await asyncio.wait_for(self._stop.wait(), timeout=seconds)

	async def _run(self) -> None:
		"""Выполняет задания по одному, пока есть готовые.

		Остановка движка выводит из цикла между заданиями; начатое
		задание доигрывается своим путём — его обрывает запрос отмены
		из :meth:`shutdown`, а не отмена воркера.
		"""
		while not self.stopping and (job := self._next_pending()) is not None:
			await self._run_one(job)

	def _next_pending(self) -> _J | None:
		"""Первое задание, готовое к выполнению."""
		for job in self._jobs:
			if job.status is JobStatus.PENDING:
				return job
		return None

	async def _run_one(self, job: _J) -> None:
		"""Выполняет одно задание и записывает исход в его статус.

		Исходов три: успех (DONE), отмена (CANCELLED) и ошибка (ERROR
		с текстом для человека). Недоменные исключения сворачиваются
		так же, как это делает мост интерфейса, — карточка показывает
		текст как есть (контракт ``errors.py``).

		Об отмене исполнитель сообщает броском :class:`JobCancelled`.
		Но у очереди с ``hard_cancel`` работа обрывается отменой самой
		задачи, и тогда отмену от остановки движка отличает пара
		«флаг задания + состояние очереди»: первое — исход задания
		(очередь идёт дальше), второе — конец работы очереди, и статус
		в памяти дописывать некому (ADR-0020).
		"""
		job.status = JobStatus.RUNNING
		task = asyncio.create_task(self._execute(job))
		self._active = (job.id, task)
		try:
			await task
		except JobCancelled:
			job.status = JobStatus.CANCELLED
			logger.info("%s id=%s: отменено.", self._name, job.id)
		except asyncio.CancelledError:
			task.cancel()
			if job.cancel_requested and not self.stopping:
				# отмену запросил человек, и работа висела на сетевом
				# вызове (`hard_cancel`): это исход задания, а не беда
				# очереди — она продолжает со следующего
				job.status = JobStatus.CANCELLED
				logger.info("%s id=%s: отменено.", self._name, job.id)
				return
			# остановка движка или снос цикла событий: очередь
			# не продолжается, а исход недоделанного задания запишет
			# следующий запуск — в памяти его дописывать некому
			raise
		except Exception as exc:  # noqa: BLE001 — исход задания, не очереди
			job.status = JobStatus.ERROR
			job.error = user_message(exc)
			logger.exception("%s id=%s: не удалось.", self._name, job.id)
		else:
			job.status = JobStatus.DONE
			job.progress = 1.0
		finally:
			self._active = None
