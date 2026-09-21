"""Общие типы телеграм-слоя (граница «сервисы → транспорты»)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.rights import ExecutorRights

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # разбор клавиатуры и текста живёт в своих модулях,
	# а они опираются на этот — ссылки держим для проверки типов
	from pxcontrol.engine.telegram.markup import PostMarkup
	from pxcontrol.engine.telegram.poll import PollDraft
	from pxcontrol.engine.telegram.rich_text import TextEntity


@dataclass(frozen=True)
class BotRef:
	"""Ссылка на бота для операций бот-пути: id в нашей БД и токен.

	Id нужен шлюзу для дорожки и учёта активности бота (ADR-0030),
	токен — для самого запроса. Токен исключён из ``repr``: ссылка
	попадает в журнал, а секрет — нет.
	"""

	id: int
	token: str = field(repr=False)


class OwnerKind(StrEnum):
	"""Вид исполнителя: пользователь (userbot-аккаунт) или бот (ADR-0030)."""

	USER = "user"
	BOT = "bot"


@dataclass(frozen=True)
class ExecutorRef:
	"""Ссылка на исполнителя в Telegram: вид и id в нашей БД.

	Один ключ от базы до дорожки (ADR-0035): строка пула сообщества,
	владелец записей активности и ключ пула дорожек в шлюзе — у
	пользователя и бота свои таблицы, и id могут совпадать. До сверки
	решения (20.09.2026) звался ``LaneOwner`` и жил в ``lane.py``: имя
	говорило о дорожке, а ключ общий.
	"""

	kind: OwnerKind
	id: int


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


#: Схемы адресов, которые Telegram принимает у ссылки — и в тексте,
#: и под кнопкой. Отсекаем сами: на прочие сервер отвечает невнятной
#: ошибкой разбора. Одна точка на оба случая — пока их было две,
#: они разошлись: «HTTPS://…» кнопка принимала, а ссылка в тексте нет.
ALLOWED_URL_SCHEMES = ("https://", "http://", "tg://")


def known_scheme(url: str) -> bool:
	"""Начинается ли адрес со схемы, которую принимает Telegram.

	Регистр схемы не важен: «HTTPS://» — тот же адрес, и отказывать
	в нём человеку не за что.
	"""
	return url.lower().startswith(ALLOWED_URL_SCHEMES)


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
	# опрос: у Telegram это тоже вложение (InputMediaPoll), только
	# без файла — его содержимое живёт в PollDraft (ADR-0033, C5)
	POLL = "poll"
	# вложение не из наших видов (геопозиция, контакт, стикер…):
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

	@property
	def has_caption(self) -> bool:
		"""Бывает ли у поста с таким вложением текст, который правится.

		У опроса текста нет вовсе (вопрос и варианты после отправки
		не меняются ничем), у чужих видов — тоже: Telegram не даёт
		менять ни вопрос опроса, ни подпись геопозиции.
		"""
		return self not in (MediaKind.POLL, MediaKind.OTHER)

	@property
	def needs_file(self) -> bool:
		"""Нужен ли этому виду файл с диска.

		Вид вложения и файл — не одно и то же: у текста файла нет,
		у опроса тоже (его содержимое — вопрос и варианты), а чужие
		виды приложение только читает. Спрашивать признак надёжнее,
		чем перечислять виды в каждом месте, где выбирается файл.
		"""
		return self in (MediaKind.PHOTO, MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.DOCUMENT)


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


class ChatReactionsMode(StrEnum):
	"""Какие реакции разрешены в сообществе (``ChatReactions`` Telegram)."""

	ALL = "all"  # любые стандартные эмодзи
	SOME = "some"  # только перечисленные
	NONE = "none"  # реакции запрещены


@dataclass(frozen=True)
class ReactionOption:
	"""Стандартная реакция из глобального списка Telegram.

	Attributes:
		emoji: сам эмодзи — то, что уходит в ``sendReaction``.
		title: описание Telegram («Thumbs Up»).
		premium: доступна только аккаунтам с Premium.
	"""

	emoji: str
	title: str
	premium: bool = False


@dataclass(frozen=True)
class ChatReactions:
	"""Реакции, разрешённые в сообществе, с их описаниями (ADR-0039).

	Attributes:
		mode: режим сообщества.
		options: разрешённые стандартные реакции; при режиме «любые» —
			весь глобальный список без неактивных.
	"""

	mode: ChatReactionsMode
	options: tuple[ReactionOption, ...] = ()


@dataclass(frozen=True)
class ReactablePost:
	"""Запись ленты, на которую можно поставить реакцию (ADR-0039).

	Attributes:
		id: идентификатор сообщения.
		date: когда вышло.
		mine: реакции текущего аккаунта на этой записи (эмодзи).
	"""

	id: int
	date: datetime
	mine: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReactionsPage:
	"""Страница ленты для задачи реакций: записи и место продолжения.

	Служебные записи отброшены: реакции ставят постам. Сведения
	о реакциях текущего аккаунта дочитаны транспортом, если история
	пришла без них (флаг ``min`` у ``messageReactions``).

	Attributes:
		posts: записи страницы от новых к старым.
		scanned: сколько сообщений просмотрено (включая служебные).
		next_offset_id: с какого сообщения читать дальше; None — конец.
	"""

	posts: list[ReactablePost]
	scanned: int
	next_offset_id: int | None


@dataclass(frozen=True)
class PublishedMessage:
	"""Вышедший пост ленты сообщества, прочитанный транспортом.

	Собственный тип границы слоёв: сырые сообщения Telethon до сервисов
	не доезжают (как у :class:`ScheduledMessage`).

	Attributes:
		id: номер поста в ленте сообщества.
		text: текст поста или подпись к вложению.
		date: когда пост вышел.
		entities: разметка текста поста (ADR-0033) — правка обязана
			передать её заново, иначе оформление слетит.
		media_kind: вид вложения; ``NONE`` — текст (превью ссылки
			вложением не считается), ``OTHER`` — то, чего приложение
			не создаёт (опрос, геопозиция…).
		topic_id: тема форума (None — общая лента или сообщество без тем).
		buttons: сколько кнопок стоит под постом (0 — клавиатуры нет).
			Пост с кнопками виден сразу: обещание выполнено (ADR-0031).
		markup: клавиатура в нашем виде — ею наполняется форма правки.
			None означает «клавиатуры нет либо она не наших видов»
			(различить помогает ``buttons``): чужую заменяют целиком.
		views: сколько раз пост просмотрели (None — Telegram не сказал).
		group_id: номер группы у файлов одного альбома (None — обычный
			пост). В ленте альбом приходит несколькими записями подряд
			с общим номером — читателю же он виден одной (ADR-0033, C4).
	"""

	id: int
	text: str
	date: datetime
	entities: tuple[TextEntity, ...] = ()
	media_kind: MediaKind = MediaKind.NONE
	group_id: int | None = None
	topic_id: int | None = None
	buttons: int = 0
	markup: PostMarkup | None = None
	views: int | None = None


@dataclass(frozen=True)
class PublishedPage:
	"""Страница ленты: вышедшие посты и место, с которого продолжать.

	Лента читается от новых постов к старым страницами — так же, как
	история у обслуживания (ADR-0026): одна страница — один запрос
	через дорожку аккаунта (ADR-0024), между страницами дорожка
	пропускает вперёд публикацию.

	Attributes:
		messages: посты этой страницы (служебные записи отброшены —
			их показывает и чистит обслуживание).
		next_offset_id: с какого поста читать дальше; None — лента
			кончилась.
	"""

	messages: list[PublishedMessage]
	next_offset_id: int | None


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
		rights: полный снимок прав **проверявшего** исполнителя в этом
			сообществе (ADR-0035): как он участвует, что выдано ему как
			администратору и что можно как участнику. Снимок складывает
			сам транспорт, поэтому выше границы не видно, кто принёс
			ответ — бот или пользователь. До ADR-0035 здесь ездили
			четыре огрызка (роль, удаление, исключение, правка чужого),
			причём половину вычислял только один транспорт, а вторую —
			только другой.
	"""

	chat_id: str
	title: str
	username: str | None
	kind: CommunityKind
	rights: ExecutorRights
	forum: bool = False


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
class LinkPreview:
	"""Как показать превью ссылки у текстового поста (ADR-0033, подача C3).

	Превью — не часть текста и не вложение: Telegram собирает его сам
	по первой ссылке. Управлять им можно тремя способами, и все три
	просил владелец.

	Attributes:
		disabled: не показывать превью вовсе.
		large: крупное превью (обычно Telegram выбирает размер сам).
		above: превью над текстом, а не под ним.
		url: какую ссылку показывать (пусто — первую в тексте).

	У поста с вложением превью не бывает: там место занято файлом.
	"""

	disabled: bool = False
	large: bool = False
	above: bool = False
	url: str = ""

	def __bool__(self) -> bool:
		"""Просили ли что-то, кроме обычного поведения Telegram."""
		return self.disabled or self.large or self.above or bool(self.url)

	@property
	def needs_media(self) -> bool:
		"""Нужен ли путь «превью отдельным вложением».

		Крупное превью и превью над текстом Telegram принимает только
		вместе с самой ссылкой (``InputMediaWebPage``) — обычной
		отправкой текста их не задать. Выключение превью, наоборот,
		задаётся флагом обычной отправки.
		"""
		return not self.disabled and (self.large or self.above)


