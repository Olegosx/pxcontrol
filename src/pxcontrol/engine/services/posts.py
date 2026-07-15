"""Сервис постов: fire-and-forget, источник истины — сам канал (ADR-0010).

Публикация любого контента — единой сущностью ``PostDraft`` через userbot
(ADR-0011): «сейчас» — обычная отправка, отложенно — запись прямо в канале
(её хранит и публикует сервер Telegram). Локальной таблицы постов нет;
страница «Расписание» читает отложенные из Telegram. Путь через бота
(``send_now``) законсервирован для будущей генерации ИИ.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
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
from pxcontrol.engine.telegram.types import OutgoingPost
from pxcontrol.engine.video.frames import make_thumbnail, resolve_timestamp
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video

logger = logging.getLogger(__name__)

#: Минимальный запас до времени публикации (Telegram не берёт «почти сейчас»).
MIN_SCHEDULE_AHEAD = timedelta(seconds=60)

#: Лимит Bot API на отправку файла ботом.
BOT_MAX_FILE_BYTES = 50 * 1024 * 1024

#: Колбэк прогресса загрузки: доля 0.0..1.0.
ProgressCallback = Callable[[float], None]


@dataclass(frozen=True)
class PublishCapabilities:
	"""Возможности публикации канала (из способов администрирования).

	Attributes:
		userbot: полный набор — любые типы, до 2 ГБ, «сейчас» и отложенные.
		bot: запасной путь — текст и медиа до 50 МБ, только «сейчас».
	"""

	userbot: bool
	bot: bool


def publish_capabilities(
	bot_assigned: bool, userbot_admin: bool
) -> PublishCapabilities:
	"""Возможности публикации по способам администрирования канала.

	Единственный источник правды для движка и интерфейса; приоритет
	транспорта — MTProto (ADR-0011).
	"""
	return PublishCapabilities(userbot=userbot_admin, bot=bot_assigned)


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
		rename_to: новое имя файла (без пути) перед отправкой; вместе
			с файлом переименовывается его кадр-превью (сосед ``.png``).
	"""

	channel_id: int
	text: str = ""
	media_path: str | None = None
	media_kind: MediaKind = MediaKind.NONE
	when: datetime | None = None
	rename_to: str | None = None


class _PostPort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	async def send_text(self, token: str, chat_id: str, text: str) -> int: ...

	async def publish(
		self, chat_id: str, post: OutgoingPost,
		on_progress: ProgressCallback | None,
	) -> None: ...

	async def send_media(
		self, token: str, chat_id: str, kind: str, path: str, caption: str
	) -> int: ...

	async def get_scheduled(self, chat_id: str) -> list[Any]: ...


@dataclass(frozen=True)
class ScheduledPostDto:
	"""Отложенная запись канала (прочитана из Telegram) для интерфейса."""

	channel_title: str
	text_preview: str
	scheduled_at: datetime


