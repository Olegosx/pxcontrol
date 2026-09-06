"""Тесты транспорта MTProto (публикация постов) на подставном клиенте."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from pxcontrol.engine.telegram.mtproto import (
	MtprotoTransport,
	UserbotNotConnectedError,
	UserbotSessionExpiredError,
	UserbotUnavailableError,
	community_kind_from_entity,
	ensure_userbot_can_post,
	ensure_userbot_can_send,
)
from pxcontrol.engine.telegram.types import (
	CommunityKind,
	MediaKind,
	OutgoingPost,
	UserbotRole,
)


class _FakeClient:
	"""Подставной клиент Telethon для операций с постами."""

	def __init__(self) -> None:
		self.connected = False
		self.connect_calls = 0
		self.me_premium = False
		self.sent: list[tuple[Any, str, Any]] = []
		self.files: list[dict[str, Any]] = []
		# сущность и права для check_community (тесты задают под сценарий)
		self.entity: Any = None
		self.permissions: Any = None

	async def connect(self) -> None:
		self.connect_calls += 1
		self.connected = True

	def is_connected(self) -> bool:
		return self.connected

	async def get_me(self) -> Any:
		return SimpleNamespace(premium=self.me_premium)

	async def is_user_authorized(self) -> bool:
		return True

	async def disconnect(self) -> None:
		self.connected = False

	async def send_message(
		self, entity: Any, text: str, schedule: Any = None, reply_to: Any = None
	) -> None:
		self.sent.append((entity, text, schedule, reply_to))

	async def send_file(self, entity: Any, file: str, **kwargs: Any) -> None:
		progress = kwargs.pop("progress_callback", None)
		if callable(progress):
			progress(50, 100)
			progress(100, 100)
		self.files.append({"entity": entity, "file": file, **kwargs})

	async def get_input_entity(self, entity_id: Any) -> str:
		return f"entity:{entity_id}"

	async def get_entity(self, ref: Any) -> Any:
		return self.entity

	async def get_permissions(self, entity: Any, user: Any) -> Any:
		return self.permissions

	async def __call__(self, request: Any) -> Any:
		if type(request).__name__ == "GetForumTopicsRequest":
			return SimpleNamespace(
				topics=[
					SimpleNamespace(id=1, title="General", closed=False),
					SimpleNamespace(id=7, title="Новости", closed=False),
					SimpleNamespace(id=8, title="Архив", closed=True),
					SimpleNamespace(id=9, title=None),  # ForumTopicDeleted
				],
				count=4,
			)
		return SimpleNamespace(
			messages=[
				SimpleNamespace(message="из телеграма", date=datetime(2026, 7, 13, tzinfo=UTC)),
			]
		)


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
			self.connect_calls += 1
			raise ConnectionError("нет сети")

	broken = _BrokenClient()
	transport = _transport(broken)
	with pytest.raises(UserbotNotConnectedError):
		await transport.start()
	# клиент не сохранён — операция сама пробует подключиться ещё раз
	with pytest.raises(UserbotNotConnectedError, match="подключить"):
		await transport.publish("-1001", OutgoingPost(text="x"))
	assert broken.connect_calls == 2


async def test_operation_repairs_failed_start_when_network_returns() -> None:
	"""Запуск без сети: первая же операция чинит транспорт повторным стартом."""

	class _FlakyClient(_FakeClient):
		def __init__(self) -> None:
			super().__init__()
			self.fail_first = True

		async def connect(self) -> None:
			self.connect_calls += 1
			if self.fail_first:
				self.fail_first = False
				raise ConnectionError("нет сети")
			self.connected = True

	flaky = _FlakyClient()
	transport = _transport(flaky)
	with pytest.raises(UserbotNotConnectedError):
		await transport.start()  # приложение запустили до появления сети
	await transport.publish("-1001", OutgoingPost(text="x"))  # сеть вернулась
	assert flaky.sent  # пост ушёл без перезапуска приложения и повторного входа


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


async def test_premium_tracked_across_lifecycle() -> None:
	"""Статус Premium берётся при подключении, живёт до отключения."""
	fake = _FakeClient()
	fake.me_premium = True
	transport = _transport(fake)
	assert transport.premium is False  # до подключения статус неизвестен
	await transport.start()
	assert transport.premium is True
	await transport.stop()
	assert transport.premium is False


async def test_premium_refreshed_on_reconnect() -> None:
	"""Переподключение перечитывает статус: подписка могла кончиться."""
	fake = _FakeClient()
	fake.me_premium = True
	transport = _transport(fake)
	await transport.start()
	assert transport.premium is True
	fake.connected = False  # обрыв; за время простоя подписка кончилась
	fake.me_premium = False
	await transport.publish("-1001", OutgoingPost(text="x"))
	assert transport.premium is False


async def test_get_me_failure_defaults_to_no_premium() -> None:
	"""Сбой get_me не мешает подключению: действует меньший лимит."""

	class _NoMeClient(_FakeClient):
		async def get_me(self) -> Any:
			raise RuntimeError("нет ответа")

	client = _NoMeClient()
	transport = _transport(client)
	await transport.start()  # подключение не сорвалось
	assert transport.premium is False


async def test_reconnects_on_demand_after_network_drop() -> None:
	"""Обрыв соединения чинится перед операцией — без перезапуска приложения."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	fake.connected = False  # Telethon исчерпал свои попытки и отключился
	await transport.publish("-1001234", OutgoingPost(text="после обрыва"))
	assert fake.connected is True
	assert fake.sent == [(-1001234, "после обрыва", None, None)]