def preview_to_json(preview: LinkPreview) -> dict[str, object] | None:
	"""Настройки превью в JSON для колонки БД (None — обычное поведение).

	Пустая настройка даёт None: в базе не должно быть двух способов
	сказать «как решит Telegram».
	"""
	if not preview:
		return None
	return {
		"disabled": preview.disabled,
		"large": preview.large,
		"above": preview.above,
		"url": preview.url,
	}


def preview_from_json(raw: object) -> LinkPreview:
	"""Собирает настройки превью из значения колонки БД.

	Повреждённая запись не роняет восстановление очереди: пост уедет
	с обычным превью, а разбор останется в журнале — как у клавиатуры
	и разметки текста.
	"""
	if not raw:
		return LinkPreview()
	try:
		values = dict(raw)  # type: ignore[call-overload]
		return LinkPreview(
			disabled=bool(values.get("disabled")),
			large=bool(values.get("large")),
			above=bool(values.get("above")),
			url=str(values.get("url", "")),
		)
	except (TypeError, ValueError):
		logger.warning("Настройки превью в БД не разобрались — пост уедет с обычным.")
		return LinkPreview()


@dataclass(frozen=True)
class OutgoingFile:
	"""Файл исходящего поста: путь, вид и миниатюра.

	Attributes:
		path: путь к файлу на диске.
		kind: вид вложения.
		thumb_path: JPEG-миниатюра видео (None — без неё). У альбома
			миниатюр нет: Telegram берёт их из самих файлов.
	"""

	path: str
	kind: MediaKind
	thumb_path: str | None = None


