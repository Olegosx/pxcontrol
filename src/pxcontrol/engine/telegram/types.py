"""Общие типы телеграм-слоя (граница «сервисы → транспорты»)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from pxcontrol.engine.errors import EngineError


@dataclass(frozen=True)
class BotRef:
	"""Ссылка на бота для операций бот-пути: id в нашей БД и токен.

	Id нужен шлюзу для дорожки и учёта активности бота (ADR-0030),
	токен — для самого запроса. Токен исключён из ``repr``: ссылка
	попадает в журнал, а секрет — нет.
	"""

	id: int
	token: str = field(repr=False)


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

#: Лимиты Telegram на длину текста поста. Premium удваивает предел
#: сообщения и учетверяет предел подписи к медиа: клиентский конфиг
#: отдаёт пределы подписи ключами ``caption_length_limit_default``
#: и ``caption_length_limit_premium``. Проверено 2026-09-11
#: (core.telegram.org/api/config, core.telegram.org/api/premium,
#: limits.tginfo.me).
TEXT_LENGTH_LIMIT = 4096
TEXT_LENGTH_LIMIT_PREMIUM = 8192
CAPTION_LENGTH_LIMIT = 1024
CAPTION_LENGTH_LIMIT_PREMIUM = 4096


def text_length_limit(premium: bool, with_media: bool) -> int:
	"""Предел длины текста поста по подписке аккаунта и типу поста.

	Пост с вложением ограничен пределом подписи — он вчетверо меньше
	предела обычного сообщения у не-Premium аккаунта.

	Args:
		premium: есть ли Premium у аккаунта-публикатора (бот — всегда
			False: подписки у ботов не бывает).
		with_media: пост с вложением (текст идёт подписью к файлу).
	"""
	if with_media:
		return CAPTION_LENGTH_LIMIT_PREMIUM if premium else CAPTION_LENGTH_LIMIT
	return TEXT_LENGTH_LIMIT_PREMIUM if premium else TEXT_LENGTH_LIMIT


def telegram_text_length(text: str) -> int:
	"""Длина текста в кодовых единицах UTF-16.

	Именно в них Telegram считает смещения разметки сообщений, поэтому
	для длины берётся та же единица: символы основной таблицы (включая
	кириллицу и латиницу) считаются как ``len(text)``, а эмодзи и прочие
	символы за её пределами — за два.

	Оговорка честности: официальная документация называет пределы то
	«символами», то «UTF-8 length» (core.telegram.org/api/config)
	и однозначного определения единицы не даёт. Выбран счёт UTF-16 —
	он совпадает с обычным на всём, кроме эмодзи, а на эмодзи строже,
	то есть приложение откажет раньше сервера, а не позже.
	"""
	return len(text.encode("utf-16-le")) // 2


#: Лимит Telegram на отложенные сообщения в одном чате/канале.
#: Premium его НЕ увеличивает (даёт только повторяющиеся отложки);
#: горизонт — до года вперёд. Превышение — ошибка API SCHEDULE_TOO_MUCH.
#: Проверено 2026-08-26 (limits.tginfo.me, core.telegram.org) — ADR-0016.
TELEGRAM_MAX_SCHEDULED = 100

#: Сколько тем форума запрашивать за раз. Это наш размер выборки,
#: а не предел Telegram: столько же тем одним запросом берут клиенты,
#: и живых тем в сообществе обычно единицы. Если тем окажется больше,
#: выборка обрежется — операция предупредит об этом в журнале.
FORUM_TOPICS_PAGE = 100


def limit_gb(limit_bytes: int) -> int:
	"""Предел на файл в гигабайтах — так его называют человеку.

	Гигабайт здесь десятичный (10⁹), как в текстах самого Telegram
	про «2 ГБ» и «4 ГБ», хотя лимит задан двоичными частями. Правило
	перевода одно на движок и интерфейс: врозь тексты уже расходились
	бы с проверкой, которую они объясняют.
	"""
	return limit_bytes // 10**9


def limit_mb(limit_bytes: int) -> int:
	"""Предел на файл в мегабайтах (двоичных) — для бот-пути."""
	return limit_bytes // 2**20


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
	# вложение не из наших видов (опрос, геопозиция, контакт, стикер…):
	# приложение таких не создаёт, но читает — их ставят отложенными
	# из клиента Telegram; править у них можно только время
	OTHER = "other"

	@property
	def creatable(self) -> bool:
		"""Умеет ли приложение отправлять пост с таким вложением.

		``OTHER`` — только чтение: у него нет ни формы, ни фильтра
		файлов, и черновик с ним отклоняется до отправки.
		"""
		return self is not MediaKind.OTHER


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


class ServiceMessageKind(StrEnum):
	"""Вид служебной записи сообщества (ADR-0026).

	Служебная запись (``MessageService``) — строка в ленте, которую
	создал сам Telegram: «такой-то вступил», «сообщение закреплено»,
	«название изменено». Видов действий в схеме Telegram больше
	шестидесяти, но человеку важны не они по отдельности, а группы:
	чистить он решает не «messageActionChatJoinedByLink», а «шум
	от участников».
	"""

	#: вступления и уходы — главный шум активной группы
	MEMBERS = "members"
	#: закрепления сообщений (в живой группе копятся заметно)
	PINS = "pins"
	#: оформление сообщества: название, аватар, тема, обои, автоудаление
	APPEARANCE = "appearance"
	#: видеочаты и звонки: начались, кончились, запланированы
	CALLS = "calls"
	#: всё прочее служебное (подарки, бусты, платежи, опросы…)
	OTHER = "other"
	#: записи, которые не удаляются никогда (см. ADR-0026):
	#: корневые сообщения тем форума, создание и переезд сообщества,
	#: передача владения. Показываются, но выбрать их нельзя
	PROTECTED = "protected"

	def removable(self) -> bool:
		"""Можно ли предлагать записи этого вида к удалению."""
		return self is not ServiceMessageKind.PROTECTED


@dataclass(frozen=True)
class ServiceMessageInfo:
	"""Служебная запись, прочитанная транспортом из истории сообщества.

	Собственный тип границы слоёв: сырые объекты Telethon до сервисов
	не доезжают (как и у :class:`ScheduledMessage`).

	Attributes:
		id: идентификатор сообщения в сообществе.
		kind: вид записи — по нему человек выбирает, что чистить.
		date: когда запись появилась.
	"""

	id: int
	kind: ServiceMessageKind
	date: datetime


@dataclass(frozen=True)
class ServiceMessagesPage:
	"""Страница истории: служебные записи и место, с которого продолжать.

	Обход идёт от новых записей к старым страницами по сотне: серверного
	фильтра «только служебные» у Telegram нет, историю приходится читать
	целиком (ADR-0026). Страница — единица работы: между страницами
	очередь обслуживания проверяет отмену, а дорожка аккаунта (ADR-0024)
	пропускает вперёд публикацию.

	Attributes:
		messages: служебные записи этой страницы (обычные отброшены).
		scanned: сколько сообщений просмотрено (включая обычные).
		next_offset_id: с какого сообщения читать дальше;
			None — история кончилась.
		oldest_date: дата самой старой записи страницы (для отчёта
			«просмотрено до такого-то числа»); None — страница пуста.
	"""

	messages: list[ServiceMessageInfo]
	scanned: int
	next_offset_id: int | None
	oldest_date: datetime | None


@dataclass(frozen=True)
class DeletedAccount:
	"""Ссылка на удалённый аккаунт в списке участников (ADR-0026).

	Кроме идентификатора несёт **хеш доступа** — без него Telegram
	не принимает ссылку на пользователя (`InputPeerUser` = id + hash).
	Хеш приходит в том же ответе, что и сам участник, и передаётся
	дальше: полагаться на то, что клиент запомнил пользователя раньше,
	нельзя — между поиском и исключением проходит время, а кеш живёт
	в памяти сессии. None — Telegram хеша не дал (редкость; тогда
	ссылку соберёт сам клиент по своему кешу).

	Attributes:
		user_id: идентификатор учётки.
		access_hash: хеш доступа к ней.
	"""

	user_id: int
	access_hash: int | None = None


@dataclass(frozen=True)
class ParticipantsPage:
	"""Страница списка участников: удалённые аккаунты и место продолжения.

	Список участников Telegram отдаёт порциями и только администратору;
	«удалённый аккаунт» — учётка, которую владелец удалил, у неё поднят
	флаг ``deleted`` (ADR-0026). Наружу отдаются только ссылки на них:
	живые участники обслуживанию не нужны, а тащить их через границу
	слоёв незачем.

	Attributes:
		deleted: удалённые аккаунты этой страницы (со ссылками,
			годными для исключения).
		scanned: сколько участников просмотрено (включая живых).
		next_offset: смещение следующей страницы; None — список кончился.
		total: сколько участников всего, по мнению Telegram
			(None — не сказал).
	"""

	deleted: list[DeletedAccount]
	scanned: int
	next_offset: int | None
	total: int | None


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
		can_delete: аккаунт может удалять чужие сообщения (право админа
			``delete_messages`` или владение сообществом). Нужно
			обслуживанию (ADR-0026): роль «админ» сама по себе такого
			права не гарантирует. Бот-путь его не вычисляет — False.
		can_ban: аккаунт может исключать участников (право админа
			``ban_users`` или владение). Тоже нужно обслуживанию:
			чистка удалённых аккаунтов — это исключение участников.
	"""

	chat_id: str
	title: str
	username: str | None
	kind: CommunityKind
	forum: bool = False
	role: UserbotRole | None = None
	can_delete: bool = False
	can_ban: bool = False


