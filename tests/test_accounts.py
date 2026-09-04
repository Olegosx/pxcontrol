"""Тесты сервиса аккаунтов: боты, userbot-аккаунты, ключи ИИ."""

from __future__ import annotations

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.accounts import (
	AccountsError,
	AccountsService,
	mask_secret,
)
from pxcontrol.engine.telegram.bot_api import InvalidBotTokenError
from pxcontrol.engine.telegram.mtproto import LoginError


class _FakeLogin:
	"""Подмена пошагового входа userbot — без сети."""

	def __init__(self) -> None:
		self.cancelled: list[int] = []

	async def start(self, account_id: int, api_id: int, api_hash: str, phone: str) -> None:
		return None

	async def confirm_code(self, account_id: int, code: str) -> str | None:
		if code == "need-2fa":
			return None
		if code == "bad":
			raise LoginError("Неверный код — начните вход заново.")
		return "session-string-ok"

	async def confirm_password(self, account_id: int, password: str) -> str:
		return "session-after-2fa"

	async def cancel(self, account_id: int) -> None:
		self.cancelled.append(account_id)


class _FakeGateway:
	"""Подмена шлюза: пул userbot-клиентов (ADR-0019) — без похода в сеть.

	``activated`` — снимок пула: id аккаунта → (api_id, сессия);
	``premium_ids`` — аккаунты «с подпиской».
	"""

	def __init__(self) -> None:
		self.login = _FakeLogin()
		self.activated: dict[int, tuple[int, str]] = {}
		self.deactivations: list[int] = []
		self.premium_ids: set[int] = set()

	def userbot_premium(self, account_id: int | None) -> bool:
		return account_id in self.premium_ids

	async def activate_userbot(
		self, account_id: int, api_id: int, api_hash: str, session: str
	) -> None:
		self.activated[account_id] = (api_id, session)

	async def deactivate_userbot(self, account_id: int) -> None:
		self.deactivations.append(account_id)
		self.activated.pop(account_id, None)

	async def check_bot_token(self, token: str) -> str:
		if token == "bad-token":
			raise InvalidBotTokenError("Telegram отклонил токен (Unauthorized).")
		return "test_bot"

	async def bot_events(self, token: str) -> list[str]:
		return ["01.07 12:00 — «Канал» (channel, id=-1001): статус бота «administrator»"]


async def test_bot_lifecycle(db: Database) -> None:
	"""Бот добавляется с проверкой токена, виден в списке, удаляется."""
	service = AccountsService(db, _FakeGateway())
	token = "123456:AAAbbbCCCddd"
	dto = await service.add_bot("Основной", token)
	assert dto.username == "test_bot"
	assert token not in dto.token_masked, "токен не должен попадать в UI целиком"
	assert [b.label for b in await service.list_bots()] == ["Основной"]
	await service.delete_bot(dto.id)
	assert await service.list_bots() == []


async def test_bad_token_not_saved(db: Database) -> None:
	"""Отклонённый токен не сохраняется в БД."""
	service = AccountsService(db, _FakeGateway())
	with pytest.raises(InvalidBotTokenError):
		await service.add_bot("Плохой", "bad-token")
	assert await service.list_bots() == []