class Urgency(StrEnum):
	"""Срочность публикации — одно понятие на дорожку и очередь (ADR-0036).

	**Срочный** пост — тот, чья минута уже наступила или не назначена
	вовсе: пост «сейчас», догон просроченного, режим «кнопки важнее»,
	дождавшийся своего срока. **Плановый** — отложенная запись с датой
	в будущем: её единственный срок — свободный слот, и ждать за короткими
	операциями ей ничего не стоит. Значение спрашивают у :func:`urgency`,
	а не считают заново: два потребителя (приоритет дорожки и порядок
	выбора в очереди отправки) обязаны понимать срочность одинаково.
	"""

	DUE = "due"
	PLANNED = "planned"


def urgency(when: datetime | None, now: datetime) -> Urgency:
	"""Срочность поста по его моменту публикации (ADR-0036).

	Args:
		when: назначенный момент публикации; None — «сейчас».
		now: текущее время (той же зоны, что ``when``).

	Returns:
		``DUE``, если момента нет или он уже наступил; иначе ``PLANNED``.
	"""
	if when is None or when <= now:
		return Urgency.DUE
	return Urgency.PLANNED


@dataclass(frozen=True)
class OutgoingPost:
	"""Исходящий пост для транспорта: текст или медиа с подписью.

	Одна сущность вместо длинного списка параметров: новые атрибуты
	поста не раздувают сигнатуры шлюза и транспорта.

	Attributes:
		text: текст поста или подпись к медиа.
		entities: разметка текста (ADR-0033; пусто — обычный текст,
			и тогда транспорт разбирает строку по-старому).
		preview: как показать превью ссылки (у поста с вложением
			превью не бывает).
		files: файлы поста: пусто — текст, один — обычное вложение,
			несколько — альбом (ADR-0033, подача C4).
		poll: опрос (None — обычный пост). Опрос исключает и текст,
			и файлы: у Telegram это самостоятельное вложение
			(ADR-0033, подача C5).
		when: момент публикации (None — «сейчас»).
		topic_id: тема форума (id корневого сообщения темы;
			None — общая лента, для каналов и обычных групп всегда None).
		as_community: опубликовать **от имени сообщества** (ADR-0036):
			транспорт передаёт ``send_as`` самим чатом, а не полагается
			на умолчание, выставленное в чужом клиенте. Имеет смысл
			только в группе от анонимного администратора — в канале
			пост и так от имени канала, и флаг там не ставится.
	"""

	text: str = ""
	entities: tuple[TextEntity, ...] = ()
	preview: LinkPreview = field(default_factory=LinkPreview)
	files: tuple[OutgoingFile, ...] = ()
	poll: PollDraft | None = None
	when: datetime | None = None
	topic_id: int | None = None
	as_community: bool = False

	@property
	def single(self) -> OutgoingFile | None:
		"""Единственный файл поста (None — текст или альбом)."""
		return self.files[0] if len(self.files) == 1 else None

	@property
	def is_album(self) -> bool:
		"""Пост — альбом: несколько файлов одной записью."""
		return len(self.files) > 1


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
		entities: разметка текста записи (ADR-0033) — форма правки
			обязана её показать, иначе сохранение сотрёт оформление.
		media_kind: вид вложения; ``NONE`` — текст (превью ссылки
			вложением не считается), ``OTHER`` — вложение, которого
			приложение не создаёт (опрос, геопозиция…).
		topic_id: тема форума, в которую адресована запись
			(None — общая лента или сообщество без тем).
	"""

	id: int
	text: str
	scheduled_at: datetime
	entities: tuple[TextEntity, ...] = ()
	media_kind: MediaKind = MediaKind.NONE
	topic_id: int | None = None
