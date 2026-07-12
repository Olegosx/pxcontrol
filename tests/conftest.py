"""Общие фикстуры тестов."""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend

from pxcontrol.engine.security.secrets import get_secret_store


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


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[None]:
	"""Подменяет системное хранилище на память и сбрасывает кэш ключа."""
	previous = keyring.get_keyring()
	keyring.set_keyring(MemoryKeyring())
	get_secret_store.cache_clear()
	yield
	keyring.set_keyring(previous)
	get_secret_store.cache_clear()
