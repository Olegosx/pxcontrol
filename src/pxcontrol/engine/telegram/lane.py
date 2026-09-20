"""Дорожка аккаунта Telegram: очередь, зазор, приоритет, заморозка (ADR-0024).

Единица дефицита в Telegram — **аккаунт**: его нельзя дёргать параллельно,
нельзя опрашивать быстрее некоторого темпа, а флуд-лимит («подождите N
секунд») сервер назначает аккаунту целиком, а не отдельной операции.
Дорожка выражает это одним объектом: операции одного аккаунта проходят
через неё по очереди (приоритет решает, кто следующий), между запросами
выдерживается зазор, а пойманный флуд-лимит замораживает дорожку до
названного сервером срока — и следующий желающий получает отказ сразу,
не тревожа Telegram (настойчивость сроки только удлиняет, ADR-0017).

**Дорожка не ждёт за вызывающего.** Заморозку обойти нельзя никому,
поэтому вместо молчаливого сна наружу уходит :class:`TelegramFloodError`
с остатком срока — тот же класс, что приходит от самого Telegram.
Кто умеет ждать (очередь отправки), подождёт сам своим прерываемым
ожиданием (ADR-0020); кто не умеет (фоновые чтения), пропустит аккаунт
до следующего прохода. Так дорожка не заводит внутри себя долгих пауз,
которые нечем прервать при остановке движка.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pxcontrol.engine.telegram.types import ExecutorRef, OwnerKind, TelegramFloodError

logger = logging.getLogger(__name__)

#: Минимальный зазор между запросами одного аккаунта, секунды.
#: Точных цифр Telegram не публикует (ADR-0017 запрещал гадать о них
#: до появления потребителя), поэтому значение консервативное: около
#: трёх запросов в секунду. Редким операциям (публикация, проверка прав)
#: зазор незаметен — он срабатывает только на запросах подряд, то есть
#: там, где и нужен: в массовых проходах по истории и участникам.
DEFAULT_MIN_INTERVAL_S = 0.3

#: Зазор дорожки бота (ADR-0030): у Bot API лимиты пер-бот и щедрые
#: (десятки сообщений в секунду), темп держать незачем — дорожка нужна
#: боту ради очереди, приоритета и заморозки после «подождите N секунд».
BOT_MIN_INTERVAL_S = 0.0


class Outcome(StrEnum):
	"""Исход операции на дорожке (ADR-0030).

	Флуд-лимит и отмена — не ошибки: первый — состояние аккаунта,
	названное сервером, вторая — решение человека (обрыв загрузки).
	"""

	OK = "ok"
	ERROR = "error"
	FLOOD = "flood"
	CANCELLED = "cancelled"


@dataclass(frozen=True)
class OperationRecord:
	"""Одна завершённая операция на дорожке — единица учёта активности.

	Attributes:
		owner: чья дорожка.
		kind: вид операции — приоритет, с которым шлюз занял дорожку
			(публикация, действие человека, обслуживание, фон).
		started_at: момент начала обращения (после очереди и зазора).
		finished_at: момент конца — по нему считается занятость.
		outcome: исход.
		wait_s: срок, названный Telegram при флуд-лимите (0 — не было).
	"""

	owner: ExecutorRef
	kind: TelegramPriority
	started_at: datetime
	finished_at: datetime
	outcome: Outcome
	wait_s: int = 0


#: Предел вместимости журнала операций. Штатно журнал живёт секунды —
#: учёт активности забирает его раз в 10 с (ADR-0030); предел нужен
#: единственному случаю: хранилище отказывает подряд, пачка за пачкой
#: возвращается обратно, и без предела журнал рос бы в памяти без края
#: и молча. Десять тысяч записей — это часы работы даже при десятке
#: сообществ, то есть с запасом переживаемый обрыв.
LOG_CAPACITY = 10_000


class OperationLog:
	"""Журнал выполненных операций: пишут дорожки, забирает учёт (ADR-0030).

	Список записей **принадлежит журналу** и наружу не отдаётся:
	:meth:`drain` опустошает его на месте, а не подменяет новым. Это
	не мелочь стиля, а инвариант: дорожки держат ссылку на журнал
	надолго (всю жизнь аккаунта), и стоит читателю подменить список —
	пишущие остаются у прежнего, которого никто больше не читает.
	Именно так учёт и терял всё, кроме первых секунд работы: ошибка
	была невидима, потому что правило «не подменять» нигде не жило,
	кроме головы автора. Теперь оно живёт в типе — списка снаружи
	просто нет.

	Переполнение (см. :data:`LOG_CAPACITY`) вытесняет самые старые
	записи и сообщает об этом в журнал приложения: потеря названа,
	а не случается молча.
	"""

	def __init__(self, capacity: int = LOG_CAPACITY) -> None:
		"""Args:
		capacity: сколько записей журнал держит, прежде чем вытеснять
			самые старые (не меньше одной).
		"""
		self._items: list[OperationRecord] = []
		self._capacity = max(1, capacity)
		#: вытеснено с прошлой выемки — счётчик эпизода переполнения
		self._dropped = 0

	def __len__(self) -> int:
		"""Сколько записей ждут выемки."""
		return len(self._items)

	def record(self, record: OperationRecord) -> None:
		"""Принимает запись о завершённой операции (зовёт дорожка)."""
		self._items.append(record)
		self._trim()

	def drain(self) -> list[OperationRecord]:
		"""Забирает накопленные записи, опустошая журнал на месте.

		Returns:
			Записи в порядке появления (журнал остаётся пустым).
		"""
		items = list(self._items)
		self._items.clear()
		if self._dropped:
			logger.warning(
				"Учёт активности: %d записей вытеснено переполнением журнала.", self._dropped
			)
			self._dropped = 0
		return items

	def restore(self, records: Sequence[OperationRecord]) -> None:
		"""Возвращает записи в журнал вперёд свежих (сброс в БД не удался)."""
		self._items[:0] = list(records)
		self._trim()

	def _trim(self) -> None:
		"""Держит вместимость, вытесняя самые старые записи.

		Первое вытеснение эпизода сообщается сразу — иначе о потере
		узнали бы только при следующей удачной выемке, а её может
		и не случиться.
		"""
		excess = len(self._items) - self._capacity
		if excess <= 0:
			return
		del self._items[:excess]
		if not self._dropped:
			logger.warning(
				"Учёт активности: журнал переполнен (%d записей) — вытесняю самые старые. "
				"Похоже, сброс в базу не проходит.",
				self._capacity,
			)
		self._dropped += excess


@dataclass(frozen=True)
class LaneLiveState:
	"""Живое состояние дорожки — снимок для показа (ADR-0030).

	Attributes:
		busy_kind: вид идущей операции; None — дорожка свободна.
		busy_since: когда идущая операция началась.
		waiting: сколько операций ждут своей очереди.
		frozen_for_s: остаток заморозки после флуд-лимита (0 — нет).
	"""

	busy_kind: TelegramPriority | None
	busy_since: datetime | None
	waiting: int
	frozen_for_s: float


class TelegramPriority(IntEnum):
	"""Кто из ожидающих занимает дорожку следующим (меньше — важнее).

	Значения с шагом 10: между уровнями есть место для новых видов
	работы без перенумерации существующих. Приоритет решает только
	порядок в очереди ожидания — обойти заморозку он не помогает
	никому, это физика Telegram, а не наше правило.
	"""

	#: Публикация постов — то, ради чего приложение существует
	#: (ADR-0017: публикация приоритетнее чтения).
	PUBLISH = 10
	#: Человек ждёт ответа на экране: проверка доступов, список тем.
	INTERACTIVE = 20
	#: Массовое обслуживание сообщества (ADR-0026): сотни однотипных
	#: запросов подряд. Ниже интерактива — человек не должен ждать
	#: на экране, пока идёт уборка; выше фона — уборку он запустил сам.
	MAINTENANCE = 25
	#: Фоновые чтения: статистика сообществ, расписание, дозор слотов.
	BACKGROUND = 30


class AccountLane:
	"""Последовательный доступ к одному аккаунту Telegram.

	Экземпляр живёт рядом с транспортом аккаунта в пуле шлюза
	(ADR-0019) и переживает переподключения: флуд-лимит Telegram
	назначает аккаунту, а не соединению.
	"""

	def __init__(
		self,
		owner: ExecutorRef,
		min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
		*,
		clock: Callable[[], float] = time.monotonic,
		sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
		wall_clock: Callable[[], datetime] | None = None,
		log: OperationLog | None = None,
	) -> None:
		"""Args:
		owner: владелец дорожки — для записей активности и сообщений в логе.
		min_interval_s: минимальный зазор между запросами (0 — без зазора).
		clock: источник монотонного времени — зазор и заморозка
			(подменяется в тестах).
		sleep: способ подождать (подменяется в тестах).
		wall_clock: настенное время для записей активности (ADR-0030);
			None — текущее UTC.
		log: журнал, принимающий запись о каждой завершённой операции;
			None — учёт не ведётся. Именно журнал, а не произвольный
			колбэк: дорожка держит эту ссылку всю свою жизнь, и хранилище
			записей обязано переживать выемки (см. :class:`OperationLog`).
		"""
		self._owner = owner
		self._min_interval_s = min_interval_s
		self._clock = clock
		self._sleep = sleep
		self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
		self._log = log
		self._busy = False
		# ожидающие: (приоритет, номер по порядку, обещание разбудить).
		# Номер — и разрешение ничьих внутри приоритета (кто раньше встал,
		# тот раньше пойдёт), и защита от сравнения самих обещаний.
		self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
		self._counter = itertools.count()
		self._last_at: float | None = None
		self._frozen_until: float | None = None
		# идущая операция: вид и момент начала (None — дорожка свободна
		# или занята, но ещё не дошла до обращения: очередь, зазор)
		self._busy_kind: TelegramPriority | None = None
		self._busy_since: datetime | None = None

	@property
	def owner(self) -> ExecutorRef:
		"""Владелец дорожки."""
		return self._owner

	def live_state(self) -> LaneLiveState:
		"""Снимок живого состояния: чем занята, сколько ждут, заморозка."""
		return LaneLiveState(
			busy_kind=self._busy_kind,
			busy_since=self._busy_since,
			waiting=sum(1 for _p, _o, waiter in self._waiters if not waiter.done()),
			frozen_for_s=self.frozen_for(),
		)

	def frozen_for(self) -> float:
		"""Сколько секунд дорожка ещё заморожена (0.0 — свободна).

		Заодно снимает истёкшую заморозку: отдельного будильника нет,
		состояние проверяется по обращению.
		"""
		if self._frozen_until is None:
			return 0.0
		remaining = self._frozen_until - self._clock()
		if remaining <= 0:
			self._frozen_until = None
			return 0.0
		return remaining

	def freeze(self, seconds: float) -> None:
		"""Замораживает дорожку на названный Telegram срок.

		Более ранний срок действующую заморозку не сокращает: сервер
		мог назвать больший срок другой операции, и забывать его нельзя.
		"""
		until = self._clock() + max(0.0, seconds)
		if self._frozen_until is not None and until <= self._frozen_until:
			return
		self._frozen_until = until
		logger.warning(
			"%s id=%s под флуд-лимитом: запросы приостановлены на %.0f с.",
			"Бот" if self._owner.kind is OwnerKind.BOT else "Аккаунт",
			self._owner.id,
			seconds,
		)

	@asynccontextmanager
	async def slot(self, priority: TelegramPriority) -> AsyncIterator[None]:
		"""Занимает дорожку под одну операцию аккаунта.

		Тело блока выполняется, когда дорожка свободна и зазор выдержан.
		Флуд-лимит, вылетевший из тела, замораживает дорожку — остальные
		операции аккаунта узнают об этом, не обращаясь к Telegram.
		Каждое выполненное тело — одна запись активности (ADR-0030):
		время от входа в тело до выхода и исход; отказ до тела (заморозка)
		не записывается — обращения к Telegram не было.

		Args:
			priority: место в очереди ожидания (:class:`TelegramPriority`).

		Raises:
			TelegramFloodError: Дорожка заморожена — тело не выполнялось,
				Telegram не потревожен; в ``retry_after_s`` остаток срока.
		"""
		self._require_free()  # быстрый отказ, не занимая очередь
		await self._acquire(priority)
		try:
			# пока стояли в очереди, дорожку мог заморозить сосед
			self._require_free()
			await self._respect_interval()
		except BaseException:
			self._pass_on()  # слот не пригодился — отдаём следующему
			raise
		started = self._wall_clock()
		self._busy_kind, self._busy_since = priority, started
		outcome, wait_s = Outcome.OK, 0
		try:
			yield
		except TelegramFloodError as exc:
			self.freeze(exc.retry_after_s)
			outcome, wait_s = Outcome.FLOOD, exc.retry_after_s
			raise
		except asyncio.CancelledError:
			outcome = Outcome.CANCELLED  # обрыв загрузки человеком (ADR-0020)
			raise
		except BaseException:
			outcome = Outcome.ERROR
			raise
		finally:
			# отсчёт зазора — от конца обращения: длинная загрузка это
			# поток запросов, и её завершение тоже обращение к серверу,
			# поэтому залпа сразу после неё быть не должно
			self._last_at = self._clock()
			self._busy_kind, self._busy_since = None, None
			if self._log is not None:
				self._log.record(
					OperationRecord(
						self._owner, priority, started, self._wall_clock(), outcome, wait_s
					)
				)
			self._pass_on()

	def _require_free(self) -> None:
		"""Отказывает, пока действует заморозка.

		Raises:
			TelegramFloodError: Остаток срока — в ``retry_after_s``.
		"""
		remaining = self.frozen_for()
		if remaining <= 0:
			return
		seconds = math.ceil(remaining)
		raise TelegramFloodError(
			f"Telegram просит подождать ещё {seconds} с — аккаунт под флуд-лимитом.",
			retry_after_s=seconds,
		)

	async def _acquire(self, priority: TelegramPriority) -> None:
		"""Ждёт своей очереди на дорожке."""
		if not self._busy:
			self._busy = True
			return
		waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
		heapq.heappush(self._waiters, (int(priority), next(self._counter), waiter))
		try:
			await waiter
		except asyncio.CancelledError:
			# отмена могла прийти уже после передачи владения — тогда
			# дорожка осталась бы занятой навсегда; отдаём её дальше
			if waiter.done() and not waiter.cancelled():
				self._pass_on()
			raise

	def _pass_on(self) -> None:
		"""Передаёт дорожку следующему по приоритету или освобождает.

		Отменённые ожидающие пропускаются: снимать их из кучи в момент
		отмены дороже, чем не заметить здесь.
		"""
		while self._waiters:
			_priority, _order, waiter = heapq.heappop(self._waiters)
			if not waiter.done():
				waiter.set_result(None)  # владение переходит к нему
				return
		self._busy = False

	async def _respect_interval(self) -> None:
		"""Выдерживает зазор с предыдущим запросом этого аккаунта."""
		if self._last_at is None or self._min_interval_s <= 0:
			return
		pause = self._min_interval_s - (self._clock() - self._last_at)
		if pause > 0:
			await self._sleep(pause)
