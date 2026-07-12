"""ORM-модели. Схема согласована 03.07.2026; полное описание —
docs/05-data/data-model.md. Контентные таблицы (посты, источники, очередь)
добавляются позже миграциями, по мере появления функций (YAGNI).

Поля с типом :class:`EncryptedStr` шифруются прозрачно (ADR-0009).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from pxcontrol.engine.db.types import EncryptedStr


class Base(DeclarativeBase):
	"""Базовый класс всех ORM-моделей."""


class TimestampMixin:
	"""Общие поля времени создания и последнего изменения."""

	created_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now()
	)
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
	)


class Setting(Base):
	"""Настройка приложения: ключ → значение (JSON — любой тип)."""

	__tablename__ = "settings"

	key: Mapped[str] = mapped_column(String(128), primary_key=True)
	value: Mapped[Any] = mapped_column(JSON)


class Bot(TimestampMixin, Base):
	"""Telegram-бот для публикации. Токен шифруется."""

	__tablename__ = "bots"

	id: Mapped[int] = mapped_column(primary_key=True)
	label: Mapped[str] = mapped_column(String(128))
	token: Mapped[str] = mapped_column(EncryptedStr(512))
	username: Mapped[str | None] = mapped_column(String(255), default=None)

	channels: Mapped[list["Channel"]] = relationship(back_populates="bot")


class TgAccount(TimestampMixin, Base):
	"""Userbot-аккаунт MTProto (отдельный аккаунт, ADR-0007).

	``session`` — строка сессии, секрет уровня пароля; заполняется после
	входа по номеру телефона. ``api_hash`` и ``session`` шифруются.
	"""

	__tablename__ = "tg_accounts"

	id: Mapped[int] = mapped_column(primary_key=True)
	label: Mapped[str] = mapped_column(String(128))
	phone: Mapped[str | None] = mapped_column(String(32), default=None)
	api_id: Mapped[int] = mapped_column(Integer)
	api_hash: Mapped[str] = mapped_column(EncryptedStr(512))
	session: Mapped[str | None] = mapped_column(EncryptedStr(2048), default=None)


class AiCredential(TimestampMixin, Base):
	"""Ключ провайдера ИИ. Ключ шифруется."""

	__tablename__ = "ai_credentials"

	id: Mapped[int] = mapped_column(primary_key=True)
	provider: Mapped[str] = mapped_column(String(64), default="anthropic")
	label: Mapped[str] = mapped_column(String(128), default="")
	api_key: Mapped[str] = mapped_column(EncryptedStr(512))


class VideoPreset(TimestampMixin, Base):
	"""Шаблон обработки видео (параметры из референса makeVideo).

	Переиспользуется между каналами: канал ссылается на пресет.
	"""

	__tablename__ = "video_presets"

	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(128))
	watermark_path: Mapped[str | None] = mapped_column(String(1024), default=None)
	wm_corner: Mapped[str] = mapped_column(String(2), default="tr")
	wm_margin: Mapped[int] = mapped_column(Integer, default=24)
	wm_opacity: Mapped[float] = mapped_column(Float, default=1.0)
	wm_scale: Mapped[float] = mapped_column(Float, default=0.15)
	wm_start: Mapped[float | None] = mapped_column(Float, default=None)
	wm_end: Mapped[float | None] = mapped_column(Float, default=None)
	intro: Mapped[bool] = mapped_column(Boolean, default=False)
	intro_source: Mapped[str] = mapped_column(String(255), default="random-middle")
	intro_hold: Mapped[float] = mapped_column(Float, default=1.0)
	xfade: Mapped[float] = mapped_column(Float, default=0.5)
	cover: Mapped[bool] = mapped_column(Boolean, default=False)
	no_audio: Mapped[bool] = mapped_column(Boolean, default=False)

	channels: Mapped[list["Channel"]] = relationship(back_populates="video_preset")


class Channel(TimestampMixin, Base):
	"""Подключённый Telegram-канал.

	Ссылается на бота-публикатора и пресет видео. Оба поля допускают
	``NULL``: канал можно завести до настройки бота/пресета.
	"""

	__tablename__ = "channels"

	id: Mapped[int] = mapped_column(primary_key=True)
	title: Mapped[str] = mapped_column(String(255))
	tg_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
	username: Mapped[str | None] = mapped_column(String(255), default=None)
	enabled: Mapped[bool] = mapped_column(Boolean, default=True)
	bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), default=None)
	video_preset_id: Mapped[int | None] = mapped_column(
		ForeignKey("video_presets.id"), default=None
	)

	bot: Mapped["Bot | None"] = relationship(back_populates="channels")
	video_preset: Mapped["VideoPreset | None"] = relationship(back_populates="channels")
