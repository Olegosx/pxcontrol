"""Ядро наблюдателя очереди (ADR-0034): разбор снимков без Qt.

Наблюдатель получает снимки по подписке и разбирает их: покинувшие
очередь элементы учитываются ровно один раз (снятие асинхронное,
элемент успевает попасть в снимок ещё раз), занятость и активность
считаются по видимым, переход «работа кончилась» замечается один раз.
"""

from __future__ import annotations

from dataclasses import dataclass

from pxcontrol.engine.jobs import JobStatus
from pxcontrol.ui.queue_watcher import QueueState


@dataclass(frozen=True)
class _Item:
	id: int
	status: JobStatus


def test_finished_items_are_reported_once_and_not_visible() -> None:
	state = QueueState()
	taken = state.take([_Item(1, JobStatus.DONE), _Item(2, JobStatus.PENDING)])
	assert taken.finished == [(_Item(1, JobStatus.DONE), True)]
	assert taken.visible == [_Item(2, JobStatus.PENDING)]
	# снятие ещё не дошло до движка — элемент пришёл снимком второй раз
	again = state.take([_Item(1, JobStatus.DONE), _Item(2, JobStatus.PENDING)])
	assert again.finished == []


def test_cancelled_reports_false_and_error_stays_visible() -> None:
	state = QueueState()
	taken = state.take([_Item(1, JobStatus.CANCELLED), _Item(2, JobStatus.ERROR)])
	assert taken.finished == [(_Item(1, JobStatus.CANCELLED), False)]
	assert taken.visible == [_Item(2, JobStatus.ERROR)]  # ошибка живёт в очереди


def test_handled_set_forgets_dismissed_items() -> None:
	state = QueueState()
	state.take([_Item(1, JobStatus.DONE)])
	state.take([])  # снято движком — id исчез из снимка
	# тот же номер у нового задания другой очереди быть не может,
	# но набор учтённых не должен расти бесконечно
	assert state._handled == set()  # noqa: SLF001


def test_busy_active_and_drained_transition() -> None:
	state = QueueState()
	state.take([_Item(1, JobStatus.RUNNING), _Item(2, JobStatus.WAITING)])
	assert state.busy and state.active
	state.take([_Item(2, JobStatus.WAITING)])
	assert state.busy and not state.active  # ждущий — занятость без работы
	taken = state.take([_Item(3, JobStatus.ERROR)])
	assert taken.drained  # незавершённых не осталось — работа кончилась
	assert not state.busy
	taken = state.take([_Item(3, JobStatus.ERROR)])
	assert not taken.drained  # переход замечается один раз