async def test_failed_reconnect_gives_clear_error() -> None:
	"""Сеть так и не появилась: понятная ошибка; вернулась — чинится само."""

	class _FlakyClient(_FakeClient):
		def __init__(self) -> None:
			super().__init__()
			self.fail_connect = False

		async def connect(self) -> None:
			if self.fail_connect:
				raise ConnectionError("нет сети")
			await super().connect()

	flaky = _FlakyClient()
	transport = _transport(flaky)
	await transport.start()
	flaky.connected = False
	flaky.fail_connect = True
	with pytest.raises(UserbotNotConnectedError, match="повторите"):
		await transport.publish("-1001", OutgoingPost(text="x"))
	flaky.fail_connect = False  # сеть вернулась — следующая операция чинит сама
	await transport.publish("-1001", OutgoingPost(text="ожил"))
	assert [text for _peer, text, _when, _topic in flaky.sent] == ["ожил"]


async def test_reconnect_detects_revoked_session() -> None:
	"""Сессию отозвали за время простоя — отдельная ошибка с инструкцией."""

	class _RevocableClient(_FakeClient):
		def __init__(self) -> None:
			super().__init__()
			self.authorized = True

		async def is_user_authorized(self) -> bool:
			return self.authorized

	client = _RevocableClient()
	transport = _transport(client)
	await transport.start()
	client.connected = False
	client.authorized = False
	with pytest.raises(UserbotSessionExpiredError, match="заново"):
		await transport.get_scheduled("-1001234")


