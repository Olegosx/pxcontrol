"""ORM-модели. Полное описание схемы — docs/05-data/data-model.md.

Таблицы добавляются миграциями по мере появления функций (YAGNI).
Сейчас отложены источники контента и задания генерации ИИ; очередь
отправки, кэш статистики с историей снимков и членства уже здесь. Отправленных постов
таблицы нет и не будет: истина по вышедшему — сам канал (ADR-0010),
хранится только неотправленное (ADR-0016).

Поля с типом :class:`EncryptedStr` шифруются прозрачно (ADR-0009).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
	JSON,
	Boolean,
	DateTime,
	Float,
	ForeignKey,
	Index,
	Integer,
	String,
	Text,
	func,
	text,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from pxcontrol.engine.db.types import EncryptedStr


class Base(DeclarativeBase):
	"""Базовый класс всех ORM-моделей."""


class TimestampMixin:
	"""Общие поля времени создания и последнего изменения."""

	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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


class CommunitySetting(Base):
	"""Настройка сообщества: строка «(сообщество, имя) → значение» (ADR-0013).

	Внешний ключ с каскадом: настройки живут и умирают вместе с сообществом
	(каскад страхует и сервис — ``CommunitiesService.delete_community``).
	"""

	__tablename__ = "community_settings"

	community_id: Mapped[int] = mapped_column(
		ForeignKey("communities.id", ondelete="CASCADE"), primary_key=True
	)
	name: Mapped[str] = mapped_column(String(128), primary_key=True)
	value: Mapped[Any] = mapped_column(JSON)


class Bot(TimestampMixin, Base):
	"""Telegram-бот для публикации. Токен шифруется.

	``paused`` — приостановлен человеком (ADR-0029): приложение бота
	не использует (ни публикация, ни опрос статистики), но помнит —
	вместе с назначениями в сообществах.
	"""

	__tablename__ = "bots"

	id: Mapped[int] = mapped_column(primary_key=True)
	label: Mapped[str] = mapped_column(String(128))
	token: Mapped[str] = mapped_column(EncryptedStr(512))
	username: Mapped[str | None] = mapped_column(String(255), default=None)
	paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class TgApiCredential(TimestampMixin, Base):
	"""Ключ API Telegram с my.telegram.org — один на всё приложение (ADR-0018).

	Реквизиты приложения, а не аккаунта: с одной парой входят все
	userbot-аккаунты (так работают и обычные клиенты Telegram). Запись
	одна; ``api_hash`` шифруется. Правится на странице
	«Настройки → Общие».
	"""

	__tablename__ = "tg_api_credentials"

	id: Mapped[int] = mapped_column(primary_key=True)
	api_id: Mapped[int] = mapped_column(Integer)
	api_hash: Mapped[str] = mapped_column(EncryptedStr(512))


class TgAccount(TimestampMixin, Base):
	"""Userbot-аккаунт MTProto (отдельный аккаунт, ADR-0007).

	Реквизиты самого аккаунта: телефон и ``session`` — строка сессии,
	секрет уровня пароля (шифруется), заполняется после входа по номеру
	телефона. ``label`` — необязательная ручная пометка («рабочий»,
	«запасной»); @имя и имя (``username``/``first_name``/``last_name``,
	раздельно — как отдаёт Telegram) заполняются и актуализируются
	автоматически: вход, старт приложения, зонды прав. Ключ API
	приложения — общий, в ``tg_api_credentials`` (ADR-0018).
	``paused`` — приостановлен человеком (ADR-0029): транспорт закрыт,
	обращений к аккаунту нет, но сессия, пометка и членства сохранены —
	возобновление подключает без нового входа.
	"""

	__tablename__ = "tg_accounts"

	id: Mapped[int] = mapped_column(primary_key=True)
	label: Mapped[str | None] = mapped_column(String(128), default=None)
	phone: Mapped[str | None] = mapped_column(String(32), default=None)
	session: Mapped[str | None] = mapped_column(EncryptedStr(2048), default=None)
	username: Mapped[str | None] = mapped_column(String(255), default=None)
	first_name: Mapped[str | None] = mapped_column(String(255), default=None)
	last_name: Mapped[str | None] = mapped_column(String(255), default=None)
	paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


class AiCredential(TimestampMixin, Base):
	"""Ключ провайдера ИИ. Ключ шифруется."""

	__tablename__ = "ai_credentials"

	id: Mapped[int] = mapped_column(primary_key=True)
	provider: Mapped[str] = mapped_column(String(64), default="anthropic")
	label: Mapped[str] = mapped_column(String(128), default="")
	api_key: Mapped[str] = mapped_column(EncryptedStr(512))


#: Предел длины имени подпапки пресета: длина колонки ``subdir`` ниже.
#: SQLite длину String не проверяет — соблюдение обеспечивает
#: ``video.sanitize_subdir``, который берёт предел отсюда.
SUBDIR_MAX_CHARS = 128


class VideoPreset(TimestampMixin, Base):
	"""Шаблон обработки видео (параметры из референса makeVideo).

	Переиспользуется между сообществами: сообщество ссылается на пресет.
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
	# ступень разрешения итога: число по короткой стороне кадра
	# (720/1080/1440/2160); NULL — «как в оригинале», без масштабирования
	target_resolution: Mapped[int | None] = mapped_column(Integer)
	# NULL — «как в оригинале»: целевой битрейт берётся из исходника
	video_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
	# комментарий в метаданные файла (тег comment): «ссылка — описание»
	meta_comment: Mapped[str | None] = mapped_column(String(512))
	# подпапка внутри базовых папок видео (исходники/результаты/опубликованные);
	# пустая строка — без подпапки
	subdir: Mapped[str] = mapped_column(String(SUBDIR_MAX_CHARS), default="")