@dataclass(frozen=True)
class CommunityStatsInfo:
	"""Живая статистика сообщества из полной информации о нём.

	Userbot берёт всё из одного ответа (``GetFullChannelRequest``), бот —
	из пары запросов Bot API (``getChat`` + ``getChatMemberCount``);
	отдельных запросов на онлайн нет. None — источник поле не отдал.

	Attributes:
		participants: подписчики канала или участники группы.
		online: сколько участников сейчас онлайн (только у групп,
			только через userbot).
		can_view_stats: доступна ли аккаунту встроенная статистика
			Telegram (признак сервера; бот-путь её не видит — False).
		linked_chat_id: связанное сообщество в формате Bot API (-100…):
			у канала — чат обсуждений, у группы — канал; None — нет.
	"""

	participants: int | None
	online: int | None
	can_view_stats: bool = False
	linked_chat_id: str | None = None


@dataclass(frozen=True)
class DayPoint:
	"""Точка ряда по дням: дата и целое значение."""

	day: date
	value: int


@dataclass(frozen=True)
class NamedSeries:
	"""Именованный ряд по дням графика Telegram («Views», «Shares»…)."""

	name: str
	points: tuple[DayPoint, ...]


@dataclass(frozen=True)
class Share:
	"""Доля: имя (язык, источник, эмодзи, день недели) и её вес за период."""

	name: str
	value: int