async def test_parallel_operations_reconnect_once() -> None:
	"""Замок пускает в переподключение одну операцию, connect не дублируется."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	fake.connected = False
	await asyncio.gather(
		transport.publish("-1001", OutgoingPost(text="а")),
		transport.publish("-1001", OutgoingPost(text="б")),
	)
	assert fake.connect_calls == 2  # start() + одно переподключение на двоих
	assert len(fake.sent) == 2


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
	assert fake.sent == [(-1001234, "текст", when, None)]
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
			text="подпись",
			media_path="/tmp/v.mp4",
			media_kind=MediaKind.VIDEO,
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
		is_admin=True,
		is_creator=False,
		participant=SimpleNamespace(admin_rights=SimpleNamespace(post_messages=True)),
	)
	ensure_userbot_can_post(ok)
	creator = SimpleNamespace(
		is_admin=True,
		is_creator=True,
		participant=SimpleNamespace(admin_rights=None),
	)
	ensure_userbot_can_post(creator)
	with pytest.raises(UserbotUnavailableError, match="не администратор"):
		ensure_userbot_can_post(
			SimpleNamespace(
				is_admin=False,
				is_creator=False,
				participant=SimpleNamespace(),
			)
		)
	with pytest.raises(UserbotUnavailableError, match="нет права публиковать"):
		ensure_userbot_can_post(
			SimpleNamespace(
				is_admin=True,
				is_creator=False,
				participant=SimpleNamespace(admin_rights=SimpleNamespace(post_messages=False)),
			)
		)


async def test_activate_userbot_pool_per_account() -> None:
	"""Пул шлюза: у каждого аккаунта свой клиент, повторная активация
	заменяет клиента только этого аккаунта (ADR-0019)."""
	from pxcontrol.engine.telegram.gateway import TelegramGateway

	created: list[tuple[int, str, str | None]] = []
	clients: list[_FakeClient] = []

	def client_factory(api_id: int, api_hash: str, session: str | None) -> _FakeClient:
		created.append((api_id, api_hash, session))
		client = _FakeClient()
		clients.append(client)
		return client

	gateway = TelegramGateway()
	gateway.transport_factory = lambda: MtprotoTransport(client_factory=client_factory)
	await gateway.activate_userbot(10, 1, "h", "s1")
	await gateway.activate_userbot(20, 1, "h", "s2")
	assert created == [(1, "h", "s1"), (1, "h", "s2")]
	assert clients[0].connected and clients[1].connected  # оба аккаунта в пуле
	# повторный вход аккаунта 10 заменяет только его клиента
	await gateway.activate_userbot(10, 1, "h", "s1-new")
	assert clients[0].connected is False  # старый клиент аккаунта закрыт
	assert clients[1].connected is True  # чужой аккаунт не тронут
	assert clients[2].connected is True
	# деактивация выборочная; публикация без клиента — понятная ошибка
	await gateway.deactivate_userbot(20)
	assert clients[1].connected is False
	with pytest.raises(UserbotNotConnectedError, match="войдите"):
		await gateway.publish(20, "-1001", OutgoingPost(text="x"))
	await gateway.stop()
	assert clients[2].connected is False  # остановка гасит весь пул


async def test_gateway_premium_per_account() -> None:
	"""Premium читается по аккаунту; неизвестный аккаунт и None — False."""
	from pxcontrol.engine.telegram.gateway import TelegramGateway

	premium_client = _FakeClient()
	premium_client.me_premium = True
	plain_client = _FakeClient()
	clients = [premium_client, plain_client]
	gateway = TelegramGateway()
	gateway.transport_factory = lambda: MtprotoTransport(
		client_factory=lambda a, b, c: clients.pop(0)
	)
	await gateway.activate_userbot(10, 1, "h", "s1")
	await gateway.activate_userbot(20, 1, "h", "s2")
	assert gateway.userbot_premium(10) is True
	assert gateway.userbot_premium(20) is False
	assert gateway.userbot_premium(None) is False
	assert gateway.userbot_premium(99) is False  # не активирован
	assert gateway.any_userbot_premium() is True
	await gateway.stop()
	assert gateway.any_userbot_premium() is False


async def test_get_scheduled_returns_messages() -> None:
	"""Чтение отложенных отдаёт собственный тип границы, не Telethon."""
	transport = _transport(_FakeClient())
	await transport.start()
	messages = await transport.get_scheduled("-1001234")
	assert len(messages) == 1
	assert messages[0].text == "из телеграма"
	assert messages[0].scheduled_at == datetime(2026, 7, 13, tzinfo=UTC)


def test_translate_error_confirmed_refusals() -> None:
	"""«Выгнали из канала» и родня — подтверждённый отказ, не временный сбой.

	От класса зависит поведение системы: только UserbotAccessError даёт
	recheck_community право снять хранимый флаг userbot-админа.
	"""
	from telethon import errors

	from pxcontrol.engine.telegram.mtproto import UserbotAccessError, _translate_error

	for exc in (
		errors.UserNotParticipantError(request=None),
		errors.ChannelPrivateError(request=None),
		errors.ChatWriteForbiddenError(request=None),
		errors.ChatAdminRequiredError(request=None),
	):
		assert isinstance(_translate_error(exc), UserbotAccessError)
	# сетевой сбой — по-прежнему временная недоступность
	assert not isinstance(_translate_error(ConnectionError("x")), UserbotAccessError)
	# ValueError про entity — «канал не виден», прочие ValueError — нет:
	# ложный совет «добавьте аккаунт в канал» хуже честного общего текста
	assert isinstance(
		_translate_error(ValueError("Could not find the input entity for PeerUser")),
		UserbotAccessError,
	)
	assert not isinstance(_translate_error(ValueError("bad argument")), UserbotAccessError)


async def test_bot_errors_translate_flood_and_server_failures() -> None:
	"""Флуд-лимит, «файл велик» и 5xx Bot API — понятные тексты, не дампы."""
	from aiogram.methods import GetMe

	from pxcontrol.engine.telegram.bot_api import CommunityCheckError, _bot_errors

	async def _raise_inside(exc: BaseException) -> None:
		async with _bot_errors("нет прав", "отклонено"):
			raise exc

	from aiogram.exceptions import (
		TelegramEntityTooLarge,
		TelegramRetryAfter,
		TelegramServerError,
	)

	from pxcontrol.engine.telegram.types import TelegramFloodError

	with pytest.raises(TelegramFloodError, match="подождать 17 с") as flood:
		await _raise_inside(TelegramRetryAfter(GetMe(), "flood", retry_after=17))
	assert flood.value.retry_after_s == 17  # очередь ждёт ровно названный срок
	# «файл велик» наследует сетевую ошибку — не должен стать «нет связи»
	with pytest.raises(CommunityCheckError, match="лимита Bot API"):
		await _raise_inside(TelegramEntityTooLarge(GetMe(), "too large"))
	with pytest.raises(CommunityCheckError, match="отклонил операцию"):
		await _raise_inside(TelegramServerError(GetMe(), "internal"))


def test_community_kind_from_entity() -> None:
	"""Вид по сущности Telethon; малая группа и личный чат — отказ."""
	from telethon.tl.types import Chat

	channel = SimpleNamespace(broadcast=True, megagroup=False)
	assert community_kind_from_entity(channel) is CommunityKind.CHANNEL
	megagroup = SimpleNamespace(broadcast=False, megagroup=True)
	assert community_kind_from_entity(megagroup) is CommunityKind.GROUP
	gigagroup = SimpleNamespace(broadcast=False, megagroup=False, gigagroup=True)
	assert community_kind_from_entity(gigagroup) is CommunityKind.GROUP
	small = Chat(id=1, title="Малая", photo=None, participants_count=2, date=None, version=1)
	with pytest.raises(UserbotUnavailableError, match="супергруппу"):
		community_kind_from_entity(small)
	with pytest.raises(UserbotUnavailableError, match="личный чат"):
		community_kind_from_entity(SimpleNamespace(broadcast=False, megagroup=False))


def _group_member(
	*, admin: bool = False, left: bool = False, banned: bool = False, banned_rights: Any = None
) -> SimpleNamespace:
	"""Права участника группы для ensure_userbot_can_send."""
	return SimpleNamespace(
		is_admin=admin,
		has_left=left,
		is_banned=banned,
		participant=SimpleNamespace(banned_rights=banned_rights),
	)


def test_ensure_userbot_can_send() -> None:
	"""Права в группе: писать может любой не ограниченный участник."""
	deny_all = SimpleNamespace(send_messages=True, send_plain=False)
	deny_text = SimpleNamespace(send_messages=False, send_plain=True)
	allow = SimpleNamespace(send_messages=False, send_plain=False)
	# админу общие ограничения группы не мешают (гигагруппа — тот же случай)
	ensure_userbot_can_send(_group_member(admin=True), deny_all)
	ensure_userbot_can_send(_group_member(), None)
	ensure_userbot_can_send(_group_member(), allow)
	# ограниченный, но с правом писать — годится
	ensure_userbot_can_send(_group_member(banned=True, banned_rights=allow), None)
	with pytest.raises(UserbotUnavailableError, match="не участник"):
		ensure_userbot_can_send(_group_member(left=True), None)
	with pytest.raises(UserbotUnavailableError, match="ограничен в отправке"):
		ensure_userbot_can_send(_group_member(banned=True, banned_rights=deny_all), None)
	# гранулярный запрет текста (send_plain) — тоже запрет
	with pytest.raises(UserbotUnavailableError, match="ограничен в отправке"):
		ensure_userbot_can_send(_group_member(banned=True, banned_rights=deny_text), None)
	with pytest.raises(UserbotUnavailableError, match="только администраторы"):
		ensure_userbot_can_send(_group_member(), deny_all)


async def test_check_community_group_returns_kind_and_forum() -> None:
	"""check_community: группа-форум проходит и отдаёт вид и признак тем."""
	from telethon.tl.types import Channel

	client = _FakeClient()
	client.entity = Channel(
		id=123, title="Группа", photo=None, date=None, megagroup=True, forum=True, username="grp"
	)
	client.permissions = _group_member()
	transport = _transport(client)
	await transport.start()
	info = await transport.check_community("@grp")
	assert info.kind is CommunityKind.GROUP
	assert info.forum is True
	assert info.title == "Группа"


async def test_check_community_channel_requires_admin() -> None:
	"""check_community: канал по-прежнему требует админа с правом постить."""
	from telethon.tl.types import Channel

	client = _FakeClient()
	client.entity = Channel(
		id=124, title="Канал", photo=None, date=None, broadcast=True, username="chan"
	)
	client.permissions = SimpleNamespace(
		is_admin=False, is_creator=False, participant=SimpleNamespace()
	)
	transport = _transport(client)
	await transport.start()
	with pytest.raises(UserbotUnavailableError, match="не администратор"):
		await transport.check_community("@chan")


async def test_publish_passes_topic() -> None:
	"""Тема форума уходит транспорту ответом на её корневое сообщение."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	await transport.publish("-1001234", OutgoingPost(text="в тему", topic_id=7))
	assert fake.sent == [(-1001234, "в тему", None, 7)]
	await transport.publish(
		"-1001234", OutgoingPost(media_path="v.mp4", media_kind=MediaKind.VIDEO, topic_id=7)
	)
	assert fake.files[-1]["reply_to"] == 7