class Community(TimestampMixin, Base):
	"""Подключённое сообщество: канал или группа Telegram (ADR-0021).

	Вид (канал или группа) — колонка ``kind`` со значениями
	``CommunityKind`` (ADR-0021); признак форума — изменчивый флаг.
	Два возможных публикатора — ссылками (оба необязательны, ADR-0019):
	``tg_account_id`` — userbot-аккаунт-админ (постинг идёт из его
	сессии, приоритетный путь по ADR-0011), ``bot_id`` — бот-публикатор
	(запасной путь; самостоятелен — работает по токену, без
	пользовательской сессии). Параметры-предпочтения сообщества — строками
	в ``community_settings`` (ADR-0013), например пресет обработки
	по умолчанию.
	"""

	__tablename__ = "communities"

	id: Mapped[int] = mapped_column(primary_key=True)
	title: Mapped[str] = mapped_column(String(255))
	tg_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
	username: Mapped[str | None] = mapped_column(String(255), default=None)
	# вид сообщества (ADR-0021): значения CommunityKind («channel»/«group»),
	# определяется при подключении и не меняется жизнью записи
	kind: Mapped[str] = mapped_column(String(16), default="channel", server_default="channel")
	# темы (форум) включены; изменчивое свойство группы — обновляется
	# при подключении и перепроверке доступов (потому не часть kind)
	forum: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
	# публикатор по умолчанию (ADR-0022): его сессией идут публикация,
	# отложки и темы; инвариант «умолчание — действующий участник»
	# держит сервис; удаление аккаунта отвязывает (SET NULL)
	default_tg_account_id: Mapped[int | None] = mapped_column(
		ForeignKey("tg_accounts.id", ondelete="SET NULL"), default=None
	)
	# бот удаляется — сообщество остаётся без бота (проверку ключей включает Database)
	bot_id: Mapped[int | None] = mapped_column(
		ForeignKey("bots.id", ondelete="SET NULL"), default=None
	)
	# может ли бот править ЧУЖИЕ сообщения (право канала edit_messages):
	# от него зависят кнопки поверх поста публикателя (ADR-0031).
	# Свойство изменчивое — владелец канала может отобрать право,
	# поэтому обновляется зондами, как название и признак форума
	bot_can_edit: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))

	bot: Mapped[Bot | None] = relationship()
	default_account: Mapped[TgAccount | None] = relationship()
	# членства (ADR-0022): каскад БД дублируется ORM-каскадом, чтобы
	# удаление сообщества через сессию не пыталось занулить ключи
	members: Mapped[list[CommunityMember]] = relationship(
		cascade="all, delete-orphan", passive_deletes=True
	)


