"""Вид задачи «служебные записи»: просмотр и чистка (ADR-0026, ADR-0038).

Служебные записи («такой-то вступил», «сообщение закреплено») копятся
в ленте активной группы и мешают читать. Серверного фильтра «только
служебные» у Telegram нет, поэтому история просматривается страницами
по сотне, а отбор идёт у нас — цена работы линейна по длине истории,
и потому у неё две обязательные границы: **глубина просмотра**
и **потолок удаления за проход**.

Запуск «без изменений» (``dry_run``) ничего не удаляет и отвечает,
сколько чего нашлось; обычный запуск удаляет выбранные виды. Удаление
необратимо, а ошибиться в наборе видов легко — поэтому ручной запуск
идёт двумя шагами, а расписание чистки подтверждается при сохранении
(ADR-0038).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pxcontrol.engine.services.abilities import ExecutorAction
from pxcontrol.engine.tasks.model import (
	TaskContext,
	TaskError,
	TaskKind,
	TaskTitle,
	as_int,
	check_range,
)
from pxcontrol.engine.telegram.types import ServiceMessageKind, ServiceMessagesPage

logger = logging.getLogger(__name__)

#: Сколько сообщений читать одним запросом (предел Telegram — 100).
PAGE_SIZE = 100

#: Сколько последних сообщений просматривать по умолчанию: два десятка
#: запросов к Telegram. Серверного фильтра нет, и цена просмотра линейна
#: по длине истории — бесконтрольный проход по каналу в десятки тысяч
#: постов упёрся бы во флуд-лимит (ADR-0026).
DEFAULT_DEPTH = 2000

#: Пределы глубины просмотра: правило одно на движок и интерфейс.
DEPTH_RANGE = (PAGE_SIZE, 100_000)

#: Сколько записей удалять за один проход по умолчанию.
DEFAULT_DELETE_LIMIT = 500

#: Пределы потолка удаления. Нижняя граница — 1: удалить одну запись
#: должно быть можно (то же правило у чистки мёртвых аккаунтов).
DELETE_LIMIT_RANGE = (1, 10_000)

#: Виды записей, предлагаемые к чистке по умолчанию: шум от участников.
DEFAULT_KINDS = (ServiceMessageKind.MEMBERS,)

TITLE = TaskTitle(
	noun="Служебные записи",
	hint=(
		"Это строки, которые пишет сам Telegram: «такой-то вступил», "
		"«сообщение закреплено», «название изменено». Сначала посмотрим, "
		"сколько их и каких, — удалять будете выбранные виды."
	),
)


class ServiceMessagesPort(Protocol):
	"""Часть шлюза Telegram, нужная этому виду (для подмены в тестах)."""

	async def userbot_service_messages_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage: ...

	async def userbot_delete_messages(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> int: ...


@dataclass(frozen=True)
class ServiceMessagesParams:
	"""Параметры задачи: глубина, виды к удалению, потолок за проход.

	Attributes:
		depth: сколько последних сообщений просматривать.
		kinds: какие виды удалять (у запуска «без изменений» не нужны —
			он считает все виды разом).
		delete_limit: сколько записей удалить максимум за проход.
	"""

	depth: int = DEFAULT_DEPTH
	kinds: tuple[ServiceMessageKind, ...] = DEFAULT_KINDS
	delete_limit: int = DEFAULT_DELETE_LIMIT


@dataclass(frozen=True)
class ServiceReport:
	"""Итог прохода по служебным записям.

	Отчёт один на оба шага: просмотр — это тот же проход, просто
	без удаления, и у него честные нули в полях чистки.

	Attributes:
		found: сколько записей каждого вида найдено (пустых видов нет).
		scanned: сколько сообщений просмотрено — включая обычные.
		deleted: сколько записей удалено (просмотр — 0).
		skipped: сколько Telegram отказался удалять (защищённые им
			записи — отказ не считается сбоем).
		limited: чистка остановилась о потолок за проход.
		exhausted: история кончилась раньше глубины — дальше смотреть
			нечего.
		oldest_date: до какого числа дошёл проход; None — записей нет.
	"""

	found: dict[ServiceMessageKind, int] = field(default_factory=dict)
	scanned: int = 0
	deleted: int = 0
	skipped: int = 0
	limited: bool = False
	exhausted: bool = False
	oldest_date: datetime | None = None

	@property
	def removable(self) -> int:
		"""Сколько найденных записей вообще можно удалять."""
		return sum(count for kind, count in self.found.items() if kind.removable())


def selectable_kinds(kinds: Sequence[ServiceMessageKind]) -> tuple[ServiceMessageKind, ...]:
	"""Отсеивает виды, которые удалять нельзя (ADR-0026).

	Защищённые записи не должны попадать в чистку ни при каком наборе
	галочек: корень темы форума — это сама тема, а создание сообщества
	и передача владения — его история. Правило движка, а не интерфейса:
	запуск по расписанию подчиняется ему так же.
	"""
	return tuple(kind for kind in dict.fromkeys(kinds) if kind.removable())


def kind_title(kind: ServiceMessageKind) -> str:
	"""Человеческое название вида служебных записей."""
	return _KIND_TITLES.get(kind, str(kind))


#: Человеческие названия видов служебных записей (ADR-0026).
_KIND_TITLES = {
	ServiceMessageKind.MEMBERS: "Вступления и уходы",
	ServiceMessageKind.PINS: "Закрепления сообщений",
	ServiceMessageKind.APPEARANCE: "Оформление: название, аватар, тема, обои",
	ServiceMessageKind.CALLS: "Видеочаты и звонки",
	ServiceMessageKind.OTHER: "Прочее служебное: подарки, бусты, платежи",
	ServiceMessageKind.PROTECTED: "Темы форума и история сообщества",
}


def service_summary(report: ServiceReport) -> str:
	"""Итог прохода по служебным записям одной строкой."""
	if report.deleted or report.skipped:
		parts = [f"Удалено записей: {report.deleted}"]
		if report.skipped:
			parts.append(f"Telegram не дал удалить: {report.skipped}")
		if report.limited:
			parts.append("сработал предел за проход — повторите, чтобы продолжить")
		return ". ".join(parts) + "."
	if not report.found:
		return f"Служебных записей не найдено — просмотрено {report.scanned}."
	tail = (
		"история просмотрена целиком"
		if report.exhausted
		else f"просмотрено {report.scanned} последних сообщений"
	)
	if report.oldest_date is not None:
		tail += f", до {report.oldest_date.astimezone().strftime('%d.%m.%Y %H:%M')}"
	return f"Найдено служебных записей: {sum(report.found.values())} — {tail}."


class ServiceMessagesTask:
	"""Спецификация вида «служебные записи» (контракт :class:`TaskSpec`)."""

	kind = TaskKind.SERVICE_MESSAGES

	def default_params(self) -> ServiceMessagesParams:
		return ServiceMessagesParams()

	def params_from_payload(self, payload: dict[str, Any] | None) -> ServiceMessagesParams:
		payload = payload or {}
		raw_kinds = payload.get("kinds")
		kinds = (
			tuple(ServiceMessageKind(name) for name in raw_kinds if name in ServiceMessageKind)
			if isinstance(raw_kinds, list)
			else DEFAULT_KINDS
		)
		return ServiceMessagesParams(
			depth=as_int(payload, "depth", DEFAULT_DEPTH),
			kinds=kinds,
			delete_limit=as_int(payload, "delete_limit", DEFAULT_DELETE_LIMIT),
		)

	def params_to_payload(self, params: ServiceMessagesParams) -> dict[str, Any]:
		return {
			"depth": params.depth,
			"kinds": [str(kind) for kind in params.kinds],
			"delete_limit": params.delete_limit,
		}

	def validate(self, params: ServiceMessagesParams, *, dry_run: bool) -> None:
		check_range("Глубина просмотра", params.depth, DEPTH_RANGE)
		check_range("Предел удаления за проход", params.delete_limit, DELETE_LIMIT_RANGE)
		if not dry_run and not selectable_kinds(params.kinds):
			raise TaskError("Не выбрано ни одного вида записей — чистить нечего.")

	def action(self, params: ServiceMessagesParams, *, dry_run: bool) -> ExecutorAction:
		return ExecutorAction.READ_HISTORY if dry_run else ExecutorAction.DELETE_OTHERS

	def title(self, *, dry_run: bool) -> str:
		return "Просмотр служебных записей" if dry_run else "Чистка служебных записей"

	def report_from_payload(self, payload: dict[str, Any]) -> ServiceReport:
		raw_found = payload.get("found")
		found = (
			{
				ServiceMessageKind(name): count
				for name, count in raw_found.items()
				if name in ServiceMessageKind and isinstance(count, int)
			}
			if isinstance(raw_found, dict)
			else {}
		)
		oldest = payload.get("oldest_date")
		return ServiceReport(
			found=found,
			scanned=as_int(payload, "scanned", 0),
			deleted=as_int(payload, "deleted", 0),
			skipped=as_int(payload, "skipped", 0),
			limited=bool(payload.get("limited", False)),
			exhausted=bool(payload.get("exhausted", False)),
			oldest_date=datetime.fromisoformat(oldest) if isinstance(oldest, str) else None,
		)

	def report_to_payload(self, report: ServiceReport) -> dict[str, Any]:
		return {
			"found": {str(kind): count for kind, count in report.found.items()},
			"scanned": report.scanned,
			"deleted": report.deleted,
			"skipped": report.skipped,
			"limited": report.limited,
			"exhausted": report.exhausted,
			"oldest_date": report.oldest_date.isoformat() if report.oldest_date else None,
		}

	def summary(self, report: ServiceReport, *, dry_run: bool) -> str:
		return service_summary(report)

	async def run(
		self, ctx: TaskContext, gateway: ServiceMessagesPort, params: ServiceMessagesParams
	) -> ServiceReport:
		"""Проход по истории: считает служебные записи, при чистке удаляет."""
		chosen = () if ctx.dry_run else selectable_kinds(params.kinds)
		account_id = ctx.executor.owner.id
		chat_id = ctx.community.tg_chat_id
		found: dict[ServiceMessageKind, int] = {}
		deleted = skipped = scanned = 0
		offset_id = 0
		oldest_date: datetime | None = None
		exhausted = limited = False
		try:
			while scanned < params.depth:
				ctx.check_stop()
				page = await gateway.userbot_service_messages_page(
					account_id, chat_id, offset_id, min(PAGE_SIZE, params.depth - scanned)
				)
				scanned += page.scanned
				oldest_date = page.oldest_date or oldest_date
				for message in page.messages:
					found[message.kind] = found.get(message.kind, 0) + 1
				if chosen:
					batch = [m.id for m in page.messages if m.kind in chosen]
					room = params.delete_limit - deleted
					if len(batch) > room:
						batch = batch[:room]
						limited = True
					if batch:
						gone = await gateway.userbot_delete_messages(account_id, chat_id, batch)
						deleted += gone
						skipped += len(batch) - gone
				ctx.progress(min(1.0, scanned / params.depth), f"просмотрено {scanned}")
				if page.next_offset_id is None:
					exhausted = True
					break
				if chosen and deleted >= params.delete_limit:
					limited = True
					break
				offset_id = page.next_offset_id
		finally:
			# итог в журнал при любом исходе, включая отмену и флуд-лимит:
			# удаление необратимо, и разбирать инцидент по «ничего
			# не сохранилось» нечем. Отчёт при обрыве не строится
			# намеренно (ADR-0026, п. 7), но след обязан остаться
			ctx.log(
				f"просмотрено {scanned}, служебных {sum(found.values())}, "
				f"удалено {deleted}, пропущено {skipped}"
			)
		ctx.progress(1.0, None)
		return ServiceReport(
			found=found,
			scanned=scanned,
			deleted=deleted,
			skipped=skipped,
			limited=limited,
			exhausted=exhausted,
			oldest_date=oldest_date,
		)
