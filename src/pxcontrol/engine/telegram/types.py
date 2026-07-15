"""Общие типы телеграм-слоя (граница «сервисы → транспорты»)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class UserbotChannelInfo:
	"""Канал, проверенный через userbot (MTProto).

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
		media_kind: тип вложения: photo/video/audio/document/none.
		when: момент публикации (None — «сейчас»).
		thumb_path: JPEG-миниатюра видео (None — без неё).
	"""

	text: str = ""
	media_path: str | None = None
	media_kind: str = "none"
	when: datetime | None = None
	thumb_path: str | None = None
