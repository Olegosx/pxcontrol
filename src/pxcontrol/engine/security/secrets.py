"""Шифрование секретов: ключ — в системном хранилище ОС (ADR-0009).

Секретные поля БД шифруются схемой Fernet (симметричное шифрование из
библиотеки ``cryptography``). Ключ шифрования хранится в системном
хранилище ключей ОС через ``keyring`` (на Linux — GNOME Keyring/KWallet).
Файл БД без доступа к хранилищу ключей этой машины бесполезен.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from pxcontrol.engine.errors import EngineError

logger = logging.getLogger(__name__)

#: Имя сервиса и записи в системном хранилище ключей.
KEYRING_SERVICE = "pxcontrol"
KEYRING_KEY_NAME = "master-key"


class SecretDecryptionError(EngineError):
	"""Секрет не расшифровался: ключ не тот (с понятным человеку текстом)."""


class SecretStorageError(EngineError):
	"""Системное хранилище ключей недоступно (с понятным человеку текстом)."""


class SecretStore:
	"""Шифрует и расшифровывает секретные строки заданным ключом."""

	def __init__(self, key: bytes) -> None:
		self._fernet = Fernet(key)

	def encrypt(self, plain: str) -> str:
		"""Возвращает зашифрованное представление строки."""
		return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

	def decrypt(self, token: str) -> str:
		"""Возвращает исходную строку из зашифрованного представления.

		Raises:
			SecretDecryptionError: Шифртекст не подходит текущему ключу —
				у ``InvalidToken`` из cryptography пустой текст, и без
				перевода пользователь видел бы ошибку «ни о чём».
		"""
		try:
			return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
		except InvalidToken as exc:
			raise SecretDecryptionError(
				"Секрет зашифрован другим ключом (база перенесена с другой "
				"машины или хранилище ключей пересоздано) — введите секреты "
				"заново: Настройки → Аккаунты."
			) from exc


def _load_or_create_key() -> bytes:
	"""Читает ключ из системного хранилища; при первом запуске — создаёт.

	Raises:
		SecretStorageError: Системное хранилище ключей недоступно.
	"""
	import keyring
	from keyring.errors import KeyringError

	try:
		stored = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
		if stored is None:
			new_key = Fernet.generate_key().decode("ascii")
			keyring.set_password(KEYRING_SERVICE, KEYRING_KEY_NAME, new_key)
			logger.info("Создан новый ключ шифрования в системном хранилище.")
			return new_key.encode("ascii")
		return stored.encode("ascii")
	except KeyringError as exc:
		raise SecretStorageError(
			"Системное хранилище ключей (keyring) недоступно — не могу "
			"работать с секретами. Убедитесь, что запущен GNOME Keyring "
			"или KWallet."
		) from exc


@lru_cache
def get_secret_store() -> SecretStore:
	"""Возвращает единственный экземпляр хранилища секретов."""
	return SecretStore(_load_or_create_key())
