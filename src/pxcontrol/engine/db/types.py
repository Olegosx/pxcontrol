"""Специальные типы и помощники на границе БД."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from pxcontrol.engine.security import get_secret_store


class EncryptedStr(TypeDecorator[str]):
	"""Строка, прозрачно шифруемая на границе БД (ADR-0009).

	Код сервисов работает с обычными строками; в файле БД лежит только
	шифртекст. Ключ — в системном хранилище ОС (см. ``security.secrets``).

	Длина в объявлении колонки (``EncryptedStr(512)``) — про открытый
	текст, ориентир для читателя схемы: шифртекст Fernet длиннее (примерно
	+35 % и постоянный довесок), но SQLite длину VARCHAR не проверяет.
	При переезде на СУБД со строгими типами длины пересчитать.
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


def as_utc(moment: datetime) -> datetime:
	"""Момент из БД → со зоной UTC.

	SQLite хранит время без зоны и возвращает наивные значения, а весь
	движок считает во «взрослом» (aware) UTC: без приведения сравнение
	с ``datetime.now(UTC)`` падает с «can't compare offset-naive and
	offset-aware». Помощник живёт здесь, потому что причина — в границе
	с базой, а не в предметной логике; прежде он существовал тремя
	копиями в сервисах.
	"""
	return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def as_utc_optional(moment: datetime | None) -> datetime | None:
	"""То же для необязательного значения (None остаётся None)."""
	return None if moment is None else as_utc(moment)
