"""Тесты транспорта MTProto (публикация постов) на подставном клиенте."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pxcontrol.engine.telegram.mtproto import (
	MtprotoTransport,
	UserbotFloodError,
	UserbotMessageGoneError,
	UserbotNotConnectedError,
	UserbotSessionExpiredError,
	UserbotUnavailableError,
	community_kind_from_entity,
	ensure_userbot_can_post,
	ensure_userbot_can_send,
	media_kind_of,
)
from pxcontrol.engine.telegram.types import (
	CommunityKind,
	MediaKind,
	OutgoingFile,
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
		self.previews: list[bool] = []
		self.files: list[dict[str, Any]] = []
		# сущность и права для check_community (тесты задают под сценарий)
		self.entity: Any = None
		self.permissions: Any = None
		self.online = 0  # онлайн в ответе полной информации (0 — не отдан)
		self.has_avatar = True  # есть ли у сообщества аватар

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
		self,
		entity: Any,
		text: str,
		schedule: Any = None,
		reply_to: Any = None,
		link_preview: bool = True,
		**kwargs: Any,
	) -> None:
		self.sent.append((entity, text, schedule, reply_to))
		self.previews.append(link_preview)

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

	async def download_profile_photo(self, entity: Any, file: str) -> str | None:
		if not self.has_avatar:
			return None
		Path(file).write_bytes(b"jpg")
		return file

	async def __call__(self, request: Any) -> Any:
		if type(request).__name__ == "GetFullChannelRequest":
			return SimpleNamespace(
				full_chat=SimpleNamespace(participants_count=1234, online_count=self.online)
			)
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
				SimpleNamespace(
					id=5, message="из телеграма", date=datetime(2026, 7, 13, tzinfo=UTC)
				),
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
			files=(OutgoingFile("/tmp/v.mp4", MediaKind.VIDEO),),
		),
		on_progress=received.append,
	)
	await transport.publish(
		"-1001234",
		OutgoingPost(files=(OutgoingFile("/tmp/d.zip", MediaKind.DOCUMENT),)),
	)
	video, doc = fake.files
	assert video["file"] == "/tmp/v.mp4" and video["caption"] == "подпись"
	assert video["supports_streaming"] and not video["force_document"]
	assert doc["force_document"] and doc["caption"] is None
	assert received == [0.5, 1.0]


async def test_publish_poll_goes_as_input_media() -> None:
	"""Опрос уходит вложением без файла: вопрос, варианты, правила (C5)."""
	from telethon.tl import types

	from pxcontrol.engine.telegram.poll import PollDraft

	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	when = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
	await transport.publish(
		"-1001234",
		OutgoingPost(
			poll=PollDraft("Любимый цвет?", ("Синий", "Зелёный"), anonymous=False),
			when=when,
			topic_id=12,
		),
	)
	sent = fake.files[0]
	media = sent["file"]
	assert isinstance(media, types.InputMediaPoll)
	assert media.poll.question.text == "Любимый цвет?"
	assert [answer.text.text for answer in media.poll.answers] == ["Синий", "Зелёный"]
	# ключи вариантов различны — по ним Telegram считает голоса
	assert len({answer.option for answer in media.poll.answers}) == 2
	assert media.poll.public_voters is True  # неанонимный опрос
	assert media.poll.quiz is None and media.correct_answers is None
	assert sent["schedule"] == when and sent["reply_to"] == 12
	# подписи у опроса не бывает: вопрос и есть его текст
	assert "caption" not in sent


async def test_publish_quiz_carries_correct_answer() -> None:
	"""Викторина уезжает номером правильного варианта и пояснением."""
	from pxcontrol.engine.telegram.poll import PollDraft

	fake = _FakeClient()
	transport = _transport(fake)
	await transport.start()
	await transport.publish(
		"-1001234",
		OutgoingPost(
			poll=PollDraft(
				"Столица Франции?",
				("Берлин", "Париж"),
				quiz=True,
				correct_option=1,
				explanation="Париж с 987 года",
			)
		),
	)
	media = fake.files[0]["file"]
	assert media.poll.quiz is True
	# в нынешнем слое схемы correct_answers — номера вариантов
	assert media.correct_answers == [1]
	assert media.solution == "Париж с 987 года"


def test_message_text_reads_poll_question() -> None:
	"""У опроса своего текста нет — списки и дозор берут его вопрос."""
	from telethon.tl import types

	from pxcontrol.engine.telegram.mtproto import message_text

	poll = types.Poll(
		id=1,
		hash=0,
		question=types.TextWithEntities(text="Любимый цвет?", entities=[]),
		answers=[],
	)
	media = types.MessageMediaPoll(poll=poll, results=types.PollResults())
	assert message_text(SimpleNamespace(message="", media=media)) == "Любимый цвет?"
	# у обычного поста — его собственный текст
	assert message_text(SimpleNamespace(message="пост", media=None)) == "пост"
	assert message_text(SimpleNamespace(message="", media=None)) == ""


def test_media_kind_of_names_poll() -> None:
	"""Опрос — свой вид вложения, а не «чужое» (его приложение создаёт)."""
	from telethon.tl import types

	poll = types.Poll(
		id=1, hash=0, question=types.TextWithEntities(text="?", entities=[]), answers=[]
	)
	media = types.MessageMediaPoll(poll=poll, results=types.PollResults())
	assert media_kind_of(media) is MediaKind.POLL


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
	assert messages[0].id == 5
	assert messages[0].text == "из телеграма"
	assert messages[0].scheduled_at == datetime(2026, 7, 13, tzinfo=UTC)
	assert messages[0].media_kind is MediaKind.NONE  # у записи нет поля media
	assert messages[0].topic_id is None


async def test_community_stats_reads_full_info() -> None:
	"""Подписчики и онлайн — из одного запроса полной информации."""
	client = _FakeClient()
	client.online = 17
	transport = _transport(client)
	await transport.start()
	stats = await transport.community_stats("-1001234")
	assert stats.participants == 1234
	assert stats.online == 17
	# нулевой онлайн (каналы) нормализуется в None — «не отдан»
	client.online = 0
	assert (await transport.community_stats("-1001234")).online is None


async def test_download_avatar_and_absence(tmp_path: Path) -> None:
	"""Аватар скачивается в файл; отсутствие аватара — честный None."""
	client = _FakeClient()
	transport = _transport(client)
	await transport.start()
	target = tmp_path / "1.jpg"
	path = await transport.download_avatar("-1001234", str(target))
	assert path == str(target) and target.exists()
	client.has_avatar = False
	assert await transport.download_avatar("-1001234", str(target)) is None


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
		"-1001234", OutgoingPost(files=(OutgoingFile("v.mp4", MediaKind.VIDEO),), topic_id=7)
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

	from pxcontrol.engine.telegram.mtproto import _translate_error

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


async def test_gateway_flood_freezes_whole_account() -> None:
	"""Флуд-лимит замораживает аккаунт целиком, а не одну операцию (ADR-0024).

	Пойманный при публикации лимит действует на аккаунт: следующая
	операция того же аккаунта отказывает, **не дойдя до Telegram**
	(настойчивость удлиняет срок), а соседний аккаунт работает.
	Класс отказа — ``UserbotFloodError``: обработчики временной
	недоступности userbot продолжают его узнавать.
	"""
	from telethon import errors

	from pxcontrol.engine.telegram.gateway import TelegramGateway

	class _FloodingClient(_FakeClient):
		"""Клиент, на котором Telegram просит подождать; считает обращения."""

		def __init__(self) -> None:
			super().__init__()
			self.requests = 0

		async def send_message(self, *args: Any, **kwargs: Any) -> None:
			self.requests += 1
			raise errors.FloodWaitError(request=None, capture=45)

		async def get_input_entity(self, entity_id: Any) -> str:
			self.requests += 1
			return await super().get_input_entity(entity_id)

	flooding = _FloodingClient()
	calm = _FakeClient()
	clients: list[_FakeClient] = [flooding, calm]
	gateway = TelegramGateway()
	gateway.transport_factory = lambda: MtprotoTransport(
		client_factory=lambda a, b, c: clients.pop(0)
	)
	await gateway.activate_userbot(10, 1, "h", "s1")
	await gateway.activate_userbot(20, 1, "h", "s2")

	with pytest.raises(UserbotFloodError) as first:
		await gateway.publish(10, "-1001", OutgoingPost(text="раз"))
	assert first.value.retry_after_s == 45
	requests_after_flood = flooding.requests

	# вторая попытка тем же аккаунтом — отказ без обращения к Telegram
	with pytest.raises(UserbotFloodError) as second:
		await gateway.get_scheduled(10, "-1001")
	assert second.value.retry_after_s <= 45
	assert isinstance(second.value, UserbotUnavailableError)  # прежние ветки узнают
	assert flooding.requests == requests_after_flood  # клиента не потревожили

	# соседний аккаунт не страдает: лимит пер-аккаунтный (ADR-0019)
	await gateway.publish(20, "-1002", OutgoingPost(text="два"))
	assert len(calm.sent) == 1
	await gateway.stop()


# --- операции обслуживания сообщества (ADR-0026) ------------------------------


class _MaintenanceClient(_FakeClient):
	"""Подставной клиент для чистки: история, участники, удаление."""

	def __init__(self) -> None:
		super().__init__()
		#: страницы истории в порядке выдачи (каждая — список сообщений)
		self.history_pages: list[list[Any]] = []
		#: страницы участников: пары «участники, карточки пользователей»
		self.participant_pages: list[tuple[list[Any], list[Any]]] = []
		self.history_calls: list[tuple[int, int]] = []
		self.participant_offsets: list[int] = []
		self.deleted: list[list[int]] = []
		self.delete_error: Exception | None = None

	async def get_messages(self, entity: Any, limit: int, offset_id: int) -> list[Any]:
		self.history_calls.append((limit, offset_id))
		if not self.history_pages:
			return []
		return self.history_pages.pop(0)

	async def delete_messages(self, entity: Any, message_ids: list[int]) -> None:
		if self.delete_error is not None:
			raise self.delete_error
		self.deleted.append(list(message_ids))

	async def __call__(self, request: Any) -> Any:
		if type(request).__name__ == "GetParticipantsRequest":
			self.participant_offsets.append(request.offset)
			if not self.participant_pages:
				return SimpleNamespace(participants=[], users=[], count=0)
			participants, users = self.participant_pages.pop(0)
			return SimpleNamespace(participants=participants, users=users, count=99)
		return await super().__call__(request)


def _service_message(message_id: int, action: Any) -> Any:
	"""Служебная запись Telegram с заданным действием."""
	from telethon.tl import types

	return types.MessageService(
		id=message_id,
		peer_id=types.PeerChannel(1),
		date=datetime(2026, 9, 1, tzinfo=UTC),
		action=action,
	)


def _plain_message(message_id: int) -> Any:
	"""Обычный пост (не служебный) — чистка его не трогает."""
	from telethon.tl import types

	return types.Message(id=message_id, peer_id=types.PeerChannel(1), message="пост")


async def test_service_page_picks_only_service_messages() -> None:
	"""Со страницы истории отбираются только служебные записи."""
	from telethon.tl import types

	fake = _MaintenanceClient()
	fake.history_pages = [
		[
			_service_message(30, types.MessageActionPinMessage()),
			_plain_message(29),
			_service_message(28, types.MessageActionChatEditTitle(title="новое")),
		]
	]
	transport = _transport(fake)

	page = await transport.service_messages_page("-1001", offset_id=0, limit=100)

	assert [m.id for m in page.messages] == [30, 28]
	assert page.scanned == 3  # просмотрены все, включая обычный пост
	assert page.oldest_date == datetime(2026, 9, 1, tzinfo=UTC)


async def test_short_history_page_is_not_the_end() -> None:
	"""Короткая страница концом истории не считается.

	Telegram отдаёт меньше запрошенного и в середине истории — опора
	на «пришло меньше, чем просили» обрывала проход раньше времени
	и давала отчёт с недосчитанными записями (ADR-0026, п. 7).
	"""
	from telethon.tl import types

	fake = _MaintenanceClient()
	fake.history_pages = [[_service_message(50, types.MessageActionPinMessage())]]
	transport = _transport(fake)

	page = await transport.service_messages_page("-1001", offset_id=0, limit=100)

	assert page.next_offset_id == 50  # проход продолжится от самой старой


async def test_empty_history_page_ends_the_scan() -> None:
	"""Пустая страница — честный конец истории."""
	fake = _MaintenanceClient()
	fake.history_pages = [[]]
	transport = _transport(fake)

	page = await transport.service_messages_page("-1001", offset_id=7, limit=100)

	assert page.next_offset_id is None
	assert page.scanned == 0
	assert page.oldest_date is None


async def test_first_message_ends_the_scan() -> None:
	"""Дошли до записи номер 1 — раньше неё в чате ничего нет."""
	from telethon.tl import types

	fake = _MaintenanceClient()
	fake.history_pages = [[_service_message(1, types.MessageActionPinMessage())]]
	transport = _transport(fake)

	page = await transport.service_messages_page("-1001", offset_id=5, limit=100)

	assert page.next_offset_id is None


async def test_delete_refusal_is_a_skip_not_a_failure() -> None:
	"""Отказ Telegram удалять — пропуск пачки, а не сбой задания (ADR-0026)."""
	from telethon import errors

	fake = _MaintenanceClient()
	fake.delete_error = errors.MessageDeleteForbiddenError(request=None)
	transport = _transport(fake)

	gone = await transport.delete_messages("-1001", [11, 12, 13])

	assert gone == 0  # ни одна не удалена, но исключения нет
	assert fake.deleted == []


async def test_delete_reports_flood_to_the_caller() -> None:
	"""Флуд-лимит при удалении доходит до вызывающего — проход прекращается."""
	from telethon import errors

	fake = _MaintenanceClient()
	fake.delete_error = errors.FloodWaitError(request=None, capture=33)
	transport = _transport(fake)

	with pytest.raises(UserbotFloodError) as flood:
		await transport.delete_messages("-1001", [11])
	assert flood.value.retry_after_s == 33


async def test_participants_page_picks_deleted_and_steps_by_participants() -> None:
	"""Отбираются удалённые учётки, а смещение двигают участники.

	Карточка пользователя есть не у каждого участника, поэтому шаг
	по ``users`` уводил бы смещение назад — часть списка читалась бы
	дважды, часть не читалась бы вовсе.
	"""
	from telethon.tl import types

	fake = _MaintenanceClient()
	participants = [
		types.ChannelParticipant(user_id=n, date=datetime(2026, 9, 1, tzinfo=UTC))
		for n in (1, 2, 3)
	]
	users = [
		types.User(id=1, deleted=True, first_name=None, access_hash=111),
		types.User(id=2, deleted=False, first_name="Жив", access_hash=222),
	]
	fake.participant_pages = [(participants, users)]
	transport = _transport(fake)

	page = await transport.participants_page("-1001", offset=10, limit=200)

	assert [account.user_id for account in page.deleted] == [1]
	# хеш доступа берётся из того же ответа: без него исключить нельзя
	assert page.deleted[0].access_hash == 111
	assert page.scanned == 2
	assert page.next_offset == 13  # 10 + три участника, а не две карточки
	assert page.total == 99


async def test_empty_participants_page_ends_the_walk() -> None:
	"""Пустая страница участников — конец списка."""
	fake = _MaintenanceClient()
	fake.participant_pages = []
	transport = _transport(fake)

	page = await transport.participants_page("-1001", offset=200, limit=200)

	assert page.next_offset is None
	assert page.deleted == []


async def test_premium_upload_limit_is_a_wait_not_an_error() -> None:
	"""Лимит загрузки медиа у не-Premium аккаунта — «подождать», а не сбой.

	FLOOD_PREMIUM_WAIT приходит отдельным классом Telethon; пока его
	не узнавали, очередь хоронила пост ошибкой вместо ожидания.
	"""
	from telethon import errors

	class _PremiumLimited(_FakeClient):
		async def send_file(self, entity: Any, file: str, **kwargs: Any) -> None:
			raise errors.FloodPremiumWaitError(request=None, capture=17)

	fake = _PremiumLimited()
	transport = _transport(fake)

	with pytest.raises(UserbotFloodError) as flood:
		await transport.publish(
			"-1001",
			OutgoingPost(text="видео", files=(OutgoingFile("/tmp/x.mp4", MediaKind.VIDEO),)),
		)
	assert flood.value.retry_after_s == 17


async def test_kick_participant_survives_dict_response() -> None:
	"""Ответ Telethon на исключение приходит словарём, а не сообщением.

	``kick_participant`` разбирает обновления методом
	``_get_response_message(None, …)``, а тот при пустом запросе
	возвращает отображение «id → сообщение» (так сказано в его
	docstring). Служебную запись он отдаёт отдельной веткой напрямую,
	а когда записи нет вовсе — остаётся пустой словарь. Обращение
	к нему как к сообщению роняло чистку удалённых аккаунтов
	с «'dict' object has no attribute 'id'» (2026-09-12).
	"""
	from types import SimpleNamespace

	from pxcontrol.engine.telegram.types import DeletedAccount

	class _KickingClient(_FakeClient):
		"""Клиент, возвращающий ответ в заданном виде."""

		def __init__(self, produced: Any) -> None:
			super().__init__()
			self.produced = produced
			self.kicked: list[Any] = []

		async def kick_participant(self, entity: Any, user: Any) -> Any:
			self.kicked.append(user)
			return self.produced

	account = DeletedAccount(777, access_hash=42)

	# 1. словарь с записью — берём её идентификатор
	client = _KickingClient({314: SimpleNamespace(id=314)})
	transport = _transport(client)
	await transport.start()
	assert await transport.kick_participant("-1001", account) == 314

	# 2. пустой словарь: записи не было — это не ошибка
	client = _KickingClient({})
	transport = _transport(client)
	await transport.start()
	assert await transport.kick_participant("-1001", account) is None

	# 3. сообщение напрямую (ветка «kicking users» в Telethon)
	client = _KickingClient(SimpleNamespace(id=515))
	transport = _transport(client)
	await transport.start()
	assert await transport.kick_participant("-1001", account) == 515

	# 4. None — тоже допустимый ответ
	client = _KickingClient(None)
	transport = _transport(client)
	await transport.start()
	assert await transport.kick_participant("-1001", account) is None


async def test_kick_participant_passes_access_hash() -> None:
	"""Ссылка на пользователя собирается из id и хеша доступа.

	Без хеша Telegram пользователя не опознаёт, а кеш клиента живёт
	в памяти сессии — надеяться на него между поиском и исключением
	нельзя.
	"""
	from telethon.tl.types import InputPeerUser

	from pxcontrol.engine.telegram.types import DeletedAccount

	class _KickingClient(_FakeClient):
		def __init__(self) -> None:
			super().__init__()
			self.kicked: list[Any] = []

		async def kick_participant(self, entity: Any, user: Any) -> Any:
			self.kicked.append(user)
			return None

	client = _KickingClient()
	transport = _transport(client)
	await transport.start()
	await transport.kick_participant("-1001", DeletedAccount(777, access_hash=42))
	peer = client.kicked[0]
	assert isinstance(peer, InputPeerUser)
	assert (peer.user_id, peer.access_hash) == (777, 42)
	# хеша нет — отдаём идентификатор, ссылку соберёт сам клиент
	await transport.kick_participant("-1001", DeletedAccount(888))
	assert client.kicked[1] == 888


def _service_like(*, id: int, text: str, date: datetime, **extra: Any) -> Any:
	"""Запись отложки: минимум полей, которые читает транспорт."""
	return SimpleNamespace(id=id, message=text, date=date, **extra)


async def test_scheduled_skips_empty_records() -> None:
	"""Пустые записи в отложках не превращаются в посты без даты.

	Telegram отдаёт `messageEmpty` вместо удалённой отложки; у такой
	записи нет даты, а тип границы обещает дату. Раньше None доезжал
	до «Расписания» и ронял сортировку списка (2026-09-12).
	"""
	from telethon.tl import types

	class _ScheduledClient(_FakeClient):
		async def __call__(self, request: Any) -> Any:
			if type(request).__name__ == "GetScheduledHistoryRequest":
				return SimpleNamespace(
					messages=[
						types.MessageEmpty(id=1, peer_id=types.PeerChannel(1)),
						_service_like(id=2, text="пост", date=datetime(2026, 9, 1, tzinfo=UTC)),
					]
				)
			return await super().__call__(request)

	transport = _transport(_ScheduledClient())
	await transport.start()
	scheduled = await transport.get_scheduled("-1001")
	assert [item.text for item in scheduled] == ["пост"]


async def test_scheduled_survives_not_modified_answer() -> None:
	"""Ответ «ничего не изменилось» приходит без списка — это не сбой."""

	class _NotModifiedClient(_FakeClient):
		async def __call__(self, request: Any) -> Any:
			if type(request).__name__ == "GetScheduledHistoryRequest":
				return SimpleNamespace(count=0)  # messages.messagesNotModified
			return await super().__call__(request)

	transport = _transport(_NotModifiedClient())
	await transport.start()
	assert await transport.get_scheduled("-1001") == []


async def test_me_treats_empty_answer_as_expired_session() -> None:
	"""Пустой ответ на «кто я» — это неавторизованная сессия, а не профиль.

	Иначе профиль аккаунта затёрся бы пустыми полями, хотя ответа
	от Telegram не было вовсе.
	"""

	class _AnonymousClient(_FakeClient):
		async def get_me(self) -> Any:
			return None

	transport = _transport(_AnonymousClient())
	await transport.start()
	with pytest.raises(UserbotSessionExpiredError):
		await transport.me()


def test_admin_rights_are_read_from_real_permissions() -> None:
	"""Права читаются верно на настоящем объекте прав Telethon.

	Тест намеренно строит не подставную заглушку, а тот самый объект,
	который приходит от библиотеки: подмена легко расходится с правдой,
	а права — основание для необратимых действий.
	"""
	from telethon.tl import types
	from telethon.tl.custom.participantpermissions import ParticipantPermissions

	from pxcontrol.engine.telegram.mtproto import has_admin_right

	def _rights(**flags: bool) -> Any:
		fields = {
			"change_info": False,
			"post_messages": False,
			"edit_messages": False,
			"delete_messages": False,
			"ban_users": False,
			"invite_users": False,
			"pin_messages": False,
			"add_admins": False,
			"anonymous": False,
			"manage_call": False,
			"other": False,
		}
		fields.update(flags)
		return types.ChatAdminRights(**fields)

	admin = ParticipantPermissions(
		types.ChannelParticipantAdmin(
			user_id=1, admin_rights=_rights(delete_messages=True), promoted_by=2, date=None
		),
		chat=False,
	)
	assert has_admin_right(admin, "delete_messages") is True
	assert has_admin_right(admin, "ban_users") is False  # выдали не всё

	# владельцу можно всё, даже если присланный набор флагов неполон:
	# в Telegram права владельца урезать нельзя, а библиотечные свойства
	# читают набор как есть и ответили бы «нельзя»
	creator = ParticipantPermissions(
		types.ChannelParticipantCreator(user_id=1, admin_rights=_rights()), chat=False
	)
	assert creator.ban_users is False  # так отвечает библиотека
	assert has_admin_right(creator, "ban_users") is True  # так отвечаем мы

	# обычный участник не может ничего
	member = ParticipantPermissions(types.ChannelParticipant(user_id=1, date=None), chat=False)
	assert has_admin_right(member, "delete_messages") is False


# --- отложенные записи: вид вложения, тема, действия ---------------------------


def _document(*attributes: Any) -> Any:
	from telethon.tl import types

	return types.MessageMediaDocument(
		document=types.Document(
			id=1,
			access_hash=2,
			file_reference=b"",
			date=datetime(2026, 1, 1, tzinfo=UTC),
			mime_type="application/octet-stream",
			size=1,
			dc_id=2,
			attributes=list(attributes),
		)
	)


def test_media_kind_of_reads_real_telethon_media() -> None:
	"""Вид вложения читается по настоящим типам Telethon, а не по заглушкам.

	Превью ссылки — не вложение (это текст); документ различается
	по атрибутам, как в ``Message.video``/``audio``; всё, чего
	приложение не создаёт (опрос), — «прочее».
	"""
	from telethon.tl import types

	assert media_kind_of(None) is MediaKind.NONE
	assert media_kind_of(types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))) is (
		MediaKind.NONE
	)
	assert media_kind_of(types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))) is MediaKind.PHOTO
	video = types.DocumentAttributeVideo(duration=1.0, w=1, h=1)
	assert media_kind_of(_document(types.DocumentAttributeFilename("a.mp4"), video)) is (
		MediaKind.VIDEO
	)
	assert media_kind_of(_document(types.DocumentAttributeAudio(duration=1))) is MediaKind.AUDIO
	assert media_kind_of(_document(types.DocumentAttributeFilename("a.pdf"))) is (
		MediaKind.DOCUMENT
	)
	assert media_kind_of(_document()) is MediaKind.DOCUMENT  # документ без атрибутов
	assert media_kind_of(types.MessageMediaGeo(geo=types.GeoPointEmpty())) is MediaKind.OTHER


async def test_scheduled_reads_id_media_and_topic() -> None:
	"""Запись отложки несёт id, вид вложения и тему форума.

	Тема — корневое сообщение (ADR-0021): у сообщения темы заголовок
	ответа с ``forum_topic`` и корнем в ``reply_to_top_id`` (ответ
	внутри темы) либо ``reply_to_msg_id`` (обычное сообщение темы).
	"""
	from telethon.tl import types

	class _ScheduledClient(_FakeClient):
		async def __call__(self, request: Any) -> Any:
			if type(request).__name__ == "GetScheduledHistoryRequest":
				return SimpleNamespace(
					messages=[
						_service_like(
							id=2,
							text="в теме",
							date=datetime(2026, 9, 1, tzinfo=UTC),
							media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)),
							reply_to=types.MessageReplyHeader(
								forum_topic=True, reply_to_msg_id=8, reply_to_top_id=40
							),
						),
						_service_like(
							id=3,
							text="обычный ответ",
							date=datetime(2026, 9, 2, tzinfo=UTC),
							reply_to=types.MessageReplyHeader(reply_to_msg_id=8),
						),
						_service_like(
							id=4,
							text="в теме без вложенности",
							date=datetime(2026, 9, 3, tzinfo=UTC),
							reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=40),
						),
					]
				)
			return await super().__call__(request)

	transport = _transport(_ScheduledClient())
	await transport.start()
	scheduled = await transport.get_scheduled("-1001")
	assert [(item.id, item.media_kind, item.topic_id) for item in scheduled] == [
		(2, MediaKind.PHOTO, 40),
		(3, MediaKind.NONE, None),
		(4, MediaKind.NONE, 40),
	]


class _ScheduledActionsClient(_FakeClient):
	"""Клиент, помнящий запросы к очереди отложенных и правки."""

	def __init__(self) -> None:
		super().__init__()
		self.requests: list[Any] = []
		self.edits: list[tuple[Any, int, str, Any]] = []
		self.edit_error: Exception | None = None
		self.by_id: dict[int, Any] = {}

	async def edit_message(self, entity: Any, message: int, text: str, **kwargs: Any) -> Any:
		if self.edit_error is not None:
			raise self.edit_error
		self.edits.append((entity, message, text, kwargs.get("schedule")))
		return SimpleNamespace(id=message)

	async def __call__(self, request: Any) -> Any:
		self.requests.append(request)
		if type(request).__name__ == "GetScheduledMessagesRequest":
			found = [self.by_id[i] for i in request.id if i in self.by_id]
			return SimpleNamespace(messages=found)
		return SimpleNamespace(updates=[])


async def test_get_scheduled_message_reads_one_or_none() -> None:
	"""Одна запись читается по id; пустышка или пустой список — None."""
	from telethon.tl import types

	client = _ScheduledActionsClient()
	client.by_id[7] = _service_like(id=7, text="одна", date=datetime(2026, 9, 1, tzinfo=UTC))
	client.by_id[8] = types.MessageEmpty(id=8, peer_id=types.PeerChannel(1))
	transport = _transport(client)
	await transport.start()
	message = await transport.get_scheduled_message("-1001", 7)
	assert message is not None and (message.id, message.text) == (7, "одна")
	assert await transport.get_scheduled_message("-1001", 8) is None  # пустышка
	assert await transport.get_scheduled_message("-1001", 9) is None  # нет вовсе
	assert [list(r.id) for r in client.requests] == [[7], [8], [9]]


async def test_edit_scheduled_passes_schedule_date() -> None:
	"""Правка идёт через editMessage с датой отложки (иначе Telegram
	искал бы запись в ленте, а не в очереди отложенных)."""
	client = _ScheduledActionsClient()
	transport = _transport(client)
	await transport.start()
	when = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
	await transport.edit_scheduled("-1001", 7, "новый текст", when)
	assert client.edits == [("entity:-1001", 7, "новый текст", when)]


async def test_edit_scheduled_treats_not_modified_as_done() -> None:
	"""«Ничего не изменилось» — не сбой: запись уже в запрошенном виде."""
	from telethon import errors

	client = _ScheduledActionsClient()
	client.edit_error = errors.MessageNotModifiedError(request=None)
	transport = _transport(client)
	await transport.start()
	await transport.edit_scheduled("-1001", 7, "тот же", datetime(2026, 9, 20, tzinfo=UTC))


async def test_scheduled_actions_translate_gone_and_bad_date() -> None:
	"""Исчезнувшая запись — свой класс; отклонённое время — понятный текст."""
	from telethon import errors

	client = _ScheduledActionsClient()
	transport = _transport(client)
	await transport.start()
	client.edit_error = errors.MessageIdInvalidError(request=None)
	with pytest.raises(UserbotMessageGoneError, match="уже нет"):
		await transport.edit_scheduled("-1001", 7, "x", datetime(2026, 9, 20, tzinfo=UTC))
	client.edit_error = errors.ScheduleDateInvalidError(request=None)
	with pytest.raises(UserbotUnavailableError, match="время"):
		await transport.edit_scheduled("-1001", 7, "x", datetime(2026, 9, 20, tzinfo=UTC))


async def test_send_now_and_delete_use_scheduled_queue_requests() -> None:
	"""«Сейчас» и удаление — собственные запросы очереди отложенных.

	Обычное ``delete_messages`` для отложек не годится: у очереди
	свои id и свой метод (``messages.deleteScheduledMessages``).
	"""
	client = _ScheduledActionsClient()
	transport = _transport(client)
	await transport.start()
	await transport.send_scheduled_now("-1001", [7, 8])
	await transport.delete_scheduled("-1001", [9])
	assert [(type(r).__name__, r.peer, list(r.id)) for r in client.requests] == [
		("SendScheduledMessagesRequest", "entity:-1001", [7, 8]),
		("DeleteScheduledMessagesRequest", "entity:-1001", [9]),
	]


async def test_gateway_paused_account_refuses_without_network() -> None:
	"""Приостановленный аккаунт (ADR-0029): отказ шлюза, Telegram не тревожится.

	Класс отказа — ``UserbotPausedError``, наследник «не подключён»:
	прежние ветки временной недоступности узнают его, а текст говорит
	не «войдите», а «возобновите». Возобновление снимает пометку,
	повторная активация возвращает аккаунт в работу.
	"""
	from pxcontrol.engine.telegram.gateway import TelegramGateway
	from pxcontrol.engine.telegram.mtproto import UserbotNotConnectedError, UserbotPausedError

	client = _FakeClient()
	gateway = TelegramGateway()
	gateway.transport_factory = lambda: MtprotoTransport(client_factory=lambda a, b, c: client)
	await gateway.activate_userbot(10, 1, "h", "s1")
	await gateway.pause_userbot(10)
	assert gateway.userbot_paused(10)
	assert client.connected is False, "транспорт закрыт"
	with pytest.raises(UserbotPausedError, match="возобновите") as refused:
		await gateway.publish(10, "-1001", OutgoingPost(text="раз"))
	assert isinstance(refused.value, UserbotNotConnectedError)
	assert client.sent == []
	assert gateway.userbot_premium(10) is False

	gateway.resume_userbot(10)
	assert not gateway.userbot_paused(10)
	await gateway.activate_userbot(10, 1, "h", "s1")
	await gateway.publish(10, "-1001", OutgoingPost(text="два"))
	assert len(client.sent) == 1
	# удаление снимает пометку: id может достаться следующей записи
	await gateway.pause_userbot(10)
	await gateway.deactivate_userbot(10)
	assert not gateway.userbot_paused(10)
	await gateway.stop()


async def test_gateway_bot_lane_freezes_after_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Бот на дорожке (ADR-0030): «подождите N секунд» замораживает бота, операции учитываются.

	Второй вызов тому же боту отказывает, не дойдя до Bot API; другой бот
	работает. Записи операций складываются в буфер шлюза с владельцем-ботом.
	"""
	from pxcontrol.engine.telegram import gateway as gateway_module
	from pxcontrol.engine.telegram.gateway import TelegramGateway
	from pxcontrol.engine.telegram.lane import LaneOwner, Outcome, OwnerKind
	from pxcontrol.engine.telegram.types import BotRef, TelegramFloodError

	calls: list[str] = []

	async def fake_send_text(
		token: str,
		chat_id: str,
		text: str,
		topic_id: int | None,
		markup: object = None,
		entities: object = (),
		preview: object = None,
	) -> int:
		calls.append(token)
		if token == "flooded":
			raise TelegramFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
		return 1

	monkeypatch.setattr(gateway_module, "send_text", fake_send_text)
	gateway = TelegramGateway()
	flooded, calm = BotRef(1, "flooded"), BotRef(2, "calm")
	with pytest.raises(TelegramFloodError):
		await gateway.bot_send_text(flooded, "-1001", "раз")
	with pytest.raises(TelegramFloodError) as refused:
		await gateway.bot_send_text(flooded, "-1001", "два")
	assert refused.value.retry_after_s <= 30
	assert calls == ["flooded"], "второй раз Bot API не тревожили"
	assert await gateway.bot_send_text(calm, "-1002", "три") == 1
	records = gateway.drain_operations()
	assert [(r.owner, r.outcome, r.wait_s) for r in records] == [
		(LaneOwner(OwnerKind.BOT, 1), Outcome.FLOOD, 30),
		(LaneOwner(OwnerKind.BOT, 2), Outcome.OK, 0),
	]
	assert gateway.drain_operations() == [], "буфер очищен"
	live = gateway.live_states()
	assert live[LaneOwner(OwnerKind.BOT, 1)].frozen_for_s > 0
	assert live[LaneOwner(OwnerKind.BOT, 2)].busy_kind is None
	gateway.restore_operations(records[:1])
	assert len(gateway.drain_operations()) == 1
	await gateway.stop()