class CommunityMember(TimestampMixin, Base):
	"""Членство userbot-аккаунта в сообществе (ADR-0022).

	Пул аккаунтов сообщества: публикует умолчание
	(``Community.default_tg_account_id``), остальные — фундамент
	будущего модуля соцактивности. Роль — снимок из зондов прав
	(значения ``UserbotRole``), обновляется подключением, добавлением
	участника и перепроверкой доступов. Членство живёт и умирает
	вместе с сообществом и с аккаунтом (CASCADE с обеих сторон).
	"""

	__tablename__ = "community_members"

	community_id: Mapped[int] = mapped_column(
		ForeignKey("communities.id", ondelete="CASCADE"), primary_key=True
	)
	tg_account_id: Mapped[int] = mapped_column(
		ForeignKey("tg_accounts.id", ondelete="CASCADE"), primary_key=True
	)
	role: Mapped[str] = mapped_column(String(16))

	tg_account: Mapped[TgAccount] = relationship()
	# обратная сторона членства (страница аккаунта, ADR-0029): без
	# back_populates — сообщество грузит участников своим каскадом
	community: Mapped[Community] = relationship(viewonly=True)


class AccountOperation(Base):
	"""Выполненная операция пользователя или бота в Telegram (ADR-0030).

	Запись на каждое обращение через дорожку шлюза: кто, какого вида,
	когда началось и кончилось, чем кончилось. Из строк считаются число
	операций, занятость (сумма пересечений интервалов с окном показа),
	ошибки и флуд-лимиты за окно. Владелец — ровно одна из двух ссылок:
	у пользователей и ботов свои таблицы. Живёт и умирает с владельцем
	(CASCADE); строки старше года убирает сервис активности.
	"""

	__tablename__ = "account_operations"
	__table_args__ = (
		Index("ix_account_operations_account_finished", "tg_account_id", "finished_at"),
		Index("ix_account_operations_bot_finished", "bot_id", "finished_at"),
		Index("ix_account_operations_finished", "finished_at"),
	)

	id: Mapped[int] = mapped_column(primary_key=True)
	tg_account_id: Mapped[int | None] = mapped_column(
		ForeignKey("tg_accounts.id", ondelete="CASCADE"), default=None
	)
	bot_id: Mapped[int | None] = mapped_column(
		ForeignKey("bots.id", ondelete="CASCADE"), default=None
	)
	# вид операции — значения TelegramPriority по имени (publish, interactive…)
	kind: Mapped[str] = mapped_column(String(16))
	started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
	finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
	# исход — значения Outcome (ok, error, flood, cancelled)
	outcome: Mapped[str] = mapped_column(String(16))
	# срок, названный Telegram при флуд-лимите, секунд (0 — не было)
	wait_s: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class CommunityStats(Base):
	"""Кэш статистики сообщества: дашборд и вкладка «Обзор» (ADR-0027).

	Данные приезжают из Telegram периодическим опросом движка (бот —
	часто, userbot — редко); интерфейс читает только кэш. NULL в поле —
	данные ещё не получены (или источник их не отдаёт: онлайн есть
	только у групп и только через userbot). ``avatar_path`` указывает
	на файл в кэше на диске; сам файл при удалении сообщества убирает
	движок (строку — каскад БД).
	"""

	__tablename__ = "community_stats"

	community_id: Mapped[int] = mapped_column(
		ForeignKey("communities.id", ondelete="CASCADE"), primary_key=True
	)
	participants: Mapped[int | None] = mapped_column(Integer, default=None)
	online: Mapped[int | None] = mapped_column(Integer, default=None)
	scheduled_count: Mapped[int | None] = mapped_column(Integer, default=None)
	avatar_path: Mapped[str | None] = mapped_column(String(1024), default=None)
	# момент последнего успешного обновления любым источником («Обновлено»)
	fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	# моменты проходов по источникам: по ним считается, кто «должен»
	# (бот — раз в 15 минут, userbot — раз в 6 часов)
	bot_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	full_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	# справка «Обзора» (ADR-0027): встроенная статистика доступна,
	# связанное сообщество (формат Bot API), создано, последний пост
	can_view_stats: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
	linked_chat_id: Mapped[str | None] = mapped_column(String(64), default=None)
	tg_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	# итог последнего прохода обслуживания по удалённым аккаунтам
	# (ADR-0026): найдено, исключено, когда
	deleted_found: Mapped[int | None] = mapped_column(Integer, default=None)
	deleted_removed: Mapped[int | None] = mapped_column(Integer, default=None)
	deleted_checked_at: Mapped[datetime | None] = mapped_column(
		DateTime(timezone=True), default=None
	)