async def test_tg_account_and_ai_key_lifecycle(db: Database) -> None:
	"""Userbot-аккаунт и ключ ИИ добавляются, маскируются и удаляются."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("Личный", "+79000000000")
	assert account.logged_in is False, "до входа сессии нет"
	key = await service.add_ai_key("Основной", "sk-ant-1234567890")
	assert "sk-ant-1234567890" not in key.key_masked
	assert len(await service.list_tg_accounts()) == 1
	assert len(await service.list_ai_keys()) == 1
	await service.delete_tg_account(account.id)
	await service.delete_ai_key(key.id)
	assert await service.list_tg_accounts() == []
	assert await service.list_ai_keys() == []


async def test_tg_api_key_roundtrip_and_masking(db: Database) -> None:
	"""Ключ API приложения: один на приложение, hash маскируется, замена — без дублей."""
	service = AccountsService(db, _FakeGateway())
	assert await service.get_tg_api() is None  # до настройки ключа нет
	saved = await service.set_tg_api(37612995, "abcdef1234567890abcdef1234567890")
	assert saved.api_id == 37612995
	assert "abcdef1234567890" not in saved.api_hash_masked, "hash не должен попадать в UI целиком"
	# повторное сохранение заменяет запись, а не плодит вторую
	updated = await service.set_tg_api(111, "another-hash-value-long-enough")
	assert updated.api_id == 111
	current = await service.get_tg_api()
	assert current is not None and current.api_id == 111


async def test_tg_api_key_validation(db: Database) -> None:
	"""Негодный ключ отклоняется: api_id — положительное число, hash — непустой."""
	service = AccountsService(db, _FakeGateway())
	with pytest.raises(AccountsError, match="api_id"):
		await service.set_tg_api(0, "hash")
	with pytest.raises(AccountsError, match="api_hash"):
		await service.set_tg_api(123, "   ")
	assert await service.get_tg_api() is None


async def test_add_tg_account_requires_label_and_phone(db: Database) -> None:
	"""Аккаунт без названия или телефона — понятная ошибка (тупик входа)."""
	service = AccountsService(db, _FakeGateway())
	with pytest.raises(AccountsError, match="название"):
		await service.add_tg_account("  ", "+7900")
	with pytest.raises(AccountsError, match="телефон"):
		await service.add_tg_account("Личный", "  ")
	assert await service.list_tg_accounts() == []


async def test_login_requires_app_api_key(db: Database) -> None:
	"""Вход без ключа приложения — понятная ошибка, куда его вписать."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("Личный", "+79000000000")
	with pytest.raises(AccountsError, match="Настройки → Общие"):
		await service.start_login(account.id)


async def test_activate_stored_userbots_needs_api_key(db: Database) -> None:
	"""Активация по сессиям без ключа приложения не падает и не активирует;
	с ключом поднимаются все аккаунты с сессиями."""
	from pxcontrol.engine.db.models import TgAccount

	gateway = _FakeGateway()
	service = AccountsService(db, gateway)
	async with db.session_factory() as session:
		one = TgAccount(label="ub1", phone="+7900", session="s1")
		two = TgAccount(label="ub2", phone="+7901", session="s2")
		three = TgAccount(label="без входа", phone="+7902")  # сессии нет
		session.add_all([one, two, three])
		await session.commit()
		await session.refresh(one)
		await session.refresh(two)
	await service.activate_stored_userbots()  # ключа нет — тихо пропускается
	assert gateway.activated == {}
	await service.set_tg_api(777, "hash-hash-hash-hash")
	await service.activate_stored_userbots()
	# подключены оба вошедших, каждый своей сессией, общим ключом
	assert gateway.activated == {one.id: (777, "s1"), two.id: (777, "s2")}


async def test_delete_tg_account_deactivates_only_it(db: Database) -> None:
	"""Удаление аккаунта гасит только его клиента; остальные живут (ADR-0019).

	Движок не должен публиковать от имени удалённого аккаунта, но и
	рвать подключения чужих аккаунтов удаление не вправе — их каналы
	продолжают отправку.
	"""
	gateway = _FakeGateway()
	service = AccountsService(db, gateway)
	await service.set_tg_api(777, "app-hash-app-hash")
	first = await service.add_tg_account("Первый", "+7900")
	second = await service.add_tg_account("Второй", "+7901")
	await service.start_login(first.id)
	assert await service.confirm_login_code(first.id, "12345") is True
	await service.start_login(second.id)
	assert await service.confirm_login_code(second.id, "12345") is True
	assert set(gateway.activated) == {first.id, second.id}  # оба в пуле

	await service.delete_tg_account(second.id)
	assert gateway.deactivations == [second.id]  # погашен только удалённый
	assert set(gateway.activated) == {first.id}  # первый работает дальше

	await service.delete_tg_account(first.id)
	assert gateway.deactivations == [second.id, first.id]
	assert gateway.activated == {}  # аккаунтов не осталось


async def test_bot_whereabouts(db: Database) -> None:
	"""Диагностика возвращает строки событий; неизвестный бот — ошибка."""
	service = AccountsService(db, _FakeGateway())
	bot = await service.add_bot("Публикатор", "123456:AAAbbb")
	lines = await service.bot_whereabouts(bot.id)
	assert len(lines) == 1 and "administrator" in lines[0]
	with pytest.raises(AccountsError, match="Бот не найден"):
		await service.bot_whereabouts(999)


