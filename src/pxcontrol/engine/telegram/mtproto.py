"""Транспорт MTProto (через Telethon, отдельный аккаунт).

Роли: создание отложенных постов прямо в канале (серверное планирование
Telegram, ADR-0010), чтение отложенных, в будущем — чтение каналов-источников.
Здесь же — пошаговый вход userbot (код → 2FA → строка сессии).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class LoginError(Exception):
	"""Ошибка входа userbot с понятным человеку текстом."""


class UserbotUnavailable(Exception):
	"""Userbot не подключён или не может выполнить операцию."""


def _default_client(api_id: int, api_hash: str, session: str | None = None) -> Any:
	"""Создаёт клиента Telethon (пустая сессия — для входа)."""
	from telethon import TelegramClient
	from telethon.sessions import StringSession

	return TelegramClient(StringSession(session), api_id, api_hash)


def _map_login_error(exc: Exception) -> str:
	"""Переводит исключения Telethon в понятные сообщения."""
	from telethon import errors

	if isinstance(exc, errors.FloodWaitError):
		return f"Telegram просит подождать {exc.seconds} с перед новой попыткой."
	if isinstance(exc, errors.PhoneCodeInvalidError):
		return "Неверный код — начните вход заново."
	if isinstance(exc, errors.PhoneCodeExpiredError):
		return "Код устарел — начните вход заново."
	if isinstance(exc, errors.PhoneNumberInvalidError):
		return "Telegram не принял номер телефона."
	if isinstance(exc, errors.PasswordHashInvalidError):
		return "Неверный пароль двухфакторной защиты."
	return f"Не удалось войти: {exc}"


def _map_post_error(exc: Exception) -> str:
	"""Переводит ошибки операций с постами в понятные сообщения."""
	from telethon import errors

	if isinstance(exc, errors.ChatAdminRequiredError):
		return (
			"Userbot не администратор канала — добавьте аккаунт userbot "
			"администратором с правом публиковать."
		)
	if isinstance(exc, errors.FloodWaitError):
		return f"Telegram просит подождать {exc.seconds} с."
	if isinstance(exc, ValueError):
		return (
			"Userbot не видит этот канал — убедитесь, что аккаунт добавлен "
			"в канал администратором."
		)
	return f"Telegram отклонил операцию: {exc}"


async def _safe_disconnect(client: Any) -> None:
	"""Закрывает клиента; ошибки закрытия не роняют процесс."""
	try:
		await client.disconnect()
	except Exception:  # noqa: BLE001 — закрытие не должно ронять операцию
		logger.debug("Не удалось корректно закрыть клиента.", exc_info=True)


class MtprotoTransport:
	"""Подключённый userbot: отложенные посты и чтение каналов."""

	def __init__(
		self,
		client_factory: Callable[[int, str, str | None], Any] | None = None,
	) -> None:
		self._client_factory = client_factory or _default_client
		self._creds: tuple[int, str, str] | None = None
		self._client: Any | None = None

	def configure(self, api_id: int, api_hash: str, session: str) -> None:
		"""Задаёт реквизиты подключения (из БД, ADR-0009)."""
		self._creds = (api_id, api_hash, session)

	async def start(self) -> None:
		"""Подключает клиента, если заданы реквизиты и ещё не подключён."""
		if self._client is not None:
			return
		if self._creds is None:
			logger.info("Аккаунт MTProto не настроен — userbot отключён.")
			return
		api_id, api_hash, session = self._creds
		self._client = self._client_factory(api_id, api_hash, session)
		await self._client.connect()
		logger.info("MTProto клиент подключён.")

	async def stop(self) -> None:
		"""Отключает клиента MTProto."""
		if self._client is not None:
			await _safe_disconnect(self._client)
			self._client = None

	def _require_client(self) -> Any:
		"""Возвращает подключённого клиента или объясняет, чего не хватает."""
		if self._client is None:
			raise UserbotUnavailable(
				"Userbot не подключён — войдите в аккаунт: Настройки → Аккаунты."
			)
		return self._client

	async def schedule_post(self, chat_id: str, text: str, when: datetime) -> None:
		"""Создаёт отложенную запись прямо в канале (schedule_date).

		Дальше пост хранит и публикует сервер Telegram — приложение
		может быть выключено (ADR-0010).
		"""
		client = self._require_client()
		try:
			await client.send_message(int(chat_id), text, schedule=when)
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			raise UserbotUnavailable(_map_post_error(exc)) from exc
		logger.info("Создан отложенный пост в чате %s на %s.", chat_id, when)

	async def get_scheduled(self, chat_id: str) -> list[Any]:
		"""Читает отложенные записи канала (источник истины — Telegram)."""
		from telethon.tl.functions.messages import GetScheduledHistoryRequest

		client = self._require_client()
		try:
			entity = await client.get_input_entity(int(chat_id))
			result = await client(GetScheduledHistoryRequest(peer=entity, hash=0))
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			raise UserbotUnavailable(_map_post_error(exc)) from exc
		return list(result.messages)


class MtprotoLoginManager:
	"""Пошаговый вход userbot. Держит незавершённые входы по id аккаунта."""

	def __init__(
		self, client_factory: Callable[[int, str], Any] | None = None
	) -> None:
		self._client_factory = client_factory or (
			lambda api_id, api_hash: _default_client(api_id, api_hash, None)
		)
		self._pending: dict[int, tuple[Any, str, str]] = {}

	async def start(
		self, account_id: int, api_id: int, api_hash: str, phone: str
	) -> None:
		"""Подключается и просит Telegram отправить код на телефон.

		Raises:
			LoginError: Telegram отклонил запрос (номер, лимиты и т.п.).
		"""
		await self.cancel(account_id)  # висел незавершённый вход — закрываем
		client = self._client_factory(api_id, api_hash)
		try:
			await client.connect()
			sent = await client.send_code_request(phone)
		except Exception as exc:
			await _safe_disconnect(client)
			raise LoginError(_map_login_error(exc)) from exc
		self._pending[account_id] = (client, sent.phone_code_hash, phone)
		logger.info("Userbot id=%s: код отправлен.", account_id)

	async def confirm_code(self, account_id: int, code: str) -> str | None:
		"""Подтверждает код из Telegram.

		Returns:
			Строку сессии, либо ``None``, если дальше нужен пароль 2FA.

		Raises:
			LoginError: Код неверный/устарел или вход не был начат.
		"""
		from telethon.errors import SessionPasswordNeededError

		client, code_hash, phone = self._require(account_id)
		try:
			await client.sign_in(phone, code, phone_code_hash=code_hash)
		except SessionPasswordNeededError:
			return None  # клиент остаётся жить до ввода пароля
		except Exception as exc:
			await self.cancel(account_id)
			raise LoginError(_map_login_error(exc)) from exc
		return await self._finish(account_id, client)

	async def confirm_password(self, account_id: int, password: str) -> str:
		"""Подтверждает пароль двухфакторной защиты и завершает вход.

		Raises:
			LoginError: Пароль неверный или вход не был начат.
		"""
		client, _hash, _phone = self._require(account_id)
		try:
			await client.sign_in(password=password)
		except Exception as exc:
			raise LoginError(_map_login_error(exc)) from exc
		return await self._finish(account_id, client)

	async def cancel(self, account_id: int) -> None:
		"""Прерывает незавершённый вход и закрывает его клиента."""
		entry = self._pending.pop(account_id, None)
		if entry is not None:
			await _safe_disconnect(entry[0])

	def _require(self, account_id: int) -> tuple[Any, str, str]:
		"""Возвращает состояние входа или объясняет, что вход не начат."""
		entry = self._pending.get(account_id)
		if entry is None:
			raise LoginError("Вход не начат — нажмите «Войти» ещё раз.")
		return entry

	async def _finish(self, account_id: int, client: Any) -> str:
		"""Забирает строку сессии и закрывает клиента входа."""
		session_string = str(client.session.save())
		await _safe_disconnect(client)
		self._pending.pop(account_id, None)
		logger.info("Userbot id=%s: вход завершён.", account_id)
		return session_string
