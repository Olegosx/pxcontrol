"""Сервис настроек сообщества в Telegram: экран «Настройки › В Telegram» (ADR-0043).

Три операции для интерфейса:

- :meth:`CommunitySettingsService.open` — снимок настроек глазами
  публикатора по умолчанию и доступность каждой настройки;
- :meth:`CommunitySettingsService.save` — изменения по одному, с итогом
  по каждому, и свежий снимок после;
- :meth:`CommunitySettingsService.discussion_candidates` — группы,
  пригодные для обсуждения канала.

Исполнитель — публикатор по умолчанию (:func:`settings_executor`):
userbot, иначе бот. Что править можно, решают правила каталога
(:mod:`pxcontrol.engine.community_settings.rules`), последнее слово —
за сервером. У Telegram нет общей транзакции на несколько настроек,
поэтому сохранение идёт по одному изменению: отказ по одной настройке
не мешает остальным, а флуд-лимит и потеря связи останавливают
отправку — о неотправленном человек узнаёт честно.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.community_settings.catalog import SPECS, SettingSpec, applicable
from pxcontrol.engine.community_settings.model import (
	ChangeResult,
	CommunitySettings,
	LinkedChat,
	SettingChange,
	SettingValue,
)
from pxcontrol.engine.community_settings.rules import (
	Availability,
	availability,
	changes,
	value_problem,
)
from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, CommunityExecutor
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.communities import CommunitiesService
from pxcontrol.engine.services.community_rights import (
	bot_ref,
	executor_label,
	executor_owner,
	executor_rights,
	settings_executor,
)
from pxcontrol.engine.telegram.bot_api import BotError, BotNotInCommunityError, InvalidBotTokenError
from pxcontrol.engine.telegram.bot_settings import BOT_WRITABLE
from pxcontrol.engine.telegram.mtproto import UserbotSettingRefusedError, UserbotUnavailableError
from pxcontrol.engine.telegram.mtproto_settings import WRITERS
from pxcontrol.engine.telegram.rights import ExecutorRights
from pxcontrol.engine.telegram.types import (
	BotRef,
	CommunityKind,
	ExecutorRef,
	OwnerKind,
	TelegramFloodError,
)

logger = logging.getLogger(__name__)

#: Что показать вместо формы, когда править некому.
NO_EXECUTOR = (
	"У сообщества нет публикатора по умолчанию, который мог бы менять настройки. "
	"Назначьте userbot-аккаунт или бота на вкладке «Участники»."
)

#: Что написать у изменений, до которых после остановки не дошло.
NOT_SENT = "Не отправлено: {reason}"


class CommunitySettingsError(EngineError):
	"""Ошибка экрана настроек сообщества (с понятным человеку текстом)."""


class _SettingsGateway(Protocol):
	"""Что сервису нужно от шлюза Telegram."""

	async def userbot_community_settings(
		self, account_id: int, chat_id: str, kind: CommunityKind
	) -> CommunitySettings: ...

	async def userbot_apply_setting(
		self, account_id: int, chat_id: str, change: SettingChange
	) -> None: ...

	async def userbot_discussion_candidates(self, account_id: int) -> list[LinkedChat]: ...

	async def bot_community_settings(
		self, bot: BotRef, chat_id: str, kind: CommunityKind
	) -> CommunitySettings: ...

	async def bot_apply_setting(self, bot: BotRef, chat_id: str, change: SettingChange) -> None: ...


@dataclass(frozen=True)
class SettingsView:
	"""Экран настроек: кто правит, что прочитано и что можно менять.

	Attributes:
		community_id: сообщество.
		executor_label: человеческое имя исполнителя (пусто — некому).
		executor_kind: пользователь или бот (None — некому).
		settings: снимок (None — некому править, см. ``reason``).
		specs: показываемые настройки — есть у сообщества этого вида
			и прочитаны исполнителем — в порядке каталога.
		access: доступность каждой показываемой настройки.
		reason: почему формы нет (None — форма есть).
	"""

	community_id: int
	executor_label: str = ""
	executor_kind: OwnerKind | None = None
	settings: CommunitySettings | None = None
	specs: tuple[SettingSpec, ...] = ()
	access: Mapping[str, Availability] | None = None
	reason: str | None = None


@dataclass(frozen=True)
class SettingsSaved:
	"""Итог сохранения: по каждому изменению и свежий экран.

	Attributes:
		results: итог каждого изменения — в порядке применения.
		view: экран после сохранения (None — перечитать не удалось).
		reread_error: почему не удалось перечитать (None — удалось).
	"""

	results: tuple[ChangeResult, ...]
	view: SettingsView | None = None
	reread_error: str | None = None

	@property
	def failed(self) -> tuple[ChangeResult, ...]:
		"""Изменения, которые Telegram не принял или до которых не дошло."""
		return tuple(result for result in self.results if not result.applied)


@dataclass(frozen=True)
class _Executor:
	"""Исполнитель правки: ключ, имя, права и то, что умеет его транспорт."""

	row: CommunityExecutor
	owner: ExecutorRef
	label: str
	rights: ExecutorRights
	writable: frozenset[str]


def refusal(exc: Exception) -> tuple[str, bool]:
	"""Текст отказа для человека и «останавливать ли остальные изменения».

	Отказ по самой настройке (сервер не принял значение, нет права)
	остальным не мешает. Флуд-лимит, потеря связи, исчезнувший доступ
	останавливают отправку: следующие запросы получили бы тот же ответ,
	а флуд-лимит от них только вырос бы.

	Raises:
		Exception: Исключение не из таксономии транспортов — пробрасывается
			как есть (неожиданность, её место — журнал).
	"""
	if isinstance(exc, TelegramFloodError):
		return f"Telegram попросил подождать {exc.retry_after_s} с.", True
	if isinstance(exc, UserbotSettingRefusedError):
		return str(exc), False
	if isinstance(exc, BotNotInCommunityError | InvalidBotTokenError):
		return str(exc), True
	if isinstance(exc, BotError):
		return str(exc), False
	if isinstance(exc, UserbotUnavailableError | ConnectionError):
		return str(exc), True
	raise exc


class CommunitySettingsService:
	"""Настройки сообщества в Telegram: чтение, правка, кандидаты обсуждения."""

	def __init__(
		self, db: Database, gateway: _SettingsGateway, communities: CommunitiesService
	) -> None:
		self._db = db
		self._gateway = gateway
		self._communities = communities

	# --- чтение -----------------------------------------------------------------

	async def open(self, community_id: int) -> SettingsView:
		"""Снимок настроек глазами публикатора по умолчанию.

		Raises:
			CommunitySettingsError: Сообщество не найдено.
			UserbotUnavailableError, BotError, TelegramFloodError,
			ConnectionError: Telegram не ответил (текст — для человека).
		"""
		community, executor = await self._load(community_id)
		if executor is None:
			return SettingsView(community_id, reason=NO_EXECUTOR)
		kind = CommunityKind(community.kind)
		snapshot = await self._read(community.tg_chat_id, kind, executor)
		return self._view(community_id, snapshot, executor)

	async def _read(
		self, chat_id: str, kind: CommunityKind, executor: _Executor
	) -> CommunitySettings:
		"""Снимок через транспорт исполнителя — только настройки этого вида."""
		if executor.owner.kind is OwnerKind.USER:
			raw = await self._gateway.userbot_community_settings(executor.owner.id, chat_id, kind)
		else:
			raw = await self._gateway.bot_community_settings(bot_ref(executor.row), chat_id, kind)
		keys = {spec.key for spec in applicable(kind)}
		values = {key: value for key, value in raw.values.items() if key in keys}
		return CommunitySettings(values, raw.context)

	@staticmethod
	def _view(community_id: int, snapshot: CommunitySettings, executor: _Executor) -> SettingsView:
		specs = tuple(
			spec for spec in applicable(snapshot.context.kind) if spec.key in snapshot.values
		)
		access = {
			spec.key: availability(spec, snapshot, executor.rights, executor.writable)
			for spec in specs
		}
		return SettingsView(
			community_id,
			executor.label,
			executor.owner.kind,
			snapshot,
			specs,
			access,
		)

	async def _load(self, community_id: int) -> tuple[Community, _Executor | None]:
		"""Сообщество с пулом и исполнитель правки (None — некому).

		Raises:
			CommunitySettingsError: Сообщество не найдено.
		"""
		async with self._db.session_factory() as session:
			community = (
				await session.execute(
					select(Community)
					.options(
						selectinload(Community.executors).selectinload(
							CommunityExecutor.tg_account
						),
						selectinload(Community.executors).selectinload(CommunityExecutor.bot),
					)
					.where(Community.id == community_id)
				)
			).scalar_one_or_none()
		if community is None:
			raise CommunitySettingsError("Сообщество не найдено — обновите список.")
		row = settings_executor(community)
		if row is None:
			return community, None
		owner = executor_owner(row)
		writable = frozenset(WRITERS) if owner.kind is OwnerKind.USER else BOT_WRITABLE
		return community, _Executor(row, owner, executor_label(row), executor_rights(row), writable)

	# --- правка -------------------------------------------------------------------

	async def save(
		self,
		community_id: int,
		before: CommunitySettings,
		after: Mapping[str, SettingValue],
	) -> SettingsSaved:
		"""Применяет правку по одному изменению и перечитывает экран.

		``before`` — снимок, с которого начиналась правка: изменения
		считаются от него (:func:`changes`), чужие правки в Telegram
		за время правки не затираются.

		Raises:
			CommunitySettingsError: Сообщество не найдено или править некому.
		"""
		community, executor = await self._load(community_id)
		if executor is None:
			raise CommunitySettingsError(NO_EXECUTOR)
		results = await self._apply_all(community, executor, before, after)
		return await self._after_save(community_id, results)

	async def _apply_all(
		self,
		community: Community,
		executor: _Executor,
		before: CommunitySettings,
		after: Mapping[str, SettingValue],
	) -> tuple[ChangeResult, ...]:
		"""Изменения по порядку каталога; остановка — честный итог остальным."""
		results: list[ChangeResult] = []
		stopped: str | None = None
		for change in changes(before, after):
			if stopped is not None:
				results.append(ChangeResult(change.key, NOT_SENT.format(reason=stopped)))
				continue
			problem = self._problem(SPECS[change.key], change.value, before, executor)
			if problem is not None:
				results.append(ChangeResult(change.key, problem))
				continue
			error, stop = await self._apply_one(community.tg_chat_id, executor, change)
			results.append(ChangeResult(change.key, error))
			if stop:
				stopped = error
		return tuple(results)

	@staticmethod
	def _problem(
		spec: SettingSpec, value: SettingValue, before: CommunitySettings, executor: _Executor
	) -> str | None:
		"""Претензия до обращения к Telegram: значение или доступность."""
		problem = value_problem(spec, value)
		if problem is not None:
			return problem
		access = availability(spec, before, executor.rights, executor.writable)
		return None if access.editable else access.reason

	async def _apply_one(
		self, chat_id: str, executor: _Executor, change: SettingChange
	) -> tuple[str | None, bool]:
		"""Одно изменение: (текст отказа или None, останавливать ли остальные)."""
		try:
			if executor.owner.kind is OwnerKind.USER:
				await self._gateway.userbot_apply_setting(executor.owner.id, chat_id, change)
			else:
				await self._gateway.bot_apply_setting(bot_ref(executor.row), chat_id, change)
		except Exception as exc:  # noqa: BLE001 — разбор в refusal (неожиданное — дальше)
			text, stop = refusal(exc)
			logger.info("Настройка «%s» сообщества %s не изменена: %s", change.key, chat_id, text)
			return text, stop
		return None, False

	async def _after_save(
		self, community_id: int, results: tuple[ChangeResult, ...]
	) -> SettingsSaved:
		"""Перечитывает экран и обновляет запись сообщества (название, @имя, темы)."""
		try:
			view = await self.open(community_id)
		except (EngineError, ConnectionError) as exc:
			logger.warning("Настройки сообщества id=%s не перечитаны: %s", community_id, exc)
			return SettingsSaved(results, reread_error=str(exc))
		if view.settings is not None:
			await self._store_mutable(community_id, view.settings)
		return SettingsSaved(results, view)

	async def _store_mutable(self, community_id: int, settings: CommunitySettings) -> None:
		"""Название, @имя и признак форума — в запись сообщества сразу после правки."""
		title = settings.value("title")
		username = settings.value("username")
		forum = settings.value("forum")
		await self._communities.update_mutable(
			community_id,
			title=title if isinstance(title, str) and title else None,
			username=username if isinstance(username, str) else None,
			forum=forum if isinstance(forum, bool) else None,
		)

	# --- обсуждение -----------------------------------------------------------------

	async def discussion_candidates(self, community_id: int) -> list[LinkedChat]:
		"""Группы, которые сервер разрешает сделать обсуждением канала.

		Список отдаёт только userbot (у Bot API такого метода нет).

		Raises:
			CommunitySettingsError: Сообщество не найдено или правит бот.
			UserbotUnavailableError: Telegram не ответил.
		"""
		_community, executor = await self._load(community_id)
		if executor is None or executor.owner.kind is not OwnerKind.USER:
			raise CommunitySettingsError("Группу обсуждения выбирает только userbot-публикатор.")
		return await self._gateway.userbot_discussion_candidates(executor.owner.id)