def test_mask_secret() -> None:
	"""Маска показывает только края длинного секрета."""
	assert mask_secret("1234567890ABCDEF") == "1234…CDEF"
	assert mask_secret("short") == "•••••"


async def test_login_simple(db: Database) -> None:
	"""Вход без 2FA: код подтверждён — сессия сохранена, статус обновился."""
	service = AccountsService(db, _FakeGateway())
	await service.set_tg_api(123, "app-hash-app-hash")
	account = await service.add_tg_account("Личный", "+79000000000")
	await service.start_login(account.id)
	assert await service.confirm_login_code(account.id, "12345") is True
	updated = (await service.list_tg_accounts())[0]
	assert updated.logged_in is True


async def test_login_with_2fa(db: Database) -> None:
	"""Ветка 2FA: после кода нужен пароль, после пароля — вход выполнен."""
	service = AccountsService(db, _FakeGateway())
	await service.set_tg_api(123, "app-hash-app-hash")
	account = await service.add_tg_account("2FA", "+79000000001")
	await service.start_login(account.id)
	assert await service.confirm_login_code(account.id, "need-2fa") is False
	assert (await service.list_tg_accounts())[0].logged_in is False
	await service.confirm_login_password(account.id, "correct-horse")
	assert (await service.list_tg_accounts())[0].logged_in is True


async def test_login_requires_phone(db: Database) -> None:
	"""Аккаунт без телефона (старая запись в БД) — понятная ошибка входа.

	Новые аккаунты без телефона не создаются (add_tg_account требует его),
	но строка из старой схемы могла остаться — вход честно объясняет тупик.
	"""
	from pxcontrol.engine.db.models import TgAccount

	service = AccountsService(db, _FakeGateway())
	await service.set_tg_api(123, "app-hash-app-hash")
	async with db.session_factory() as session:
		legacy = TgAccount(label="Без номера", phone=None)
		session.add(legacy)
		await session.commit()
		await session.refresh(legacy)
	with pytest.raises(LoginError, match="телефона"):
		await service.start_login(legacy.id)


async def test_login_bad_code_keeps_logged_out(db: Database) -> None:
	"""Неверный код: ошибка наружу, сессия не сохраняется."""
	service = AccountsService(db, _FakeGateway())
	await service.set_tg_api(123, "app-hash-app-hash")
	account = await service.add_tg_account("Личный", "+79000000002")
	await service.start_login(account.id)
	with pytest.raises(LoginError, match="Неверный код"):
		await service.confirm_login_code(account.id, "bad")
	assert (await service.list_tg_accounts())[0].logged_in is False


async def test_premium_per_account(db: Database) -> None:
	"""Premium светится у каждого подключённого аккаунта честно (ADR-0019)."""
	gateway = _FakeGateway()
	service = AccountsService(db, gateway)
	await service.set_tg_api(777, "app-hash-app-hash")
	first = await service.add_tg_account("Первый", "+7900")
	second = await service.add_tg_account("Второй", "+7901")
	gateway.premium_ids = {second.id}
	flags = {a.id: a.premium for a in await service.list_tg_accounts()}
	assert flags == {first.id: False, second.id: True}


async def test_activate_stored_userbot_survives_wrong_secret_key(db: Database) -> None:
	"""Смена ключа шифрования не мешает запуску (обещание data-model.md).

	Сессия в БД зашифрована старым ключом: активация userbot при старте
	ловит ошибку расшифровки и выходит с предупреждением в лог —
	приложение работает дальше, ту же ошибку пользователь увидит
	на странице аккаунтов.
	"""
	import keyring

	from pxcontrol.engine.db.models import TgAccount, TgApiCredential
	from pxcontrol.engine.security.secrets import get_secret_store
	from tests.conftest import MemoryKeyring

	async with db.session_factory() as session:
		session.add(TgApiCredential(api_id=1, api_hash="h"))
		session.add(TgAccount(label="ub", phone="+7900", session="s"))
		await session.commit()
	# «перезапуск» с другим ключом: старое хранилище (и его ключ) исчезло
	keyring.set_keyring(MemoryKeyring())
	get_secret_store.cache_clear()
	gateway = _FakeGateway()
	service = AccountsService(db, gateway)
	await service.activate_stored_userbots()  # не должно бросить исключение
	assert gateway.activated == {}  # userbot не активирован, но и не упали
