"""Мост «интерфейс → движок».

Корутина выполняется в цикле событий движка (фоновый поток), а результат
возвращается в поток интерфейса сигналом Qt — окно не блокируется. Это
образец обращения к движку для всех экранов.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal

from pxcontrol.engine import EngineWorker


class _AsyncCall(QObject):
	"""Одноразовый носитель сигналов результата (живёт в потоке интерфейса)."""

	done = Signal(object)
	failed = Signal(str)


def run_in_engine(
	worker: EngineWorker,
	coro: Coroutine[Any, Any, Any],
	parent: QObject,
	on_done: Callable[[Any], None],
	on_error: Callable[[str], None],
) -> None:
	"""Запускает корутину в движке; колбэки вызываются в потоке интерфейса.

	Args:
		worker: Работающий носитель движка.
		coro: Корутина движка (например, ``engine.accounts.list_bots()``).
		parent: Владелец временного QObject (обычно страница).
		on_done: Колбэк успеха — получает результат корутины.
		on_error: Колбэк ошибки — получает текст ошибки.
	"""
	call = _AsyncCall(parent)
	call.done.connect(on_done)
	call.failed.connect(on_error)
	call.done.connect(lambda _result: call.deleteLater())
	call.failed.connect(lambda _error: call.deleteLater())
	future = worker.submit(coro)

	def _finished(fut: Any) -> None:
		try:
			call.done.emit(fut.result())
		except Exception as exc:  # noqa: BLE001 — любую ошибку показываем в UI
			call.failed.emit(str(exc))

	future.add_done_callback(_finished)
