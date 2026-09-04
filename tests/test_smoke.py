"""Дымовые тесты каркаса: настройки и запуск/остановка движка без интерфейса."""

from __future__ import annotations

from pathlib import Path

from pxcontrol.app import run_headless
from pxcontrol.config import Settings
from pxcontrol.config.settings import get_settings


def test_settings_defaults() -> None:
	"""Настройки создаются и имеют ожидаемые значения по умолчанию."""
	settings = Settings(_env_file=None)
	assert settings.database_url.startswith("sqlite")
	assert settings.log_level == "INFO"


def test_stop_after_failed_start_is_safe(tmp_path: Path) -> None:
	"""stop() после неудачного start() не должен падать (цикл уже закрыт)."""
	import pytest

	from pxcontrol.engine import EngineWorker

	bad_url = f"sqlite+aiosqlite:///{tmp_path}/no_such_dir/x.db"
	worker = EngineWorker(Settings(_env_file=None, database_url=bad_url))
	with pytest.raises(RuntimeError):
		worker.start()
	worker.stop()  # не должно бросить исключение


def test_engine_starts_and_stops(tmp_path: Path, monkeypatch) -> None:
	"""Движок запускается в фоновом потоке и корректно останавливается."""
	db_file = tmp_path / "smoke.db"
	monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
	get_settings.cache_clear()
	run_headless(seconds=0.0)
	get_settings.cache_clear()


class _DeadClient:
	"""Подставной клиент Telethon: сети нет, подключение не удаётся."""

	async def connect(self) -> None:
		raise ConnectionError("нет сети")

	async def disconnect(self) -> None:
		return None


class _RevokedClient:
	"""Подставной клиент Telethon: сессия отозвана Telegram."""

	async def connect(self) -> None:
		return None

	async def is_user_authorized(self) -> bool:
		return False

	async def disconnect(self) -> None:
		return None


async def test_start_survives_unreachable_userbot(tmp_path: Path) -> None:
	"""Регрессия: userbot настроен, но недоступен — старт движка не падает.

	Активация по сохранённой сессии сама ловит недоступность; повторного
	подключения в ``Engine.start`` быть не должно — оно роняло приложение
	при старте без сети, а при отозванной сессии блокировало запуск
	насовсем (войти заново через «Настройки» было бы уже негде).
	"""
	from pxcontrol.engine.db.models import TgAccount, TgApiCredential
	from pxcontrol.engine.engine import Engine
	from pxcontrol.engine.telegram.mtproto import MtprotoTransport

	url = f"sqlite+aiosqlite:///{tmp_path / 'start.db'}"
	engine = Engine(Settings(_env_file=None, database_url=url))
	await engine.db.init()
	async with engine.db.session_factory() as session:
		session.add(TgApiCredential(api_id=1, api_hash="h"))
		session.add(TgAccount(label="ub", phone="+7900", session="s"))
		await session.commit()
	for client in (_DeadClient(), _RevokedClient()):
		engine.gateway.transport_factory = lambda _c=client: MtprotoTransport(
			client_factory=lambda a, b, c, _c=_c: _c
		)
		await engine.start()  # не должно бросить исключение
	await engine.stop()
