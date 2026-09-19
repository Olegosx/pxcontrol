"""Наблюдатель очереди (ADR-0034) на поддельном мосте: подписка, зрители, прогресс.

Мост подменён: «движок» выполняет корутины сразу, в потоке теста,
и отдаёт готовый ``Future`` — так проверяется проводка наблюдателя
(подписка при создании, снимок по уведомлению, кэш для зрителей,
снятие завершённых, таймер прогресса), а не цикл событий движка.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from pxcontrol.engine.jobs import JobStatus  # noqa: E402
from pxcontrol.ui.queue_watcher import QueueView, QueueWatcher  # noqa: E402


@dataclass(frozen=True)
class _Item:
	id: int
	status: JobStatus


class _Service:
	"""Сервис очереди по контракту наблюдателя: снимок, подписка, действия."""

	def __init__(self) -> None:
		self.items: list[_Item] = []
		self.listener: Callable[[int], None] | None = None
		self.dismissed: list[int] = []
		self.state_calls = 0

	async def subscribe(self, listener: Callable[[int], None]) -> None:
		self.listener = listener

	async def state(self) -> list[_Item]:
		self.state_calls += 1
		return list(self.items)

	async def dismiss(self, item_id: int) -> None:
		self.dismissed.append(item_id)
		self.items = [item for item in self.items if item.id != item_id]

	async def retry(self, item_id: int) -> None:  # pragma: no cover — контракт
		del item_id

	async def cancel(self, item_id: int) -> None:  # pragma: no cover — контракт
		del item_id


class _Worker:
	"""Подмена ``EngineWorker``: корутина выполняется сразу, результат — готовый Future."""

	def submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
		future: Future[Any] = Future()
		try:
			future.set_result(asyncio.run(coro))
		except Exception as exc:  # noqa: BLE001 — исход уходит в Future, как у моста
			future.set_exception(exc)
		return future


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def host(qapp: QApplication) -> Iterator[QWidget]:
	del qapp
	widget = QWidget()
	yield widget
	widget.deleteLater()


def _watcher(host: QWidget, service: _Service) -> QueueWatcher:
	return QueueWatcher(_Worker(), host, service=lambda: service)  # type: ignore[arg-type]


def test_subscribes_and_delivers_first_snapshot_to_views(host: QWidget) -> None:
	service = _Service()
	service.items = [_Item(1, JobStatus.PENDING)]
	watcher = _watcher(host, service)
	assert service.listener is not None  # подписка легла при создании
	assert service.state_calls == 1  # первый снимок — сразу за подпиской
	seen: list[list[_Item]] = []
	watcher.attach(host, QueueView(on_state=seen.append))
	assert seen == [[_Item(1, JobStatus.PENDING)]]  # из кэша, без запроса
	assert service.state_calls == 1


def test_engine_notification_refreshes_snapshot(host: QWidget) -> None:
	service = _Service()
	watcher = _watcher(host, service)
	seen: list[list[_Item]] = []
	watcher.attach(host, QueueView(on_state=seen.append))
	service.items = [_Item(1, JobStatus.WAITING)]
	assert service.listener is not None
	service.listener(1)  # движок сообщил об изменении
	assert seen[-1] == [_Item(1, JobStatus.WAITING)]
	assert watcher.busy() and not watcher.active()


def test_finished_items_reported_once_and_dismissed(host: QWidget) -> None:
	service = _Service()
	watcher = _watcher(host, service)
	finished: list[tuple[_Item, bool]] = []
	watcher.attach(host, QueueView(on_finished=lambda item, done: finished.append((item, done))))
	service.items = [_Item(1, JobStatus.DONE), _Item(2, JobStatus.CANCELLED)]
	watcher.poll()
	assert finished == [(_Item(1, JobStatus.DONE), True), (_Item(2, JobStatus.CANCELLED), False)]
	assert service.dismissed == [1, 2]  # снял владелец, ровно по разу
	assert watcher.items == []


def test_progress_timer_runs_only_while_active(host: QWidget) -> None:
	service = _Service()
	watcher = _watcher(host, service)
	assert not watcher._timer.isActive()  # noqa: SLF001 — в покое таймера нет
	service.items = [_Item(1, JobStatus.RUNNING)]
	watcher.poll()
	assert watcher.active()
	assert watcher._timer.isActive()  # noqa: SLF001 — прогресс читается опросом
	service.items = [_Item(1, JobStatus.WAITING)]
	watcher.poll()
	assert not watcher._timer.isActive()  # noqa: SLF001 — работа кончилась


def test_detached_and_dead_views_get_nothing(qapp: QApplication, host: QWidget) -> None:
	service = _Service()
	watcher = _watcher(host, service)
	seen: list[int] = []
	view = QueueView(on_state=lambda items: seen.append(len(items)))
	watcher.attach(host, view)
	watcher.detach(view)
	dead_owner = QWidget()
	dead_seen: list[int] = []
	watcher.attach(dead_owner, QueueView(on_state=lambda items: dead_seen.append(len(items))))
	dead_owner.deleteLater()
	qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
	service.items = [_Item(1, JobStatus.PENDING)]
	watcher.poll()
	assert seen == [0]  # только снимок при присоединении, до отсоединения
	assert dead_seen == [0]  # владелец умер — новых доставок нет
