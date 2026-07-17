"""Тесты транспорта MTProto (публикация постов) на подставном клиенте."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from pxcontrol.engine.telegram.mtproto import (
	MtprotoTransport,
	UserbotNotConnectedError,
	UserbotSessionExpiredError,
	UserbotUnavailableError,
)
from pxcontrol.engine.telegram.types import MediaKind, OutgoingPost


class _FakeClient:
	"""Подставной клиент Telethon для операций с постами."""

	def __init__(self) -> None:
		self.connected = False
		self.sent: list[tuple[Any, str, Any]] = []
		self.files: list[dict[str, Any]] = []

	async def connect(self) -> None:
		self.connected = True

	async def is_user_authorized(self) -> bool:
		return True

	async def disconnect(self) -> None:
		self.connected = False

	async def send_message(
		self, entity: Any, text: str, schedule: Any = None
	) -> None:
		self.sent.append((entity, text, schedule))

	async def send_file(self, entity: Any, file: str, **kwargs: Any) -> None:
		progress = kwargs.pop("progress_callback", None)
		if callable(progress):
			progress(50, 100)
			progress(100, 100)
		self.files.append({"entity": entity, "file": file, **kwargs})

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
	with pytest.raises(UserbotUnavailableError, match="войдите"):
		await transport.publish("-1001", OutgoingPost(text="x"))


async def test_start_connects_once() -> None:
	"""start() подключает настроенного клиента и идемпотентен."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	assert fake.connected is True
	await transport.start()  # повторный вызов не создаёт второго клиента
	await transport.stop()
	assert fake.connected is False


async def test_failed_connect_leaves_transport_restartable() -> None:
	"""Неудачное подключение не «отравляет» транспорт: retry возможен."""

	class _BrokenClient(_FakeClient):
		async def connect(self) -> None:
			raise ConnectionError("нет сети")

	broken = _BrokenClient()
	transport = _transport(broken)
	with pytest.raises(UserbotNotConnectedError):
		await transport.start()
	# клиент не сохранён — операции честно говорят «не подключён»
	with pytest.raises(UserbotNotConnectedError, match="войдите"):
		await transport.publish("-1001", OutgoingPost(text="x"))


async def test_start_rejects_revoked_session() -> None:
	"""Отозванная сессия — отдельная ошибка с инструкцией войти заново."""

	class _RevokedClient(_FakeClient):
		async def is_user_authorized(self) -> bool:
			return False

	revoked = _RevokedClient()
	transport = _transport(revoked)
	with pytest.raises(UserbotSessionExpiredError, match="заново"):
		await transport.start()
	assert revoked.connected is False  # клиент закрыт, не подвис


async def test_bad_chat_id_gives_clear_error() -> None:
	"""Нечисловой ID из БД — понятная ошибка, а не «не видит канал»."""
	transport = _transport(_FakeClient())
	await transport.start()
	with pytest.raises(UserbotUnavailableError, match="переподключите"):
		await transport.publish("@битый-id", OutgoingPost(text="x"))


async def test_publish_text_passes_schedule() -> None:
	"""Текст уходит send_message с параметром schedule и числовым ID чата."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	when = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
	await transport.publish("-1001234", OutgoingPost(text="текст", when=when))
	assert fake.sent == [(-1001234, "текст", when)]
	assert fake.files == []


async def test_publish_media_maps_kind_to_hints() -> None:
	"""Медиа уходит send_file: видео — потоковое, документ — force_document."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	received: list[float] = []
	await transport.publish(
		"-1001234",
		OutgoingPost(
			text="подпись", media_path="/tmp/v.mp4", media_kind=MediaKind.VIDEO,
		),
		on_progress=received.append,
	)
	await transport.publish(
		"-1001234",
		OutgoingPost(media_path="/tmp/d.zip", media_kind=MediaKind.DOCUMENT),
	)
	video, doc = fake.files
	assert video["file"] == "/tmp/v.mp4" and video["caption"] == "подпись"
	assert video["supports_streaming"] and not video["force_document"]
	assert doc["force_document"] and doc["caption"] is None
	assert received == [0.5, 1.0]


def test_ensure_userbot_can_post() -> None:
	"""Права userbot: админ с публикацией или владелец; иначе — ошибка."""
	ok = SimpleNamespace(
		is_admin=True, is_creator=False,
		participant=SimpleNamespace(admin_rights=SimpleNamespace(post_messages=True)),
	)
	MtprotoTransport._ensure_userbot_can_post(ok)
	creator = SimpleNamespace(
		is_admin=True, is_creator=True,
		participant=SimpleNamespace(admin_rights=None),
	)
	MtprotoTransport._ensure_userbot_can_post(creator)
	with pytest.raises(UserbotUnavailableError, match="не администратор"):
		MtprotoTransport._ensure_userbot_can_post(SimpleNamespace(
			is_admin=False, is_creator=False, participant=SimpleNamespace(),
		))
	with pytest.raises(UserbotUnavailableError, match="нет права публиковать"):
		MtprotoTransport._ensure_userbot_can_post(SimpleNamespace(
			is_admin=True, is_creator=False,
			participant=SimpleNamespace(
				admin_rights=SimpleNamespace(post_messages=False)
			),
		))


async def test_activate_userbot_applies_new_credentials() -> None:
	"""Повторная активация закрывает старого клиента и применяет реквизиты.

	Сценарии: вход во второй аккаунт, повторный вход после отзыва сессии —
	оба должны работать без перезапуска приложения.
	"""
	from pxcontrol.engine.telegram.gateway import TelegramGateway

	created: list[tuple[int, str, str | None]] = []
	clients: list[_FakeClient] = []

	def factory(api_id: int, api_hash: str, session: str | None) -> _FakeClient:
		created.append((api_id, api_hash, session))
		client = _FakeClient()
		clients.append(client)
		return client

	gateway = TelegramGateway()
	gateway.mtproto = MtprotoTransport(client_factory=factory)
	await gateway.activate_userbot(1, "h1", "s1")
	await gateway.activate_userbot(2, "h2", "s2")
	assert created == [(1, "h1", "s1"), (2, "h2", "s2")]
	assert clients[0].connected is False  # старый клиент закрыт
	assert clients[1].connected is True


async def test_get_scheduled_returns_messages() -> None:
	"""Чтение отложенных отдаёт собственный тип границы, не Telethon."""
	transport = _transport(_FakeClient())
	await transport.start()
	messages = await transport.get_scheduled("-1001234")
	assert len(messages) == 1
	assert messages[0].text == "из телеграма"
	assert messages[0].scheduled_at == datetime(2026, 7, 13, tzinfo=UTC)