class CommunityStatsHistory(Base):
	"""Снимок статистики сообщества в момент опроса (ADR-0027).

	Локальная история для сообществ без встроенной статистики Telegram:
	участники по дням, оценка приходов и уходов, онлайн по часам. Пишется
	каждым успешным проходом опроса; строки старше срока хранения
	убирает сам опрос. Удаление сообщества уносит историю каскадом.
	"""

	__tablename__ = "community_stats_history"
	__table_args__ = (Index("ix_community_stats_history_community_at", "community_id", "at"),)

	id: Mapped[int] = mapped_column(primary_key=True)
	community_id: Mapped[int] = mapped_column(ForeignKey("communities.id", ondelete="CASCADE"))
	at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
	participants: Mapped[int | None] = mapped_column(Integer, default=None)
	online: Mapped[int | None] = mapped_column(Integer, default=None)


class CommunityAnalyticsRow(Base):
	"""Встроенная статистика Telegram сообщества — последний ответ (ADR-0027).

	Историю считает и хранит сам Telegram, поэтому копить ответы незачем:
	одна строка на сообщество, перезаписывается редким опросом userbot.
	``payload`` — разобранные ряды в JSON (сериализация ``CommunityAnalytics``
	живёт в сервисе статистики).
	"""

	__tablename__ = "community_analytics"

	community_id: Mapped[int] = mapped_column(
		ForeignKey("communities.id", ondelete="CASCADE"), primary_key=True
	)
	fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
	payload: Mapped[Any] = mapped_column(JSON)


