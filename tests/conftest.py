"""Общие фикстуры тестов."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import CommunityExecutor
from pxcontrol.engine.security.secrets import get_secret_store
from pxcontrol.engine.telegram.rights import (
	ALL_MEMBER_RIGHTS,
	AdminRights,
	ExecutorRights,
	MemberRights,
	ParticipantStatus,
)
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


@pytest.fixture(autouse=True)
def logs_to_tmp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
	"""Уводит журнал прогона из рабочего каталога приложения.

	``run_headless`` настраивает корневой логгер на боевой
	``logs/pxcontrol.log``, и дальше в него пишет весь прогон: в файле
	оседали сотни записей о создании временных схем, а история ротацией
	вытеснялась. Разбирать по такому журналу инцидент («почему пост
	не ушёл») нечем — а именно ради этого он и ведётся.
	"""
	from pxcontrol import logging_config

	original = logging_config.setup_logging
	tmp_log_dir = tmp_path_factory.mktemp("logs")

	def _to_tmp(level: str = "INFO", log_dir: Path | None = None) -> Path:
		# имя параметра — как у настоящей setup_logging: подмена должна
		# принимать и вызов по имени, иначе первый же такой вызов упал бы
		# TypeError далеко от причины
		return original(level, log_dir or tmp_log_dir)

	logging_config.setup_logging = _to_tmp  # type: ignore[assignment]
	import pxcontrol.app as app_module

	app_original = app_module.setup_logging
	app_module.setup_logging = _to_tmp  # type: ignore[assignment]
	try:
		yield
	finally:
		logging_config.setup_logging = original  # type: ignore[assignment]
		app_module.setup_logging = app_original  # type: ignore[assignment]


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


def community_executor(
	community_id: int,
	*,
	account_id: int | None = None,
	bot_id: int | None = None,
	kind: str = "channel",
	status: ParticipantStatus = ParticipantStatus.ADMIN,
	can_edit: bool = False,
	can_publish: bool = True,
) -> CommunityExecutor:
	"""Строка исполнителя сообщества для тестов (ADR-0035).

	Права собираются под вид сообщества: в канале публикует админ
	с правом ``post_messages``, в группе — участник, которому разрешён
	текст. ``can_publish=False`` даёт исполнителя, который состоит,
	но публиковать не может, — им проверяется новая причина ожидания.
	"""
	admin = AdminRights(
		post_messages=can_publish and kind == "channel" and status.administers,
		edit_messages=can_edit,
	)
	allowed = ALL_MEMBER_RIGHTS if status.administers else MemberRights(send_plain=can_publish)
	rights = ExecutorRights(status, admin, allowed)
	return CommunityExecutor(
		community_id=community_id,
		tg_account_id=account_id,
		bot_id=bot_id,
		status=rights.status,
		rights=rights.to_payload(),
		checked_at=datetime.now(UTC),
	)
