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
		try:
			# конструктор тоже под защитой: его сбой (например, битый адрес
			# БД в .env) иначе оставил бы start() ждать _ready вечно —
			# приложение зависло бы молча, без окна и без ошибки
			self._engine = Engine(self._settings)
			self._loop.run_until_complete(self._engine.start())
		except BaseException as exc:  # noqa: BLE001 — ошибку пробрасываем в start()
			self._error = exc
			self._ready.set()
			self._loop.close()
			return
		self._ready.set()
		self._loop.run_forever()
		try:
			# порядок важен (ADR-0020): сначала штатная остановка — очереди
			# гасят своих воркеров кооперативно, не обрывая запросов к БД;
			# отмена всех задач до неё убивала бы воркеров жёстко, посреди
			# запроса, и shutdown очередей доставался уже мёртвым задачам.
			# Отмена уцелевших — в finally: даже при ошибке остановки
			# задачи не должны быть уничтожены висящими
			try:
				self._loop.run_until_complete(self._engine.stop())
			finally:
				self._cancel_pending(self._loop)
		except Exception:  # noqa: BLE001 — завершение не должно ронять поток
			# ошибка остановки (сеть у Telethon, диск у БД) — в лог; цикл
			# всё равно закрывается, иначе поток умрёт с сырой трассировкой
			logger.exception("Ошибка при остановке движка.")
		finally:
			self._loop.close()

	@staticmethod
	def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
		"""Отменяет задачи, уцелевшие после остановки движка.

		Очереди к этому моменту уже погасили своих воркеров
		(:meth:`Engine.stop`, ADR-0020); здесь остаются только задачи,
		поставленные из интерфейса через :meth:`submit` в момент
		закрытия. Без отмены они бы висели: asyncio написал бы «Task was
		destroyed but it is pending», а их блоки ``finally``
		не выполнились бы. Запросов к БД у них в полёте нет — база уже
		закрыта остановкой движка.
		"""
		pending = asyncio.all_tasks(loop)
		for task in pending:
			task.cancel()
		if pending:
			loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

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
		# после аварийного старта/остановки цикл существует, но закрыт:
		# без проверки наружу летел бы «Event loop is closed» из глубин
		# asyncio плюс предупреждение о непробуждённой корутине
		if self._loop is None or self._loop.is_closed():
			coro.close()
			raise RuntimeError("Движок не запущен")
		return asyncio.run_coroutine_threadsafe(coro, self._loop)

	def stop(self, timeout: float = 10.0) -> None:
		"""Останавливает цикл и дожидается завершения потока.

		Безопасен и после неудачного :meth:`start`: закрытый цикл
		(движок не стартовал) не трогаем, только дожидаемся потока.
		"""
		if self._loop is not None and not self._loop.is_closed():
			try:
				self._loop.call_soon_threadsafe(self._loop.stop)
			except RuntimeError:
				# узкое окно: аварийная ветка _run закрыла цикл между
				# нашей проверкой и вызовом — остановка уже не нужна
				logger.debug("Цикл движка закрылся раньше запроса остановки.")
		if self._thread is not None:
			self._thread.join(timeout=timeout)
			if self._thread.is_alive():
				logger.warning(
					"Поток движка не завершился за %.0f с — процесс закроет его принудительно.",
					timeout,
				)
