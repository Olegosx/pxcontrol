"""Правила над строками пула исполнителей: кто, что может, готов ли (ADR-0035).

Чистые функции над уже прочитанными строками ``Community`` и
``CommunityExecutor`` — ни сети, ни сессии базы: связи должны быть
подгружены вызывающим. Здесь живёт единственная точка ответа на вопросы
«чем это сообщество может публиковать» (:func:`community_capabilities`),
«почему не может» (:func:`publisher_paused`, :func:`publisher_incapable`)
и «кому из пула поручить действие» (:func:`ranked_executors` — способные
по снимку прав, в порядке диспетчера ADR-0036); её спрашивают сервисы
сообществ, постов, статистики, обслуживания, дозор кнопок и — через
снимок — интерфейс. Прежде правило жило в сервисе сообществ рядом
с операциями над базой, и тот вырос до полутора тысяч строк.
"""

from __future__ import annotations

from collections.abc import Mapping

from pxcontrol.engine.db.models import Community, CommunityExecutor
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.abilities import ExecutorAction, can
from pxcontrol.engine.services.accounts import account_display
from pxcontrol.engine.services.dispatch import Candidate, rank
from pxcontrol.engine.services.publish_route import PublishCapabilities, publish_capabilities
from pxcontrol.engine.telegram.lane import LaneLiveState
from pxcontrol.engine.telegram.rights import ExecutorRights, ParticipantStatus
from pxcontrol.engine.telegram.types import BotRef, CommunityKind, ExecutorRef, OwnerKind


def executor_owner(row: CommunityExecutor) -> ExecutorRef:
	"""Владелец строки — ключ, общий со шлюзом и учётом активности (ADR-0035).

	Raises:
		CommunityError: Строка без владельца — такого не допускает схема,
			но читать данные вслепую нельзя.
	"""
	if row.tg_account_id is not None:
		return ExecutorRef(OwnerKind.USER, row.tg_account_id)
	if row.bot_id is not None:
		return ExecutorRef(OwnerKind.BOT, row.bot_id)
	raise EngineError("Строка исполнителя без владельца — данные повреждены.")


def executor_rights(row: CommunityExecutor) -> ExecutorRights:
	"""Снимок прав исполнителя из его строки."""
	return ExecutorRights.from_payload(ParticipantStatus(row.status), row.rights)


def executor_paused(row: CommunityExecutor) -> bool:
	"""Приостановлен ли исполнитель человеком (ADR-0029).

	Связь ``tg_account``/``bot`` должна быть подгружена.
	"""
	owner = row.tg_account if row.tg_account_id is not None else row.bot
	return bool(owner is not None and owner.paused)


def executor_label(row: CommunityExecutor) -> str:
	"""Человеческое имя исполнителя: пометка пользователя или название бота."""
	if row.tg_account is not None:
		return account_display(
			row.tg_account.label,
			row.tg_account.username,
			row.tg_account.first_name,
			row.tg_account.last_name,
			row.tg_account.phone,
		)
	return row.bot.label if row.bot is not None else "исполнитель"


def publisher_row(community: Community, kind: OwnerKind) -> CommunityExecutor | None:
	"""Строка назначенного публикатора этого вида (None — не назначен).

	Назначение — ссылка сообщества, исполнитель — строка пула; здесь они
	сводятся. Связь ``executors`` должна быть подгружена.
	"""
	target = community.default_tg_account_id if kind is OwnerKind.USER else community.default_bot_id
	if target is None:
		return None
	for row in community.executors:
		owner_id = row.tg_account_id if kind is OwnerKind.USER else row.bot_id
		if owner_id == target:
			return row
	return None


def bot_ref(row: CommunityExecutor) -> BotRef:
	"""Адрес бота для шлюза из строки пула (id и токен).

	Raises:
		EngineError: За строкой не стоит бот — вызывающий перепутал вид.
	"""
	if row.bot is None:
		raise EngineError("Строка исполнителя без бота — данные повреждены.")
	return BotRef(row.bot.id, row.bot.token)


def capable_rows(
	community: Community, action: ExecutorAction, kind: OwnerKind | None = None
) -> list[CommunityExecutor]:
	"""Строки пула, способные на действие сейчас (ADR-0035, ADR-0036).

	Не приостановлены человеком (ADR-0029) и по последнему снимку прав
	могут в сообществе такого вида; ``kind`` сужает до пользователей или
	ботов. Порядок — порядок пула. Связи ``executors → tg_account / bot``
	должны быть подгружены.
	"""
	community_kind = CommunityKind(community.kind)
	return [
		row
		for row in community.executors
		if (kind is None or executor_owner(row).kind is kind)
		and not executor_paused(row)
		and can(executor_rights(row), action, community_kind)
	]


