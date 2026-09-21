"""Диспетчер: кому из способных исполнителей поручить операцию (ADR-0036).

Пул исполнителей сообщества (ADR-0035) отвечает, **кто способен** на
действие; дорожки шлюза (ADR-0024, ADR-0030) — **кто чем занят**. Здесь
эти два ответа сводятся в один порядок: свободный раньше занятого долгой
операцией, менее нагруженный раньше более нагруженного, при равенстве —
предпочтение (публикатор по умолчанию), затем порядок пула.

Модуль чистый: ни сети, ни базы, ни моделей — правило проверяется
тестами целиком и живёт одной точкой. Отбор по **требованиям** операции
(Premium под большой файл, отложка только пользователю) делает
вызывающий до ранжирования: требования предметны, а порядок — общий.

Замороженные флуд-лимитом не отсекаются, а идут последними: когда
заморожен единственный способный, честный сигнал — отказ самой дорожки,
который очередь отправки уже умеет ждать (ADR-0024, п. 4); выдумывать
второй сигнал диспетчеру незачем.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pxcontrol.engine.telegram.lane import LaneLiveState, WorkKind
from pxcontrol.engine.telegram.types import ExecutorRef


@dataclass(frozen=True)
class Candidate:
	"""Исполнитель, способный на операцию, — с тем, что решает порядок.

	Attributes:
		owner: ключ исполнителя — общий с пулом, дорожкой и учётом.
		preferred: предпочтение человека (публикатор по умолчанию):
			решает при равной занятости.
		premium: подписка Premium у аккаунта (у бота всегда False) —
			нужна отбору по требованиям поста, здесь не участвует.
		live: живое состояние дорожки; None — дорожки ещё нет
			(исполнитель не работал с запуска), то есть он свободен.
	"""

	owner: ExecutorRef
	preferred: bool = False
	premium: bool = False
	live: LaneLiveState | None = None


def busy_long(live: LaneLiveState | None) -> bool:
	"""Занята ли дорожка долгой операцией — загрузкой файла публикации.

	Короткие операции соседа (проверка, фон, уборка) поводом не считаются:
	ждать за ними секунды, а переключение на другого исполнителя ради
	секунд только раздувало бы след приложения в Telegram.
	"""
	return live is not None and live.busy_kind is WorkKind.PUBLISH


def rank(candidates: Sequence[Candidate]) -> list[Candidate]:
	"""Кандидаты в порядке, в котором им стоит поручать операцию.

	Порядок: не заморожен раньше замороженного (заморозка короче — раньше);
	свободный раньше занятого долгой операцией; меньше ожидающих — раньше;
	предпочтение человека; порядок пула. Список не меняется на месте.
	"""

	def key(item: tuple[int, Candidate]) -> tuple[float, bool, int, bool, int]:
		index, candidate = item
		live = candidate.live
		frozen = live.frozen_for_s if live is not None else 0.0
		waiting = live.waiting if live is not None else 0
		return (frozen, busy_long(live), waiting, not candidate.preferred, index)

	return [candidate for _index, candidate in sorted(enumerate(candidates), key=key)]
