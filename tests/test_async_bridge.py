"""Мост «интерфейс → движок»: журнал отказов операций.

Доменная ошибка — ожидаемый исход с одним местом возникновения
и причиной в тексте: одна строка WARNING. Неожиданное исключение —
ERROR с трейсбеком. Так ERROR в журнале значит «дефект», а не
«бота ещё не добавили в сообщество».
"""

from __future__ import annotations

import logging
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pxcontrol.engine.errors import EngineError  # noqa: E402
from pxcontrol.ui.async_bridge import _log_failure  # noqa: E402


def test_domain_error_logged_as_one_warning_line(caplog: pytest.LogCaptureFixture) -> None:
	"""Доменная ошибка и «нет связи» — WARNING без трейсбека, с классом и текстом."""
	with caplog.at_level(logging.WARNING, logger="pxcontrol.ui.async_bridge"):
		_log_failure(EngineError("Бот не видит это сообщество."))
		_log_failure(ConnectionError("Нет связи с Telegram."))
	assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING]
	assert all(r.exc_info is None for r in caplog.records), "трейсбек — только неожиданному"
	assert "EngineError" in caplog.records[0].getMessage()
	assert "не видит" in caplog.records[0].getMessage()


def test_unexpected_error_logged_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
	"""Неожиданное исключение — ERROR с трейсбеком: это дефект, его разбирают по логу."""
	with caplog.at_level(logging.ERROR, logger="pxcontrol.ui.async_bridge"):
		try:
			raise RuntimeError("что-то сломалось")
		except RuntimeError as exc:
			_log_failure(exc)
	(record,) = caplog.records
	assert record.levelno == logging.ERROR
	assert record.exc_info is not None and record.exc_info[0] is RuntimeError
