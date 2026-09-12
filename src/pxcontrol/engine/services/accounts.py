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
from pxcontrol.engine.db.models import AiCredential, Bot, TgAccount, TgApiCredential
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.security.secrets import SecretDecryptionError
from pxcontrol.engine.telegram.mtproto import LoginError, UserbotUnavailableError
from pxcontrol.engine.telegram.types import UserbotProfile

logger = logging.getLogger(__name__)


def _account_not_found() -> AccountsError:
	"""Единый отказ «userbot-аккаунта нет».

	Один текст и один класс на все пути: проверка написана в трёх
	местах (чтение, смена пометки, сохранение сессии), и прежде общий
	помощник бросал ошибку входа — хотя «аккаунта нет» ко входу
	отношения не имеет, и человек получал разный текст в зависимости
	от того, каким путём попал в отказ.
	"""
	return AccountsError("Аккаунт не найден — обновите список.")


class AccountsError(EngineError):
	"""Ошибка операций с аккаунтами (с понятным человеку текстом)."""


class _LoginFlow(Protocol):
	"""Пошаговый вход userbot (для подмены в тестах)."""

	async def start(self, account_id: int, api_id: int, api_hash: str, phone: str) -> None: ...

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
		self, account_id: int, api_id: int, api_hash: str, session: str
	) -> None: ...

	async def deactivate_userbot(self, account_id: int) -> None: ...

	def userbot_premium(self, account_id: int | None) -> bool: ...

	async def userbot_me(self, account_id: int) -> UserbotProfile: ...


def account_display(
	label: str | None,
	username: str | None,
	first_name: str | None,
	last_name: str | None,
	phone: str | None,
) -> str:
	"""Отображаемое имя userbot-аккаунта — единая точка истины.

	Первое непустое: ручная пометка → «Имя Фамилия» из Telegram →
	@имя → телефон. Телефон — последний рубеж: он обязателен при
	создании, безымянным аккаунт не остаётся.
	"""
	full_name = " ".join(part for part in (first_name, last_name) if part)
	at_name = f"@{username}" if username else None
	return label or full_name or at_name or phone or "аккаунт"


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
class TgApiDto:
	"""Ключ API Telegram приложения для показа в интерфейсе (ADR-0018).

	``api_hash_masked`` — секрет в замаскированном виде (как токены ботов).
	"""

	api_id: int
	api_hash_masked: str


