"""Кэш статистики сообществ: подписчики, онлайн, отложенные, аватар.

Дашборд рисует карточки мгновенно из этого кэша; обновление идёт фоном
с TTL. Флуд-лимит действует на аккаунт целиком, и помнит об этом дорожка
аккаунта (ADR-0024): остальные его сообщества получат мгновенный отказ,
не тревожа Telegram, — своего списка «провинившихся» проходу вести
не нужно. Сбои сети кэш не затирают — остаются прежние значения.

Аватары хранятся файлами в каталоге кэша (в БД — только путь); файл
перекачивается, когда его нет или он старше суток (аватары меняются
редко, гонять скачивание каждый проход незачем).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, CommunityStats
from pxcontrol.engine.services.settings import COMMUNITY_ENABLED, SettingsService
from pxcontrol.engine.telegram.types import (
	CommunityStatsInfo,
	ScheduledMessage,
	TelegramFloodError,
)
from pxcontrol.paths import cache_dir

logger = logging.getLogger(__name__)

#: Свежесть кэша статистики: моложе — сообщество в проходе пропускается.
STATS_TTL_S = 15 * 60

#: Свежесть файла аватара: старше — перекачивается (по mtime файла).
AVATAR_TTL_S = 24 * 60 * 60


class _StatsGateway(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def userbot_community_stats(
		self, account_id: int, chat_id: str
	) -> CommunityStatsInfo: ...

	async def userbot_avatar(self, account_id: int, chat_id: str, target: str) -> str | None: ...

	async def get_scheduled(self, account_id: int, chat_id: str) -> list[ScheduledMessage]: ...

	async def bot_member_count(self, token: str, chat_id: str) -> int: ...


@dataclass(frozen=True)
class CommunityStatsDto:
	"""Снимок статистики сообщества для интерфейса (из кэша).

	None в поле — данные ещё не получены или Telegram их не отдаёт
	(онлайн есть только у групп; у бот-сообществ нет аватара и отложек).
	"""

	community_id: int
	participants: int | None
	online: int | None
	scheduled_count: int | None
	avatar_path: str | None
	fetched_at: datetime | None


def _aware(moment: datetime | None) -> datetime | None:
	"""Момент из БД → aware-UTC (SQLite возвращает наивные значения)."""
	if moment is None or moment.tzinfo is not None:
		return moment
	return moment.replace(tzinfo=UTC)


class CommunityStatsService:
	"""Читает кэш статистики и обновляет его из Telegram по TTL."""

	def __init__(
		self,
		db: Database,
		gateway: _StatsGateway,
		settings: SettingsService | None = None,
		avatars_dir: Path | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка (None — свой,
		для тестов); ``avatars_dir`` — каталог файлов аватаров
		(None — подпапка каталога кэша приложения)."""
		self._db = db
		self._gateway = gateway
		self._settings = settings if settings is not None else SettingsService(db)
		self._avatars_dir = avatars_dir if avatars_dir is not None else cache_dir() / "avatars"

	async def snapshot(self) -> list[CommunityStatsDto]:
		"""Текущий кэш статистики всех сообществ (без похода в сеть)."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(select(CommunityStats))).scalars().all()
			return [
				CommunityStatsDto(
					community_id=row.community_id,
					participants=row.participants,
					online=row.online,
					scheduled_count=row.scheduled_count,
					avatar_path=row.avatar_path,
					fetched_at=_aware(row.fetched_at),
				)
				for row in rows
			]

	async def refresh_stale(self, ttl_s: int = STATS_TTL_S) -> bool:
		"""Обновляет кэш сообществ со снимком старше ``ttl_s`` секунд.

		Опрашиваются только активные сообщества; сообщество с userbot —
		его аккаунтом-умолчанием (подписчики + онлайн одним запросом,
		отложенные, аватар), сообщество только с ботом — числом
		участников через бота. Отложки группы считаются по умолчанию
		сообщества: чужие (созданные другими участниками) в счёт
		не попадают — карточке важен порядок величины, точный список
		остаётся за «Расписанием».

		Returns:
			True — хоть одно сообщество обновилось (дашборду пора
			перечитать снимок), False — всё свежо или недоступно.
		"""
		now = datetime.now(UTC)
		enabled = await self._settings.get_for_all(COMMUNITY_ENABLED)
		async with self._db.session_factory() as session:
			communities = (
				(
					await session.execute(
						select(Community)
						.options(selectinload(Community.bot))
						.order_by(Community.id)
					)
				)
				.scalars()
				.all()
			)
			fresh_ids = {
				row.community_id
				for row in (await session.execute(select(CommunityStats))).scalars()
				if row.fetched_at is not None
				and (now - (_aware(row.fetched_at) or now)).total_seconds() < ttl_s
			}
			bot_tokens = {
				community.id: community.bot.token
				for community in communities
				if community.bot is not None
			}
		changed = False
		for community in communities:
			if not enabled.get(community.id, COMMUNITY_ENABLED.default):
				continue
			if community.id in fresh_ids:
				continue
			account_id = community.default_tg_account_id
			update = await self._collect(community, account_id, bot_tokens.get(community.id))
			if update is None:
				continue
			await self._store(community.id, update, now)
			changed = True
		return changed

	async def drop(self, community_id: int) -> None:
		"""Убирает файл аватара удаляемого сообщества (строку — каскад БД)."""
		target = self._avatar_target(community_id)
		removed = await asyncio.to_thread(self._remove_file, target)
		if removed:
			logger.info("Аватар сообщества id=%s удалён из кэша.", community_id)

	# --- сбор данных -------------------------------------------------------------

	async def _collect(
		self,
		community: Community,
		account_id: int | None,
		bot_token: str | None,
	) -> dict[str, object] | None:
		"""Собирает свежие значения; None — не удалось ничего.

		Каждый источник независим: сбой одного не отменяет остальные —
		уже собранное сообществу засчитывается.
		"""
		update: dict[str, object] = {}
		if account_id is not None:
			try:
				stats = await self._gateway.userbot_community_stats(
					account_id, community.tg_chat_id
				)
				update["participants"] = stats.participants
				update["online"] = stats.online
				scheduled = await self._gateway.get_scheduled(account_id, community.tg_chat_id)
				update["scheduled_count"] = len(scheduled)
				avatar_changed, avatar_path = await self._refresh_avatar(account_id, community)
				if avatar_changed:
					update["avatar_path"] = avatar_path
			except TelegramFloodError as exc:
				# дорожка аккаунта уже заморожена этим лимитом (ADR-0024):
				# остальные его сообщества получат отказ, не дойдя до сети
				logger.info(
					"Статистика «%s» пропущена: аккаунт id=%s под флуд-лимитом (%s).",
					community.title,
					account_id,
					exc,
				)
			except Exception as exc:  # noqa: BLE001 — фоновая сводка, кэш не затираем
				logger.info(
					"Статистика «%s» не обновлена (%s: %s).",
					community.title,
					type(exc).__name__,
					exc,
				)
		elif bot_token is not None:
			try:
				update["participants"] = await self._gateway.bot_member_count(
					bot_token, community.tg_chat_id
				)
			except Exception as exc:  # noqa: BLE001 — фоновая сводка, кэш не затираем
				logger.info(
					"Участники «%s» через бота не обновлены (%s: %s).",
					community.title,
					type(exc).__name__,
					exc,
				)
		return update or None

	async def _refresh_avatar(
		self, account_id: int, community: Community
	) -> tuple[bool, str | None]:
		"""Файл аватара: скачивает отсутствующий или устаревший (сутки).

		Returns:
			Пара «менять ли путь в кэше, путь»: (False, None) — файл
			свеж, качать не нужно; (True, путь) — скачан; (True, None) —
			аватара у сообщества нет, старый файл удалён.
		"""
		target = self._avatar_target(community.id)
		fresh = await asyncio.to_thread(self._file_is_fresh, target)
		if fresh:
			return False, None
		await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
		path = await self._gateway.userbot_avatar(account_id, community.tg_chat_id, str(target))
		if path is None:
			await asyncio.to_thread(self._remove_file, target)
			return True, None
		return True, path

	def _avatar_target(self, community_id: int) -> Path:
		"""Путь файла аватара сообщества в каталоге кэша."""
		return self._avatars_dir / f"{community_id}.jpg"

	@staticmethod
	def _file_is_fresh(target: Path) -> bool:
		"""Файл существует и моложе суток (по mtime)."""
		try:
			age = datetime.now(UTC).timestamp() - target.stat().st_mtime
		except OSError:
			return False
		return age < AVATAR_TTL_S

	@staticmethod
	def _remove_file(target: Path) -> bool:
		"""Удаляет файл, если он есть; ошибки — не критичны (след в логе)."""
		try:
			target.unlink()
			return True
		except FileNotFoundError:
			return False
		except OSError:
			logger.warning("Не удалось удалить файл кэша %s.", target, exc_info=True)
			return False

	async def _store(self, community_id: int, update: dict[str, object], now: datetime) -> None:
		"""Пишет собранные значения в кэш (создаёт строку при первом заходе)."""
		async with self._db.session_factory() as session:
			row = await session.get(CommunityStats, community_id)
			if row is None:
				row = CommunityStats(community_id=community_id)
				session.add(row)
			for field, value in update.items():
				setattr(row, field, value)
			row.fetched_at = now
			await session.commit()
