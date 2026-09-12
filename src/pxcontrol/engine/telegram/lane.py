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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import IntEnum

from pxcontrol.engine.telegram.types import TelegramFloodError

logger = logging.getLogger(__name__)

#: Минимальный зазор между запросами одного аккаунта, секунды.
#: Точных цифр Telegram не публикует (ADR-0017 запрещал гадать о них
#: до появления потребителя), поэтому значение консервативное: около
#: трёх запросов в секунду. Редким операциям (публикация, проверка прав)
#: зазор незаметен — он срабатывает только на запросах подряд, то есть
#: там, где и нужен: в массовых проходах по истории и участникам.
DEFAULT_MIN_INTERVAL_S = 0.3


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
		account_id: int,
		min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
		*,
		clock: Callable[[], float] = time.monotonic,
		sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
	) -> None:
		"""Args:
		account_id: id аккаунта — только для сообщений в логе.
		min_interval_s: минимальный зазор между запросами (0 — без зазора).
		clock: источник монотонного времени (подменяется в тестах).
		sleep: способ подождать (подменяется в тестах).
		"""
		self._account_id = account_id
		self._min_interval_s = min_interval_s
		self._clock = clock
		self._sleep = sleep
		self._busy = False
		# ожидающие: (приоритет, номер по порядку, обещание разбудить).
		# Номер — и разрешение ничьих внутри приоритета (кто раньше встал,
		# тот раньше пойдёт), и защита от сравнения самих обещаний.
		self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
		self._counter = itertools.count()
		self._last_at: float | None = None
		self._frozen_until: float | None = None

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
			"Аккаунт id=%s под флуд-лимитом: запросы к нему приостановлены на %.0f с.",
			self._account_id,
			seconds,
		)

	@asynccontextmanager
	async def slot(self, priority: TelegramPriority) -> AsyncIterator[None]:
		"""Занимает дорожку под одну операцию аккаунта.

		Тело блока выполняется, когда дорожка свободна и зазор выдержан.
		Флуд-лимит, вылетевший из тела, замораживает дорожку — остальные
		операции аккаунта узнают об этом, не обращаясь к Telegram.

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
		try:
			yield
		except TelegramFloodError as exc:
			self.freeze(exc.retry_after_s)
			raise
		finally:
			# отсчёт зазора — от конца обращения: длинная загрузка это
			# поток запросов, и её завершение тоже обращение к серверу,
			# поэтому залпа сразу после неё быть не должно
			self._last_at = self._clock()
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
