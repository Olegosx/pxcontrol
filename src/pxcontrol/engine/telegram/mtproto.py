"""Транспорт MTProto (через Telethon, отдельный аккаунт).

Роли: создание отложенных постов прямо в канале (серверное планирование
Telegram, ADR-0010), чтение отложенных, в будущем — чтение каналов-источников.
Здесь же — пошаговый вход userbot (код → 2FA → строка сессии).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.refs import normalize_chat_ref
from pxcontrol.engine.telegram.types import (
	ChannelInfo,
	MediaKind,
	OutgoingPost,
	ScheduledMessage,
)

logger = logging.getLogger(__name__)


class LoginError(EngineError):
	"""Ошибка входа userbot с понятным человеку текстом."""


class UserbotUnavailableError(EngineError):
	"""Userbot не может выполнить операцию (базовый класс, понятный текст).

	Подклассы разводят причины, по которым сервисы принимают разные
	решения: временная недоступность (не подключён, нет связи, лимит) —
	не повод менять сохранённые в БД права; подтверждённый отказ
	Telegram (:class:`UserbotAccessError`) — повод.
	"""


class UserbotNotConnectedError(UserbotUnavailableError):
	"""Userbot не подключён или соединение с Telegram не удалось."""


class UserbotSessionExpiredError(UserbotUnavailableError):
	"""Сессия userbot отозвана или недействительна — нужен повторный вход."""


class UserbotAccessError(UserbotUnavailableError):
	"""Подтверждённый отказ Telegram: нет прав или канал не виден."""


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


def _translate_error(exc: Exception) -> UserbotUnavailableError:
	"""Переводит исключение операции userbot в доменную ошибку.

	Разводит причины по подклассам :class:`UserbotUnavailableError`:
	сервисы различают «подтверждённый отказ» и «временную недоступность».
	"""
	from telethon import errors

	if isinstance(exc, errors.ChatAdminRequiredError):
		return UserbotAccessError(
			"Userbot не администратор канала — добавьте аккаунт userbot "
			"администратором с правом публиковать."
		)
	if isinstance(
		exc,
		errors.AuthKeyUnregisteredError | errors.SessionRevokedError
		| errors.SessionExpiredError | errors.UserDeactivatedError,
	):
		return UserbotSessionExpiredError(
			"Сессия userbot недействительна — войдите в аккаунт заново: "
			"Настройки → Аккаунты."
		)
	if isinstance(exc, errors.FloodWaitError):
		return UserbotUnavailableError(
			f"Telegram просит подождать {exc.seconds} с."
		)
	if isinstance(exc, ValueError):
		# Telethon: «Could not find the input entity» — канал не в поле
		# зрения аккаунта (числовые ID валидируются до этой точки).
		return UserbotAccessError(
			"Userbot не видит этот канал — убедитесь, что аккаунт добавлен "
			"в канал администратором."
		)
	if isinstance(exc, ConnectionError | OSError | TimeoutError):
		return UserbotNotConnectedError(
			"Нет связи с Telegram — проверьте сеть и попробуйте ещё раз."
		)
	return UserbotUnavailableError(f"Telegram отклонил операцию: {exc}")


@asynccontextmanager
async def _mtproto_errors() -> AsyncIterator[None]:
	"""Переводит исключения Telethon в доменные ошибки userbot.

	Единый маппер операций транспорта (парный ``_bot_errors`` в bot_api);
	уже доменные ошибки пропускает без изменений.
	"""
	try:
		yield
	except UserbotUnavailableError:
		raise
	except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
		raise _translate_error(exc) from exc


def _peer_id(chat_id: str) -> int:
	"""Числовой ID канала из строки БД (контракт ``ChannelInfo.chat_id``).

	Raises:
		UserbotUnavailableError: В БД оказался нечисловой ID.
	"""
	try:
		return int(chat_id)
	except ValueError as exc:
		raise UserbotUnavailableError(
			f"Некорректный ID канала в базе: {chat_id!r} — переподключите "
			"канал на странице «Каналы»."
		) from exc


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
		"""Подключает клиента, если заданы реквизиты и ещё не подключён.

		Клиент считается подключённым только после успешного соединения
		и проверки, что сессия жива: неудачная попытка не оставляет
		«полуживого» клиента — повторный ``start()`` попробует заново.

		Raises:
			UserbotNotConnectedError: Соединение с Telegram не удалось.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
		"""
		if self._client is not None:
			return
		if self._creds is None:
			logger.info("Аккаунт MTProto не настроен — userbot отключён.")
			return
		api_id, api_hash, session = self._creds
		client = self._client_factory(api_id, api_hash, session)
		try:
			await client.connect()
			authorized = bool(await client.is_user_authorized())
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			await _safe_disconnect(client)
			raise UserbotNotConnectedError(
				f"Не удалось подключить userbot: {exc}"
			) from exc
		if not authorized:
			await _safe_disconnect(client)
			raise UserbotSessionExpiredError(
				"Сессия userbot недействительна — войдите в аккаунт заново: "
				"Настройки → Аккаунты."
			)
		self._client = client
		logger.info("MTProto клиент подключён.")

	async def stop(self) -> None:
		"""Отключает клиента MTProto."""
		if self._client is not None:
			await _safe_disconnect(self._client)
			self._client = None

	def _require_client(self) -> Any:
		"""Возвращает подключённого клиента или объясняет, чего не хватает."""
		if self._client is None:
			raise UserbotNotConnectedError(
				"Userbot не подключён — войдите в аккаунт: Настройки → Аккаунты."
			)
		return self._client

	async def publish(
		self,
		chat_id: str,
		post: OutgoingPost,
		on_progress: Callable[[float], None] | None = None,
	) -> None:
		"""Публикует пост: текст или медиа с подписью, сразу или отложенно.

		Единый транспорт публикации — userbot (ADR-0011): лимит Bot API
		на файлы (50 МБ) мал для видео, отложенные (schedule_date) хранит
		и публикует сервер Telegram (ADR-0010). ``on_progress`` получает
		долю загрузки файла 0.0..1.0 (большие файлы — это минуты).
		Миниатюру Telegram принимает, только когда известны размеры
		видео — их извлекает hachoir.
		"""
		client = self._require_client()
		peer = _peer_id(chat_id)

		def _progress(sent: int, total: int) -> None:
			if on_progress is not None and total > 0:
				on_progress(sent / total)

		async with _mtproto_errors():
			if post.media_path is None:
				await client.send_message(peer, post.text, schedule=post.when)
			else:
				await client.send_file(
					peer, post.media_path, caption=post.text or None,
					schedule=post.when,
					supports_streaming=post.media_kind is MediaKind.VIDEO,
					force_document=post.media_kind is MediaKind.DOCUMENT,
					progress_callback=_progress,
					thumb=post.thumb_path,
				)
		logger.info(
			"Пост отправлен в чат %s (%s, %s).", chat_id,
			post.media_kind if post.media_path else "текст",
			f"отложено на {post.when}" if post.when else "сразу",
		)

	async def check_channel(self, chat_ref: str) -> ChannelInfo:
		"""Проверяет канал и права userbot: админ с правом публиковать.

		Принимает @имя, ссылку t.me/… или ID -100… (разбор общий
		с бот-путём — ``normalize_chat_ref``).

		Raises:
			ChatRefError: Введённую ссылку/имя не удалось разобрать.
			UserbotUnavailableError: Userbot не подключён, канал не найден,
				userbot не админ или без права публиковать.
		"""
		from telethon import utils

		client = self._require_client()
		ref = normalize_chat_ref(chat_ref)
		async with _mtproto_errors():
			entity = await client.get_entity(ref)
			perms = await client.get_permissions(entity, "me")
		self._ensure_userbot_can_post(perms)
		return ChannelInfo(
			chat_id=str(utils.get_peer_id(entity)),
			title=str(getattr(entity, "title", "") or chat_ref),
			username=getattr(entity, "username", None),
		)

	@staticmethod
	def _ensure_userbot_can_post(perms: Any) -> None:
		"""Требует права админа с публикацией (владельцу можно всё).

		Raises:
			UserbotUnavailableError: Прав не хватает.
		"""
		if not perms.is_admin:
			raise UserbotAccessError(
				"Userbot не администратор канала — добавьте аккаунт "
				"администратором с правом публиковать."
			)
		rights = getattr(perms.participant, "admin_rights", None)
		if not perms.is_creator and not getattr(rights, "post_messages", False):
			raise UserbotAccessError(
				"У userbot нет права публиковать сообщения в канале."
			)

	async def get_scheduled(self, chat_id: str) -> list[ScheduledMessage]:
		"""Читает отложенные записи канала (источник истины — Telegram)."""
		from telethon.tl.functions.messages import GetScheduledHistoryRequest

		client = self._require_client()
		peer_id = _peer_id(chat_id)
		async with _mtproto_errors():
			entity = await client.get_input_entity(peer_id)
			result = await client(GetScheduledHistoryRequest(peer=entity, hash=0))
		return [
			ScheduledMessage(
				text=getattr(message, "message", "") or "",
				scheduled_at=message.date,
			)
			for message in result.messages
		]


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
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
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
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
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
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			# как и confirm_code: неудачный шаг закрывает незавершённый вход
			await self.cancel(account_id)
			raise LoginError(_map_login_error(exc)) from exc
		return await self._finish(account_id, client)

	async def cancel(self, account_id: int) -> None:
		"""Прерывает незавершённый вход и закрывает его клиента."""
		entry = self._pending.pop(account_id, None)
		if entry is not None:
			await _safe_disconnect(entry[0])

	async def cancel_all(self) -> None:
		"""Закрывает все незавершённые входы (при остановке движка)."""
		for account_id in list(self._pending):
			await self.cancel(account_id)

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
