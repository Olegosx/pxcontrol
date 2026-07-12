"""Тесты логирования: файл создаётся, пишет и уважает уровень."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from pxcontrol.logging_config import setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
	"""Возвращает корневой логгер в исходное состояние после теста."""
	root = logging.getLogger()
	old_handlers = list(root.handlers)
	old_level = root.level
	yield
	for handler in list(root.handlers):
		handler.close()
		root.removeHandler(handler)
	for handler in old_handlers:
		root.addHandler(handler)
	root.setLevel(old_level)


def _flush_root() -> None:
	for handler in logging.getLogger().handlers:
		handler.flush()


def test_log_file_created_and_written(tmp_path: Path) -> None:
	"""Файл лога создаётся в заданной папке и принимает сообщения."""
	log_file = setup_logging("DEBUG", log_dir=tmp_path)
	logging.getLogger("тест").warning("контрольное сообщение")
	_flush_root()
	text = log_file.read_text(encoding="utf-8")
	assert "логирование настроено" in text
	assert "контрольное сообщение" in text
	assert log_file.parent == tmp_path


def test_level_filters_messages(tmp_path: Path) -> None:
	"""Сообщения ниже установленного уровня в файл не попадают."""
	log_file = setup_logging("WARNING", log_dir=tmp_path)
	logging.getLogger("тест").info("шёпот — не должен попасть")
	logging.getLogger("тест").error("крик — должен попасть")
	_flush_root()
	text = log_file.read_text(encoding="utf-8")
	assert "шёпот" not in text
	assert "крик" in text
