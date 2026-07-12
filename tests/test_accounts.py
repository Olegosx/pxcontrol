"""Тесты сервиса аккаунтов: боты, userbot-аккаунты, ключи ИИ."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.accounts import AccountsService, mask_secret
from pxcontrol.engine.telegram.bot_api import InvalidBotToken
from pxcontrol.engine.telegram.mtproto import LoginError


class _FakeLogin:
	"""Подмена пошагового входа userbot — без сети."""

	def __init__(self) -> None:
		self.cancelled: list[int] = []

	async def start(
		self, account_id: int, api_id: int, api_hash: str, phone: str
	) -> None:
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
	"""Подмена шлюза: проверка токена, вход и события — без похода в сеть."""

	def __init__(self) -> None:
		self.login = _FakeLogin()
		self.activated: tuple[int, str] | None = None

	async def activate_userbot(self, api_id: int, api_hash: str, session: str) -> None:
		self.activated = (api_id, session)

	async def check_bot_token(self, token: str) -> str:
		if token == "bad-token":
			raise InvalidBotToken("Telegram отклонил токен (Unauthorized).")
		return "test_bot"

	async def bot_events(self, token: str) -> list[str]:
		return ["01.07 12:00 — «Канал» (channel, id=-1001): статус бота «administrator»"]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}")
	await database.init()
	yield database
	await database.close()


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
	with pytest.raises(InvalidBotToken):
		await service.add_bot("Плохой", "bad-token")
	assert await service.list_bots() == []


async def test_tg_account_and_ai_key_lifecycle(db: Database) -> None:
	"""Userbot-аккаунт и ключ ИИ добавляются, маскируются и удаляются."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("Личный", "+79000000000", 12345, "hash-abc")
	assert account.logged_in is False, "до входа сессии нет"
	key = await service.add_ai_key("Основной", "sk-ant-1234567890")
	assert "sk-ant-1234567890" not in key.key_masked
	assert len(await service.list_tg_accounts()) == 1
	assert len(await service.list_ai_keys()) == 1
	await service.delete_tg_account(account.id)
	await service.delete_ai_key(key.id)
	assert await service.list_tg_accounts() == []
	assert await service.list_ai_keys() == []


async def test_bot_whereabouts(db: Database) -> None:
	"""Диагностика возвращает строки событий; неизвестный бот — ошибка."""
	service = AccountsService(db, _FakeGateway())
	bot = await service.add_bot("Публикатор", "123456:AAAbbb")
	lines = await service.bot_whereabouts(bot.id)
	assert len(lines) == 1 and "administrator" in lines[0]
	with pytest.raises(ValueError, match="Бот не найден"):
		await service.bot_whereabouts(999)


def test_mask_secret() -> None:
	"""Маска показывает только края длинного секрета."""
	assert mask_secret("1234567890ABCDEF") == "1234…CDEF"
	assert mask_secret("short") == "•••••"


async def test_login_simple(db: Database) -> None:
	"""Вход без 2FA: код подтверждён — сессия сохранена, статус обновился."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("Личный", "+79000000000", 123, "hash")
	await service.start_login(account.id)
	assert await service.confirm_login_code(account.id, "12345") is True
	updated = (await service.list_tg_accounts())[0]
	assert updated.logged_in is True


async def test_login_with_2fa(db: Database) -> None:
	"""Ветка 2FA: после кода нужен пароль, после пароля — вход выполнен."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("2FA", "+79000000001", 123, "hash")
	await service.start_login(account.id)
	assert await service.confirm_login_code(account.id, "need-2fa") is False
	assert (await service.list_tg_accounts())[0].logged_in is False
	await service.confirm_login_password(account.id, "correct-horse")
	assert (await service.list_tg_accounts())[0].logged_in is True


async def test_login_requires_phone(db: Database) -> None:
	"""Без телефона вход не начинается — понятная ошибка."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("Без номера", None, 123, "hash")
	with pytest.raises(LoginError, match="телефона"):
		await service.start_login(account.id)


async def test_login_bad_code_keeps_logged_out(db: Database) -> None:
	"""Неверный код: ошибка наружу, сессия не сохраняется."""
	service = AccountsService(db, _FakeGateway())
	account = await service.add_tg_account("Личный", "+79000000002", 123, "hash")
	await service.start_login(account.id)
	with pytest.raises(LoginError, match="Неверный код"):
		await service.confirm_login_code(account.id, "bad")
	assert (await service.list_tg_accounts())[0].logged_in is False
