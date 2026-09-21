"""Виды задач сообщества и их реестр (ADR-0038).

Пакет держит предметные модули видов — каждый описывает параметры,
нужное право, выполнение и отчёт по контракту :class:`TaskSpec`.
Сервис задач (:mod:`pxcontrol.engine.services.tasks`) берёт вид
из реестра :data:`SPECS` по ключу :class:`TaskKind` и о его внутренностях
не знает: постановка, очередь, журнал и расписание общие для всех.

Новый вид задачи = новый модуль здесь + строка в реестре + форма
во вкладке «Задачи». Заводить виды впрок в проекте не принято.
"""

from __future__ import annotations

from typing import Any

from pxcontrol.engine.tasks.deleted_accounts import (
	DeletedAccountsParams,
	DeletedAccountsTask,
	MembersReport,
)
from pxcontrol.engine.tasks.join_requests import (
	JoinRequestsParams,
	JoinRequestsReport,
	JoinRequestsTask,
)
from pxcontrol.engine.tasks.model import (
	RunEvent,
	RunOutcome,
	TaskContext,
	TaskError,
	TaskKind,
	TaskSpec,
	TaskTitle,
	TaskTrigger,
)
from pxcontrol.engine.tasks.reactions import (
	ReactionChoice,
	ReactionScope,
	ReactionsParams,
	ReactionsReport,
	ReactionsTask,
)
from pxcontrol.engine.tasks.service_messages import (
	ServiceMessagesParams,
	ServiceMessagesTask,
	ServiceReport,
)

#: Параметры любого вида (объединение — по одному типу на вид).
TaskParams = ServiceMessagesParams | DeletedAccountsParams | ReactionsParams | JoinRequestsParams

#: Отчёт любого вида.
TaskReport = ServiceReport | MembersReport | ReactionsReport | JoinRequestsReport

#: Реестр видов: ключ колонки → спецификация.
SPECS: dict[TaskKind, TaskSpec[Any, Any]] = {
	TaskKind.SERVICE_MESSAGES: ServiceMessagesTask(),
	TaskKind.DELETED_ACCOUNTS: DeletedAccountsTask(),
	TaskKind.REACTIONS: ReactionsTask(),
	TaskKind.JOIN_REQUESTS: JoinRequestsTask(),
}


def spec_of(kind: TaskKind) -> TaskSpec[Any, Any]:
	"""Спецификация вида по ключу.

	Raises:
		TaskError: Вид не зарегистрирован (данные из будущей версии).
	"""
	try:
		return SPECS[kind]
	except KeyError as exc:
		raise TaskError(f"Неизвестный вид задачи: {kind}.") from exc


__all__ = [
	"SPECS",
	"DeletedAccountsParams",
	"DeletedAccountsTask",
	"JoinRequestsParams",
	"JoinRequestsReport",
	"JoinRequestsTask",
	"MembersReport",
	"ReactionChoice",
	"ReactionScope",
	"ReactionsParams",
	"ReactionsReport",
	"ReactionsTask",
	"RunEvent",
	"RunOutcome",
	"ServiceMessagesParams",
	"ServiceMessagesTask",
	"ServiceReport",
	"TaskContext",
	"TaskError",
	"TaskKind",
	"TaskParams",
	"TaskReport",
	"TaskSpec",
	"TaskTitle",
	"TaskTrigger",
	"spec_of",
]
