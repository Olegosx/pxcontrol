"""Тесты транспорта MTProto (отложенные посты) на подставном клиенте."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from pxcontrol.engine.telegram.mtproto import MtprotoTransport, UserbotUnavailable


class _FakeClient:
	"""Подставной клиент Telethon для операций с постами."""

	def __init__(self) -> None:
		self.connected = False
		self.sent: list[tuple[Any, str, Any]] = []

	async def connect(self) -> None:
		self.connected = True

	async def disconnect(self) -> None:
		self.connected = False

	async def send_message(
		self, entity: Any, text: str, schedule: Any = None
	) -> None:
		self.sent.append((entity, text, schedule))

	async def get_input_entity(self, entity_id: Any) -> str:
		return f"entity:{entity_id}"

	async def __call__(self, request: Any) -> Any:
		return SimpleNamespace(messages=[
			SimpleNamespace(message="из телеграма", date=datetime(2026, 7, 13, tzinfo=UTC)),
		])


def _transport(client: _FakeClient) -> MtprotoTransport:
	transport = MtprotoTransport(client_factory=lambda a, b, c: client)
	transport.configure(111, "hash", "session-string")
	return transport


async def test_requires_connected_userbot() -> None:
	"""Без подключения — понятная ошибка с инструкцией."""
	transport = MtprotoTransport()
	with pytest.raises(UserbotUnavailable, match="войдите"):
		await transport.schedule_post("-1001", "x", datetime.now(UTC))


async def test_start_connects_once() -> None:
	"""start() подключает настроенного клиента и идемпотентен."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	assert fake.connected is True
	await transport.start()  # повторный вызов не создаёт второго клиента
	await transport.stop()
	assert fake.connected is False


async def test_schedule_post_passes_schedule() -> None:
	"""Отложка уходит с параметром schedule и числовым ID чата."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	when = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
	await transport.schedule_post("-1001234", "текст", when)
	assert fake.sent == [(-1001234, "текст", when)]


async def test_get_scheduled_returns_messages() -> None:
	"""Чтение отложенных возвращает сообщения Telegram."""
	transport = _transport(_FakeClient())
	await transport.start()
	messages = await transport.get_scheduled("-1001234")
	assert len(messages) == 1 and messages[0].message == "из телеграма"