@dataclass(frozen=True)
class RecentPost:
	"""Недавний пост канала: просмотры, пересылки, реакции (None — нет)."""

	msg_id: int
	views: int | None
	forwards: int | None
	reactions: int | None


@dataclass(frozen=True)
class TopPoster:
	"""Самый активный участник группы: сообщений и средняя длина."""

	name: str
	messages: int
	avg_chars: int


@dataclass(frozen=True)
class TopAdmin:
	"""Самый активный администратор: удалил записей, исключил, забанил."""

	name: str
	deleted: int
	kicked: int
	banned: int


@dataclass(frozen=True)
class TopInviter:
	"""Кто привёл больше всех участников."""

	name: str
	invitations: int


@dataclass(frozen=True)
class CommunityAnalytics:
	"""Встроенная статистика Telegram, разобранная транспортом.

	Доступна администратору сообщества достаточного размера (признак
	``can_view_stats`` в :class:`CommunityStatsInfo`); историю считает
	и хранит сам Telegram — приложение получает готовые ряды. Чего
	в ответе не оказалось (график не построен, ряд не опознан) —
	пустой ряд или None: вкладка покажет «нет данных», а не сломается.

	Attributes:
		period_from: начало периода абсолютных чисел (обычно неделя).
		period_to: конец периода.
		members: участники сейчас и в прошлый период (current, previous);
			None — Telegram не отдал.
		growth: участники по дням (абсолютные значения).
		joined: пришедшие по дням.
		left: ушедшие по дням.
		hours: активность по часам суток (24 значения; у канала —
			просмотры, у группы — сообщения); None — графика нет.
		views_per_post: просмотров на пост сейчас и раньше (только
			каналы); None — нет.
		recent_post_views: просмотры последних постов (только каналы),
			от новых к старым.
		shares_per_post / reactions_per_post: пересылок и реакций
			на пост (канал), сейчас и раньше.
		views_per_story / shares_per_story / reactions_per_story:
			то же по историям канала.
		notifications: подписчики с включёнными уведомлениями —
			(часть, всего); None — нет.
		messages / viewers / posters: сообщений, читающих и пишущих
			за период (группа), сейчас и раньше.
		interactions: просмотры и пересылки по дням (канал).
		iv_interactions: просмотры Instant View по дням (канал).
		mute: заглушили и включили звук по дням (канал).
		story_interactions: просмотры и пересылки историй по дням.
		messages_daily: сообщения по дням (группа).
		actions: действия по дням — читающие и пишущие (группа).
		views_by_source: откуда просмотры (канал) — доли за период.
		members_by_source: откуда новые подписчики / участники — доли.
		languages: языки аудитории — доли.
		reactions_by_emotion / story_reactions: реакции по эмодзи — доли.
		weekdays: активность по дням недели (группа) — доли.
		recent_posts: недавние посты с просмотрами, пересылками, реакциями.
		top_posters / top_admins / top_inviters: самые активные (группа).
	"""

	period_from: date
	period_to: date
	members: tuple[int, int] | None = None
	growth: tuple[DayPoint, ...] = ()
	joined: tuple[DayPoint, ...] = ()
	left: tuple[DayPoint, ...] = ()
	hours: tuple[int, ...] | None = None
	views_per_post: tuple[int, int] | None = None
	recent_post_views: tuple[int, ...] = ()
	shares_per_post: tuple[int, int] | None = None
	reactions_per_post: tuple[int, int] | None = None
	views_per_story: tuple[int, int] | None = None
	shares_per_story: tuple[int, int] | None = None
	reactions_per_story: tuple[int, int] | None = None
	notifications: tuple[int, int] | None = None
	messages: tuple[int, int] | None = None
	viewers: tuple[int, int] | None = None
	posters: tuple[int, int] | None = None
	interactions: tuple[NamedSeries, ...] = ()
	iv_interactions: tuple[NamedSeries, ...] = ()
	mute: tuple[NamedSeries, ...] = ()
	story_interactions: tuple[NamedSeries, ...] = ()
	messages_daily: tuple[NamedSeries, ...] = ()
	actions: tuple[NamedSeries, ...] = ()
	views_by_source: tuple[Share, ...] = ()
	members_by_source: tuple[Share, ...] = ()
	languages: tuple[Share, ...] = ()
	reactions_by_emotion: tuple[Share, ...] = ()
	story_reactions: tuple[Share, ...] = ()
	weekdays: tuple[Share, ...] = ()
	recent_posts: tuple[RecentPost, ...] = ()
	top_posters: tuple[TopPoster, ...] = ()
	top_admins: tuple[TopAdmin, ...] = ()
	top_inviters: tuple[TopInviter, ...] = ()


