"""Владелец очереди движка в интерфейсе: опрос, завершённые, занятость.

Очередь движка (каркас заданий, ADR-0025) живёт в фоновом потоке,
а интерфейс читает её снимками — опросом раз в полсекунды (ADR-0016).
Работы над этим снимком две, и они разной природы:

- **показ** — карточки, прогресс, кнопки: делает :class:`QueuePanel`
  той страницы, на которую человек смотрит, и только пока смотрит;
- **владение** — снятие завершённых заданий из состояния движка,
  итоговая плашка об исходе и ответ на вопрос «идёт ли работа прямо
  сейчас» (его задаёт закрытие окна): делается ровно один раз
  на очередь и не зависит от того, открыт ли хоть один экран.

Раньше обе работы жили в панели, и владельцем очереди отправки
оказывалась страница «Публикация» — не по замыслу, а потому что она
всегда существовала и никогда не гасила свой опрос. ADR-0032 разнёс
стадии поста по отдельным экранам, и такое владение стало случайным:
экран стадии затихает, когда его не видно. Наблюдатель — та же
механика, вынутая из панели в самостоятельный объект: панель стала
его надстройкой для показа, а владелец очереди отправки живёт при
главном окне.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import error_reporter, noop

#: Период опроса состояния очередей движка (мс). Опрос вместо событий —
#: осознанный дизайн ADR-0016 (унаследован от ADR-0012): интерфейс
#: читает снимок состояния.
QUEUE_POLL_MS = 500


class QueueWatcher:
	"""Опрос очереди движка, реакция на завершённые, признаки занятости.

	Контракт сервиса очереди (все три очереди движка ему следуют):
	корутины ``state()``, ``cancel(id)``, ``retry(id)``, ``dismiss(id)``;
	элементы с полями ``id`` и ``status`` (общий :class:`JobStatus`,
	ADR-0025 — наблюдатель спрашивает его признаками ``active()`` /
	``finished()`` / ``left_queue()``, а не сверяет имена).

	Владение снятием завершённых — ``dismiss_finished``. Наблюдателей
	над одной очередью может быть несколько (панель на экране и владелец
	при окне), но снимать завершённые должен ровно один: иначе они
	снимут их наперегонки и итоговая плашка потеряется.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		host: QWidget,
		*,
		service: Callable[[], Any],
		on_state: Callable[[list[Any]], None] | None = None,
		on_finished: Callable[[Any, bool], None] | None = None,
		on_drained: Callable[[list[Any]], None] | None = None,
		dismiss_finished: bool = True,
	) -> None:
		"""Args:
		worker: мост к движку.
		host: виджет-владелец — родитель таймера и плашек об ошибках
			действий (страница у панели, окно у владельца очереди).
		service: провайдер сервиса очереди (``lambda: worker.engine.…``).
		on_state: снимок очереди без покинувших её элементов — зовётся
			после каждого опроса (панель по нему рисует карточки).
		on_finished: разовая реакция на покинувший очередь элемент
			(``True`` — готово, ``False`` — отменено) до его снятия.
		on_drained: зовётся с видимым остатком, когда занятость кончилась
			(итоговая плашка вместо плашки на каждый файл).
		dismiss_finished: снимать ли завершённые из состояния движка.
		"""
		self._worker = worker
		self._host = host
		self._service = service
		self._on_state = on_state
		self._on_finished = on_finished
		self._on_drained = on_drained
		self._dismiss_finished = dismiss_finished
		self._show_error = error_reporter(host)
		self._handled: set[int] = set()  # завершённые, уже учтённые
		self._busy = False
		self._active = False
		self._timer = QTimer(host)
		self._timer.setInterval(QUEUE_POLL_MS)
		self._timer.timeout.connect(self.poll)
		self._timer.start()

	# --- опрос ---------------------------------------------------------------

	def set_polling(self, active: bool) -> None:
		"""Включает или приостанавливает опрос (экран скрылся или открылся).

		Возобновление опрашивает сразу, не дожидаясь тика: человек
		открыл экран и ждёт свежих карточек. Владелец очереди опрос
		не гасит никогда — иначе завершённые перестанут сниматься.
		"""
		if active:
			if not self._timer.isActive():
				self._timer.start()
				self.poll()
		else:
			self._timer.stop()

	def poll(self) -> None:
		"""Запрашивает состояние очереди (по таймеру и после постановки)."""
		# ошибки опроса не показываем плашками: мост пишет их в лог,
		# а раз в полсекунды спамить пользователя нечем и незачем
		run_in_engine(self._worker, self._service().state(), self._host, self._take, noop)

	def busy(self) -> bool:
		"""Есть ли незавершённое в очереди (включая ждущих слота)."""
		return self._busy

	def active(self) -> bool:
		"""Идёт ли работа прямо сейчас (загрузка или обработка).

		В отличие от :meth:`busy`, ждущие элементы не в счёт: для
		персистентной очереди отправки (ADR-0016) вопрос при закрытии
		окна заслуживает только обрыв активной загрузки — ожидающие
		посты выход переживают.
		"""
		return self._active

	# --- действия над элементом ------------------------------------------------

	def dismiss(self, item_id: int, *, silent: bool = False) -> None:
		"""Убирает завершённый элемент из состояния очереди.

		``silent`` — снятие автоматическое (наблюдатель убирает
		завершённые сам): о таком человеку говорить нечего, след
		останется в логе. Нажатие «Убрать» — не автоматика: движок при
		нём двигает файл из папки очереди обратно в результаты и может
		отказать, и тогда молчание оставило бы человека в уверенности,
		что всё убрано.
		"""
		run_in_engine(
			self._worker,
			self._service().dismiss(item_id),
			self._host,
			lambda *_a: self.poll(),
			noop if silent else self._show_error,
		)

	def retry(self, item_id: int) -> None:
		"""Просит движок вернуть элемент с ошибкой в очередь на повтор."""
		run_in_engine(
			self._worker,
			self._service().retry(item_id),
			self._host,
			lambda *_a: self.poll(),  # карточка обновляется сразу, не по таймеру
			self._show_error,
		)

	def cancel(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди."""
		run_in_engine(
			self._worker,
			self._service().cancel(item_id),
			self._host,
			noop,
			self._show_error,
		)

	# --- внутреннее -------------------------------------------------------------

	def _take(self, items: list[Any]) -> None:
		"""Разбирает снимок: покинувшие очередь — в реакцию, остальные — дальше."""
		visible: list[Any] = []
		for item in items:
			if item.status.left_queue():
				self._finish(item, done=item.status is JobStatus.DONE)
			else:
				visible.append(item)
		# id, исчезнувшие из состояния движка (после dismiss), больше
		# не встретятся — набор «уже учтённых» не растёт бесконечно
		self._handled &= {item.id for item in items}
		busy = any(not item.status.finished() for item in visible)
		if self._busy and not busy and self._on_drained is not None:
			self._on_drained(visible)
		self._busy = busy
		self._active = any(item.status.active() for item in visible)
		if self._on_state is not None:
			self._on_state(visible)

	def _finish(self, item: Any, done: bool) -> None:
		"""Разовая реакция на завершённый элемент и снятие его с показа.

		Снятие асинхронное, до него элемент успевает попасть в опрос ещё
		раз-другой — набор «уже учтённых» защищает от повторной реакции.
		"""
		if item.id in self._handled:
			return
		self._handled.add(item.id)
		if self._on_finished is not None:
			self._on_finished(item, done)
		if self._dismiss_finished:
			self.dismiss(item.id, silent=True)
