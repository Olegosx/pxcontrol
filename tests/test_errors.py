"""Тесты контракта читаемых ошибок: интерфейс не должен видеть дампы.

Мост интерфейса показывает пользователю текст исключения. Контракт:
доменные ошибки (EngineError) несут понятный текст и показываются
как есть; всё остальное сворачивается короткой сводкой (user_message),
а полные детали остаются в логе.
"""

from __future__ import annotations

import pytest

from pxcontrol.engine.errors import EngineError, user_message
from pxcontrol.engine.security.secrets import (
	SecretDecryptionError,
	SecretStorageError,
)
from pxcontrol.engine.services.accounts import AccountsError
from pxcontrol.engine.services.captions import CaptionsError
from pxcontrol.engine.services.channels import ChannelError
from pxcontrol.engine.services.posts import PostError
from pxcontrol.engine.services.settings import SettingsError
from pxcontrol.engine.services.video import VideoError
from pxcontrol.engine.telegram.bot_api import ChannelCheckError, InvalidBotTokenError
from pxcontrol.engine.telegram.mtproto import LoginError, UserbotUnavailableError
from pxcontrol.engine.telegram.refs import ChatRefError

#: Все доменные ошибки проекта — их текст безопасен для интерфейса.
DOMAIN_ERRORS = [
	AccountsError, CaptionsError, ChannelCheckError, ChannelError,
	ChatRefError, InvalidBotTokenError, LoginError, PostError,
	SecretDecryptionError, SecretStorageError, SettingsError,
	UserbotUnavailableError, VideoError,
]


@pytest.mark.parametrize("error_class", DOMAIN_ERRORS)
def test_domain_errors_inherit_engine_error(error_class: type) -> None:
	"""Каждая доменная ошибка — EngineError: мост покажет её как есть."""
	assert issubclass(error_class, EngineError)


def test_user_message_passes_domain_text_verbatim() -> None:
	"""Текст доменной ошибки уходит пользователю без изменений."""
	text = "Канал не найден — обновите список."
	assert user_message(ChannelError(text)) == text
	# сетевые ошибки на границах транспортов тоже несут наш текст
	assert user_message(ConnectionError("Нет связи с Telegram.")) == (
		"Нет связи с Telegram."
	)


def test_user_message_collapses_dump() -> None:
	"""Многострочный дамп (стиль ошибок СУБД) сворачивается в одну строку."""
	dump = (
		"(sqlite3.IntegrityError) FOREIGN KEY constraint failed\n"
		"[SQL: INSERT INTO caption_fields (channel_id, name) VALUES (?, ?)]\n"
		"[parameters: (99, 'Genre')]\n"
		"(Background on this error at: https://sqlalche.me/e/20/gkpj)"
	)
	message = user_message(RuntimeError(dump))
	assert "\n" not in message
	assert "Внутренняя ошибка" in message and "логе" in message
	assert "IntegrityError" in message  # суть причины видна
	assert "sqlalche.me" not in message  # хвост дампа отрезан


def test_user_message_survives_empty_text() -> None:
	"""Исключение с пустым текстом (InvalidToken) не даёт пустой ошибки."""
	message = user_message(KeyError())
	assert "Внутренняя ошибка" in message and "KeyError" in message
