"""Сервис аккаунтов: боты, userbot-аккаунты MTProto, ключи ИИ.

Интерфейсу возвращаются лёгкие DTO (простые структуры данных), а не
ORM-объекты — интерфейс не зависит от слоя БД. Секреты в DTO попадают
только в замаскированном виде.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import AiCredential, Bot, TgAccount
from pxcontrol.engine.security.secrets import SecretDecryptionError
from pxcontrol.engine.telegram.mtproto import LoginError, UserbotUnavailableError

logger = logging.getLogger(__name__)


class AccountsError(Exception):
	"""Ошибка операций с аккаунтами (с понятным человеку текстом)."""


class _LoginFlow(Protocol):
	"""Пошаговый вход userbot (для подмены в тестах)."""

	async def start(
		self, account_id: int, api_id: int, api_hash: str, phone: str
	) -> None: ...

	async def confirm_code(self, account_id: int, code: str) -> str | None: ...

	async def confirm_password(self, account_id: int, password: str) -> str: ...

	async def cancel(self, account_id: int) -> None: ...


class _TelegramPort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	@property
	def login(self) -> _LoginFlow: ...

	async def check_bot_token(self, token: str) -> str: ...

	async def bot_events(self, token: str) -> list[str]: ...

	async def activate_userbot(
		self, api_id: int, api_hash: str, session: str
	) -> None: ...

	async def deactivate_userbot(self) -> None: ...


def mask_secret(secret: str) -> str:
	"""Возвращает замаскированное представление секрета для показа в UI.

	Короткие секреты (до 15 символов) маскируются целиком: показывать
	8 символов из 9 — почти раскрыть секрет. Реальные токены ботов
	и ключи ИИ длиннее, для них видны только края.
	"""
	if len(secret) < 16:
		return "•" * len(secret)
	return f"{secret[:4]}…{secret[-4:]}"


@dataclass(frozen=True)
class BotDto:
	"""Бот для показа в интерфейсе."""

	id: int
	label: str
	username: str | None
	token_masked: str


@dataclass(frozen=True)
class TgAccountDto:
	"""Userbot-аккаунт для показа в интерфейсе."""

	id: int
	label: str
	phone: str | None
	api_id: int
	logged_in: bool


@dataclass(frozen=True)
class AiKeyDto:
	"""Ключ провайдера ИИ для показа в интерфейсе."""

	id: int
	provider: str
	label: str
	key_masked: str


class AccountsService:
	"""Управление ботами, userbot-аккаунтами и ключами ИИ."""

	def __init__(self, db: Database, gateway: _TelegramPort) -> None:
		self._db = db
		self._gateway = gateway

	# --- боты -------------------------------------------------------------

	async def list_bots(self) -> list[BotDto]:
		"""Возвращает всех ботов."""
		async with self._db.session_factory() as session:
			bots = (await session.execute(select(Bot).order_by(Bot.id))).scalars()
			return [self._bot_dto(b) for b in bots]

	async def add_bot(self, label: str, token: str) -> BotDto:
		"""Проверяет токен через Telegram (getMe) и сохраняет бота.

		Raises:
			InvalidBotTokenError: Токен отклонён — в БД ничего не пишется.
			ConnectionError: Нет связи с Telegram.
		"""
		username = await self._gateway.check_bot_token(token)
		async with self._db.session_factory() as session:
			bot = Bot(label=label, token=token, username=username)
			session.add(bot)
			await session.commit()
			await session.refresh(bot)
		logger.info("Добавлен бот @%s (%s).", username, label)
		return self._bot_dto(bot)

	async def delete_bot(self, bot_id: int) -> None:
		"""Удаляет бота; каналы, публиковавшие через него, остаются без бота.

		``channels.bot_id`` обнуляет политика внешнего ключа (SET NULL) —
		канал не «прилипнет» к чужому боту, если SQLite переиспользует id.
		"""
		async with self._db.session_factory() as session:
			await session.execute(delete(Bot).where(Bot.id == bot_id))
			await session.commit()

	async def bot_whereabouts(self, bot_id: int) -> list[str]:
		"""Диагностика «где состоит бот»: события Telegram за 24 часа.

		Строки пишутся в лог и возвращаются для показа в интерфейсе.

		Raises:
			AccountsError: Бот не найден.
		"""
		bot = await self._require_bot(bot_id)
		lines = await self._gateway.bot_events(bot.token)
		logger.info("Диагностика бота @%s: событий за 24 ч — %d.", bot.username, len(lines))
		for line in lines:
			logger.info("  %s", line)
		return lines

	async def _require_bot(self, bot_id: int) -> Bot:
		"""Возвращает бота или объясняет, что он не найден.

		Raises:
			AccountsError: Бот не найден.
		"""
		async with self._db.session_factory() as session:
			bot = await session.get(Bot, bot_id)
		if bot is None:
			raise AccountsError("Бот не найден — обновите список.")
		return bot

	@staticmethod
	def _bot_dto(bot: Bot) -> BotDto:
		return BotDto(bot.id, bot.label, bot.username, mask_secret(bot.token))

	# --- userbot (MTProto) --------------------------------------------------

	async def list_tg_accounts(self) -> list[TgAccountDto]:
		"""Возвращает все userbot-аккаунты."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(select(TgAccount).order_by(TgAccount.id))).scalars()
			return [self._acc_dto(a) for a in rows]

	async def add_tg_account(
		self, label: str, phone: str | None, api_id: int, api_hash: str
	) -> TgAccountDto:
		"""Сохраняет реквизиты userbot-аккаунта (вход — отдельным шагом)."""
		async with self._db.session_factory() as session:
			acc = TgAccount(label=label, phone=phone, api_id=api_id, api_hash=api_hash)
			session.add(acc)
			await session.commit()
			await session.refresh(acc)
		logger.info("Добавлен userbot-аккаунт «%s».", label)
		return self._acc_dto(acc)

	async def delete_tg_account(self, account_id: int) -> None:
		"""Удаляет userbot-аккаунт и переподключает userbot без него.

		Движок не должен продолжать публиковать от имени удалённого
		аккаунта: если у аккаунта была сессия, userbot отключается
		и активируется заново по оставшимся сессиям (или остаётся
		выключенным, если сессий больше нет).
		"""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
			if account is None:
				return
			had_session = account.session is not None
			await session.delete(account)
			await session.commit()
		if had_session:
			await self._gateway.deactivate_userbot()
			await self.activate_stored_userbot()

	async def activate_stored_userbot(self) -> None:
		"""Подключает userbot по сохранённой сессии, если она есть.

		Правило выбора: первый по id аккаунт с сессией (обычно userbot
		один). Неудача подключения (нет сети, сессия отозвана) не ошибка:
		приложение работает дальше, userbot подключится после повторного
		входа. Вызывается движком при старте и после удаления аккаунта.
		"""
		try:
			async with self._db.session_factory() as session:
				account = (
					(await session.execute(
						select(TgAccount)
						.where(TgAccount.session.is_not(None))
						.order_by(TgAccount.id)
					)).scalars().first()
				)
		except SecretDecryptionError as exc:
			# сменился ключ шифрования — не мешаем запуску приложения:
			# пользователь увидит ту же ошибку на странице аккаунтов
			logger.warning("Userbot не активирован: %s", exc)
			return
		if account is None or account.session is None:
			return
		try:
			await self._gateway.activate_userbot(
				account.api_id, account.api_hash, account.session
			)
		except UserbotUnavailableError as exc:
			logger.warning(
				"Userbot «%s» не подключён: %s", account.label, exc
			)
			return
		logger.info("Userbot «%s» подключён.", account.label)

	@staticmethod
	def _acc_dto(acc: TgAccount) -> TgAccountDto:
		return TgAccountDto(
			acc.id, acc.label, acc.phone, acc.api_id, logged_in=acc.session is not None
		)

	# --- вход userbot ---------------------------------------------------------

	async def start_login(self, account_id: int) -> None:
		"""Просит Telegram отправить код входа на телефон аккаунта.

		Raises:
			LoginError: Нет телефона у аккаунта или Telegram отклонил запрос.
		"""
		account = await self._get_account(account_id)
		if not account.phone:
			raise LoginError("У аккаунта не указан номер телефона.")
		await self._gateway.login.start(
			account.id, account.api_id, account.api_hash, account.phone
		)

	async def confirm_login_code(self, account_id: int, code: str) -> bool:
		"""Подтверждает код. ``True`` — вход завершён; ``False`` — нужен 2FA.

		Raises:
			LoginError: Код неверный/устарел.
		"""
		session_string = await self._gateway.login.confirm_code(account_id, code)
		if session_string is None:
			return False
		await self._save_session(account_id, session_string)
		return True

	async def confirm_login_password(self, account_id: int, password: str) -> None:
		"""Подтверждает пароль 2FA и завершает вход.

		Raises:
			LoginError: Пароль неверный.
		"""
		session_string = await self._gateway.login.confirm_password(
			account_id, password
		)
		await self._save_session(account_id, session_string)

	async def cancel_login(self, account_id: int) -> None:
		"""Прерывает незавершённый вход (пользователь закрыл диалог)."""
		await self._gateway.login.cancel(account_id)

	async def _get_account(self, account_id: int) -> TgAccount:
		"""Возвращает аккаунт или объясняет, что он не найден."""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
		if account is None:
			raise LoginError("Аккаунт не найден.")
		return account

	async def _save_session(self, account_id: int, session_string: str) -> None:
		"""Сохраняет строку сессии (шифруется прозрачно, ADR-0009)
		и сразу подключает userbot — без перезапуска приложения."""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
			if account is None:
				raise LoginError("Аккаунт не найден.")
			account.session = session_string
			await session.commit()
			api_id, api_hash = account.api_id, account.api_hash
		logger.info("Userbot id=%s: сессия сохранена.", account_id)
		try:
			await self._gateway.activate_userbot(api_id, api_hash, session_string)
		except Exception:  # noqa: BLE001 — вход удался, подключение не критично
			logger.exception("Не удалось подключить userbot сразу после входа.")

	# --- ключи ИИ -----------------------------------------------------------

	async def list_ai_keys(self) -> list[AiKeyDto]:
		"""Возвращает все ключи ИИ."""
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(select(AiCredential).order_by(AiCredential.id))
			).scalars()
			return [self._key_dto(k) for k in rows]

	async def add_ai_key(self, label: str, api_key: str) -> AiKeyDto:
		"""Сохраняет ключ провайдера ИИ (провайдер пока один — Anthropic)."""
		async with self._db.session_factory() as session:
			cred = AiCredential(label=label, api_key=api_key)
			session.add(cred)
			await session.commit()
			await session.refresh(cred)
		logger.info("Добавлен ключ ИИ «%s».", label)
		return self._key_dto(cred)

	async def delete_ai_key(self, key_id: int) -> None:
		"""Удаляет ключ ИИ по идентификатору."""
		async with self._db.session_factory() as session:
			await session.execute(delete(AiCredential).where(AiCredential.id == key_id))
			await session.commit()

	@staticmethod
	def _key_dto(cred: AiCredential) -> AiKeyDto:
		return AiKeyDto(cred.id, cred.provider, cred.label, mask_secret(cred.api_key))
