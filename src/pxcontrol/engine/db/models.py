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


class AppSetting(Base):
	"""Настройка приложения: строка «имя → значение» (ADR-0013).

	Состав, типы и умолчания задаёт реестр ключей
	(:mod:`pxcontrol.engine.services.settings`) — сюда попадают только
	имена из реестра. Секретам здесь не место (ADR-0009).
	"""

	__tablename__ = "app_settings"

	name: Mapped[str] = mapped_column(String(128), primary_key=True)
	value: Mapped[Any] = mapped_column(JSON)


class ChannelSetting(Base):
	"""Настройка канала: строка «(канал, имя) → значение» (ADR-0013).

	Внешний ключ с каскадом: настройки живут и умирают вместе с каналом
	(каскад страхует и сервис — ``ChannelsService.delete_channel``).
	"""

	__tablename__ = "channel_settings"

	channel_id: Mapped[int] = mapped_column(
		ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
	)
	name: Mapped[str] = mapped_column(String(128), primary_key=True)
	value: Mapped[Any] = mapped_column(JSON)


class Bot(TimestampMixin, Base):
	"""Telegram-бот для публикации. Токен шифруется."""

	__tablename__ = "bots"

	id: Mapped[int] = mapped_column(primary_key=True)
	label: Mapped[str] = mapped_column(String(128))
	token: Mapped[str] = mapped_column(EncryptedStr(512))
	username: Mapped[str | None] = mapped_column(String(255), default=None)

	channels: Mapped[list[Channel]] = relationship(back_populates="bot")


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
	Значения по умолчанию здесь не задаются: их единственный источник —
	``PresetFields`` (сервис видео), а запись всегда создаётся со всеми
	полями (:meth:`VideoService.save_preset`).
	"""

	__tablename__ = "video_presets"

	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(128))
	# обрезка с краёв: сколько секунд отрезать в начале и в конце (0 — нет);
	# остальные параметры считаются от обрезанной версии
	trim_start: Mapped[float] = mapped_column(Float)
	trim_end: Mapped[float] = mapped_column(Float)
	# затухание на краях итога: появление из чёрного / уход в чёрное
	# (сек; 0 — без эффекта); видео и звук вместе, к обрезке не привязано
	fade_in: Mapped[float] = mapped_column(Float)
	fade_out: Mapped[float] = mapped_column(Float)
	watermark_path: Mapped[str | None] = mapped_column(String(1024))
	wm_corner: Mapped[str] = mapped_column(String(2))
	wm_margin: Mapped[int] = mapped_column(Integer)
	wm_opacity: Mapped[float] = mapped_column(Float)
	wm_scale: Mapped[float] = mapped_column(Float)
	# окно показа вотермарка: отступ от начала и отступ ДО КОНЦА (сек)
	wm_start_offset: Mapped[float | None] = mapped_column(Float)
	wm_end_offset: Mapped[float | None] = mapped_column(Float)
	# плавность появления/исчезания на краях окна (сек; 0 — резко)
	wm_fade: Mapped[float] = mapped_column(Float)
	intro: Mapped[bool] = mapped_column(Boolean)
	intro_source: Mapped[str] = mapped_column(String(255))
	intro_hold: Mapped[float] = mapped_column(Float)
	xfade: Mapped[float] = mapped_column(Float)
	cover: Mapped[bool] = mapped_column(Boolean)
	no_audio: Mapped[bool] = mapped_column(Boolean)
	# NULL — «как в оригинале»: целевой битрейт берётся из исходника
	video_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
	# комментарий в метаданные файла (тег comment): «ссылка — описание»
	meta_comment: Mapped[str | None] = mapped_column(String(512))


class Channel(TimestampMixin, Base):
	"""Подключённый Telegram-канал.

	Ссылается на бота-публикатора (``NULL`` — канал подключён через
	userbot). Параметры-предпочтения канала — строками
	в ``channel_settings`` (ADR-0013), например пресет обработки
	по умолчанию.
	"""

	__tablename__ = "channels"

	id: Mapped[int] = mapped_column(primary_key=True)
	title: Mapped[str] = mapped_column(String(255))
	tg_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
	username: Mapped[str | None] = mapped_column(String(255), default=None)
	enabled: Mapped[bool] = mapped_column(Boolean, default=True)
	# userbot — админ канала (проверено при подключении); бот — через bot_id
	userbot_admin: Mapped[bool] = mapped_column(Boolean, default=False)
	bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), default=None)

	bot: Mapped[Bot | None] = relationship(back_populates="channels")


class CaptionField(TimestampMixin, Base):
	"""Поле подписи канала: пул полей + словарь значений.

	Поле («Genre», «Year»…) и его словарь существуют у канала в одном
	экземпляре; шаблоны лишь включают поле в свой состав — так значение,
	добавленное при сборке по одному шаблону, видно и в остальных.
	"""

	__tablename__ = "caption_fields"

	id: Mapped[int] = mapped_column(primary_key=True)
	channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
	name: Mapped[str] = mapped_column(String(64))
	hashtag: Mapped[bool] = mapped_column(Boolean, default=True)
	multiple: Mapped[bool] = mapped_column(Boolean, default=False)

	values: Mapped[list[CaptionValue]] = relationship(
		back_populates="field", cascade="all, delete-orphan",
		order_by="CaptionValue.value",
	)


class CaptionValue(TimestampMixin, Base):
	"""Значение словаря поля подписи (например, конкретный жанр)."""

	__tablename__ = "caption_values"

	id: Mapped[int] = mapped_column(primary_key=True)
	field_id: Mapped[int] = mapped_column(ForeignKey("caption_fields.id"))
	value: Mapped[str] = mapped_column(String(128))

	field: Mapped[CaptionField] = relationship(back_populates="values")


class CaptionTemplate(TimestampMixin, Base):
	"""Именованный шаблон подписи канала (упорядоченный набор полей)."""

	__tablename__ = "caption_templates"

	id: Mapped[int] = mapped_column(primary_key=True)
	channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
	name: Mapped[str] = mapped_column(String(64))
	# для предвыбора «последнего использованного» шаблона в диалоге
	last_used_at: Mapped[datetime | None] = mapped_column(
		DateTime(timezone=True), default=None
	)
	# шаблон имени файла при отправке: {video}, {ИмяПоля}, {quality}, {channel}
	filename_pattern: Mapped[str | None] = mapped_column(String(255), default=None)

	fields: Mapped[list[CaptionTemplateField]] = relationship(
		back_populates="template", cascade="all, delete-orphan",
		order_by="CaptionTemplateField.position",
	)


class CaptionTemplateField(Base):
	"""Строка состава шаблона: поле, порядок, включено ли по умолчанию."""

	__tablename__ = "caption_template_fields"

	id: Mapped[int] = mapped_column(primary_key=True)
	template_id: Mapped[int] = mapped_column(ForeignKey("caption_templates.id"))
	field_id: Mapped[int] = mapped_column(ForeignKey("caption_fields.id"))
	position: Mapped[int] = mapped_column(Integer, default=0)
	enabled: Mapped[bool] = mapped_column(Boolean, default=True)

	template: Mapped[CaptionTemplate] = relationship(back_populates="fields")
	field: Mapped[CaptionField] = relationship()