#: Поля :class:`CommunityAnalytics` с парами «сейчас, раньше».
ANALYTICS_PAIRS = (
	"members",
	"views_per_post",
	"shares_per_post",
	"reactions_per_post",
	"views_per_story",
	"shares_per_story",
	"reactions_per_story",
	"notifications",
	"messages",
	"viewers",
	"posters",
)
#: Поля с именованными рядами по дням.
ANALYTICS_DAILY = (
	"interactions",
	"iv_interactions",
	"mute",
	"story_interactions",
	"messages_daily",
	"actions",
)
#: Поля с долями.
ANALYTICS_SHARES = (
	"views_by_source",
	"members_by_source",
	"languages",
	"reactions_by_emotion",
	"story_reactions",
	"weekdays",
)


@dataclass(frozen=True)
class HistoryMarks:
	"""Крайние точки истории сообщества, прочитанные транспортом.

	Attributes:
		last_post_at: момент последнего сообщения в ленте; None — лента
			пуста или недоступна.
		created_at: момент первого сообщения — служебной записи
			о создании; None — не запрашивали или история скрыта
			(у супергруппы после переезда первых записей может не быть).
	"""

	last_post_at: datetime | None
	created_at: datetime | None


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
		id: идентификатор записи в очереди отложенных сообщества — им
			адресуются правка, «опубликовать сейчас» и удаление. Живёт
			только в этой очереди: после публикации у записи в ленте
			будет другой id.
		text: текст записи (пустая строка — медиа без текста).
		scheduled_at: момент будущей публикации.
		media_kind: вид вложения; ``NONE`` — текст (превью ссылки
			вложением не считается), ``OTHER`` — вложение, которого
			приложение не создаёт (опрос, геопозиция…).
		topic_id: тема форума, в которую адресована запись
			(None — общая лента или сообщество без тем).
	"""

	id: int
	text: str
	scheduled_at: datetime
	media_kind: MediaKind = MediaKind.NONE
	topic_id: int | None = None
