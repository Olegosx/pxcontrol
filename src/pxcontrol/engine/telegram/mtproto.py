"""Транспорт MTProto (через Telethon, отдельный аккаунт).

Здесь же — пошаговый вход userbot: телефон → код → (пароль 2FA) →
строка сессии. Клиент входа живёт между шагами, потому что одноразовый
``phone_code_hash`` привязан к соединению.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class LoginError(Exception):
	"""Ошибка входа userbot с понятным человеку текстом."""


def _default_client(api_id: int, api_hash: str) -> Any:
	"""Создаёт клиента Telethon с пустой сессией (для входа)."""
	from telethon import TelegramClient
	from telethon.sessions import StringSession

	return TelegramClient(StringSession(), api_id, api_hash)


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


async def _safe_disconnect(client: Any) -> None:
	"""Закрывает клиента; ошибки закрытия не роняют процесс входа."""
	try:
		await client.disconnect()
	except Exception:  # noqa: BLE001 — закрытие не должно ронять вход
		logger.debug("Не удалось корректно закрыть клиента входа.", exc_info=True)


class MtprotoLoginManager:
	"""Пошаговый вход userbot. Держит незавершённые входы по id аккаунта."""

	def __init__(
		self, client_factory: Callable[[int, str], Any] | None = None
	) -> None:
		self._client_factory = client_factory or _default_client
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


class MtprotoTransport:
	"""Обёртка над userbot. В первую очередь — чтение каналов-источников.

	Реквизиты аккаунта хранятся в БД в зашифрованном виде (таблица
	``tg_accounts``, ADR-0009) и передаются при подключении аккаунта.
	"""

	def __init__(
		self,
		api_id: int | None = None,
		api_hash: str | None = None,
		session: str | None = None,
	) -> None:
		self._api_id = api_id
		self._api_hash = api_hash
		self._session = session
		self._client: Any | None = None

	async def start(self) -> None:
		"""Подключает клиента MTProto, если заданы все реквизиты."""
		if not (self._api_id and self._api_hash and self._session):
			logger.info("Аккаунт MTProto не настроен — userbot отключён.")
			return
		from telethon import TelegramClient
		from telethon.sessions import StringSession

		self._client = TelegramClient(
			StringSession(self._session), self._api_id, self._api_hash
		)
		await self._client.connect()
		logger.info("MTProto клиент подключён.")

	async def stop(self) -> None:
		"""Отключает клиента MTProto."""
		if self._client is not None:
			await self._client.disconnect()
			self._client = None

	async def read_channel(self, username: str, limit: int = 20) -> list[Any]:
		"""Читает последние сообщения канала-источника.

		Каркас: реальная вычитка добавляется при реализации источников.
		"""
		raise NotImplementedError("Чтение канала через MTProto ещё не реализовано")
