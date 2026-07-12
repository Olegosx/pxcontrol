"""Тесты менеджера входа MTProto на подставном клиенте Telethon (без сети)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pxcontrol.engine.telegram.mtproto import LoginError, MtprotoLoginManager


class _FakeTelethonClient:
	"""Подставной клиент: имитирует connect/send_code/sign_in Telethon."""

	def __init__(self, need_2fa: bool = False) -> None:
		self._need_2fa = need_2fa
		self.connected = False
		self.session = SimpleNamespace(save=lambda: "STRING-SESSION")

	async def connect(self) -> None:
		self.connected = True

	async def disconnect(self) -> None:
		self.connected = False

	async def send_code_request(self, phone: str) -> Any:
		return SimpleNamespace(phone_code_hash="hash123")

	async def sign_in(
		self,
		phone: str | None = None,
		code: str | None = None,
		*,
		password: str | None = None,
		phone_code_hash: str | None = None,
	) -> None:
		from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

		if password is not None:
			return
		if code == "bad":
			raise PhoneCodeInvalidError(request=None)
		if self._need_2fa:
			raise SessionPasswordNeededError(request=None)


def _manager(client: _FakeTelethonClient) -> MtprotoLoginManager:
	return MtprotoLoginManager(client_factory=lambda api_id, api_hash: client)


async def test_login_without_2fa() -> None:
	"""Обычный вход: код принят, менеджер вернул строку сессии."""
	client = _FakeTelethonClient()
	manager = _manager(client)
	await manager.start(1, 111, "hash", "+7900")
	assert await manager.confirm_code(1, "12345") == "STRING-SESSION"
	assert client.connected is False, "клиент входа должен быть закрыт"


async def test_login_with_2fa() -> None:
	"""Вход с 2FA: после кода — None, после пароля — сессия."""
	manager = _manager(_FakeTelethonClient(need_2fa=True))
	await manager.start(1, 111, "hash", "+7900")
	assert await manager.confirm_code(1, "12345") is None
	assert await manager.confirm_password(1, "secret") == "STRING-SESSION"


async def test_bad_code_maps_to_readable_error() -> None:
	"""Ошибка Telethon переводится в понятный текст, вход сбрасывается."""
	manager = _manager(_FakeTelethonClient())
	await manager.start(1, 111, "hash", "+7900")
	with pytest.raises(LoginError, match="Неверный код"):
		await manager.confirm_code(1, "bad")
	with pytest.raises(LoginError, match="Вход не начат"):
		await manager.confirm_code(1, "12345")


async def test_confirm_without_start() -> None:
	"""Подтверждение без начала входа — понятная ошибка."""
	manager = _manager(_FakeTelethonClient())
	with pytest.raises(LoginError, match="Вход не начат"):
		await manager.confirm_code(42, "12345")


async def test_cancel_closes_client() -> None:
	"""Отмена входа закрывает клиента и чистит состояние."""
	client = _FakeTelethonClient()
	manager = _manager(client)
	await manager.start(1, 111, "hash", "+7900")
	assert client.connected is True
	await manager.cancel(1)
	assert client.connected is False