class PublishQueueItem(TimestampMixin, Base):
	"""Элемент очереди отправки: черновик, ждущий отправки (ADR-0016).

	Хранятся только неотправленные (pending/waiting/error): успешные
	и отменённые удаляются — истина по вышедшим постам остаётся сообществом
	(ADR-0010). Очередь живёт и умирает вместе с сообществом (CASCADE);
	файл такого элемента остаётся в папке очереди (см. ADR-0016).

	Значения по умолчанию не задаются: очередь заполняет все поля явно,
	а истина статусов и типов вложений — перечисления в сервисе и шлюзе
	(дублировать их литералами здесь — риск молчаливого расхождения).
	"""

	__tablename__ = "publish_queue_items"

	id: Mapped[int] = mapped_column(primary_key=True)
	community_id: Mapped[int] = mapped_column(ForeignKey("communities.id", ondelete="CASCADE"))
	text: Mapped[str] = mapped_column(Text)
	media_path: Mapped[str | None] = mapped_column(String(1024))
	media_kind: Mapped[str] = mapped_column(String(16))
	# желаемый момент публикации (UTC); NULL — «сейчас»
	when: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
	rename_to: Mapped[str | None] = mapped_column(String(255))
	# тема форума (id корневого сообщения темы); NULL — общая лента (ADR-0021)
	topic_id: Mapped[int | None] = mapped_column(Integer)
	# pending — готов к отправке; waiting — ждёт слота отложек; error
	status: Mapped[str] = mapped_column(String(16))
	error: Mapped[str | None] = mapped_column(Text)
	# обещанная клавиатура (ADR-0031), формат — markup_to_json;
	# NULL — кнопок у поста нет
	markup: Mapped[Any | None] = mapped_column(JSON, default=None)
	# разметка текста (ADR-0033), формат — rich_to_json; NULL — текст
	# без разметки: такой уходит прежним путём (разбор разделителей),
	# и так же читаются элементы, поставленные до ADR-0033
	entities: Mapped[Any | None] = mapped_column(JSON, default=None)
	# режим «кнопки важнее» (ADR-0031, п. 4): пост ждёт своей минуты
	# здесь, а не отложкой на сервере, и уходит ботом вместе с кнопками.
	# Значение по умолчанию задаётся через sql_text: имя text в этом
	# классе занято колонкой текста поста
	markup_first: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("0"))


class PromisedMarkup(TimestampMixin, Base):
	"""Обещанная клавиатура для поста, у которого нет своей строки (ADR-0031).

	Кнопки ставит бот и только после публикации, а отложенные записи живут
	на сервере Telegram (ADR-0010) — своей строки у такого поста у нас нет.
	Значит обещание нужно где-то держать: сообщество, номер отложенной
	записи, время публикации, текст для опознания вышедшего поста и сама
	клавиатура. Применённое обещание удаляется — в базе остаётся только
	то, чего в Telegram ещё нет.

	Текст хранится целиком, а не хешем (решение владельца): постов
	в ожидании единицы, а по строке должно быть видно, о каком посте речь.

	Живёт и умирает вместе с сообществом (CASCADE). Имя класса отличается
	от имени типа клавиатуры (:class:`~pxcontrol.engine.telegram.markup.PostMarkup`)
	намеренно: это строка об обещании, а не сама клавиатура.
	"""

	__tablename__ = "post_markups"
	# читают обещания по сообществу и в порядке времени публикации
	__table_args__ = (Index("ix_post_markups_community_when", "community_id", "when"),)

	id: Mapped[int] = mapped_column(primary_key=True)
	community_id: Mapped[int] = mapped_column(ForeignKey("communities.id", ondelete="CASCADE"))
	# номер отложенной записи на сервере; NULL — отложки нет
	scheduled_message_id: Mapped[int | None] = mapped_column(Integer, default=None)
	# номер уже вышедшего поста; NULL — он ещё не вышел. Разные колонки
	# намеренно: отложка и вышедший пост — разные сущности с разными
	# номерами (проверено опытом), и путать их нельзя
	message_id: Mapped[int | None] = mapped_column(Integer, default=None)
	# ожидаемый момент публикации (UTC) — по нему дозор берёт ближайшие
	when: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	# текст или подпись поста: опознание вышедшего поста требует точного
	# совпадения (ADR-0031, п. 9) — промах приклеил бы кнопки к чужому
	match_text: Mapped[str] = mapped_column(Text)
	markup: Mapped[Any] = mapped_column(JSON)
	attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
	error: Mapped[str | None] = mapped_column(Text, default=None)


