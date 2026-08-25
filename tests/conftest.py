"""Общие фикстуры тестов."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.security.secrets import get_secret_store
from pxcontrol.engine.video import ProcessingOptions


class FakeProcessor:
	"""Подмена process(): фиксирует параметры, создаёт файл результата.

	Общая для тестов сервиса видео и очереди обработки (раньше дублировалась
	в обоих файлах дословно).
	"""

	def __init__(self) -> None:
		self.calls: list[ProcessingOptions] = []

	def __call__(self, options: ProcessingOptions, on_progress: object = None) -> None:
		self.calls.append(options)
		if callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		Path(options.output).parent.mkdir(parents=True, exist_ok=True)
		Path(options.output).write_bytes(b"video")


class MemoryKeyring(KeyringBackend):
	"""Хранилище ключей в памяти — подмена системного в тестах."""

	priority = 1

	def __init__(self) -> None:
		super().__init__()
		self._data: dict[tuple[str, str], str] = {}

	def get_password(self, service: str, username: str) -> str | None:
		return self._data.get((service, username))

	def set_password(self, service: str, username: str, password: str) -> None:
		self._data[(service, username)] = password

	def delete_password(self, service: str, username: str) -> None:
		self._data.pop((service, username), None)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями (общая для всех файлов тестов)."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
	await database.init()
	yield database
	await database.close()


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[None]:
	"""Подменяет системное хранилище на память и сбрасывает кэш ключа."""
	previous = keyring.get_keyring()
	keyring.set_keyring(MemoryKeyring())
	get_secret_store.cache_clear()
	yield
	keyring.set_keyring(previous)
	get_secret_store.cache_clear()
