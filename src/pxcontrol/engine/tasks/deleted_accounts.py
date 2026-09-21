"""Вид задачи «удалённые аккаунты»: поиск и исключение (ADR-0026, ADR-0038).

«Удалённый аккаунт» — учётка, которую владелец удалил; в списке
участников она остаётся мёртвой душой. Запуск «без изменений»
(``dry_run``) никого не трогает и отвечает числом; обычный запуск
исключает не больше выбранного количества за проход: число участников
видно всем, и резкое падение бьёт по охватам сообщества.

Исключение — блокировка со снятием; в супергруппе оно порождает
служебную запись «X удалил Y», и чистка убирает их за собой одной
пачкой в конце прохода — если у исполнителя есть право удалять
сообщения; иначе записи остаются, и отчёт говорит сколько.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pxcontrol.engine.services.abilities import ExecutorAction, can
from pxcontrol.engine.services.communities import ExecutorDto
from pxcontrol.engine.tasks.model import (
	TaskContext,
	TaskKind,
	TaskTitle,
	as_int,
	check_range,
	first_capable,
)
from pxcontrol.engine.telegram.mtproto import UserbotAccessError
from pxcontrol.engine.telegram.types import DeletedAccount, ParticipantsPage

logger = logging.getLogger(__name__)

#: Сколько участников читать одним запросом (предел Telegram — 200).
MEMBERS_PAGE_SIZE = 200

#: Сколько сообщений удалять одним запросом (предел Telegram — 100).
#: Численно совпадает с размером страницы чтения истории, но это другой
#: предел того же сервера: измени Telegram один — второй останется
#: прежним, и общая константа сломала бы соседнюю операцию молча.
DELETE_BATCH_SIZE = 100

#: Сколько удалённых аккаунтов исключать за проход по умолчанию.
#: Умолчание намеренно скромное: число участников — видимая всем
#: величина, и резкое падение бьёт по охватам сообщества.
DEFAULT_KICK_LIMIT = 20

#: Пределы потолка исключений. Нижняя граница — 1: убрать один
#: мёртвый аккаунт должно быть можно.
KICK_LIMIT_RANGE = (1, 1000)

TITLE = TaskTitle(
	noun="Удалённые аккаунты",
	hint=(
		"Удалённый аккаунт — учётка, которую владелец удалил; в списке "
		"участников она остаётся мёртвой душой. Исключение необратимо "
		"и уменьшает число участников, поэтому за проход убирается "
		"не больше выбранного количества."
	),
)


class DeletedAccountsPort(Protocol):
	"""Часть шлюза Telegram, нужная этому виду (для подмены в тестах)."""

	async def userbot_participants_page(
		self, account_id: int, chat_id: str, offset: int, limit: int
	) -> ParticipantsPage: ...

	async def userbot_kick_participant(
		self, account_id: int, chat_id: str, account: DeletedAccount
	) -> int | None: ...

	async def userbot_delete_messages(
		self, account_id: int, chat_id: str, message_ids: list[int]
	) -> int: ...


@dataclass(frozen=True)
class DeletedAccountsParams:
	"""Параметры задачи: потолок исключений за проход."""

	kick_limit: int = DEFAULT_KICK_LIMIT


@dataclass(frozen=True)
class MembersReport:
	"""Итог прохода по участникам.

	Attributes:
		found: сколько удалённых аккаунтов найдено.
		scanned: сколько участников просмотрено.
		total: сколько участников всего, по мнению Telegram (None —
			не сказал).
		removed: сколько удалённых аккаунтов исключено (просмотр — 0).
		skipped: сколько исключить не удалось — Telegram отказал
			по конкретной учётке. Не сбой прохода: остальные убираются
			(ADR-0026, то же правило, что у отказа в удалении записи).
		service_left: сколько служебных записей об исключении осталось
			в ленте — их не удалось убрать без права удалять сообщения.
		limited: чистка остановилась о потолок за проход.
		exhausted: список участников кончился.
		capped: Telegram перестал отдавать участников раньше, чем
			кончился список (``scanned`` меньше ``total``). Признак
			того самого предела выдачи, который обсуждался при
			проектировании (ADR-0026) — теперь он виден в отчёте,
			а не предполагается.
	"""

	found: int = 0
	scanned: int = 0
	total: int | None = None
	removed: int = 0
	skipped: int = 0
	service_left: int = 0
	limited: bool = False
	exhausted: bool = False
	capped: bool = False


def members_summary(report: MembersReport) -> str:
	"""Итог прохода по участникам одной строкой."""
	if report.removed or report.skipped:
		parts = [f"Исключено удалённых аккаунтов: {report.removed}"]
		if report.skipped:
			parts.append(f"Telegram не дал исключить: {report.skipped}")
		if report.limited:
			parts.append("сработал предел за проход — повторите, чтобы продолжить")
		if report.service_left:
			parts.append(
				f"записей «удалил участника» осталось в ленте: {report.service_left} "
				"(нет права удалять сообщения)"
			)
		return ". ".join(parts) + "."
	seen = f"просмотрено участников: {report.scanned}"
	if report.total:
		seen += f" из {report.total}"
	if report.capped:
		seen += " — дальше Telegram список не отдаёт"
	if not report.found:
		return f"Удалённых аккаунтов не найдено ({seen})."
	return f"Найдено удалённых аккаунтов: {report.found} ({seen})."


class DeletedAccountsTask:
	"""Спецификация вида «удалённые аккаунты» (контракт :class:`TaskSpec`)."""

	kind = TaskKind.DELETED_ACCOUNTS

	def default_params(self) -> DeletedAccountsParams:
		return DeletedAccountsParams()

	def params_from_payload(self, payload: dict[str, Any] | None) -> DeletedAccountsParams:
		return DeletedAccountsParams(
			kick_limit=as_int(payload or {}, "kick_limit", DEFAULT_KICK_LIMIT)
		)

	def params_to_payload(self, params: DeletedAccountsParams) -> dict[str, Any]:
		return {"kick_limit": params.kick_limit}

	def validate(self, params: DeletedAccountsParams, *, dry_run: bool) -> None:
		check_range("Предел исключений за проход", params.kick_limit, KICK_LIMIT_RANGE)

	def action(self, params: DeletedAccountsParams, *, dry_run: bool) -> ExecutorAction:
		return ExecutorAction.READ_HISTORY if dry_run else ExecutorAction.BAN

	def choose_executor(
		self,
		params: DeletedAccountsParams,
		cursor: dict[str, Any] | None,
		capable: Sequence[ExecutorDto],
	) -> ExecutorDto | None:
		return first_capable(capable)

	def title(self, *, dry_run: bool) -> str:
		return "Поиск удалённых аккаунтов" if dry_run else "Исключение удалённых аккаунтов"

	def report_from_payload(self, payload: dict[str, Any]) -> MembersReport:
		total = payload.get("total")
		return MembersReport(
			found=as_int(payload, "found", 0),
			scanned=as_int(payload, "scanned", 0),
			total=total if isinstance(total, int) else None,
			removed=as_int(payload, "removed", 0),
			skipped=as_int(payload, "skipped", 0),
			service_left=as_int(payload, "service_left", 0),
			limited=bool(payload.get("limited", False)),
			exhausted=bool(payload.get("exhausted", False)),
			capped=bool(payload.get("capped", False)),
		)

	def report_to_payload(self, report: MembersReport) -> dict[str, Any]:
		return {
			"found": report.found,
			"scanned": report.scanned,
			"total": report.total,
			"removed": report.removed,
			"skipped": report.skipped,
			"service_left": report.service_left,
			"limited": report.limited,
			"exhausted": report.exhausted,
			"capped": report.capped,
		}

	def summary(self, report: MembersReport, *, dry_run: bool) -> str:
		return members_summary(report)

	async def run(
		self, ctx: TaskContext, gateway: DeletedAccountsPort, params: DeletedAccountsParams
	) -> MembersReport:
		"""Проход по участникам: ищет удалённые учётки, при чистке исключает."""
		account_id = ctx.executor.owner.id
		chat_id = ctx.community.tg_chat_id
		clean = not ctx.dry_run
		# чистка исключением попутно убирает служебные записи о выходах —
		# если этому же исполнителю их удалять разрешено
		can_delete = can(ctx.executor.rights, ExecutorAction.DELETE_OTHERS, ctx.community.kind)
		found = removed = skipped = scanned = 0
		offset = 0
		total: int | None = None
		exhausted = limited = False
		service_ids: list[int] = []
		completed = False
		try:
			while True:
				ctx.check_stop()
				page = await gateway.userbot_participants_page(
					account_id, chat_id, offset, MEMBERS_PAGE_SIZE
				)
				scanned += page.scanned
				total = page.total if page.total is not None else total
				found += len(page.deleted)
				if clean:
					for account in page.deleted:
						if removed >= params.kick_limit:
							limited = True
							break
						ctx.check_stop()
						try:
							service_id = await gateway.userbot_kick_participant(
								account_id, chat_id, account
							)
						except UserbotAccessError as exc:
							# Telegram отказал по этой учётке (её не видно,
							# она администратор, хеш доступа устарел). Это
							# пропуск, а не конец прохода: остальные мёртвые
							# души убрать всё ещё можно (ADR-0026)
							skipped += 1
							ctx.log(f"аккаунт id={account.user_id} исключить не удалось: {exc}")
							continue
						removed += 1
						if service_id is not None:
							service_ids.append(service_id)
				ctx.progress(
					min(1.0, scanned / total) if total else 0.0,
					f"просмотрено участников {scanned}",
				)
				if page.next_offset is None:
					exhausted = True
					break
				if clean and removed >= params.kick_limit:
					limited = True
					break
				offset = page.next_offset
			completed = True
		finally:
			# исключение участника необратимо: итог в журнал при любом
			# исходе, включая отмену и флуд-лимит
			ctx.log(
				f"участников просмотрено {scanned} из "
				f"{total if total is not None else '?'}, мёртвых {found}, "
				f"исключено {removed}, пропущено {skipped}"
			)
			if not completed and service_ids:
				# уборка за собой идёт после прохода, и обрыв её отменяет:
				# записи «X удалил Y» остаются в ленте. Молчать об этом
				# нельзя — обещание «чистка убирает за собой» не сбылось
				ctx.log(
					f"проход прерван: {len(service_ids)} служебных записей об исключении "
					"остались в ленте — уберите их чисткой служебных записей"
				)
		left = await self._sweep_service_notes(
			gateway, account_id, chat_id, service_ids, can_delete
		)
		ctx.progress(1.0, None)
		return MembersReport(
			found=found,
			scanned=scanned,
			total=total,
			removed=removed,
			skipped=skipped,
			service_left=left,
			limited=limited,
			exhausted=exhausted,
			# Telegram перестал отдавать участников раньше конца списка
			capped=exhausted and total is not None and scanned < total,
		)

	@staticmethod
	async def _sweep_service_notes(
		gateway: DeletedAccountsPort,
		account_id: int,
		chat_id: str,
		ids: list[int],
		can_delete: bool,
	) -> int:
		"""Убирает служебные записи, которые породили исключения.

		Чистка мёртвых душ сама производит тот мусор, который убирает
		первый вид обслуживания, — оставлять его нечестно. Без права
		удалять сообщения записи остаются: сколько именно, скажет отчёт.

		Returns:
			Сколько записей осталось в ленте.
		"""
		if not ids:
			return 0
		if not can_delete:
			return len(ids)
		left = 0
		for start in range(0, len(ids), DELETE_BATCH_SIZE):
			batch = ids[start : start + DELETE_BATCH_SIZE]
			gone = await gateway.userbot_delete_messages(account_id, chat_id, batch)
			left += len(batch) - gone
		return left