@dataclass(frozen=True)
class TgAccountDto:
	"""Userbot-аккаунт для показа в интерфейсе.

	``label`` — необязательная ручная пометка; ``username`` и имя —
	профиль из Telegram (актуализируется автоматически). ``display`` —
	готовое отображаемое имя (:func:`account_display`): интерфейс
	не собирает его сам. ``premium`` — статус подписки подключённого
	аккаунта (True только у активного: от него зависит лимит файла
	2/4 ГБ).
	"""

	id: int
	label: str | None
	phone: str | None
	logged_in: bool
	premium: bool = False
	username: str | None = None
	first_name: str | None = None
	last_name: str | None = None
	display: str = ""


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

		``communities.bot_id`` обнуляет политика внешнего ключа (SET NULL) —
		канал не «прилипнет» к чужому боту, если SQLite переиспользует id.
		"""
		async with self._db.session_factory() as session:
			await session.execute(delete(Bot).where(Bot.id == bot_id))
			await session.commit()
		logger.info("Удалён бот id=%s.", bot_id)

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

	# --- ключ API Telegram (один на приложение, ADR-0018) ---------------------

	async def get_tg_api(self) -> TgApiDto | None:
		"""Возвращает ключ API приложения (None — ещё не задан)."""
		credential = await self._read_tg_api()
		if credential is None:
			return None
		return TgApiDto(credential.api_id, mask_secret(credential.api_hash))

	async def set_tg_api(self, api_id: int, api_hash: str) -> TgApiDto:
		"""Сохраняет ключ API приложения (запись одна: создаёт или заменяет).

		Raises:
			AccountsError: api_id не положительный или api_hash пуст.
		"""
		api_hash = api_hash.strip()
		if api_id <= 0:
			raise AccountsError("api_id — положительное число с my.telegram.org.")
		if not api_hash:
			raise AccountsError("Укажите api_hash с my.telegram.org.")
		async with self._db.session_factory() as session:
			credential = (
				(await session.execute(select(TgApiCredential).order_by(TgApiCredential.id)))
				.scalars()
				.first()
			)
			if credential is None:
				credential = TgApiCredential(api_id=api_id, api_hash=api_hash)
				session.add(credential)
			else:
				credential.api_id = api_id
				credential.api_hash = api_hash
			await session.commit()
		logger.info("Ключ API Telegram сохранён (api_id=%s).", api_id)
		return TgApiDto(api_id, mask_secret(api_hash))

	async def _read_tg_api(self) -> TgApiCredential | None:
		"""Читает запись ключа API (или None — не задан)."""
		async with self._db.session_factory() as session:
			return (
				(await session.execute(select(TgApiCredential).order_by(TgApiCredential.id)))
				.scalars()
				.first()
			)

	async def _require_tg_api(self) -> TgApiCredential:
		"""Ключ API приложения — или понятная ошибка, где его задать.

		Raises:
			AccountsError: Ключ ещё не задан.
		"""
		credential = await self._read_tg_api()
		if credential is None:
			raise AccountsError(
				"Сначала укажите api_id и api_hash приложения (my.telegram.org): Настройки → Общие."
			)
		return credential

	# --- userbot (MTProto) --------------------------------------------------

	async def list_tg_accounts(self) -> list[TgAccountDto]:
		"""Возвращает все userbot-аккаунты.

		Статус Premium запрашивается у шлюза по id каждого аккаунта
		(пул клиентов, ADR-0019): True — только у фактически
		подключённого клиента, приписывать его всем вошедшим нельзя.
		"""
		async with self._db.session_factory() as session:
			rows = list((await session.execute(select(TgAccount).order_by(TgAccount.id))).scalars())
		return [self._acc_dto(a, premium=self._gateway.userbot_premium(a.id)) for a in rows]

	async def add_tg_account(self, label: str, phone: str) -> TgAccountDto:
		"""Сохраняет userbot-аккаунт: телефон и необязательную пометку.

		Ключ API у аккаунта не спрашивается — он один на приложение
		(ADR-0018) и задаётся в «Настройки → Общие». Телефон обязателен:
		аккаунт без телефона — тупик, в него нельзя войти. Пометка —
		для себя («рабочий», «запасной»); имя и @имя заполнит Telegram
		после входа (:meth:`sync_profile`).

		Raises:
			AccountsError: Пустой телефон.
		"""
		phone = phone.strip()
		if not phone:
			raise AccountsError("Укажите телефон аккаунта — на него придёт код входа.")
		async with self._db.session_factory() as session:
			acc = TgAccount(label=label.strip() or None, phone=phone)
			session.add(acc)
			await session.commit()
			await session.refresh(acc)
		dto = self._acc_dto(acc)
		logger.info("Добавлен userbot-аккаунт «%s».", dto.display)
		return dto

	async def set_account_label(self, account_id: int, label: str) -> TgAccountDto:
		"""Переназначает ручную пометку аккаунта (пустая строка — снимает).

		Пометка меняется в любой момент и не трогает профиль
		из Telegram — карточку тогда подписывают имя и @имя.

		Raises:
			AccountsError: Аккаунт не найден.
		"""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
			if account is None:
				raise _account_not_found()
			account.label = label.strip() or None
			await session.commit()
			await session.refresh(account)
		dto = self._acc_dto(account)
		logger.info("Аккаунт id=%s: пометка — %s.", account_id, dto.label or "снята")
		return dto

	async def sync_profile(self, account_id: int) -> None:
		"""Актуализирует профиль аккаунта из Telegram (живой запрос «кто я»).

		Вызывается там, где соединение аккаунта заведомо живое: после
		входа, при активации сессий на старте и из зондов прав (крючок
		сервиса сообществ). Сбой запроса — не ошибка вызывающей
		операции: профиль остаётся прежним, в лог — след. NULL в полях
		пишется только по явному ответу Telegram (имени/@имени нет).
		"""
		try:
			profile = await self._gateway.userbot_me(account_id)
		except Exception:  # noqa: BLE001 — актуализация вспомогательная
			logger.info("Профиль аккаунта id=%s не обновлён (нет связи или сессии).", account_id)
			return
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
			if account is None:
				return  # аккаунт удалили за время запроса
			fresh = (profile.username, profile.first_name, profile.last_name)
			if (account.username, account.first_name, account.last_name) == fresh:
				return
			account.username, account.first_name, account.last_name = fresh
			await session.commit()
		logger.info(
			"Аккаунт id=%s: профиль обновлён — %s (@%s).",
			account_id,
			" ".join(p for p in (profile.first_name, profile.last_name) if p) or "без имени",
			profile.username or "—",
		)

	async def delete_tg_account(self, account_id: int) -> None:
		"""Удаляет userbot-аккаунт и отключает его транспорт.

		Движок не должен продолжать публиковать от имени удалённого
		аккаунта: его клиент в пуле шлюза закрывается. Остальные аккаунты
		не трогаются — их подключения живут независимо (ADR-0019).
		Каналы, привязанные к удалённому аккаунту, отвязывает политика
		внешнего ключа (SET NULL) — интерфейс предупреждает об этом
		до удаления.
		"""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
			if account is None:
				return
			display = account_display(
				account.label,
				account.username,
				account.first_name,
				account.last_name,
				account.phone,
			)
			await session.delete(account)
			await session.commit()
		logger.info("Удалён userbot-аккаунт «%s» (id=%s).", display, account_id)
		await self._gateway.deactivate_userbot(account_id)

	async def activate_stored_userbots(self) -> None:
		"""Подключает все аккаунты с сохранёнными сессиями (при старте).

		Реквизиты подключения — общий ключ API приложения (ADR-0018),
		сессия у каждого аккаунта своя (ADR-0019). Неудача подключения
		одного аккаунта (нет сети, сессия отозвана) не ошибка и не мешает
		остальным: приложение работает дальше, транспорт чинится первой
		операцией или повторным входом.
		"""
		try:
			credential = await self._read_tg_api()
			async with self._db.session_factory() as session:
				accounts = (
					(
						await session.execute(
							select(TgAccount)
							.where(TgAccount.session.is_not(None))
							.order_by(TgAccount.id)
						)
					)
					.scalars()
					.all()
				)
		except SecretDecryptionError as exc:
			# сменился ключ шифрования — не мешаем запуску приложения:
			# пользователь увидит ту же ошибку на странице аккаунтов
			logger.warning("Userbot-аккаунты не активированы: %s", exc)
			return
		if credential is None:
			if accounts:
				logger.info("Ключ API Telegram не задан — userbot-аккаунты отключены.")
			return
		for account in accounts:
			if account.session is None:  # для mypy: выборка уже отфильтровала
				continue
			display = account_display(
				account.label,
				account.username,
				account.first_name,
				account.last_name,
				account.phone,
			)
			try:
				await self._gateway.activate_userbot(
					account.id, credential.api_id, credential.api_hash, account.session
				)
			except UserbotUnavailableError as exc:
				logger.warning("Userbot «%s» не подключён: %s", display, exc)
				continue
			logger.info("Userbot «%s» подключён.", display)
			# соединение только что установлено — момент актуализации
			await self.sync_profile(account.id)

	@staticmethod
	def _acc_dto(acc: TgAccount, premium: bool = False) -> TgAccountDto:
		return TgAccountDto(
			acc.id,
			acc.label,
			acc.phone,
			logged_in=acc.session is not None,
			premium=premium,
			username=acc.username,
			first_name=acc.first_name,
			last_name=acc.last_name,
			display=account_display(
				acc.label, acc.username, acc.first_name, acc.last_name, acc.phone
			),
		)

	# --- вход userbot ---------------------------------------------------------

	async def start_login(self, account_id: int) -> None:
		"""Просит Telegram отправить код входа на телефон аккаунта.

		Реквизиты подключения — общий ключ API приложения (ADR-0018).

		Raises:
			AccountsError: Ключ API приложения ещё не задан.
			LoginError: Нет телефона у аккаунта или Telegram отклонил запрос.
		"""
		account = await self._require_account(account_id)
		if not account.phone:
			raise LoginError("У аккаунта не указан номер телефона.")
		credential = await self._require_tg_api()
		await self._gateway.login.start(
			account.id, credential.api_id, credential.api_hash, account.phone
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
		session_string = await self._gateway.login.confirm_password(account_id, password)
		await self._save_session(account_id, session_string)

	async def cancel_login(self, account_id: int) -> None:
		"""Прерывает незавершённый вход (пользователь закрыл диалог)."""
		await self._gateway.login.cancel(account_id)

	async def _require_account(self, account_id: int) -> TgAccount:
		"""Возвращает userbot-аккаунт или объясняет, что он не найден.

		Парная форма :meth:`_require_bot`: один текст и один класс
		ошибки на все пути. Прежде проверка была написана трижды,
		причём общий помощник бросал ошибку входа — хотя «аккаунта
		нет» ко входу отношения не имеет, и человек получал разный
		текст в зависимости от того, каким путём попал в отказ.

		Raises:
			AccountsError: Аккаунт не найден.
		"""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
		if account is None:
			raise _account_not_found()
		return account

	async def _save_session(self, account_id: int, session_string: str) -> None:
		"""Сохраняет строку сессии (шифруется прозрачно, ADR-0009)
		и сразу подключает userbot — без перезапуска приложения."""
		async with self._db.session_factory() as session:
			account = await session.get(TgAccount, account_id)
			if account is None:
				raise _account_not_found()
			account.session = session_string
			await session.commit()
		logger.info("Userbot id=%s: сессия сохранена.", account_id)
		try:
			# ключ не мог исчезнуть между шагами входа, но страховка честная:
			# _require_tg_api даст понятный текст вместо падения активации
			credential = await self._require_tg_api()
			await self._gateway.activate_userbot(
				account_id, credential.api_id, credential.api_hash, session_string
			)
		except Exception:  # noqa: BLE001 — вход удался, подключение не критично
			logger.exception("Не удалось подключить userbot сразу после входа.")
			return
		# вход завершён, соединение живое — заполняем имя и @имя из Telegram
		await self.sync_profile(account_id)

	# --- ключи ИИ -----------------------------------------------------------

	async def list_ai_keys(self) -> list[AiKeyDto]:
		"""Возвращает все ключи ИИ."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(select(AiCredential).order_by(AiCredential.id))).scalars()
			return [self._key_dto(k) for k in rows]

	async def add_ai_key(self, label: str, api_key: str) -> AiKeyDto:
		"""Сохраняет ключ провайдера ИИ (провайдер пока один — Anthropic).

		Raises:
			AccountsError: Название или ключ пустые (валидация — правило
				движка, не интерфейса).
		"""
		label = label.strip()
		api_key = api_key.strip()
		if not label or not api_key:
			raise AccountsError("Укажите и название, и сам ключ ИИ.")
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
		logger.info("Удалён ключ ИИ id=%s.", key_id)

	@staticmethod
	def _key_dto(cred: AiCredential) -> AiKeyDto:
		return AiKeyDto(cred.id, cred.provider, cred.label, mask_secret(cred.api_key))
