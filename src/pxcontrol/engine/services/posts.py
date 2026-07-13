"""Сервис постов: fire-and-forget, источник истины — сам канал (ADR-0010).

Приложение создаёт пост и отправляет: «сейчас» — через бота (Bot API),
отложенно — userbot создаёт отложенную запись прямо в канале, дальше её
хранит и публикует сервер Telegram. Локальной таблицы постов нет;
страница «Расписание» читает отложенные из Telegram.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Channel

logger = logging.getLogger(__name__)

#: Минимальный запас до времени публикации (Telegram не берёт «почти сейчас»).
MIN_SCHEDULE_AHEAD = timedelta(seconds=60)

#: Колбэк прогресса загрузки: доля 0.0..1.0.
ProgressCallback = Callable[[float], None]


class PostError(Exception):
	"""Ошибка создания/отправки поста (с понятным человеку текстом)."""


class _PostPort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def send_text(self, token: str, chat_id: str, text: str) -> int: ...

	async def schedule_post(
		self, chat_id: str, text: str, when: datetime
	) -> None: ...

	async def send_video(
		self, chat_id: str, video_path: str, caption: str,
		when: datetime | None, on_progress: ProgressCallback | None,
	) -> None: ...

	async def get_scheduled(self, chat_id: str) -> list[Any]: ...


@dataclass(frozen=True)
class ScheduledPostDto:
	"""Отложенная запись канала (прочитана из Telegram) для интерфейса."""

	channel_title: str
	text_preview: str
	scheduled_at: datetime


class PostsService:
	"""Создание постов: сразу через бота или отложенно через userbot."""

	def __init__(self, db: Database, gateway: _PostPort) -> None:
		self._db = db
		self._gateway = gateway

	async def send_now(self, channel_id: int, text: str) -> int:
		"""Публикует пост немедленно через бота канала.

		Returns:
			ID сообщения в Telegram.

		Raises:
			PostError: Канал не найден или у него не назначен бот.
		"""
		channel = await self._get_channel(channel_id)
		if channel.bot is None:
			raise PostError("У канала не назначен бот — переподключите канал.")
		message_id = await self._gateway.send_text(
			channel.bot.token, channel.tg_chat_id, text
		)
		logger.info(
			"Пост опубликован в «%s» (message_id=%s).", channel.title, message_id
		)
		return message_id

	async def schedule(self, channel_id: int, text: str, when: datetime) -> None:
		"""Создаёт отложенную запись в канале (публикует сервер Telegram).

		Raises:
			PostError: Время не в будущем или канал не найден.
			UserbotUnavailable: Userbot не подключён / не админ канала.
		"""
		if when.astimezone(UTC) - datetime.now(UTC) < MIN_SCHEDULE_AHEAD:
			raise PostError("Время публикации должно быть хотя бы на минуту в будущем.")
		channel = await self._get_channel(channel_id)
		await self._gateway.schedule_post(channel.tg_chat_id, text, when)
		logger.info("Отложенный пост создан в «%s» на %s.", channel.title, when)

	async def send_video(
		self,
		channel_id: int,
		video_path: str,
		caption: str = "",
		when: datetime | None = None,
		on_progress: ProgressCallback | None = None,
	) -> None:
		"""Публикует видео в канал: сразу (when=None) или отложенно.

		Оба режима идут через userbot (MTProto): лимит Bot API на отправку
		файлов ботом — 50 МБ, обработанные видео значительно больше.

		Raises:
			PostError: Файл/канал не найдены или время не в будущем.
			UserbotUnavailable: Userbot не подключён / не админ канала.
		"""
		if not Path(video_path).is_file():
			raise PostError(f"Видеофайл не найден: {video_path}")
		if when is not None:
			if when.astimezone(UTC) - datetime.now(UTC) < MIN_SCHEDULE_AHEAD:
				raise PostError(
					"Время публикации должно быть хотя бы на минуту в будущем."
				)
		channel = await self._get_channel(channel_id)
		await self._gateway.send_video(
			channel.tg_chat_id, video_path, caption, when, on_progress
		)
		logger.info(
			"Видео %s → «%s» (%s).", Path(video_path).name, channel.title,
			f"отложено на {when}" if when else "опубликовано",
		)

	async def list_scheduled(self) -> list[ScheduledPostDto]:
		"""Собирает отложенные записи всех активных каналов из Telegram."""
		async with self._db.session_factory() as session:
			channels = (
				(await session.execute(
					select(Channel).where(Channel.enabled).order_by(Channel.id)
				)).scalars().all()
			)
		items: list[ScheduledPostDto] = []
		for channel in channels:
			for message in await self._gateway.get_scheduled(channel.tg_chat_id):
				items.append(self._dto(channel.title, message))
		items.sort(key=lambda item: item.scheduled_at)
		return items

	async def _get_channel(self, channel_id: int) -> Channel:
		"""Возвращает канал с ботом или объясняет, что канал не найден."""
		async with self._db.session_factory() as session:
			channel = (
				await session.execute(
					select(Channel)
					.options(selectinload(Channel.bot))
					.where(Channel.id == channel_id)
				)
			).scalar_one_or_none()
		if channel is None:
			raise PostError("Канал не найден — обновите список каналов.")
		return channel

	@staticmethod
	def _dto(channel_title: str, message: Any) -> ScheduledPostDto:
		text = getattr(message, "message", "") or "(медиа без текста)"
		preview = text if len(text) <= 80 else f"{text[:77]}…"
		return ScheduledPostDto(channel_title, preview, message.date)
