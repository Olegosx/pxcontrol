"""Сервис постов: fire-and-forget, источник истины — сам канал (ADR-0010).

Публикация любого контента — единой сущностью ``PostDraft`` через userbot
(ADR-0011): «сейчас» — обычная отправка, отложенно — запись прямо в канале
(её хранит и публикует сервер Telegram). Локальной таблицы постов нет;
страница «Расписание» читает отложенные из Telegram. Путь через бота
(``send_now``) законсервирован для будущей генерации ИИ.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
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


class MediaKind(StrEnum):
	"""Тип вложения поста."""

	NONE = "none"  # чистый текст
	PHOTO = "photo"
	VIDEO = "video"
	AUDIO = "audio"
	DOCUMENT = "document"  # любой файл «как документ»


@dataclass(frozen=True)
class PostDraft:
	"""Черновик публикации — единая сущность для всех типов контента.

	Attributes:
		channel_id: подключённый канал (id в нашей БД).
		text: текст поста или подпись к медиа.
		media_path: путь к файлу вложения (None — чистый текст).
		media_kind: тип вложения.
		when: момент публикации (None — «сейчас»).
	"""

	channel_id: int
	text: str = ""
	media_path: str | None = None
	media_kind: MediaKind = MediaKind.NONE
	when: datetime | None = None


class _PostPort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def send_text(self, token: str, chat_id: str, text: str) -> int: ...

	async def publish(
		self, chat_id: str, text: str, media_path: str | None,
		media_kind: str, when: datetime | None,
		on_progress: ProgressCallback | None,
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
		"""Публикует текст немедленно через бота канала (законсервировано).

		Интерфейс этот путь не использует (публикация идёт через userbot,
		ADR-0011); метод сохранён для будущей генерации ИИ ботом.

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

	async def publish(
		self, draft: PostDraft, on_progress: ProgressCallback | None = None
	) -> None:
		"""Публикует черновик через userbot: сразу или отложенно (ADR-0011).

		Единый вход для всех типов контента: текст, фото, видео, аудио,
		документ. ``on_progress`` получает долю загрузки файла 0.0..1.0.

		Raises:
			PostError: Черновик пуст, файл/канал не найдены, время не в будущем.
			UserbotUnavailable: Userbot не подключён / не админ канала.
		"""
		self._validate(draft)
		channel = await self._get_channel(draft.channel_id)
		await self._gateway.publish(
			channel.tg_chat_id, draft.text, draft.media_path,
			draft.media_kind, draft.when, on_progress,
		)
		logger.info(
			"Пост (%s) → «%s» (%s).",
			draft.media_kind if draft.media_path else "текст", channel.title,
			f"отложено на {draft.when}" if draft.when else "опубликовано",
		)

	@staticmethod
	def _validate(draft: PostDraft) -> None:
		"""Отклоняет пустой черновик, битый путь и время «почти сейчас».

		Raises:
			PostError: Черновик не готов к отправке.
		"""
		if not draft.text and draft.media_path is None:
			raise PostError("Пост пуст — добавьте текст или файл.")
		if draft.media_path is not None:
			if draft.media_kind is MediaKind.NONE:
				raise PostError("У вложения не указан тип контента.")
			if not Path(draft.media_path).is_file():
				raise PostError(f"Файл не найден: {draft.media_path}")
		if draft.when is not None:
			if draft.when.astimezone(UTC) - datetime.now(UTC) < MIN_SCHEDULE_AHEAD:
				raise PostError(
					"Время публикации должно быть хотя бы на минуту в будущем."
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
