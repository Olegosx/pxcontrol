"""Тесты кэша статистики сообществ: TTL, флуд-дисциплина, аватары."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Bot, Community, TgAccount
from pxcontrol.engine.services.community_stats import CommunityStatsService
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.mtproto import UserbotFloodError, UserbotNotConnectedError
from pxcontrol.engine.telegram.types import CommunityStatsInfo, ScheduledMessage


class _FakeStatsGateway:
	"""Подставной шлюз статистики: значения и счётчики вызовов."""

	def __init__(self) -> None:
		self.participants = 1000
		self.online: int | None = 5
		self.scheduled = 3
		self.has_avatar = True
		self.fail_userbot = False  # имитация «нет связи»
		self.flood_accounts: set[int] = set()
		self.stats_calls: list[int] = []
		self.bot_calls: list[str] = []

	def _check(self, account_id: int) -> None:
		if account_id in self.flood_accounts:
			raise UserbotFloodError("Telegram просит подождать 30 с.", retry_after_s=30)
		if self.fail_userbot:
			raise UserbotNotConnectedError("Нет связи с Telegram.")

	async def userbot_community_stats(self, account_id: int, chat_id: str) -> CommunityStatsInfo:
		self._check(account_id)
		self.stats_calls.append(account_id)
		return CommunityStatsInfo(participants=self.participants, online=self.online)

	async def userbot_avatar(self, account_id: int, chat_id: str, target: str) -> str | None:
		self._check(account_id)
		if not self.has_avatar:
			return None
		Path(target).write_bytes(b"jpg")
		return target

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]:
		self._check(account_id)
		return [
			ScheduledMessage(text=f"пост {i}", scheduled_at=datetime(2026, 9, 9, tzinfo=UTC))
			for i in range(self.scheduled)
		]

	async def bot_member_count(self, token: str, chat_id: str) -> int:
		self.bot_calls.append(token)
		return 77


async def _add_community(
	db: Database,
	chat_id: str,
	account_id: int | None = None,
	bot_id: int | None = None,
) -> int:
	async with db.session_factory() as session:
		community = Community(
			title=f"Сообщество {chat_id}",
			tg_chat_id=chat_id,
			kind="channel",
			default_tg_account_id=account_id,
			bot_id=bot_id,
		)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def _add_account(db: Database) -> int:
	async with db.session_factory() as session:
		account = TgAccount(label="ub", phone="+7900", session="s")
		session.add(account)
		await session.commit()
		await session.refresh(account)
		return account.id


def _service(db: Database, gateway: _FakeStatsGateway, tmp_path: Path) -> CommunityStatsService:
	return CommunityStatsService(db, gateway, avatars_dir=tmp_path / "avatars")


async def test_refresh_fills_cache_and_snapshot(db: Database, tmp_path: Path) -> None:
	"""Проход заполняет кэш: подписчики, онлайн, отложенные, аватар."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_stale() is True
	row = (await service.snapshot())[0]
	assert row.community_id == community_id
	assert row.participants == 1000 and row.online == 5
	assert row.scheduled_count == 3
	assert row.avatar_path is not None and Path(row.avatar_path).exists()
	assert row.fetched_at is not None


async def test_refresh_respects_ttl(db: Database, tmp_path: Path) -> None:
	"""Свежий кэш не перечитывается; нулевой TTL — перечитывается."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_stale() is True
	assert await service.refresh_stale() is False, "кэш свеж — сеть не трогается"
	assert len(gateway.stats_calls) == 1
	assert await service.refresh_stale(ttl_s=0) is True
	assert len(gateway.stats_calls) == 2


async def test_flood_skips_rest_of_account(db: Database, tmp_path: Path) -> None:
	"""Флуд-лимит аккаунта пропускает его остальные сообщества (ADR-0017)."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	await _add_community(db, "-1002", account_id)
	gateway.flood_accounts.add(account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_stale() is False
	assert gateway.stats_calls == [], "после флуда аккаунт не опрашивается"


async def test_failure_keeps_previous_values(db: Database, tmp_path: Path) -> None:
	"""Сбой сети не затирает собранное ранее."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_stale() is True
	gateway.fail_userbot = True
	assert await service.refresh_stale(ttl_s=0) is False
	row = (await service.snapshot())[0]
	assert row.participants == 1000 and row.scheduled_count == 3


async def test_bot_only_community_gets_member_count(db: Database, tmp_path: Path) -> None:
	"""Сообщество только с ботом получает число участников бот-путём."""
	gateway = _FakeStatsGateway()
	async with db.session_factory() as session:
		bot = Bot(label="b", token="123456:AAAbbb")
		session.add(bot)
		await session.commit()
		await session.refresh(bot)
	await _add_community(db, "-1001", account_id=None, bot_id=bot.id)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_stale() is True
	row = (await service.snapshot())[0]
	assert row.participants == 77
	assert row.scheduled_count is None and row.avatar_path is None
	assert gateway.bot_calls == ["123456:AAAbbb"]


async def test_disabled_community_not_polled(db: Database, tmp_path: Path) -> None:
	"""Выключенное сообщество не опрашивается."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	await SettingsService(db).set_for(COMMUNITY_ENABLED, community_id, False)
	service = _service(db, gateway, tmp_path)
	assert await service.refresh_stale() is False
	assert gateway.stats_calls == []


async def test_avatar_absence_and_drop(db: Database, tmp_path: Path) -> None:
	"""Нет аватара — NULL и удаление старого файла; drop чистит кэш."""
	gateway = _FakeStatsGateway()
	account_id = await _add_account(db)
	community_id = await _add_community(db, "-1001", account_id)
	service = _service(db, gateway, tmp_path)
	await service.refresh_stale()
	avatar = (await service.snapshot())[0].avatar_path
	assert avatar is not None and Path(avatar).exists()
	# аватар свеж (моложе суток) — второй проход его не перекачивает
	gateway.has_avatar = False
	await service.refresh_stale(ttl_s=0)
	assert (await service.snapshot())[0].avatar_path == avatar
	# состарим файл руками — теперь отсутствие аватара честно фиксируется
	import os

	old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
	os.utime(avatar, (old, old))
	await service.refresh_stale(ttl_s=0)
	assert (await service.snapshot())[0].avatar_path is None
	assert not Path(avatar).exists()
	# drop убирает файл удаляемого сообщества
	gateway.has_avatar = True
	await service.refresh_stale(ttl_s=0)
	restored = (await service.snapshot())[0].avatar_path
	assert restored is not None and Path(restored).exists()
	await service.drop(community_id)
	assert not Path(restored).exists()
