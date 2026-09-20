"""Лестница ввода исполнителя в сообщество (ADR-0035, п. 9) — над шлюзом, без базы.

Ввод отвечает на один вопрос: как сделать так, чтобы исполнитель оказался
в сообществе, — и делает это ступенями, от простого к сложному, где
каждое усложнение названо. Модуль знает шлюз Telegram и строки сообщества,
которые ему передали, но **не пишет в базу**: итог ввода — исход, снимок
прав и свежие данные сообщества, а строку пула по ним заводит сервис.
Так лестница проверяется подставным шлюзом целиком, а сервис сообществ
не разрастается операциями, которым база не нужна.

Вступление и приглашение здесь — только по явному действию человека:
сервис зовёт лестницу из одной операции, и никакой фоновый путь к ней
не ведёт (ADR-0035, п. 11).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from pxcontrol.engine.db.models import Bot, Community, TgAccount
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.abilities import BOT_ADMIN_RIGHTS, ExecutorAction, can
from pxcontrol.engine.services.community_rights import executor_paused, executor_rights
from pxcontrol.engine.telegram.bot_api import BotError
from pxcontrol.engine.telegram.mtproto import UserbotAccessError, UserbotUnavailableError
from pxcontrol.engine.telegram.rights import AdminRights, ExecutorRights, ParticipantStatus
from pxcontrol.engine.telegram.types import BotRef, CommunityInfo, CommunityKind

logger = logging.getLogger(__name__)

#: Крючок актуализации профиля аккаунта после живого ответа Telegram.
ProfileSync = Callable[[int], Awaitable[None]]


class JoinError(EngineError):
	"""Ввести исполнителя нечем — и сказано, что сделать руками."""


class _JoinGateway(Protocol):
	"""Часть шлюза Telegram, нужная лестнице (для подмены в тестах)."""

	async def bot_check_community(self, bot: BotRef, chat_ref: str) -> CommunityInfo: ...

	async def userbot_check_community(self, account_id: int, chat_ref: str) -> CommunityInfo: ...

	async def userbot_join_public(self, account_id: int, username: str) -> None: ...

	async def userbot_join_by_invite(self, account_id: int, link: str) -> bool: ...

	async def userbot_invite_link(self, account_id: int, chat_id: str) -> str | None: ...

	async def userbot_invite_participant(
		self, account_id: int, chat_id: str, target: str
	) -> None: ...

	async def userbot_promote(
		self, account_id: int, chat_id: str, target: str, rights: AdminRights
	) -> None: ...


class JoinOutcome(StrEnum):
	"""Чем кончился ввод исполнителя в сообщество (ADR-0035).

	Исходов несколько не ради дробности: человеку важно разное — «он
	уже был там» ничего не изменило в Telegram, «вступил» и «приглашён»
	изменили, а «заявка отправлена» требует чьего-то одобрения, и без
	него исполнитель не заработает.
	"""

	ALREADY_IN = "already_in"  # состоял и раньше — в Telegram ничего не делали
	JOINED = "joined"  # вступил сам: по @имени или по ссылке
	REQUESTED = "requested"  # заявка на вступление отправлена и ждёт одобрения
	INVITED = "invited"  # приглашён нашим исполнителем
	PROMOTED = "promoted"  # принят в канал назначением администратором
	# ввести нечем: сообщество приватное, готовой ссылки Telegram не отдал,
	# пригласить некому. Это **исход**, а не ошибка: в Telegram ничего
	# не изменилось, и операция продолжится, когда человек даст ссылку.
	# Исключением такой случай быть не может — мост отдаёт интерфейсу
	# текст ошибки, а не её тип, и распознавание свелось бы к разбору строки
	NEEDS_LINK = "needs_link"


class ExecutorJoiner:
	"""Проводит исполнителя по ступеням ввода и возвращает, чем всё кончилось."""

	def __init__(self, gateway: _JoinGateway, profile_sync: ProfileSync | None = None) -> None:
		self._gateway = gateway
		self._profile_sync = profile_sync

	async def _sync_profile(self, account_id: int) -> None:
		"""Актуализирует профиль аккаунта после живого ответа Telegram (крючок)."""
		if self._profile_sync is not None:
			await self._profile_sync(account_id)

	async def bring_user(
		self, community: Community, account: TgAccount, invite: str | None
	) -> tuple[JoinOutcome, ExecutorRights, CommunityInfo | None]:
		"""Вводит пользователя: лестница «состоит → @имя → ссылка → приглашение»."""
		account_id = account.id
		info = await self._seen_by(account_id, community.tg_chat_id)
		if info is not None and info.rights.status.in_community:
			return JoinOutcome.ALREADY_IN, info.rights, info
		outcome = await self._let_user_in(community, account_id, account, invite)
		if outcome is JoinOutcome.NEEDS_LINK:
			return outcome, ExecutorRights(ParticipantStatus.LEFT), None
		if outcome is JoinOutcome.REQUESTED:
			# заявку ещё не одобрили: прав нет и спрашивать их не у кого
			return outcome, ExecutorRights(ParticipantStatus.REQUESTED), None
		fresh = await self._gateway.userbot_check_community(account_id, community.tg_chat_id)
		await self._sync_profile(account_id)
		return outcome, fresh.rights, fresh

	async def _let_user_in(
		self, community: Community, account_id: int, account: TgAccount, invite: str | None
	) -> JoinOutcome:
		"""Заводит пользователя в сообщество — ступенями (ADR-0035, п. 9).

		Порядок ступеней — от независимой к самой отказоопасной: вступить
		по @имени можно без чьей-либо помощи; ссылка нужна приватному;
		приглашение упирается в чужие настройки приватности. Последним
		идёт не приглашение, а вопрос человеку: это единственная ступень,
		которая останавливает операцию, и уводить в неё, пока остаются
		автоматические пути, значило бы звать человека зря.
		"""
		if community.username and invite is None:
			await self._gateway.userbot_join_public(account_id, f"@{community.username}")
			return JoinOutcome.JOINED
		link = invite or await self._known_invite_link(community)
		if link is not None:
			joined = await self._gateway.userbot_join_by_invite(account_id, link)
			return JoinOutcome.JOINED if joined else JoinOutcome.REQUESTED
		if account.username and await self._invite_by_pool(community, f"@{account.username}"):
			return JoinOutcome.INVITED
		return JoinOutcome.NEEDS_LINK

	async def bring_bot(
		self, community: Community, bot: Bot
	) -> tuple[JoinOutcome, ExecutorRights, CommunityInfo | None]:
		"""Вводит бота: сам он вступить не может — его вводит наш администратор.

		Raises:
			JoinError: У бота нет @имени или в пуле некому его ввести.
		"""
		ref = BotRef(bot.id, bot.token)
		info = await self._seen_by_bot(ref, community.tg_chat_id)
		if info is not None and info.rights.status.in_community:
			return JoinOutcome.ALREADY_IN, info.rights, info
		if not bot.username:
			raise JoinError(
				f"У бота «{bot.label}» не известно @имя — без него Telegram не найдёт, "
				"кого добавлять. Проверьте бота в разделе «Пользователи и боты»."
			)
		outcome = await self._let_bot_in(community, f"@{bot.username}")
		fresh = await self._gateway.bot_check_community(ref, community.tg_chat_id)
		return outcome, fresh.rights, fresh

	async def _let_bot_in(self, community: Community, target: str) -> JoinOutcome:
		"""Вводит бота руками исполнителя пула.

		В канале бот бывает только администратором — значит ввод это
		назначение, и нужен исполнитель с правом назначать; в группу
		бота приглашают, как обычного участника.

		Raises:
			JoinError: В пуле нет исполнителя с нужным правом.
		"""
		if CommunityKind(community.kind) is CommunityKind.CHANNEL:
			account_id = self._executor_for(community, ExecutorAction.PROMOTE)
			if account_id is None:
				raise JoinError(
					f"Некому принять бота в канал «{community.title}»: нужен исполнитель "
					"с правом назначать администраторов. Добавьте бота администратором "
					"вручную в Telegram."
				)
			await self._gateway.userbot_promote(
				account_id, community.tg_chat_id, target, BOT_ADMIN_RIGHTS
			)
			return JoinOutcome.PROMOTED
		account_id = self._executor_for(community, ExecutorAction.INVITE)
		if account_id is None:
			raise JoinError(
				f"Некому пригласить бота в «{community.title}»: нужен исполнитель "
				"с правом приглашать. Добавьте бота в группу вручную в Telegram."
			)
		await self._gateway.userbot_invite_participant(account_id, community.tg_chat_id, target)
		return JoinOutcome.INVITED

	async def _seen_by(self, account_id: int, chat_id: str) -> CommunityInfo | None:
		"""Что видит аккаунт в сообществе (None — не видит вовсе).

		От :meth:`_probe_userbot` отличается тем, что не глушит сбои:
		ввод исполнителя — действие человека, и «нет связи» он должен
		увидеть ошибкой, а не молчаливым переходом к вступлению.

		Удавшийся зонд — подтверждённая связь с аккаунтом, поэтому здесь
		же актуализируется его профиль: ответ «не состоит» для этого
		годится не хуже ответа «состоит».
		"""
		try:
			info = await self._gateway.userbot_check_community(account_id, chat_id)
		except UserbotAccessError:
			return None
		await self._sync_profile(account_id)
		return info

	async def _seen_by_bot(self, bot: BotRef, chat_id: str) -> CommunityInfo | None:
		"""Что видит бот в сообществе (None — Telegram его туда не пускает)."""
		try:
			return await self._gateway.bot_check_community(bot, chat_id)
		except BotError:
			return None

	async def _known_invite_link(self, community: Community) -> str | None:
		"""Основная ссылка-приглашение сообщества, если её кто-то из пула видит.

		Ссылка у приватного сообщества уже есть, и Telegram отдаёт её
		администратору с правом приглашать. Приложение её **читает**,
		а не создаёт: создание — изменение состояния сообщества, за
		которым тянется вопрос «кто её завёл и кто по ней пришёл».
		"""
		account_id = self._executor_for(community, ExecutorAction.INVITE)
		if account_id is None:
			return None
		try:
			return await self._gateway.userbot_invite_link(account_id, community.tg_chat_id)
		except UserbotUnavailableError as exc:
			logger.info("Ссылку-приглашение «%s» прочитать не удалось: %s", community.title, exc)
			return None

	async def _invite_by_pool(self, community: Community, target: str) -> bool:
		"""Приглашает исполнителя силами пула; False — не вышло.

		Самая отказоопасная ступень: она упирается не в наши права,
		а в чужие настройки приватности, поэтому отказ здесь не ошибка
		операции, а повод попросить у человека ссылку.
		"""
		account_id = self._executor_for(community, ExecutorAction.INVITE)
		if account_id is None:
			return False
		try:
			await self._gateway.userbot_invite_participant(account_id, community.tg_chat_id, target)
		except UserbotUnavailableError as exc:
			logger.info("Пригласить %s в «%s» не вышло: %s", target, community.title, exc)
			return False
		return True

	@staticmethod
	def _executor_for(community: Community, action: ExecutorAction) -> int | None:
		"""Аккаунт из пула, способный на названное действие (None — такого нет).

		Ввод исполнителя делают руками своих же администраторов, и выбрать
		их можно только по правам. Приостановленные (ADR-0029)
		не рассматриваются — приложение их не использует ни для чего.
		"""
		kind = CommunityKind(community.kind)
		for row in community.executors:
			if row.tg_account_id is None or executor_paused(row):
				continue
			if can(executor_rights(row), action, kind):
				return row.tg_account_id
		return None
