"""Планировщик на APScheduler (AsyncIOScheduler)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class Scheduler:
	"""Обёртка над APScheduler для отложенного постинга.

	Работает в цикле событий движка. Пропущенные за время простоя задачи
	догоняются отдельно (см. :meth:`PostsService.publish_due`).
	"""

	def __init__(self, timezone: str = "UTC") -> None:
		self._timezone = timezone
		self._scheduler: Any | None = None

	def start(self) -> None:
		"""Создаёт и запускает планировщик."""
		from apscheduler.schedulers.asyncio import AsyncIOScheduler

		self._scheduler = AsyncIOScheduler(timezone=self._timezone)
		self._scheduler.start()
		logger.info("Планировщик запущен (TZ=%s).", self._timezone)

	def shutdown(self) -> None:
		"""Останавливает планировщик."""
		if self._scheduler is not None:
			self._scheduler.shutdown(wait=False)
			self._scheduler = None

	def schedule(self, run_at: datetime, func: Callable[..., Any], *args: Any) -> None:
		"""Ставит разовую задачу на указанное время.

		Args:
			run_at: Момент запуска.
			func: Вызываемый объект (обычно корутинная функция).
			*args: Аргументы для ``func``.
		"""
		if self._scheduler is None:
			raise RuntimeError("Планировщик не запущен")
		self._scheduler.add_job(func, "date", run_date=run_at, args=args)
