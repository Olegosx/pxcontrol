"""Специальные типы колонок SQLAlchemy."""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from pxcontrol.engine.security import get_secret_store


class EncryptedStr(TypeDecorator[str]):
	"""Строка, прозрачно шифруемая на границе БД (ADR-0009).

	Код сервисов работает с обычными строками; в файле БД лежит только
	шифртекст. Ключ — в системном хранилище ОС (см. ``security.secrets``).
	"""

	impl = String
	cache_ok = True

	def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
		"""Шифрует значение перед записью в БД."""
		if value is None:
			return None
		return get_secret_store().encrypt(value)

	def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
		"""Расшифровывает значение при чтении из БД."""
		if value is None:
			return None
		return get_secret_store().decrypt(str(value))