class PostsService:
	"""Публикация постов через userbot; путь через бота законсервирован."""

	def __init__(
		self, db: Database, gateway: _PostPort, ffmpeg_path: str = "ffmpeg"
	) -> None:
		self._db = db
		self._gateway = gateway
		self._ffmpeg = ffmpeg_path  # для миниатюры видео

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
		"""Публикует черновик: userbot в приоритете, бот — запасной путь.

		Единый вход для всех типов контента. Транспорт выбирается по
		возможностям канала (:func:`publish_capabilities`): userbot —
		полный набор; только бот — текст и медиа до 50 МБ, «сейчас».
		``on_progress`` получает долю загрузки файла 0.0..1.0
		(бот-путь прогресс не отдаёт).

		Raises:
			PostError: Черновик/канал/файл не годятся или у канала
				нет способа публикации.
			UserbotUnavailable: Userbot отвалился по дороге.
		"""
		self._validate(draft)
		channel = await self._get_channel(draft.channel_id)
		caps = publish_capabilities(channel.bot is not None, channel.userbot_admin)
		media_path = draft.media_path
		if media_path is not None and draft.rename_to:
			media_path = self._apply_rename(media_path, draft.rename_to)
		if caps.userbot:
			await self._publish_userbot(channel, draft, media_path, on_progress)
		elif caps.bot:
			await self._publish_bot(channel, draft, media_path)
		else:
			raise PostError(
				"У канала нет способа публикации — проверьте доступы "
				"на странице «Каналы»."
			)
		logger.info(
			"Пост (%s) → «%s» (%s, %s).",
			draft.media_kind if draft.media_path else "текст", channel.title,
			"userbot" if caps.userbot else "бот",
			f"отложено на {draft.when}" if draft.when else "опубликовано",
		)

	async def _publish_userbot(
		self,
		channel: Channel,
		draft: PostDraft,
		media_path: str | None,
		on_progress: ProgressCallback | None,
	) -> None:
		"""Полный путь через userbot (MTProto): всё, включая отложенные."""
		with tempfile.TemporaryDirectory() as tmp:
			thumb: str | None = None
			if draft.media_kind is MediaKind.VIDEO and media_path:
				thumb = await asyncio.to_thread(
					self._video_thumbnail, media_path, tmp
				)
			post = OutgoingPost(
				text=draft.text, media_path=media_path,
				media_kind=draft.media_kind, when=draft.when, thumb_path=thumb,
			)
			await self._gateway.publish(channel.tg_chat_id, post, on_progress)

	async def _publish_bot(
		self, channel: Channel, draft: PostDraft, media_path: str | None
	) -> None:
		"""Запасной путь через бота: текст и медиа до 50 МБ, только «сейчас».

		Raises:
			PostError: Отложенный пост или файл больше лимита Bot API.
		"""
		if draft.when is not None:
			raise PostError(
				"Отложенные посты требуют userbot-админа в канале — "
				"через бота доступно только «сейчас»."
			)
		assert channel.bot is not None  # гарантировано publish_capabilities
		if media_path is None:
			await self._gateway.send_text(
				channel.bot.token, channel.tg_chat_id, draft.text
			)
			return
		if Path(media_path).stat().st_size > BOT_MAX_FILE_BYTES:
			raise PostError(
				"Файл больше 50 МБ — лимит отправки ботом. Добавьте "
				"userbot администратором канала или уменьшите файл."
			)
		await self._gateway.send_media(
			channel.bot.token, channel.tg_chat_id,
			draft.media_kind, media_path, draft.text,
		)

	@staticmethod
	def _apply_rename(media_path: str, rename_to: str) -> str:
		"""Переименовывает файл (и его кадр-превью) перед отправкой.

		Returns:
			Путь к файлу под новым именем (папка не меняется).

		Raises:
			PostError: Имя содержит путь или целевое имя уже занято.
		"""
		if "/" in rename_to or "\\" in rename_to:
			raise PostError("Новое имя файла не должно содержать путь.")
		source = Path(media_path)
		target = source.with_name(rename_to)
		if target == source:
			return str(source)
		if target.exists():
			raise PostError(f"Файл «{rename_to}» уже существует — смените имя.")
		source.rename(target)
		preview = source.with_suffix(".png")
		if preview.is_file():
			preview.rename(target.with_suffix(".png"))
		logger.info("Файл переименован: %s → %s", source.name, target.name)
		return str(target)

	def _video_thumbnail(self, video_path: str, tmp_dir: str) -> str | None:
		"""Готовит JPEG-миниатюру видео для Telegram (вписана в 320×320).

		Источник: кадр-превью конвейера (сосед видео с расширением .png),
		а без него — случайный кадр из середины видео. Миниатюра —
		вспомогательная: любой сбой не мешает публикации (None + лог).
		"""
		thumb = str(Path(tmp_dir) / "thumb.jpg")
		preview = Path(video_path).with_suffix(".png")
		try:
			if preview.is_file():
				make_thumbnail(str(preview), thumb, self._ffmpeg)
			else:
				info = probe_video(video_path, ffprobe_bin_for(self._ffmpeg))
				timestamp = resolve_timestamp("random-middle", info)
				make_thumbnail(video_path, thumb, self._ffmpeg, timestamp)
		except (OSError, RuntimeError, ValueError):
			logger.warning(
				"Миниатюра для %s не получилась — публикуем без неё.",
				video_path, exc_info=True,
			)
			return None
		return thumb

	@staticmethod
	def _validate(draft: PostDraft) -> None:
		"""Отклоняет пустой черновик, битый путь и время «почти сейчас».

		Raises:
			PostError: Черновик не готов к отправке.
		"""
		if not draft.text and draft.media_path is None:
			raise PostError("Пост пуст — добавьте текст или файл.")
		if draft.media_path is not None and draft.media_kind is MediaKind.NONE:
			raise PostError("У вложения не указан тип контента.")
		if draft.media_path is not None and not Path(draft.media_path).is_file():
			raise PostError(f"Файл не найден: {draft.media_path}")
		when = draft.when
		if when is not None and when.astimezone(UTC) - datetime.now(UTC) < MIN_SCHEDULE_AHEAD:
			raise PostError("Время публикации должно быть хотя бы на минуту в будущем.")

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