def _post_message(
	message_id: int,
	*,
	text: str = "пост",
	media: Any = None,
	markup: Any = None,
	views: int | None = None,
	grouped_id: int | None = None,
) -> Any:
	"""Обычный пост ленты с датой (её проверяет чтение «Опубликовано»)."""
	from telethon.tl import types

	return types.Message(
		id=message_id,
		peer_id=types.PeerChannel(1),
		message=text,
		date=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
		media=media,
		reply_markup=markup,
		views=views,
		grouped_id=grouped_id,
	)


def _inline_markup(*labels: str) -> Any:
	"""Клавиатура с кнопками-ссылками в одном ряду."""
	from telethon.tl import types

	return types.ReplyInlineMarkup(
		rows=[
			types.KeyboardButtonRow(
				buttons=[types.KeyboardButtonUrl(text=label, url="https://x") for label in labels]
			)
		]
	)


def test_markup_button_count() -> None:
	"""Счёт кнопок под постом: по всем рядам; без клавиатуры — ноль."""
	from telethon.tl import types

	from pxcontrol.engine.telegram.mtproto import markup_button_count

	assert markup_button_count(None) == 0
	assert markup_button_count(_inline_markup("Открыть")) == 1
	two_rows = types.ReplyInlineMarkup(
		rows=[
			types.KeyboardButtonRow(buttons=[types.KeyboardButtonUrl(text="a", url="https://x")]),
			types.KeyboardButtonRow(
				buttons=[
					types.KeyboardButtonUrl(text="b", url="https://x"),
					types.KeyboardButtonUrl(text="c", url="https://x"),
				]
			),
		]
	)
	assert markup_button_count(two_rows) == 3


