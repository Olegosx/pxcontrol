"""Запуск движка в фоновом потоке со своим циклом событий asyncio.

Интерфейс (Qt) работает в главном потоке, а асинхронная и тяжёлая работа
движка — в отдельном потоке, чтобы окно не зависало (см. ADR-0006). Вызвать
корутину движка из потока интерфейса можно через :meth:`EngineWorker.submit`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

from pxcontrol.config import Settings
from pxcontrol.engine.engine import Engine

logger = logging.getLogger(__name__)


class EngineWorker:
	"""Владеет фоновым потоком и циклом событий, в которых живёт движок."""

	def __init__(self, settings: Settings) -> None:
		self._settings = settings
		self._engine: Engine | None = None
		self._loop: asyncio.AbstractEventLoop | None = None
		self._thread: threading.Thread | None = None
		self._ready = threading.Event()
		self._error: BaseException | None = None

	def start(self) -> None:
		"""Запускает поток и ждёт готовности движка (или ошибки запуска)."""
		self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
		self._thread.start()
		self._ready.wait()
		if self._error is not None:
			raise RuntimeError("Не удалось запустить движок") from self._error

	def _run(self) -> None:
		"""Тело потока: создаёт цикл, стартует движок, крутит цикл."""
		self._loop = asyncio.new_event_loop()
		asyncio.set_event_loop(self._loop)
		self._engine = Engine(self._settings)
		try:
			self._loop.run_until_complete(self._engine.start())
		except BaseException as exc:  # noqa: BLE001 — ошибку пробрасываем в start()
			self._error = exc
			self._ready.set()
			self._loop.close()
			return
		self._ready.set()
		self._loop.run_forever()
		self._loop.run_until_complete(self._engine.stop())
		self._loop.close()

	@property
	def engine(self) -> Engine:
		"""Движок (доступен после успешного :meth:`start`)."""
		if self._engine is None:
			raise RuntimeError("Движок не запущен")
		return self._engine

	def submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
		"""Планирует корутину в цикле движка из другого потока.

		Args:
			coro: Корутина движка.

		Returns:
			``Future`` с результатом выполнения.
		"""
		if self._loop is None:
			raise RuntimeError("Движок не запущен")
		return asyncio.run_coroutine_threadsafe(coro, self._loop)

	def stop(self, timeout: float = 10.0) -> None:
		"""Останавливает цикл и дожидается завершения потока.

		Безопасен и после неудачного :meth:`start`: закрытый цикл
		(движок не стартовал) не трогаем, только дожидаемся потока.
		"""
		if self._loop is not None and not self._loop.is_closed():
			self._loop.call_soon_threadsafe(self._loop.stop)
		if self._thread is not None:
			self._thread.join(timeout=timeout)
