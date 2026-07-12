"""Настройки приложения, читаются из переменных окружения и файла ``.env``."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pxcontrol.paths import app_dir, default_db_url


class Settings(BaseSettings):
	"""Бутстрап-настройки приложения (ADR-0009).

	В ``.env`` остаётся только то, что нужно ДО открытия базы данных:
	путь к БД и технические параметры. Секреты (токены ботов, аккаунты
	MTProto, ключи ИИ) хранятся в БД в зашифрованном виде и управляются
	из интерфейса.

	БД по умолчанию — в каталоге приложения (портативный режим), из какого
	бы каталога приложение ни запускали. ``.env`` ищется там же.
	"""

	database_url: str = Field(default_factory=default_db_url)
	ffmpeg_path: str = "ffmpeg"
	tz: str = "UTC"
	log_level: str = "INFO"

	model_config = SettingsConfigDict(
		env_file=app_dir() / ".env",
		env_file_encoding="utf-8",
		extra="ignore",
	)


@lru_cache
def get_settings() -> Settings:
	"""Возвращает единственный (кэшированный) экземпляр настроек."""
	return Settings()
