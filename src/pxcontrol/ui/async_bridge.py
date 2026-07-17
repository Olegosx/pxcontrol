"""Мост «интерфейс → движок».

Корутина выполняется в цикле событий движка (фоновый поток), а результат
возвращается в поток интерфейса сигналом Qt — окно не блокируется. Это
образец обращения к движку для всех экранов.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from PySide6.QtCore import QObject, Signal
from shiboken6 import isValid

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.errors import user_message

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class _AsyncCall(QObject):
	"""Одноразовый носитель сигналов результата (живёт в потоке интерфейса)."""

	done = Signal(object)
	failed = Signal(str)


def run_in_engine(
	worker: EngineWorker,
	coro: Coroutine[Any, Any, _T],
	parent: QObject,
	on_done: Callable[[_T], None] | Callable[[], None],
	on_error: Callable[[str], None] | Callable[[], None],
) -> None:
	"""Запускает корутину в движке; колбэки вызываются в потоке интерфейса.

	Тип результата сквозной: mypy сверяет корутину с ``on_done``.

	Args:
		worker: Работающий носитель движка.
		coro: Корутина движка (например, ``engine.accounts.list_bots()``).
		parent: Владелец временного QObject (обычно страница). Если
			владельца удалили до завершения корутины (закрытый диалог),
			результат тихо выбрасывается — колбэки не вызываются.
		on_done: Колбэк успеха. Может принимать результат корутины одним
			аргументом или не принимать ничего — Qt усечёт лишнее.
		on_error: Колбэк ошибки — получает текст ошибки (или ничего).
	"""
	call = _AsyncCall(parent)
	call.done.connect(on_done)
	call.failed.connect(on_error)
	call.done.connect(lambda _result: call.deleteLater())
	call.failed.connect(lambda _error: call.deleteLater())
	future = worker.submit(coro)

	def _finished(fut: Any) -> None:
		# носитель — ребёнок владельца: удалили владельца (диалог закрыт,
		# страница умерла) — излучать сигнал уже некуда и опасно
		if not isValid(call):
			logger.debug("Результат операции движка выброшен: владелец удалён.")
			return
		try:
			call.done.emit(fut.result())
		except Exception as exc:  # noqa: BLE001 — любую ошибку показываем в UI
			# полный трейсбек — в лог; пользователю — читаемый текст:
			# доменные ошибки как есть, неожиданные — короткой сводкой
			# (дампы СУБД/библиотек в интерфейс не попадают)
			logger.exception("Ошибка операции движка: %s", exc)
			call.failed.emit(user_message(exc))

	future.add_done_callback(_finished)