def ranked_executors(
	community: Community,
	action: ExecutorAction,
	live: Mapping[ExecutorRef, LaneLiveState],
	*,
	kind: OwnerKind | None = None,
) -> list[CommunityExecutor]:
	"""Способные на действие — в порядке, в котором им стоит поручать (ADR-0036).

	Способность — снимок прав (:func:`capable_rows`), порядок — диспетчер
	(:func:`rank`) по живому состоянию дорожек: свободный раньше занятого
	загрузкой, меньше ожидающих — раньше, при равенстве — публикатор
	по умолчанию своего вида, затем порядок пула. Единственная точка
	ответа «кому поручить» для всех сервисов: публикации, чтений,
	кнопок, статистики, обслуживания и ввода в сообщество.
	"""
	rows = capable_rows(community, action, kind)
	by_owner = {executor_owner(row): row for row in rows}
	candidates = [
		Candidate(owner, preferred=_is_default(community, owner), live=live.get(owner))
		for owner in by_owner
	]
	return [by_owner[candidate.owner] for candidate in rank(candidates)]


def _is_default(community: Community, owner: ExecutorRef) -> bool:
	"""Назначен ли исполнитель публикатором по умолчанию своего вида."""
	if owner.kind is OwnerKind.USER:
		return owner.id == community.default_tg_account_id
	return owner.id == community.default_bot_id


def publishing_users(community: Community) -> list[CommunityExecutor]:
	"""Пользователи пула, способные публиковать сейчас (ADR-0036).

	Пул публикаторов, а не один назначенный: публикатор по умолчанию
	среди них — предпочтение диспетчера, а не единственный маршрут.
	"""
	return capable_rows(community, ExecutorAction.PUBLISH, OwnerKind.USER)


def community_capabilities(community: Community) -> PublishCapabilities:
	"""Чем это сообщество может публиковать (ADR-0011, ADR-0035, ADR-0036).

	Одна точка на весь движок и интерфейс: подготовка публикации, дозор
	кнопок, дашборд и формы спрашивают её, а не собирают правило заново.
	Каждый путь открыт, когда в пуле есть хоть один способный исполнитель
	этого вида; кнопки к чужому посту — когда есть бот с правом править
	чужое. Связи ``executors`` и учётки исполнителей должны быть подгружены.
	"""
	return publish_capabilities(
		bool(capable_rows(community, ExecutorAction.PUBLISH, OwnerKind.BOT)),
		bool(publishing_users(community)),
		markup_edit=bool(capable_rows(community, ExecutorAction.EDIT_OTHERS, OwnerKind.BOT)),
	)


def _capable_by_rights(row: CommunityExecutor, kind: CommunityKind) -> bool:
	"""Мог бы публиковать по правам, если бы не пауза."""
	return can(executor_rights(row), ExecutorAction.PUBLISH, kind)


def publisher_paused(community: Community) -> bool:
	"""Публиковать некому **только из-за паузы** (ADR-0029, ADR-0036).

	Истинно, когда способного публикатора нет, но среди приостановленных
	есть тот, кто по правам мог бы: дашборд показывает «публикатор
	приостановлен» вместо «нет публикатора» — назначать нового не нужно,
	нужно возобновить прежнего. Пока сообщество публикует хоть кем-то,
	пауза одного из пула — не состояние сообщества.
	"""
	caps = community_capabilities(community)
	if caps.userbot or caps.bot:
		return False
	kind = CommunityKind(community.kind)
	return any(
		executor_paused(row) and _capable_by_rights(row, kind) for row in community.executors
	)


def publisher_incapable(community: Community) -> bool:
	"""Публиковать некому из-за **прав**, а не из-за паузы или пустого пула (ADR-0035).

	Третья причина ожидания рядом с «выключено» и «приостановлен»:
	права в Telegram меняет владелец сообщества, и приложение узнаёт
	об этом перепроверкой доступов. Пост в таком случае ждёт, а не падает.
	Истинно, когда способных нет, а в пуле есть не приостановленный
	исполнитель, лишённый права публиковать.
	"""
	caps = community_capabilities(community)
	if caps.userbot or caps.bot:
		return False
	kind = CommunityKind(community.kind)
	return any(
		not executor_paused(row) and not _capable_by_rights(row, kind)
		for row in community.executors
	)