async def test_history_page_reads_posts_without_service_records() -> None:
	"""Страница ленты: посты с кнопками и просмотрами, служебные отброшены."""
	from telethon.tl import types

	fake = _MaintenanceClient()
	fake.history_pages = [
		[
			_post_message(40, text="с кнопками", markup=_inline_markup("Открыть"), views=120),
			_service_message(39, types.MessageActionPinMessage()),
			_post_message(38, text="обычный"),
		]
	]
	transport = _transport(fake)
	page = await transport.history_page("-1001", offset_id=0, limit=50)
	assert [message.id for message in page.messages] == [40, 38]
	assert page.messages[0].buttons == 1
	assert page.messages[0].views == 120
	assert page.messages[0].text == "с кнопками"
	assert page.messages[1].buttons == 0
	# служебная запись задаёт продолжение: читаем от самой старой записи
	assert page.next_offset_id == 38
	assert fake.history_calls == [(50, 0)]


async def test_history_page_reads_album_group() -> None:
	"""Номер группы альбома доезжает до ленты: без него альбом не собрать."""
	fake = _MaintenanceClient()
	fake.history_pages = [
		[
			_post_message(41, text="", grouped_id=1234567890123),
			_post_message(40, text="подпись", grouped_id=1234567890123),
			_post_message(39, text="обычный"),
		]
	]
	transport = _transport(fake)
	page = await transport.history_page("-1001", offset_id=0, limit=50)
	assert [message.group_id for message in page.messages] == [
		1234567890123,
		1234567890123,
		None,
	]


