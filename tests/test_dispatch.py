"""Тесты диспетчера: кому из способных поручить операцию (ADR-0036)."""

from __future__ import annotations

from datetime import UTC, datetime

from pxcontrol.engine.services.dispatch import Candidate, busy_long, rank
from pxcontrol.engine.telegram.lane import LaneLiveState, WorkKind
from pxcontrol.engine.telegram.types import ExecutorRef, OwnerKind

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _user(number: int, **kwargs: object) -> Candidate:
	return Candidate(ExecutorRef(OwnerKind.USER, number), **kwargs)  # type: ignore[arg-type]


def _live(kind: WorkKind | None = None, waiting: int = 0, frozen: float = 0.0) -> LaneLiveState:
	return LaneLiveState(kind, NOW if kind is not None else None, waiting, frozen)


def test_free_goes_before_uploading() -> None:
	"""Свободный исполнитель раньше занятого загрузкой — даже если тот предпочтён."""
	uploading = _user(1, preferred=True, live=_live(WorkKind.PUBLISH))
	free = _user(2, live=_live())
	assert [c.owner.id for c in rank([uploading, free])] == [2, 1]


def test_short_work_is_not_a_reason_to_switch() -> None:
	"""Короткая операция соседа поводом не считается: предпочтение решает."""
	checking = _user(1, preferred=True, live=_live(WorkKind.INTERACTIVE))
	free = _user(2, live=None)
	assert [c.owner.id for c in rank([free, checking])] == [1, 2]
	assert not busy_long(_live(WorkKind.MAINTENANCE)) and busy_long(_live(WorkKind.PUBLISH))


def test_fewer_waiting_goes_first_then_preference_then_pool_order() -> None:
	"""Меньше ожидающих — раньше; при равенстве — предпочтение, затем порядок пула."""
	crowded = _user(1, preferred=True, live=_live(waiting=3))
	quiet = _user(2, live=_live(waiting=1))
	quiet_preferred = _user(3, preferred=True, live=_live(waiting=1))
	quiet_too = _user(4, live=_live(waiting=1))
	order = [c.owner.id for c in rank([crowded, quiet, quiet_preferred, quiet_too])]
	assert order == [3, 2, 4, 1]


def test_frozen_goes_last_but_is_not_dropped() -> None:
	"""Замороженный идёт последним, а единственный замороженный остаётся кандидатом."""
	frozen = _user(1, preferred=True, live=_live(frozen=30.0))
	uploading = _user(2, live=_live(WorkKind.PUBLISH))
	assert [c.owner.id for c in rank([frozen, uploading])] == [2, 1]
	assert [c.owner.id for c in rank([frozen])] == [1]
	# заморозка короче — раньше: отказ дорожки назовёт меньший срок
	longer = _user(3, live=_live(frozen=90.0))
	assert [c.owner.id for c in rank([longer, frozen])] == [1, 3]


def test_rank_does_not_mutate_input() -> None:
	"""Список кандидатов не меняется на месте."""
	items = [_user(1, live=_live(WorkKind.PUBLISH)), _user(2)]
	ranked = rank(items)
	assert [c.owner.id for c in items] == [1, 2] and [c.owner.id for c in ranked] == [2, 1]
