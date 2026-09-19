"""Наблюдатель очереди движка: подписка, снимок, завершённые, занятость.

Очередь движка (каркас заданий, ADR-0025) живёт в фоновом потоке,
а интерфейс работает с её снимками (``state()``). Как узнавать, что
снимок изменился, решает ADR-0034: движок сам сообщает об изменении
состава и состояний (подписка через переправу моста), а доля выполнения
читается опросом раз в полсекунды — **только пока в очереди есть
задание в работе**. В покое обращений к движку нет.

Наблюдатель один на очередь и живёт при главном окне всё время работы
приложения (реестр :class:`QueueWatchers`). Он держит последний снимок,
снимает завершённые задания из состояния движка и ведёт опрос прогресса.
Все остальные — **зрители** (:class:`QueueView`): панель экрана, плашки
об исходе у окна, перечитывание готовых видео у страницы «Видео».
Зритель присоединяется к наблюдателю (:meth:`QueueWatcher.attach`)
и сразу получает последний снимок из кэша; отсоединяется, когда его
экран скрыт (:meth:`QueueWatcher.detach`), а зритель с умершим
владельцем отсеивается сам.

Разбор снимка — покинувшие очередь, видимые, занятость, переход
«работа кончилась» — вынесен в :class:`QueueState` без Qt: он
проверяется обычными тестами.

До ADR-0034 наблюдатель опрашивал движок таймером, и свой наблюдатель
был у каждой панели: три из семи работали с запуска и никогда
не гасились — шесть холостых обращений в секунду в покое (аудит
19.09.2026). Владение снятием завершённых при этом держалось
на договорённости (``dismiss_finished``); теперь владелец единственный
по построению.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.ui.async_bridge import run_in_engine, ui_callback
from pxcontrol.ui.pages.common import error_reporter, noop

logger = logging.getLogger(__name__)

#: Шаг опроса доли выполнения (мс), пока в очереди есть задание в работе.
#: Прогресс в версию состояния не входит (ADR-0034): его пишут из рабочих
#: потоков сотни раз в секунду, а показать нужно дважды в секунду.
PROGRESS_POLL_MS = 500


@dataclass(frozen=True)
class Taken:
	"""Разбор одного снимка очереди.

	Attributes:
		visible: элементы, ещё живущие в очереди (в порядке снимка).
		finished: покинувшие очередь элементы, ещё не учтённые,
			с исходом (True — готово, False — отменено).
		drained: занятость кончилась именно этим снимком.
	"""

	visible: list[Any]
	finished: list[tuple[Any, bool]]
	drained: bool


class QueueState:
	"""Чистое ядро наблюдателя: разбор снимков без Qt.

	Элементы с полями ``id`` и ``status`` (общий :class:`JobStatus`,
	ADR-0025 — признаки ``active()`` / ``finished()`` / ``left_queue()``,
	а не имена статусов).
	"""

	def __init__(self) -> None:
		#: есть ли незавершённое в очереди (включая ждущих слота)
		self.busy = False
		#: идёт ли работа прямо сейчас (ждущие — не в счёт)
		self.active = False
		self._handled: set[int] = set()  # покинувшие очередь, уже учтённые

	def take(self, items: list[Any]) -> Taken:
		"""Разбирает снимок: покинувшие очередь — в исходы, остальные — видимые.

		Снятие покинувшего элемента асинхронное: до него элемент успевает
		попасть в снимок ещё раз-другой — набор учтённых защищает
		от повторной реакции. Набор не растёт бесконечно: id, исчезнувшие
		из снимка (после снятия), из него выметаются.
		"""
		visible: list[Any] = []
		finished: list[tuple[Any, bool]] = []
		for item in items:
			if not item.status.left_queue():
				visible.append(item)
			elif item.id not in self._handled:
				self._handled.add(item.id)
				finished.append((item, item.status is JobStatus.DONE))
		self._handled &= {item.id for item in items}
		busy = any(not item.status.finished() for item in visible)
		drained = self.busy and not busy
		self.busy = busy
		self.active = any(item.status.active() for item in visible)
		return Taken(visible, finished, drained)


@dataclass
class QueueView:
	"""Зритель очереди: что делать со снимком и с исходами.

	Attributes:
		on_state: снимок без покинувших очередь элементов — после каждого
			обновления (панель по нему рисует карточки).
		on_finished: разовая реакция на покинувший очередь элемент
			(``True`` — готово, ``False`` — отменено) до его снятия.
		on_drained: зовётся с видимым остатком, когда занятость кончилась
			(итоговая плашка вместо плашки на каждый файл).
	"""

	on_state: Callable[[list[Any]], None] | None = None
	on_finished: Callable[[Any, bool], None] | None = None
	on_drained: Callable[[list[Any]], None] | None = None


class QueueWatcher:
	"""Владелец очереди движка в интерфейсе: подписка, снимок, зрители.

	Контракт сервиса очереди (все три очереди движка ему следуют):
	корутины ``state()``, ``cancel(id)``, ``retry(id)``, ``dismiss(id)``
	и ``subscribe(listener)`` (ADR-0034).

	Запросов снимка в полёте не больше одного: уведомления, пришедшие
	во время запроса, сливаются в один повторный запрос после ответа.
	Снятие завершённых, повтор и отмена сами меняют состояние очереди —
	движок сообщит об этом подпиской, отдельный запрос после них не нужен.
	"""

	def __init__(self, worker: EngineWorker, host: QWidget, *, service: Callable[[], Any]) -> None:
		"""Args:
		worker: мост к движку.
		host: главное окно — владелец подписки, таймера прогресса
			и плашек об ошибках действий над заданиями.
		service: провайдер сервиса очереди (``lambda: worker.engine.…``).
		"""
		self._worker = worker
		self._host = host
		self._service = service
		self._show_error = error_reporter(host)
		self._state = QueueState()
		self._items: list[Any] = []
		self._received = False  # первый снимок уже пришёл
		self._views: list[tuple[QObject, QueueView]] = []
		self._in_flight = False  # запрос снимка ушёл, ответа ещё нет
		self._dirty = False  # за время запроса очередь менялась ещё
		self._timer = QTimer(host)
		self._timer.setInterval(PROGRESS_POLL_MS)
		self._timer.timeout.connect(self.poll)
		# подписка ложится в цикл движка; первый снимок — сразу за ней
		run_in_engine(
			worker,
			service().subscribe(ui_callback(host, self._on_changed)),
			host,
			self.poll,
			self._subscribe_failed,
		)

	# --- зрители -------------------------------------------------------------

	def attach(self, owner: QObject, view: QueueView) -> None:
		"""Присоединяет зрителя; последний снимок отдаётся ему сразу.

		``owner`` — виджет, с жизнью которого связан зритель: умер
		владелец — зритель отсеивается при следующей доставке, явного
		отсоединения от закрывающегося окна не требуется. Повторное
		присоединение того же зрителя — не ошибка и не дубль.
		"""
		if any(known is view for _owner, known in self._views):
			return
		self._views.append((owner, view))
		if self._received and view.on_state is not None:
			view.on_state(list(self._items))

	def detach(self, view: QueueView) -> None:
		"""Отсоединяет зрителя (экран скрыт); незнакомый — не ошибка."""
		self._views = [(owner, known) for owner, known in self._views if known is not view]

	@property
	def items(self) -> list[Any]:
		"""Последний снимок без покинувших очередь элементов (копия)."""
		return list(self._items)

	def busy(self) -> bool:
		"""Есть ли незавершённое в очереди (включая ждущих слота)."""
		return self._state.busy

	def active(self) -> bool:
		"""Идёт ли работа прямо сейчас (загрузка или обработка).

		В отличие от :meth:`busy`, ждущие элементы не в счёт: для
		персистентной очереди отправки (ADR-0016) вопрос при закрытии
		окна заслуживает только обрыв активной загрузки — ожидающие
		посты выход переживают.
		"""
		return self._state.active

	# --- снимок --------------------------------------------------------------

	def poll(self) -> None:
		"""Запрашивает снимок очереди (по уведомлению, по таймеру прогресса,
		после постановки — карточка должна появиться сразу)."""
		if self._in_flight:
			self._dirty = True
			return
		self._in_flight = True
		# ошибки чтения снимка не показываем плашками: мост пишет их
		# в журнал, а следующее уведомление повторит запрос
		run_in_engine(self._worker, self._service().state(), self._host, self._take, self._failed)

	def _on_changed(self, _version: int) -> None:
		"""Движок сообщил об изменении состава или состояний — берём снимок."""
		self.poll()

	def _subscribe_failed(self, message: str) -> None:
		"""Подписка не легла: без неё снимок не обновится — сказать честно."""
		self._show_error(f"Очередь не наблюдается — изменения не будут видны: {message}")
		self.poll()

	def _failed(self, _message: str) -> None:
		self._in_flight = False
		self._repoll_if_dirty()

	def _repoll_if_dirty(self) -> None:
		if self._dirty:
			self._dirty = False
			self.poll()

	def _take(self, items: list[Any]) -> None:
		"""Разбирает снимок и раздаёт его зрителям; ведёт опрос прогресса."""
		self._in_flight = False
		taken = self._state.take(items)
		self._items = taken.visible
		self._received = True
		views = self._alive_views()
		for item, done in taken.finished:
			for view in views:
				if view.on_finished is not None:
					view.on_finished(item, done)
			# снять завершённое — работа владельца; о снятии движок сообщит сам
			self.dismiss(item.id, silent=True)
		if taken.drained:
			for view in views:
				if view.on_drained is not None:
					view.on_drained(list(taken.visible))
		for view in views:
			if view.on_state is not None:
				view.on_state(list(taken.visible))
		# прогресс читается опросом, пока есть задание в работе (ADR-0034)
		if self._state.active and not self._timer.isActive():
			self._timer.start()
		elif not self._state.active and self._timer.isActive():
			self._timer.stop()
		self._repoll_if_dirty()

	def _alive_views(self) -> list[QueueView]:
		"""Зрители с живыми владельцами; умершие выметаются."""
		self._views = [(owner, view) for owner, view in self._views if isValid(owner)]
		return [view for _owner, view in self._views]

	# --- действия над элементом ------------------------------------------------

	def dismiss(self, item_id: int, *, silent: bool = False) -> None:
		"""Убирает завершённый элемент из состояния очереди.

		``silent`` — снятие автоматическое (владелец убирает завершённые
		сам): о таком человеку говорить нечего, след останется в логе.
		Нажатие «Убрать» — не автоматика: движок при нём двигает файл
		из папки очереди обратно в результаты и может отказать, и тогда
		молчание оставило бы человека в уверенности, что всё убрано.
		"""
		run_in_engine(
			self._worker,
			self._service().dismiss(item_id),
			self._host,
			noop,
			noop if silent else self._show_error,
		)

	def retry(self, item_id: int) -> None:
		"""Просит движок вернуть элемент с ошибкой в очередь на повтор."""
		run_in_engine(
			self._worker, self._service().retry(item_id), self._host, noop, self._show_error
		)

	def cancel(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди."""
		run_in_engine(
			self._worker, self._service().cancel(item_id), self._host, noop, self._show_error
		)


class QueueWatchers:
	"""Наблюдатели приложения — по одному на очередь движка, при главном окне.

	Реестр передаётся страницам явно, как и мост ``worker``: у очереди
	один владелец по построению, а панели и реакции экранов — зрители.

	Attributes:
		publish: очередь отправки постов (ADR-0016).
		video: очередь обработки видео (ADR-0014).
		maintenance: очередь обслуживания сообществ (ADR-0026).
	"""

	def __init__(self, worker: EngineWorker, host: QWidget) -> None:
		self.publish = QueueWatcher(worker, host, service=lambda: worker.engine.publish_queue)
		self.video = QueueWatcher(worker, host, service=lambda: worker.engine.video_queue)
		self.maintenance = QueueWatcher(worker, host, service=lambda: worker.engine.maintenance)
