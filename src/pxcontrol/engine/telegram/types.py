"""Общие типы телеграм-слоя (граница «сервисы → транспорты»)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pxcontrol.engine.errors import EngineError


class TelegramFloodError(EngineError):
	"""Флуд-лимит Telegram: «подождите N секунд перед новой попыткой».

	Временное состояние, а не исход операции: сервер сам называет срок
	повтора. Очередь отправки по этому классу ждёт и повторяет
	(не ошибка элемента); переводят в него оба транспорта — Bot API
	(``TelegramRetryAfter``) и MTProto (``FloodWaitError``).

	Attributes:
		retry_after_s: сколько секунд просил подождать Telegram.
	"""

	def __init__(self, message: str, retry_after_s: int) -> None:
		super().__init__(message)
		self.retry_after_s = retry_after_s


#: Лимит Bot API на отправку файла ботом.
BOT_MAX_FILE_BYTES = 50 * 1024 * 1024

#: Лимит Telegram на файл через userbot: 4000 частей по 512 КиБ
#: (ровно 2000 МиБ — меньше «круглых» 2 ГиБ).
USERBOT_MAX_FILE_BYTES = 4000 * 512 * 1024

#: То же с подпиской Premium: 8000 частей (4000 МиБ).
USERBOT_PREMIUM_MAX_FILE_BYTES = 8000 * 512 * 1024

#: Лимит Telegram на отложенные сообщения в одном чате/канале.
#: Premium его НЕ увеличивает (даёт только повторяющиеся отложки);
#: горизонт — до года вперёд. Превышение — ошибка API SCHEDULE_TOO_MUCH.
#: Проверено 2026-08-26 (limits.tginfo.me, core.telegram.org) — ADR-0016.
TELEGRAM_MAX_SCHEDULED = 100


def userbot_max_file_bytes(premium: bool) -> int:
	"""Лимит на файл через userbot по статусу подписки аккаунта."""
	return USERBOT_PREMIUM_MAX_FILE_BYTES if premium else USERBOT_MAX_FILE_BYTES


class MediaKind(StrEnum):
	"""Тип вложения поста."""

	NONE = "none"  # чистый текст
	PHOTO = "photo"
	VIDEO = "video"
	AUDIO = "audio"
	DOCUMENT = "document"  # любой файл «как документ»


@dataclass(frozen=True)
class ChannelInfo:
	"""Канал, проверенный любым транспортом (бот или userbot).

	Attributes:
		chat_id: идентификатор канала в формате Bot API (-100…).
		title: название канала.
		username: @имя без собаки (None — приватный).
	"""

	chat_id: str
	title: str
	username: str | None


@dataclass(frozen=True)
class OutgoingPost:
	"""Исходящий пост для транспорта: текст или медиа с подписью.

	Одна сущность вместо длинного списка параметров: новые атрибуты
	поста не раздувают сигнатуры шлюза и транспорта.

	Attributes:
		text: текст поста или подпись к медиа.
		media_path: путь к файлу вложения (None — чистый текст).
		media_kind: тип вложения.
		when: момент публикации (None — «сейчас»).
		thumb_path: JPEG-миниатюра видео (None — без неё).
	"""

	text: str = ""
	media_path: str | None = None
	media_kind: MediaKind = MediaKind.NONE
	when: datetime | None = None
	thumb_path: str | None = None


@dataclass(frozen=True)
class ScheduledMessage:
	"""Отложенная запись канала, прочитанная транспортом из Telegram.

	Собственный тип границы слоёв: сырые сообщения Telethon не должны
	доезжать до сервисов.

	Attributes:
		text: текст записи (пустая строка — медиа без текста).
		scheduled_at: момент будущей публикации.
	"""

	text: str
	scheduled_at: datetime