async def test_get_forum_topics_skips_deleted() -> None:
	"""Список тем: удалённые (без названия) пропускаются, id и названия целы."""
	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	topics = await transport.get_forum_topics("-1001234")
	assert [(t.id, t.title, t.closed) for t in topics] == [
		(1, "General", False),
		(7, "Новости", False),
		(8, "Архив", True),
	]


def test_translate_slow_mode_into_flood() -> None:
	"""Медленный режим группы — «подожди и повтори», не ошибка элемента.

	ADR-0022: участник группы подчиняется slow mode; очередь отправки
	уже умеет ждать по UserbotFloodError — перевод обязан попасть
	именно в этот класс с сроком от сервера.
	"""
	from telethon import errors

	from pxcontrol.engine.telegram.mtproto import UserbotFloodError, _translate_error

	exc = errors.SlowModeWaitError(request=None)
	exc.seconds = 30
	translated = _translate_error(exc)
	assert isinstance(translated, UserbotFloodError)
	assert translated.retry_after_s == 30


async def test_check_community_reports_role() -> None:
	"""Зонд отдаёт роль аккаунта: участник и админ различимы (ADR-0022)."""
	from telethon.tl.types import Channel

	client = _FakeClient()
	client.entity = Channel(
		id=125, title="Группа", photo=None, date=None, megagroup=True, username="grp2"
	)
	client.permissions = _group_member()
	transport = _transport(client)
	await transport.start()
	info = await transport.check_community("@grp2")
	assert info.role is UserbotRole.MEMBER
	client.permissions = _group_member(admin=True)
	info = await transport.check_community("@grp2")
	assert info.role is UserbotRole.ADMIN
