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


class UserbotRole(StrEnum):
	"""Роль userbot-аккаунта в сообществе (ADR-0022): снимок из зондов.

	Владелец — тоже ``ADMIN`` (надмножество прав). Роль обновляется
	подключением, добавлением участника и перепроверкой доступов;
	истина — Telegram, публикация роли слепо не доверяет.
	"""

	ADMIN = "admin"  # админ/владелец: без медленного режима, пишет в закрытые темы
	MEMBER = "member"  # участник: медленный режим, закрытые темы недоступны


class CommunityKind(StrEnum):
	"""Вид сообщества (ADR-0021): определяется при подключении по сущности
	из API и не меняется жизнью записи. Малые (не супер-) группы
	не подключаются вовсе — вида для них нет."""

	CHANNEL = "channel"  # канал-вещалка: публикует админ с правом post_messages
	GROUP = "group"  # супергруппа: публикует участник, не ограниченный в отправке


@dataclass(frozen=True)
class CommunityInfo:
	"""Сообщество, проверенное любым транспортом (бот или userbot).

	Attributes:
		chat_id: идентификатор чата в формате Bot API (-100…).
		title: название сообщества.
		username: @имя без собаки (None — приватное).
		kind: вид сообщества (канал или группа), определён по сущности API.
		forum: включены ли темы (форум); изменчивое свойство группы —
			обновляется при подключении и перепроверке доступов.
		role: роль проверявшего userbot-аккаунта (ADR-0022);
			None — проверка шла бот-путём, роли userbot он не знает.
	"""

	chat_id: str
	title: str
	username: str | None
	kind: CommunityKind
	forum: bool = False
	role: UserbotRole | None = None


@dataclass(frozen=True)
class CommunityStatsInfo:
	"""Живая статистика сообщества из полной информации о канале.

	Оба поля — из одного ответа Telegram (``GetFullChannelRequest``):
	отдельных запросов на онлайн нет. None — Telegram поле не отдал.

	Attributes:
		participants: подписчики канала или участники группы.
		online: сколько участников сейчас онлайн (только у групп).
	"""

	participants: int | None
	online: int | None


@dataclass(frozen=True)
class UserbotProfile:
	"""Профиль владельца userbot-сессии (ответ Telegram «кто я»).

	Поля раздельные, как отдаёт Telegram; None — не заполнено
	у самого аккаунта (@имени может не быть, фамилия необязательна).

	Attributes:
		username: @имя без собаки.
		first_name: имя.
		last_name: фамилия.
	"""

	username: str | None
	first_name: str | None
	last_name: str | None


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
		topic_id: тема форума (id корневого сообщения темы;
			None — общая лента, для каналов и обычных групп всегда None).
	"""

	text: str = ""
	media_path: str | None = None
	media_kind: MediaKind = MediaKind.NONE
	when: datetime | None = None
	thumb_path: str | None = None
	topic_id: int | None = None


#: Тема «General» форума: её id всегда 1, публикация в неё — обычная
#: отправка без адресации темы (страница показывает её «Общей лентой»).
GENERAL_TOPIC_ID = 1


@dataclass(frozen=True)
class ForumTopicInfo:
	"""Тема форума, прочитанная транспортом из Telegram (ADR-0021).

	Темы не хранятся в БД — список читается живьём (истина — Telegram,
	принцип ADR-0010). Перечислять темы умеет только userbot: у Bot API
	такого метода нет.

	Attributes:
		id: идентификатор темы (id корневого сообщения; «General» — 1).
		title: название темы.
		closed: тема закрыта — писать в неё может только админ
			(ADR-0022: участнику выбор блокируется в интерфейсе).
	"""

	id: int
	title: str
	closed: bool = False


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
