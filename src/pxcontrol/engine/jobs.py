"""Каркас заданий движка: жизненный цикл, прогресс, отмена, повтор (ADR-0025).

В движке есть работы, которые идут минутами и за которыми человек
наблюдает: кодирование видео, отправка постов, обслуживание сообществ.
Все они устроены одинаково — очередь заданий, строго по одному за раз,
у каждого статус, доля выполнения, возможность отменить, повторить
после ошибки и убрать с показа. Различается только **что именно делает
шаг** и **каким ресурсом он ограничен**: кодирование упирается
в процессор, обращения к Telegram — в дорожку аккаунта (ADR-0024).

Этот модуль выражает общую часть — жизненный цикл и **версию
состояния** (ADR-0034: интерфейс узнаёт об изменениях подпиской,
а не опросом), — и ничего не знает ни про ffmpeg, ни про Telegram.
Конкретная очередь заводит свой подкласс :class:`Job` со своими полями,
отдаёт каркасу исполнителя одного задания и строит из заданий свои
снимки для интерфейса.

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
from collections.abc import Callable, Coroutine, Hashable
from contextlib import suppress
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pxcontrol.engine.errors import user_message

logger = logging.getLogger(__name__)

#: Сколько ждать завершения активного задания при остановке движка.
#: Кооперативная отмена (ADR-0020) штатно срабатывает за секунды;
#: предел страхует от фазы, в которой отмену подхватить нечем.
#:
#: Про бюджет: очереди гасятся последовательно, поэтому в худшем случае
#: сумма их пределов превышает 10 секунд, которые ``EngineWorker.stop``
#: ждёт поток движка, — тогда поток отцепляется с предупреждением
#: и процесс всё равно завершается. Это осознанный размен: жёстко
#: обрывать начатую отправку ради быстрого выхода хуже, чем подождать.
#: Очередь отправки ставит себе предел меньше (её задание рвётся
#: по сети), обработка видео и обслуживание берут этот.
DEFAULT_SHUTDOWN_TIMEOUT_S = 30.0

#: Пометка на карточке, когда исход задания не удалось сохранить.
#: Предупреждение нужно человеку: работа сделана, но хранилище о ней
#: не знает — после перезапуска приложение будет считать иначе.
OUTCOME_NOT_SAVED_NOTE = "Исход не сохранён — подробности в журнале приложения."


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


class JobDeferred(Exception):  # noqa: N818 — сигнал исхода, а не ошибка
	"""Задание не выполнено и не провалено — его нужно отложить.

	Исполнитель бросает его, когда работа упёрлась во внешнее условие,
	снять которое он не может: свободных слотов отложек не осталось
	(задание ждёт, ADR-0016), Telegram попросил подождать (задание
	возвращается в очередь и повторится само). Это не ошибка: карточке
	показывать нечего, кроме пометки состояния.

	Attributes:
		status: куда перевести задание (WAITING — ждать условия,
			PENDING — вернуться в очередь).
		note: пометка состояния для карточки на время ожидания.
		delay_s: пауза перед возвратом к работе (0 — без паузы).
		hold: что удерживать на время паузы (ADR-0036): ключи ресурсов,
			которые задания называют в ``locks`` или ``alternatives``, —
			например, исполнитель под флуд-лимитом. None — удерживается
			только само задание; соседи, которым этот ресурс не нужен,
			идут дальше, а не ждут за чужой паузой.
	"""

	def __init__(
		self,
		status: JobStatus,
		*,
		note: str | None = None,
		delay_s: float = 0.0,
		hold: frozenset[Hashable] | None = None,
	) -> None:
		super().__init__(f"задание отложено: {status}")
		self.status = status
		self.note = note
		self.delay_s = delay_s
		self.hold = hold


class Job:
	"""Задание очереди: общее состояние выполнения.

	Конкретная очередь заводит подкласс со своими полями (заявка,
	путь результата, черновик поста) — каркас о них не знает.

	Статус, ошибка, пометка и предупреждение — **наблюдаемые** поля:
	запись нового значения сообщает очереди, что её состояние изменилось
	(:meth:`JobQueue.mark_changed`), и та поднимает версию для подписчиков
	(ADR-0034). Сервисы пишут в эти поля напрямую в двух десятках мест —
	учитывать каждое руками значило бы забыть следующее. Пишутся они
	только в цикле событий движка. Доля выполнения (``progress``)
	и флаг отмены — простые поля: их пишут из рабочих потоков, и в версию
	они не входят — прогресс интерфейс читает опросом, пока идёт работа.
	"""

	def __init__(self, job_id: int) -> None:
		self.id = job_id
		self._status = JobStatus.PENDING
		self.progress = 0.0
		self._error: str | None = None
		#: пометка состояния для карточки (автоснижение битрейта,
		#: пауза после флуд-лимита); None — нечего сказать. Живёт
		#: столько же, сколько состояние: кончилось — снимается
		self._note: str | None = None
		#: предупреждение, которое состояние пережить обязано: исход
		#: задания не удалось сохранить. Отдельным полем именно потому,
		#: что у него другая жизнь — оно про задание целиком, а не про
		#: его нынешнее состояние, и затирать его пометкой нельзя
		self._warning: str | None = None
		#: отмену запросил человек — отличает её от остановки движка
		self.cancel_requested = False
		#: ресурсы, которые задание занимает целиком, пока идёт (ADR-0036):
		#: второе задание с тем же ключом не начнётся — так очередь
		#: отправки держит «одну загрузку на сообщество». Удержание
		#: ключа (пауза после флуда, щадящий догон) тоже останавливает
		#: только задания с этим ключом
		self.locks: frozenset[Hashable] = frozenset()
		#: ресурсы, из которых заданию хватит любого свободного (кандидаты
		#: в исполнители): задание не начнётся, пока удержаны **все**;
		#: пусто — ограничения нет
		self.alternatives: frozenset[Hashable] = frozenset()
		#: кому сообщать об изменении наблюдаемых полей (ставит очередь)
		self._on_change: Callable[[], None] | None = None

	def bind_changes(self, on_change: Callable[[], None]) -> None:
		"""Подключает задание к очереди: изменения полей поднимают её версию."""
		self._on_change = on_change

	def _set_observed(self, name: str, value: object) -> None:
		"""Пишет наблюдаемое поле; изменившееся значение сообщается очереди.

		Запись того же значения — не изменение: повторные ``note = None``
		при снятии пометки не должны будить интерфейс впустую.
		"""
		if getattr(self, name) == value:
			return
		setattr(self, name, value)
		if self._on_change is not None:
			self._on_change()

	@property
	def status(self) -> JobStatus:
		"""Состояние задания (наблюдаемое)."""
		return self._status

	@status.setter
	def status(self, value: JobStatus) -> None:
		self._set_observed("_status", value)

	@property
	def error(self) -> str | None:
		"""Текст ошибки для человека (наблюдаемое; None — ошибки нет)."""
		return self._error

	@error.setter
	def error(self, value: str | None) -> None:
		self._set_observed("_error", value)

	@property
	def note(self) -> str | None:
		"""Пометка состояния для карточки (наблюдаемое)."""
		return self._note

	@note.setter
	def note(self, value: str | None) -> None:
		self._set_observed("_note", value)

	@property
	def warning(self) -> str | None:
		"""Предупреждение о несохранённом исходе (наблюдаемое)."""
		return self._warning

	@warning.setter
	def warning(self, value: str | None) -> None:
		self._set_observed("_warning", value)

	def card_note(self) -> str | None:
		"""Подпись карточки: пометка состояния вместе с предупреждением.

		Одна строка на два разных факта — так человек видит и то, чего
		задание ждёт, и то, что его исход не сохранился.
		"""
		parts = [text for text in (self.note, self.warning) if text]
		return " · ".join(parts) or None


_J = TypeVar("_J", bound=Job)


class JobQueue(Generic[_J]):
	"""Исполнитель заданий одной очереди: по одному или несколькими слотами.

	По умолчанию задания идут строго по одному: кодирование
	и обслуживание — работы, которые от параллельности не выигрывают
	(x264 сам занимает все ядра; темп запросов к аккаунту держит
	дорожка шлюза, ADR-0024). Очередь отправки просит несколько слотов
	(ADR-0036): загрузки в разные сообщества разными исполнителями
	независимы, а «одну загрузку на сообщество» держат **замки**
	заданий (:attr:`Job.locks`). Паузы — флуд-лимит, обрыв связи,
	щадящий догон — это **удержания** ключей, а не сон воркера: пока
	удержан один исполнитель или одно сообщество, остальные работают.

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
		cancel_pending_on_shutdown: bool = False,
		shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
		cooldown: Callable[[_J], float] | None = None,
		ready: Callable[[_J], bool] | None = None,
		sleep: Callable[[float], Coroutine[Any, Any, None]] | None = None,
		record: Callable[[_J, JobStatus, str | None], Coroutine[Any, Any, None]] | None = None,
		concurrency: int = 1,
		order: Callable[[_J], Any] | None = None,
	) -> None:
		"""Args:
		execute: исполнитель одного задания. Ошибку переводит сам
			(её текст попадёт на карточку), отмену сообщает броском
			:class:`JobCancelled`, а нужду подождать — броском
			:class:`JobDeferred`.
		name: имя очереди для сообщений в логе («обработка», «отправка»).
		cooldown: сколько секунд подождать после успешного задания,
			прежде чем брать следующее (0 — не ждать). Щадящий темп —
			предметное правило очереди: догон просроченных постов
			не должен выглядеть залпом (ADR-0016).
		ready: можно ли брать это задание в работу прямо сейчас
			(по умолчанию — да). Правило предметное: очередь отправки
			придерживает элемент, пока сохраняется его правка, иначе
			ушёл бы наполовину применённый черновик (ADR-0016, п. 7).
			Гарантия для таких правил: между этой проверкой и переводом
			задания в ``RUNNING`` нет точки приостановки — успевший
			взвести признак не опоздал.
		sleep: чем держатся паузы. По умолчанию — ожидание, которое
			прерывает остановка движка; подменяется в тестах, чтобы
			прогон не ждал настоящие минуты.
		cancel_pending_on_shutdown: помечать ли ожидающие задания
			отменёнными при остановке движка. Для очереди без
			персистентности это честно (после перезапуска её нет),
			для персистентной — нет: там задания переживают выход.
		shutdown_timeout_s: сколько ждать активное задание при остановке.
		record: крючок «запиши исход» — зовётся **до** того, как новый
			статус появится в памяти. Порядок именно такой: наблюдатель,
			увидевший исход в памяти, должен быть уверен, что хранилище
			о нём уже знает, а обрыв между шагами (остановка движка)
			оставляет хранилище в состоянии «не доделано» — это
			переживаемо, в отличие от обратного (ADR-0016). Очередь
			без хранилища крючка не передаёт.
		concurrency: сколько заданий может идти одновременно (не меньше
			одного). Замки заданий (:attr:`Job.locks`) и удержания
			действуют поверх: слот свободен, а задание всё равно ждёт,
			если его ресурс занят.
		order: ключ порядка выбора среди готовых (меньше — раньше);
			None — порядок постановки. Правило предметное: очередь
			отправки берёт срочные посты раньше плановых (ADR-0036).
		"""
		self._execute = execute
		self._record = record
		self._name = name
		self._cooldown = cooldown
		self._ready = ready
		self._sleep = sleep
		self._cancel_pending_on_shutdown = cancel_pending_on_shutdown
		self._shutdown_timeout_s = shutdown_timeout_s
		self._concurrency = max(1, concurrency)
		self._order = order
		self._jobs: list[_J] = []
		self._next_id = 1
		#: версия состояния очереди: растёт при смене состава и наблюдаемых
		#: полей заданий; подписчики узнают о ней одним уведомлением
		#: на итерацию цикла событий (ADR-0034)
		self._version = 0
		self._listeners: list[Callable[[int], None]] = []
		self._notify_scheduled = False
		self._worker: asyncio.Task[None] | None = None
		#: идущие задания: номер → задача выполнения
		self._running: dict[int, asyncio.Task[None]] = {}
		#: удержанные ключи ресурсов → задача, которая их отпустит
		self._holds: dict[Hashable, asyncio.Task[None]] = {}
		#: будильник воркера: новое задание, конец задания, отпущенный ключ
		self._wake: asyncio.Event | None = None
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
		job.bind_changes(self.mark_changed)
		self._jobs.append(job)
		self.mark_changed()

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
		try:
			self._jobs.remove(job)
		except ValueError:
			return  # уже снято — состояние не изменилось
		self.mark_changed()

	# --- версия состояния и подписка (ADR-0034) ------------------------------------

	@property
	def version(self) -> int:
		"""Версия состояния очереди: другое число — снимок ``state()`` другой."""
		return self._version

	def subscribe(self, listener: Callable[[int], None]) -> None:
		"""Подписывает на изменения: ``listener(version)`` в цикле событий движка.

		Уведомления сливаются: сколько бы изменений ни случилось за одну
		итерацию цикла (постановка пакета из ста заданий, переход статуса
		с записью пометки), подписчик получает одно с итоговой версией.
		Слушатель интерфейса — переправа моста (``ui_callback``): она
		безопасна к вызову из потока движка и лишь ставит событие в очередь
		потока интерфейса.
		"""
		self._listeners.append(listener)

	def unsubscribe(self, listener: Callable[[int], None]) -> None:
		"""Снимает подписку (незнакомый слушатель — не ошибка)."""
		with suppress(ValueError):
			self._listeners.remove(listener)

	def mark_changed(self) -> None:
		"""Поднимает версию и назначает уведомление подписчиков.

		Зовётся каркасом (состав очереди, наблюдаемые поля заданий)
		и сервисом — там, где снимок меняется без записи в задание
		(правка черновика поста меняет заголовок и вложение карточки).
		Вне работающего цикла событий (загрузка до старта, тесты)
		версия растёт, а уведомлять некого и нечем.
		"""
		self._version += 1
		if self._notify_scheduled or not self._listeners:
			return
		try:
			loop = asyncio.get_running_loop()
		except RuntimeError:
			return
		self._notify_scheduled = True
		loop.call_soon(self._notify)

	def _notify(self) -> None:
		"""Одно уведомление на итерацию: слушателям — итоговая версия.

		Сбой слушателя — его беда, не очереди: запись в журнал,
		остальные слушатели получают своё.
		"""
		self._notify_scheduled = False
		version = self._version
		for listener in list(self._listeners):
			try:
				listener(version)
			except Exception:  # noqa: BLE001 — сбой подписчика не роняет очередь
				logger.exception("%s: подписчик на изменения упал.", self._name)

	@property
	def stopping(self) -> bool:
		"""Движок останавливается — новую работу начинать нельзя."""
		return self._stop.is_set()

	@property
	def active_ids(self) -> frozenset[int]:
		"""Номера заданий, выполняющихся прямо сейчас."""
		return frozenset(self._running)

	def held(self, key: Hashable) -> bool:
		"""Удержан ли ключ ресурса (пауза после флуда, обрыва, догона)."""
		return key in self._holds

	# --- выполнение -----------------------------------------------------------

	def request_cancel(self, job: _J) -> None:
		"""Взводит отмену задания; исход запишет само выполнение.

		Каркас ставит только флаг — прервать работу может лишь тот,
		кто её ведёт: один исполнитель гасит посторонний процесс
		(ffmpeg отменой задачи не остановить), другой рвёт сетевую
		загрузку, но не запрос к БД, которым она подготовлена
		(ADR-0020). Граница отменяемого предметна, и каркасу
		её знать неоткуда.
		"""
		job.cancel_requested = True

	def reset_for_retry(self, job: _J, status: JobStatus = JobStatus.PENDING) -> None:
		"""Возвращает задание с ошибкой в очередь на новую попытку.

		Общий сброс состояния: доля выполнения, текст ошибки, пометка
		и флаг отмены. Предметное (перепроверить файл, обновить черновик,
		выбрать начальный статус) очередь делает до вызова — каркас
		знает только, как выглядит задание, готовое к новой попытке.

		Флаг отмены сбрасывается обязательно: он мог взвестись, если
		отмена совпала с ошибкой прошлой попытки, и тогда новая попытка
		либо отменилась бы сразу, либо приняла бы остановку движка
		за отмену человеком.

		Args:
			job: задание, возвращаемое в очередь.
			status: с какого состояния начать — ``PENDING`` (в работу)
				или ``WAITING``, если задание снова ждёт внешнего
				условия (слот отложек у очереди отправки, ADR-0016).
		"""
		job.status = status
		job.progress = 0.0
		job.error = None
		job.note = None
		job.cancel_requested = False
		self.ensure_worker()

	def ensure_worker(self) -> None:
		"""Запускает фоновую задачу выполнения, если она не крутится, и будит её."""
		if self._worker is None or self._worker.done():
			self._worker = asyncio.create_task(self._run())
			return
		self._wake_up()

	def _wake_up(self) -> None:
		"""Будит воркер: состав или ресурсы изменились — пора пересмотреть готовых."""
		if self._wake is not None:
			self._wake.set()

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
		а начатые задания доигрываются своим путём — флаг отмены им
		здесь не взводится (см. комментарий ниже: отмена человеком
		и остановка движка — разные исходы). Удержания снимаются:
		досиживать чужую паузу при выходе незачем. Задание,
		не завершившееся за отведённый срок, отменяется жёстко —
		последнее средство.
		"""
		self._stop.set()
		if self._cancel_pending_on_shutdown:
			for job in self._jobs:
				if job.status is JobStatus.PENDING:
					job.status = JobStatus.CANCELLED
		for hold in list(self._holds.values()):
			hold.cancel()
		self._holds.clear()
		self._wake_up()
		# флаг отмены активным заданиям здесь не взводится: он значит
		# «отмену запросил человек», и исход у неё другой (задание
		# покидает очередь). Остановку движка исполнитель узнаёт
		# по `stopping`, а сетевую часть рвёт сама очередь — её
		# недоделанное задание уйдёт после перезапуска (ADR-0020)
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

	async def _pause(self, seconds: float) -> None:
		"""Держит паузу (флуд, щадящий темп) — подменяемо в тестах.

		Отдельно от :meth:`wait_stop`: то — чистое ожидание остановки,
		и подменять его нельзя, иначе подмена зациклилась бы на себе.
		"""
		if self._sleep is not None:
			await self._sleep(seconds)
			return
		await self.wait_stop(seconds)

	# --- воркер ---------------------------------------------------------------

	async def _run(self) -> None:
		"""Раздаёт готовые задания по слотам, пока есть что делать.

		Цикл спит на будильнике: его будят новое задание, конец идущего
		и отпущенный ключ. Остановка движка выводит из цикла, когда
		идущие задания доиграют своим путём — их отмена приходит
		из :meth:`shutdown` запросом, а не отменой воркера.
		"""
		self._wake = asyncio.Event()
		try:
			while True:
				if not self.stopping:
					self._start_ready()
				idle = not self._running
				if idle and (self.stopping or (not self._holds and not self._eligible())):
					break
				self._wake.clear()
				await self._wake.wait()
		finally:
			self._wake = None

	def _start_ready(self) -> None:
		"""Занимает свободные слоты готовыми заданиями.

		Между выбором задания и переводом его в ``RUNNING`` нет точки
		приостановки — гарантия для предметных правил готовности
		(``ready``): успевший взвести признак не опоздал.
		"""
		while len(self._running) < self._concurrency:
			job = self._next_ready()
			if job is None:
				return
			job.status = JobStatus.RUNNING
			self._running[job.id] = asyncio.create_task(self._run_one(job))

	def _next_ready(self) -> _J | None:
		"""Первое готовое задание в порядке выбора (None — готовых нет)."""
		eligible = self._eligible()
		if not eligible:
			return None
		if self._order is not None:
			eligible.sort(key=self._order)
		return eligible[0]

	def _eligible(self) -> list[_J]:
		"""Задания, которые можно начать прямо сейчас, в порядке постановки.

		Готово задание, которое ждёт своей очереди, не придержано
		очередью (``ready``), не удержано само и чьи ресурсы свободны:
		ни один замок не занят идущим заданием и не удержан, а из
		альтернатив (кандидатов) хоть одна не удержана.
		"""
		locked: set[Hashable] = set()
		for job_id in self._running:
			running = self.get(job_id)
			if running is not None:
				locked.update(running.locks)
		return [
			job
			for job in self._jobs
			if job.status is JobStatus.PENDING
			and (self._ready is None or self._ready(job))
			and not self.held(_own(job))
			and not any(key in locked or self.held(key) for key in job.locks)
			and (not job.alternatives or any(not self.held(key) for key in job.alternatives))
		]

	def _hold(
		self, keys: frozenset[Hashable], seconds: float, *, note_of: _J | None = None
	) -> None:
		"""Удерживает ключи на срок; по истечении отпускает и будит воркер.

		Удержание — задача, а не отметка времени: пауза идёт через
		:meth:`_pause`, которую подменяют тесты и прерывает остановка.
		Повторное удержание того же ключа продлевает его: прежняя задача
		снимается, чтобы не отпустить ключ раньше нового срока.
		``note_of`` — задание, чью пометку снять, когда пауза кончится.
		"""
		for key in keys:
			previous = self._holds.pop(key, None)
			if previous is not None:
				previous.cancel()
		task = asyncio.create_task(self._release_later(keys, seconds, note_of))
		for key in keys:
			self._holds[key] = task

	async def _release_later(
		self, keys: frozenset[Hashable], seconds: float, note_of: _J | None
	) -> None:
		"""Отпускает ключи после паузы (тело задачи удержания)."""
		try:
			await self._pause(seconds)
		finally:
			task = asyncio.current_task()
			for key in keys:
				if self._holds.get(key) is task:
					del self._holds[key]
			if note_of is not None:
				note_of.note = None
			self._wake_up()

	async def _run_one(self, job: _J) -> None:
		"""Выполняет одно задание и записывает исход в его статус.

		Исходов четыре: успех (DONE), отмена (CANCELLED), ошибка (ERROR
		с текстом для человека) и отсрочка (:class:`JobDeferred` —
		задание возвращается ждать, ошибкой это не считается).
		Недоменные исключения сворачиваются так же, как это делает мост
		интерфейса, — карточка показывает текст как есть (контракт
		``errors.py``).

		Об отмене исполнитель сообщает броском :class:`JobCancelled` —
		в том числе когда её причиной была отмена его собственной
		сетевой задачи. Отмена, дошедшая до каркаса, означает другое:
		сносится цикл событий — исход недоделанного задания запишет
		следующий запуск.
		"""
		task = asyncio.create_task(self._execute(job))
		try:
			await task
		except JobCancelled:
			await self._apply(job, JobStatus.CANCELLED)
			logger.info("%s id=%s: отменено.", self._name, job.id)
		except JobDeferred as deferred:
			await self._defer(job, deferred)
		except asyncio.CancelledError:
			task.cancel()
			raise
		except Exception as exc:  # noqa: BLE001 — исход задания, не очереди
			await self._apply(job, JobStatus.ERROR, user_message(exc))
			logger.exception("%s id=%s: не удалось.", self._name, job.id)
		else:
			await self._apply(job, JobStatus.DONE)
			job.progress = 1.0
			self._cool_down(job)
		finally:
			self._running.pop(job.id, None)
			self._wake_up()

	async def _apply(self, job: _J, status: JobStatus, error: str | None = None) -> None:
		"""Переводит задание в новый статус, сперва сохранив исход.

		Порядок «хранилище → память» — инвариант очередей с БД
		(ADR-0016): статус, видный в памяти, уже сохранён.

		Отказ хранилища (диск кончился, БД заблокирована) исход
		не отменяет: работа уже сделана, и молчать о ней нельзя.
		Воркера такой отказ не роняет — иначе задание навсегда
		осталось бы «выполняется», а очередь встала бы целиком,
		не начав следующее. Поэтому сбой записи идёт в журнал
		с полной трассировкой, на карточку — пометкой, а статус
		в память применяется всё равно: человек видит исход
		и предупреждение, что сохранить его не удалось.
		"""
		if self._record is not None:
			try:
				await self._record(job, status, error)
			except Exception:  # noqa: BLE001 — сбой хранилища не исход задания
				logger.exception(
					"%s id=%s: исход «%s» не сохранён — хранилище отказало.",
					self._name,
					job.id,
					status,
				)
				job.warning = OUTCOME_NOT_SAVED_NOTE
		job.status = status
		job.error = error

	async def _defer(self, job: _J, deferred: JobDeferred) -> None:
		"""Возвращает задание в ожидание и удерживает, что просили.

		Статус записывается сразу: карточка всё время паузы показывает
		пометку состояния. Сама пауза — удержание ключей (ADR-0036):
		названных в отсрочке (исполнитель под флуд-лимитом) или самого
		задания; соседи, которым эти ресурсы не нужны, идут дальше.
		Остановка движка прерывает удержание, не заставляя `shutdown`
		досиживать чужой срок.
		"""
		await self._apply(job, deferred.status)
		job.progress = 0.0
		job.note = deferred.note
		logger.info("%s id=%s: отложено (%s).", self._name, job.id, deferred.status)
		if deferred.delay_s > 0 and not self.stopping:
			keys = deferred.hold if deferred.hold else frozenset({_own(job)})
			self._hold(keys, deferred.delay_s, note_of=job)

	def _cool_down(self, job: _J) -> None:
		"""Удерживает ресурсы задания после успеха, если очередь так просит.

		Пауза идёт при уже записанном исходе: карточка показывает
		«готово», а не мнимую работу. Удерживаются замки задания
		(у очереди отправки — сообщество: щадящий догон не должен
		выглядеть залпом, ADR-0016), а у задания без замков — вся
		очередь. Ждать незачем, если следующего задания нет.
		"""
		if self._cooldown is None or self.stopping:
			return
		delay = self._cooldown(job)
		if delay <= 0 or not any(
			other.status is JobStatus.PENDING and other.id != job.id for other in self._jobs
		):
			return
		logger.info("%s: пауза %.0f с перед следующим заданием.", self._name, delay)
		self._hold(job.locks or frozenset({_QUEUE}), delay)


#: Ключ удержания всей очереди — для заданий без замков.
_QUEUE = object()


def _own(job: Job) -> Hashable:
	"""Личный ключ задания: его удержание останавливает только его."""
	return ("job", job.id)
