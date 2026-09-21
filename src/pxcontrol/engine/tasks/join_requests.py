"""Вид задачи «приём заявок»: одобрение заявок на вступление (ADR-0040).

Сообщество, куда попадают по заявке, копит ожидающих; задача
разбирает их: удалённые аккаунты отклоняет (если так настроено),
остальных одобряет, а в **группе** заявителя со ссылками в профиле —
одобряет с полным ограничением прав: читать может, писать, слать медиа,
приглашать и реагировать — нет. Ссылки в профиле — признак спамера;
в канале ограничения участника бессмысленны, и там флажки не действуют.

Запуск «без изменений» (``dry_run``) только читает заявки и считает,
что бы с ними сделал обычный запуск. Потолок за проход обязателен:
одобрение — массовая операция того же класса, что уборка (ADR-0026).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pxcontrol.engine.services.abilities import ExecutorAction, can
from pxcontrol.engine.services.communities import ExecutorDto
from pxcontrol.engine.tasks.model import (
	TaskContext,
	TaskKind,
	TaskTitle,
	as_int,
	check_range,
)
from pxcontrol.engine.telegram.mtproto import UserbotJoinRequestError
from pxcontrol.engine.telegram.types import CommunityKind, JoinRequest, JoinRequestsPage

logger = logging.getLogger(__name__)

#: Сколько заявок читать одним запросом.
PAGE_SIZE = 100

#: Сколько заявок разбирать за проход по умолчанию.
DEFAULT_LIMIT = 50

#: Пределы потолка за проход. Нижняя граница — 1: разобрать одну
#: заявку должно быть можно.
LIMIT_RANGE = (1, 1000)

TITLE = TaskTitle(
	noun="Приём заявок",
	hint=(
		"Разбирает заявки на вступление: удалённые аккаунты отклоняет, "
		"остальных принимает; в группе заявителя со ссылками в профиле "
		"можно принять с полным ограничением — читать сможет, писать нет."
	),
)

#: Что считается ссылкой в описании профиля: адрес, домен, @упоминание.
_LINK = re.compile(
	r"(https?://|t\.me/|telegram\.me/|www\.|@[a-z0-9_]{4,}|\b[a-z0-9-]+\.(?:com|ru|org|net|io|me|app|xyz|site|online|shop|store|info|biz|pro|top|club|link|tv|su|ua|by|kz)\b)",
	re.IGNORECASE,
)


def profile_has_links(bio: str | None) -> bool:
	"""Есть ли в описании профиля ссылка (чистая функция, ADR-0040)."""
	return bool(bio) and _LINK.search(bio or "") is not None


class JoinRequestsPort(Protocol):
	"""Часть шлюза Telegram, нужная этому виду (для подмены в тестах)."""

	async def userbot_join_requests_page(
		self, account_id: int, chat_id: str, offset: tuple[datetime, int] | None, limit: int
	) -> JoinRequestsPage: ...

	async def userbot_handle_join_request(
		self, account_id: int, chat_id: str, request: JoinRequest, *, approve: bool
	) -> None: ...

	async def userbot_restrict_fully(
		self, account_id: int, chat_id: str, request: JoinRequest
	) -> None: ...

	async def userbot_has_personal_channel(self, account_id: int, request: JoinRequest) -> bool: ...


@dataclass(frozen=True)
class JoinRequestsParams:
	"""Параметры задачи.

	Attributes:
		decline_deleted: отклонять заявки удалённых аккаунтов
			(иначе они пропускаются нетронутыми).
		restrict_bio_links: в группе принимать с полным ограничением,
			если в описании профиля есть ссылка.
		restrict_personal_channel: то же, если в профиле указан канал
			(стоит по запросу на заявителя — отдельный флажок).
		limit: сколько заявок разобрать за проход.
	"""

	decline_deleted: bool = True
	restrict_bio_links: bool = False
	restrict_personal_channel: bool = False
	limit: int = DEFAULT_LIMIT

	@property
	def restricts(self) -> bool:
		"""Включено ли хоть одно правило ограничения."""
		return self.restrict_bio_links or self.restrict_personal_channel


@dataclass(frozen=True)
class JoinRequestsReport:
	"""Итог прохода по заявкам.

	Attributes:
		found: сколько заявок ждало (по мнению Telegram).
		reviewed: сколько разобрано (просмотр — сколько посмотрено).
		approved: одобрено (у просмотра — сколько было бы одобрено).
		restricted: из одобренных — с полным ограничением.
		declined: отклонено удалённых.
		skipped: Telegram не дал обработать (заявки уже нет, заявитель
			негоден) — пропуск, а не сбой.
		limited: проход остановился о потолок.
	"""

	found: int = 0
	reviewed: int = 0
	approved: int = 0
	restricted: int = 0
	declined: int = 0
	skipped: int = 0
	limited: bool = False


def join_requests_summary(report: JoinRequestsReport, *, dry_run: bool) -> str:
	"""Итог прохода одной строкой."""
	if dry_run:
		if not report.found:
			return "Заявок на вступление нет."
		parts = [
			f"Заявок ждёт: {report.found}; просмотрено {report.reviewed} — "
			f"к приёму {report.approved}, из них с ограничением {report.restricted}, "
			f"удалённых к отклонению {report.declined}"
		]
		return ". ".join(parts) + "."
	if not report.reviewed:
		return "Заявок на вступление нет."
	parts = [f"Принято: {report.approved}"]
	if report.restricted:
		parts.append(f"из них с полным ограничением: {report.restricted}")
	if report.declined:
		parts.append(f"отклонено удалённых: {report.declined}")
	if report.skipped:
		parts.append(f"Telegram не дал обработать: {report.skipped}")
	if report.limited:
		parts.append("сработал предел за проход — остальные ждут следующего")
	return ". ".join(parts) + "."


class JoinRequestsTask:
	"""Спецификация вида «приём заявок» (контракт :class:`TaskSpec`)."""

	kind = TaskKind.JOIN_REQUESTS

	def default_params(self) -> JoinRequestsParams:
		return JoinRequestsParams()

	def params_from_payload(self, payload: dict[str, Any] | None) -> JoinRequestsParams:
		payload = payload or {}
		return JoinRequestsParams(
			decline_deleted=bool(payload.get("decline_deleted", True)),
			restrict_bio_links=bool(payload.get("restrict_bio_links", False)),
			restrict_personal_channel=bool(payload.get("restrict_personal_channel", False)),
			limit=as_int(payload, "limit", DEFAULT_LIMIT),
		)

	def params_to_payload(self, params: JoinRequestsParams) -> dict[str, Any]:
		return {
			"decline_deleted": params.decline_deleted,
			"restrict_bio_links": params.restrict_bio_links,
			"restrict_personal_channel": params.restrict_personal_channel,
			"limit": params.limit,
		}

	def validate(self, params: JoinRequestsParams, *, dry_run: bool) -> None:
		check_range("Предел заявок за проход", params.limit, LIMIT_RANGE)

	def action(self, params: JoinRequestsParams, *, dry_run: bool) -> ExecutorAction:
		return ExecutorAction.APPROVE_REQUESTS

	def choose_executor(
		self,
		params: JoinRequestsParams,
		cursor: dict[str, Any] | None,
		capable: Sequence[ExecutorDto],
	) -> ExecutorDto | None:
		"""Первый способный принимать заявки; с ограничением — ещё и исключать.

		Право «блокировка пользователей» проверяется у того же
		исполнителя: одобрить одним, а ограничить другим значило бы
		вести одну заявку двумя аккаунтами. Вид сообщества здесь
		не известен — в канале ограничение просто не применяется.
		"""
		if not params.restricts:
			return capable[0] if capable else None
		for executor in capable:
			# вид сообщества у права «исключать» не участвует — годится любой
			if can(executor.rights, ExecutorAction.BAN, CommunityKind.GROUP):
				return executor
		return None

	def title(self, *, dry_run: bool) -> str:
		return "Заявки: просмотр" if dry_run else "Заявки: приём"

	def report_from_payload(self, payload: dict[str, Any]) -> JoinRequestsReport:
		return JoinRequestsReport(
			found=as_int(payload, "found", 0),
			reviewed=as_int(payload, "reviewed", 0),
			approved=as_int(payload, "approved", 0),
			restricted=as_int(payload, "restricted", 0),
			declined=as_int(payload, "declined", 0),
			skipped=as_int(payload, "skipped", 0),
			limited=bool(payload.get("limited", False)),
		)

	def report_to_payload(self, report: JoinRequestsReport) -> dict[str, Any]:
		return {
			"found": report.found,
			"reviewed": report.reviewed,
			"approved": report.approved,
			"restricted": report.restricted,
			"declined": report.declined,
			"skipped": report.skipped,
			"limited": report.limited,
		}

	def summary(self, report: JoinRequestsReport, *, dry_run: bool) -> str:
		return join_requests_summary(report, dry_run=dry_run)

	async def run(
		self, ctx: TaskContext, gateway: JoinRequestsPort, params: JoinRequestsParams
	) -> JoinRequestsReport:
		"""Проход по заявкам страницами: решение по каждой, потолок за проход."""
		account_id = ctx.executor.owner.id
		chat_id = ctx.community.tg_chat_id
		# ограничения участника есть только у групп; в канале правила
		# ограничения не действуют, и это сказано в форме
		restricting = ctx.community.kind is CommunityKind.GROUP and params.restricts
		found = reviewed = approved = restricted = declined = skipped = 0
		limited = False
		offset: tuple[datetime, int] | None = None
		try:
			while reviewed < params.limit:
				ctx.check_stop()
				page = await gateway.userbot_join_requests_page(
					account_id, chat_id, offset, min(PAGE_SIZE, params.limit - reviewed)
				)
				found = max(found, page.total)
				if not page.requests:
					break
				for request in page.requests:
					if reviewed >= params.limit:
						limited = True
						break
					ctx.check_stop()
					reviewed += 1
					try:
						verdict = await self._review(
							ctx, gateway, account_id, chat_id, request, params, restricting
						)
					except UserbotJoinRequestError as exc:
						skipped += 1
						ctx.log(f"{request.label}: {exc}")
						continue
					if verdict == "declined":
						declined += 1
					elif verdict == "restricted":
						approved += 1
						restricted += 1
					elif verdict == "approved":
						approved += 1
					ctx.progress(min(1.0, reviewed / max(found, 1)), f"разобрано заявок {reviewed}")
				if limited or page.next_offset is None:
					break
				offset = page.next_offset
			if reviewed >= params.limit and found > reviewed:
				limited = True
		finally:
			# одобрение и отклонение необратимы: итог в журнал при любом исходе
			ctx.log(
				f"заявок {found}, разобрано {reviewed}, принято {approved} "
				f"(с ограничением {restricted}), отклонено {declined}, пропущено {skipped}"
			)
		ctx.progress(1.0, None)
		return JoinRequestsReport(
			found=found,
			reviewed=reviewed,
			approved=approved,
			restricted=restricted,
			declined=declined,
			skipped=skipped,
			limited=limited,
		)

	async def _review(
		self,
		ctx: TaskContext,
		gateway: JoinRequestsPort,
		account_id: int,
		chat_id: str,
		request: JoinRequest,
		params: JoinRequestsParams,
		restricting: bool,
	) -> str:
		"""Решение по одной заявке: «declined» · «approved» · «restricted» · «left».

		В запуске «без изменений» решение только вычисляется — Telegram
		не трогается, кроме чтения профиля, если так настроено.
		"""
		if request.deleted:
			if not params.decline_deleted:
				ctx.log(f"{request.label}: аккаунт удалён — оставлен без решения")
				return "left"
			if not ctx.dry_run:
				await gateway.userbot_handle_join_request(
					account_id, chat_id, request, approve=False
				)
			ctx.log(f"{request.label}: аккаунт удалён — отклонён")
			return "declined"
		suspicious = restricting and await self._has_links(gateway, account_id, request, params)
		if not ctx.dry_run:
			await gateway.userbot_handle_join_request(account_id, chat_id, request, approve=True)
			if suspicious:
				await gateway.userbot_restrict_fully(account_id, chat_id, request)
		if suspicious:
			ctx.log(f"{request.label}: принят с полным ограничением — ссылки в профиле")
			return "restricted"
		ctx.log(f"{request.label}: принят")
		return "approved"

	@staticmethod
	async def _has_links(
		gateway: JoinRequestsPort, account_id: int, request: JoinRequest, params: JoinRequestsParams
	) -> bool:
		"""Ссылки в профиле по включённым правилам (канал в профиле — запросом)."""
		if params.restrict_bio_links and profile_has_links(request.bio):
			return True
		if params.restrict_personal_channel:
			return await gateway.userbot_has_personal_channel(account_id, request)
		return False