class CaptionField(TimestampMixin, Base):
	"""Поле подписи сообщества: пул полей + словарь значений.

	Поле («Genre», «Year»…) и его словарь существуют у сообщества в одном
	экземпляре; шаблоны лишь включают поле в свой состав — так значение,
	добавленное при сборке по одному шаблону, видно и в остальных.
	"""

	__tablename__ = "caption_fields"

	id: Mapped[int] = mapped_column(primary_key=True)
	community_id: Mapped[int] = mapped_column(ForeignKey("communities.id", ondelete="CASCADE"))
	name: Mapped[str] = mapped_column(String(64))
	hashtag: Mapped[bool] = mapped_column(Boolean, default=True)
	multiple: Mapped[bool] = mapped_column(Boolean, default=False)
	# выключен — строка подписи собирается без префикса «Имя: »,
	# в подпись уходят только значения
	show_name: Mapped[bool] = mapped_column(Boolean, default=True)
	# поле зависит от другого поля сообщества: его значения живут внутри
	# значений родителя («Character» внутри «Title»); родительское поле
	# удалили — зависимое становится независимым (SET NULL)
	parent_field_id: Mapped[int | None] = mapped_column(
		ForeignKey("caption_fields.id", ondelete="SET NULL"), default=None
	)

	values: Mapped[list[CaptionValue]] = relationship(
		back_populates="field",
		cascade="all, delete-orphan",
		# удаление каскадом делает сама БД (ondelete=CASCADE, проверка
		# ключей включена) — без passive_deletes ORM грузил бы весь словарь
		# и удалял его построчно
		passive_deletes=True,
		order_by="CaptionValue.value",
	)


class CaptionValue(TimestampMixin, Base):
	"""Значение словаря поля подписи (например, конкретный жанр).

	У зависимого поля значение может быть привязано к значению
	родительского словаря (персонаж — к тайтлу): тайтл удаляется —
	его персонажи уходят каскадом. NULL — «без тайтла».
	"""

	__tablename__ = "caption_values"

	id: Mapped[int] = mapped_column(primary_key=True)
	field_id: Mapped[int] = mapped_column(ForeignKey("caption_fields.id", ondelete="CASCADE"))
	value: Mapped[str] = mapped_column(String(128))
	parent_value_id: Mapped[int | None] = mapped_column(
		ForeignKey("caption_values.id", ondelete="CASCADE"), default=None
	)

	field: Mapped[CaptionField] = relationship(back_populates="values")
	parent: Mapped[CaptionValue | None] = relationship(remote_side="CaptionValue.id")


class CaptionTemplate(TimestampMixin, Base):
	"""Именованный шаблон подписи канала (упорядоченный набор полей)."""

	__tablename__ = "caption_templates"

	id: Mapped[int] = mapped_column(primary_key=True)
	community_id: Mapped[int] = mapped_column(ForeignKey("communities.id", ondelete="CASCADE"))
	name: Mapped[str] = mapped_column(String(64))
	# для предвыбора «последнего использованного» шаблона в диалоге
	last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
	# шаблон имени файла при отправке: {video}, {ИмяПоля}, {quality}, {channel}
	filename_pattern: Mapped[str | None] = mapped_column(String(255), default=None)

	fields: Mapped[list[CaptionTemplateField]] = relationship(
		back_populates="template",
		cascade="all, delete-orphan",
		# каскад удаления — на стороне БД (см. CaptionField.values)
		passive_deletes=True,
		order_by="CaptionTemplateField.position",
	)


class CaptionTemplateField(Base):
	"""Строка состава шаблона: поле, порядок, включено ли по умолчанию."""

	__tablename__ = "caption_template_fields"

	id: Mapped[int] = mapped_column(primary_key=True)
	template_id: Mapped[int] = mapped_column(ForeignKey("caption_templates.id", ondelete="CASCADE"))
	field_id: Mapped[int] = mapped_column(ForeignKey("caption_fields.id", ondelete="CASCADE"))
	position: Mapped[int] = mapped_column(Integer, default=0)
	enabled: Mapped[bool] = mapped_column(Boolean, default=True)

	template: Mapped[CaptionTemplate] = relationship(back_populates="fields")
	field: Mapped[CaptionField] = relationship()
