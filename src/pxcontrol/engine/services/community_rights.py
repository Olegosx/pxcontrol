"""Правила над строками пула исполнителей: кто, что может, готов ли (ADR-0035).

Чистые функции над уже прочитанными строками ``Community`` и
``CommunityExecutor`` — ни сети, ни сессии базы: связи должны быть
подгружены вызывающим. Здесь живёт единственная точка ответа на вопросы
«чем это сообщество может публиковать» (:func:`community_capabilities`)
и «почему не может» (:func:`publisher_paused`, :func:`publisher_incapable`);
её спрашивают сервис сообществ, подготовка публикации, дозор кнопок
и — через снимок — интерфейс. Прежде правило жило в сервисе сообществ
рядом с операциями над базой, и тот вырос до полутора тысяч строк.
"""

from __future__ import annotations

from pxcontrol.engine.db.models import Community, CommunityExecutor
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.abilities import ExecutorAction, can
from pxcontrol.engine.services.accounts import account_display
from pxcontrol.engine.services.publish_route import PublishCapabilities, publish_capabilities
from pxcontrol.engine.telegram.rights import ExecutorRights, ParticipantStatus
from pxcontrol.engine.telegram.types import CommunityKind, ExecutorRef, OwnerKind


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


def publisher_ready(community: Community, kind: OwnerKind) -> bool:
	"""Может ли назначенный публикатор этого вида публиковать сейчас.

	Три условия: назначен, не приостановлен человеком (ADR-0029)
	и по последнему снимку прав способен публиковать в сообществе
	такого вида (ADR-0035).
	"""
	row = publisher_row(community, kind)
	if row is None or executor_paused(row):
		return False
	return can(executor_rights(row), ExecutorAction.PUBLISH, CommunityKind(community.kind))


def community_capabilities(community: Community) -> PublishCapabilities:
	"""Чем это сообщество может публиковать (ADR-0011, ADR-0035).

	Одна точка на весь движок и интерфейс: подготовка публикации, дозор
	кнопок, дашборд и формы спрашивают её, а не собирают правило заново.
	Связи ``executors`` и учётки исполнителей должны быть подгружены.
	"""
	bot_ready = publisher_ready(community, OwnerKind.BOT)
	markup_edit = False
	if bot_ready:
		row = publisher_row(community, OwnerKind.BOT)
		markup_edit = row is not None and can(
			executor_rights(row), ExecutorAction.EDIT_OTHERS, CommunityKind(community.kind)
		)
	return publish_capabilities(
		bot_ready, publisher_ready(community, OwnerKind.USER), markup_edit=markup_edit
	)


def publisher_paused(community: Community) -> bool:
	"""Есть ли у сообщества **приостановленный** публикатор (ADR-0029).

	Зовут это только из ветки «публиковать некем», чтобы отличить
	«нет публикатора» от «публикатор на паузе»: в первом случае человеку
	нужно назначить нового, во втором — возобновить прежнего.
	"""
	rows = (publisher_row(community, OwnerKind.USER), publisher_row(community, OwnerKind.BOT))
	return any(row is not None and executor_paused(row) for row in rows)


def publisher_incapable(community: Community) -> bool:
	"""Назначен, не на паузе — и по правам публиковать не может (ADR-0035).

	Третья причина ожидания рядом с «выключено» и «приостановлен»:
	права в Telegram меняет владелец сообщества, и приложение узнаёт
	об этом перепроверкой доступов. Пост в таком случае ждёт, а не падает.
	"""
	kind = CommunityKind(community.kind)
	for owner_kind in (OwnerKind.USER, OwnerKind.BOT):
		row = publisher_row(community, owner_kind)
		if row is None or executor_paused(row):
			continue
		if not can(executor_rights(row), ExecutorAction.PUBLISH, kind):
			return True
	return False
