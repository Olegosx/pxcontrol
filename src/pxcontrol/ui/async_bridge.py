"""Мост «интерфейс → движок».

Корутина выполняется в цикле событий движка (фоновый поток), а результат
возвращается в поток интерфейса сигналом Qt — окно не блокируется. Это
образец обращения к движку для всех экранов.

Устройство (важно для безопасности потоков): поток движка излучает
сигналы ТОЛЬКО на бессмертном маршрутизаторе (:class:`_Dispatcher`),
который живёт в потоке интерфейса и не удаляется до конца работы.
Излучать на одноразовых объектах-носителях нельзя: интерфейс удаляет
их (``deleteLater``), пока поток движка может находиться внутри
Qt-механики излучения — это использование освобождённой памяти
и сегфолт в libQt6Core (ловилось дампами ядра: поток «engine»,
PySide SignalManager::qt_metacall). Проверки ``isValid`` перед
излучением не спасают — между проверкой и излучением объект успевает
освободиться. Владелец колбэков проверяется уже в потоке интерфейса,
где удаление с проверкой не гонится.
"""

from __future__ import annotations

import inspect
import itertools
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from PySide6.QtCore import QObject, Signal
from shiboken6 import isValid

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.errors import user_message

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Виды параметров, способные принять позиционный аргумент.
_POSITIONAL_KINDS = (
	inspect.Parameter.POSITIONAL_ONLY,
	inspect.Parameter.POSITIONAL_OR_KEYWORD,
	inspect.Parameter.VAR_POSITIONAL,
)


def _accepts_argument(callback: Callable[..., None]) -> bool:
	"""Может ли колбэк принять один позиционный аргумент.

	Прежний мост подключал колбэки к сигналам напрямую, и Qt усекал
	лишние аргументы — колбэки без параметров легальны по контракту.
	Теперь колбэки зовутся вручную, усечение воспроизводится здесь.
	"""
	try:
		signature = inspect.signature(callback)
	except (TypeError, ValueError):  # встроенный без сигнатуры — передаём как есть
		return True
	return any(parameter.kind in _POSITIONAL_KINDS for parameter in signature.parameters.values())


def _invoke(callback: Callable[..., None], payload: object) -> None:
	"""Зовёт колбэк с результатом или без (см. :func:`_accepts_argument`)."""
	if _accepts_argument(callback):
		callback(payload)
	else:
		callback()


class _Dispatcher(QObject):
	"""Бессмертный маршрутизатор событий движка (живёт в потоке интерфейса).

	Разовые результаты (:meth:`register`) доставляются один раз и запись
	снимается; подписки на повторяющиеся события (:meth:`register_listener`,
	прогресс) живут, пока жив владелец, — запись снимается при первой
	доставке после его удаления.
	"""

	_delivered = Signal(int, bool, object)  # токен, успех, результат или текст ошибки
	_progressed = Signal(int, object)  # токен подписки, кортеж аргументов

	def __init__(self) -> None:
		super().__init__()
		self._tokens = itertools.count(1)
		self._pending: dict[int, tuple[QObject, Callable[..., None], Callable[..., None]]] = {}
		self._listeners: dict[int, tuple[QObject, Callable[..., None]]] = {}
		# авто-подключение: излучение из потока движка → очередь событий
		# потока интерфейса (маршрутизатор создан в потоке интерфейса)
		self._delivered.connect(self._deliver)
		self._progressed.connect(self._forward_progress)

	# --- разовые результаты (run_in_engine) ---------------------------------------

	def register(
		self,
		owner: QObject,
		on_done: Callable[..., None],
		on_error: Callable[..., None],
	) -> int:
		"""Регистрирует колбэки результата; возвращает токен доставки."""
		token = next(self._tokens)
		self._pending[token] = (owner, on_done, on_error)
		return token

	def emit_result(self, token: int, ok: bool, payload: object) -> None:
		"""Из потока движка: безопасная доставка результата в интерфейс."""
		self._delivered.emit(token, ok, payload)

	def _deliver(self, token: int, ok: bool, payload: object) -> None:
		"""В потоке интерфейса: находит колбэки и зовёт нужный."""
		entry = self._pending.pop(token, None)
		if entry is None:
			return
		owner, on_done, on_error = entry
		if not isValid(owner):
			logger.debug("Результат операции движка выброшен: владелец удалён.")
			return
		_invoke(on_done if ok else on_error, payload)

	# --- повторяющиеся события (прогресс) -------------------------------------------

	def register_listener(self, owner: QObject, callback: Callable[..., None]) -> int:
		"""Регистрирует подписку на повторяющиеся события; возвращает токен."""
		token = next(self._tokens)
		self._listeners[token] = (owner, callback)
		return token

	def emit_progress(self, token: int, args: tuple[Any, ...]) -> None:
		"""Из потока движка: безопасная доставка события подписки."""
		self._progressed.emit(token, args)

	def _forward_progress(self, token: int, args: tuple[Any, ...]) -> None:
		"""В потоке интерфейса: зовёт подписчика, пока жив владелец."""
		entry = self._listeners.get(token)
		if entry is None:
			return
		owner, callback = entry
		if not isValid(owner):
			self._listeners.pop(token, None)
			return
		callback(*args)


_dispatcher: _Dispatcher | None = None


def _get_dispatcher() -> _Dispatcher:
	"""Маршрутизатор — лениво, при первом вызове из потока интерфейса."""
	global _dispatcher  # noqa: PLW0603 — единый объект на процесс, по замыслу
	if _dispatcher is None:
		_dispatcher = _Dispatcher()
	return _dispatcher


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
		parent: Владелец колбэков (обычно страница или диалог). Если
			владельца удалили до завершения корутины (закрытый диалог),
			результат тихо выбрасывается — колбэки не вызываются.
		on_done: Колбэк успеха. Может принимать результат корутины одним
			аргументом или не принимать ничего.
		on_error: Колбэк ошибки — получает текст ошибки (или ничего).
	"""
	dispatcher = _get_dispatcher()
	token = dispatcher.register(parent, on_done, on_error)
	future = worker.submit(coro)

	def _finished(fut: Any) -> None:
		# выполняется в потоке движка: отсюда — только излучение
		# на бессмертном маршрутизаторе (см. докстринг модуля)
		try:
			result = fut.result()
		except Exception as exc:  # noqa: BLE001 — любую ошибку показываем в UI
			# полный трейсбек — в лог; пользователю — читаемый текст:
			# доменные ошибки как есть, неожиданные — короткой сводкой
			# (дампы СУБД/библиотек в интерфейс не попадают)
			logger.exception("Ошибка операции движка: %s", exc)
			dispatcher.emit_result(token, False, user_message(exc))
			return
		dispatcher.emit_result(token, True, result)

	future.add_done_callback(_finished)


def ui_callback(owner: QObject, callback: Callable[..., None]) -> Callable[..., None]:
	"""Колбэк для потока движка с безопасной доставкой в поток интерфейса.

	Для повторяющихся событий (прогресс сканирования, ход загрузки):
	возвращённая функция зовётся из потока движка сколько угодно раз,
	``callback`` выполняется в потоке интерфейса, пока жив ``owner``.
	Излучение — на бессмертном маршрутизаторе: передавать в движок
	``signal.emit`` объекта страницы/диалога нельзя (см. докстринг модуля).
	"""
	token = _get_dispatcher().register_listener(owner, callback)

	def _relay(*args: Any) -> None:
		_get_dispatcher().emit_progress(token, args)

	return _relay