async def test_history_page_marks_end_of_feed() -> None:
	"""Конец ленты: пустая страница и пост с номером 1 — дальше нечего читать."""
	fake = _MaintenanceClient()
	fake.history_pages = [[], [_post_message(1, text="первый")]]
	transport = _transport(fake)
	assert (await transport.history_page("-1001", 0, 50)).next_offset_id is None
	last = await transport.history_page("-1001", 0, 50)
	assert [message.id for message in last.messages] == [1]
	assert last.next_offset_id is None


def test_sent_message_id_reads_raw_answer() -> None:
	"""Номер поста достаётся из сырого ответа Telegram (ADR-0033, C3).

	Приватным помощником библиотеки не пользуемся: он сломался бы молча
	при её обновлении, а номер нужен кнопкам под постом.
	"""
	from telethon.tl import types

	from pxcontrol.engine.telegram.mtproto import sent_message_id

	message = types.Message(id=77, peer_id=types.PeerChannel(1), message="пост")
	by_random = SimpleNamespace(
		updates=[
			types.UpdateMessageID(id=77, random_id=42),
			types.UpdateNewChannelMessage(message, 0, 0),
		]
	)
	assert sent_message_id(by_random, 42) == 77
	# чужой random_id — берём сам пост из обновления
	assert sent_message_id(by_random, 999) == 77
	scheduled = SimpleNamespace(updates=[types.UpdateNewScheduledMessage(message)])
	assert sent_message_id(scheduled, 1) == 77
	assert sent_message_id(SimpleNamespace(updates=[]), 1) == 0
