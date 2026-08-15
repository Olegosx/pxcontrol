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


def _safe_emit(signal: Any, payload: Any) -> None:
	"""Излучает сигнал, переживая удаление носителя в момент доставки.

	Колбэк выполняется в потоке движка: владелец может быть удалён
	потоком интерфейса между проверкой isValid и излучением — тогда
	shiboken бросает RuntimeError, а результат просто выбрасывается.
	"""
	try:
		signal.emit(payload)
	except RuntimeError:
		logger.debug("Результат операции движка выброшен: владелец удалён в момент доставки.")


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
		# результат — отдельно от излучения: RuntimeError может быть
		# и честной ошибкой операции (ffmpeg), и признаком удалённого
		# носителя при emit — смешивать их в одном try нельзя
		try:
			result = fut.result()
		except Exception as exc:  # noqa: BLE001 — любую ошибку показываем в UI
			# полный трейсбек — в лог; пользователю — читаемый текст:
			# доменные ошибки как есть, неожиданные — короткой сводкой
			# (дампы СУБД/библиотек в интерфейс не попадают)
			logger.exception("Ошибка операции движка: %s", exc)
			_safe_emit(call.failed, user_message(exc))
			return
		_safe_emit(call.done, result)

	future.add_done_callback(_finished)
