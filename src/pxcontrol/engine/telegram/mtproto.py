"""Транспорт MTProto (через Telethon, отдельный аккаунт).

Основной путь публикации (ADR-0011): все типы контента, файлы до
2000/4000 МиБ (по Premium), отложенные — прямо в канале (серверное
планирование Telegram, ADR-0010). Здесь же — чтение отложенных, проверка
каналов и пошаговый вход userbot (код → 2FA → строка сессии); в будущем —
чтение каналов-источников.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.telegram.markup import ButtonKind, PostButton, PostMarkup
from pxcontrol.engine.telegram.refs import normalize_chat_ref, numeric_chat_id
from pxcontrol.engine.telegram.stats_graph import (
	GraphSeries,
	daily,
	hourly,
	named_daily,
	parse_graph,
	pick_series,
	shares,
)
from pxcontrol.engine.telegram.types import (
	FORUM_TOPICS_PAGE,
	TELEGRAM_MAX_SCHEDULED,
	CommunityAnalytics,
	CommunityInfo,
	CommunityKind,
	CommunityStatsInfo,
	DayPoint,
	DeletedAccount,
	ForumTopicInfo,
	HistoryMarks,
	MediaKind,
	NamedSeries,
	OutgoingPost,
	ParticipantsPage,
	PublishedMessage,
	PublishedPage,
	RecentPost,
	ScheduledMessage,
	ServiceMessageInfo,
	ServiceMessageKind,
	ServiceMessagesPage,
	Share,
	TelegramFloodError,
	TopAdmin,
	TopInviter,
	TopPoster,
	UserbotProfile,
	UserbotRole,
)

logger = logging.getLogger(__name__)


class LoginError(EngineError):
	"""Ошибка входа userbot с понятным человеку текстом."""


class UserbotUnavailableError(EngineError):
	"""Userbot не может выполнить операцию (базовый класс, понятный текст).

	Подклассы разводят причины, по которым сервисы принимают разные
	решения: временная недоступность (не подключён, нет связи, лимит) —
	не повод менять сохранённые в БД права; подтверждённый отказ
	Telegram (:class:`UserbotAccessError`) — повод.
	"""


class UserbotNotConnectedError(UserbotUnavailableError):
	"""Userbot не подключён или соединение с Telegram не удалось."""


class UserbotPausedError(UserbotNotConnectedError):
	"""Аккаунт приостановлен человеком (ADR-0029) — обращений к нему нет.

	Подкласс «не подключён» сознательно: для всех потребителей это
	та же временная недоступность (очередь ждёт, фоновые чтения
	пропускают аккаунт, зонды прав ничего не меняют), отличается лишь
	причина и текст — человеку нужно не «войти», а возобновить
	аккаунт в разделе «Пользователи и боты». Бросает шлюз, не транспорт:
	пауза — состояние приложения, а не соединения.
	"""


class UserbotSessionExpiredError(UserbotUnavailableError):
	"""Сессия userbot отозвана или недействительна — нужен повторный вход."""


class UserbotAccessError(UserbotUnavailableError):
	"""Подтверждённый отказ Telegram: нет прав или канал не виден."""


class UserbotDeleteForbiddenError(UserbotUnavailableError):
	"""Telegram отказался удалять сообщения (``MESSAGE_DELETE_FORBIDDEN``).

	Отдельный класс: обслуживание по нему считает пачку пропущенной
	и продолжает проход, а не валит задание (ADR-0026). Отказ приходит
	на защищённых записях (создание сообщества, передача владения)
	и на чужих сообщениях без права их удалять.
	"""


class UserbotScheduleFullError(UserbotUnavailableError):
	"""Все слоты отложенных сообщений канала заняты (лимит 100, ADR-0016).

	Отдельный класс: очередь отправки по нему возвращает элемент
	в ожидание слота, а не в ошибку (гонка с ручными отложками
	из клиента Telegram — штатная ситуация).
	"""


class UserbotMessageGoneError(UserbotUnavailableError):
	"""Записи, к которой обращались, в Telegram уже нет (``MESSAGE_ID_INVALID``).

	Отложка ушла в ленту или удалена из другого клиента между чтением
	списка и действием — штатная гонка (истина живёт на сервере,
	ADR-0010). Отдельный класс: это не отказ в правах и не сбой связи,
	а сигнал перечитать список.
	"""


class UserbotFloodError(TelegramFloodError, UserbotUnavailableError):
	"""Флуд-лимит на userbot-аккаунте: «подождите N секунд».

	Двойное наследование сознательно: для очереди отправки это
	``TelegramFloodError`` (подождать и повторить — не исход элемента),
	для остальных потребителей — прежняя временная недоступность
	userbot: существующие except-ветки продолжают работать без правок.
	"""


def _default_client(api_id: int, api_hash: str, session: str | None = None) -> Any:
	"""Создаёт клиента Telethon (пустая сессия — для входа)."""
	from telethon import TelegramClient
	from telethon.sessions import StringSession

	return TelegramClient(StringSession(session), api_id, api_hash)


def _map_login_error(exc: Exception) -> str:
	"""Переводит исключения Telethon в понятные сообщения."""
	from telethon import errors

	if isinstance(exc, errors.FloodWaitError):
		return f"Telegram просит подождать {exc.seconds} с перед новой попыткой."
	if isinstance(exc, errors.PhoneCodeInvalidError):
		return "Неверный код — начните вход заново."
	if isinstance(exc, errors.PhoneCodeExpiredError):
		return "Код устарел — начните вход заново."
	if isinstance(exc, errors.PhoneNumberInvalidError):
		return "Telegram не принял номер телефона."
	if isinstance(exc, errors.PasswordHashInvalidError):
		return "Неверный пароль двухфакторной защиты."
	return f"Не удалось войти: {exc}"


def _translate_error(exc: Exception) -> UserbotUnavailableError:
	"""Переводит исключение операции userbot в доменную ошибку.

	Разводит причины по подклассам :class:`UserbotUnavailableError`:
	сервисы различают «подтверждённый отказ» и «временную недоступность».
	"""
	from telethon import errors

	if isinstance(exc, errors.ChatAdminRequiredError):
		return UserbotAccessError(_NOT_ADMIN_TEXT)
	if isinstance(
		exc,
		errors.UserNotParticipantError
		| errors.ChannelPrivateError
		| errors.ChatWriteForbiddenError,
	):
		# подтверждённый отказ: userbot удалили из канала / канал закрыли
		# от него / запретили писать — основание снять хранимый флаг прав
		return UserbotAccessError(
			"Userbot не состоит в канале или не может в нём публиковать — "
			"добавьте аккаунт администратором с правом публиковать."
		)
	if isinstance(
		exc,
		errors.AuthKeyUnregisteredError
		| errors.SessionRevokedError
		| errors.SessionExpiredError
		| errors.UserDeactivatedError,
	):
		return UserbotSessionExpiredError(_SESSION_EXPIRED_TEXT)
	if isinstance(exc, errors.MessageDeleteForbiddenError):
		# отказ в удалении — не сбой прохода, а пропуск записи (ADR-0026).
		# Перевод живёт здесь, а не отдельной веткой except у операции:
		# там его уже не поймать — этот маппер переводит раньше
		return UserbotDeleteForbiddenError("Telegram не разрешает удалить эти записи.")
	if isinstance(exc, errors.MessageIdInvalidError):
		return UserbotMessageGoneError(
			"Этой записи в Telegram уже нет — она опубликована или удалена. Обновите список."
		)
	if isinstance(exc, errors.ScheduleDateInvalidError):
		return UserbotUnavailableError(
			"Telegram отклонил время публикации — выберите другое время."
		)
	if isinstance(exc, errors.SlowModeWaitError):
		# медленный режим группы действует на участников (ADR-0022):
		# по природе это «подожди и повтори» — очередь умеет сама
		return UserbotFloodError(
			f"Медленный режим группы: подождать {exc.seconds} с.",
			retry_after_s=exc.seconds,
		)
	if isinstance(exc, errors.FloodError):
		# всё семейство лимитов, назвавшее срок: обычный флуд-лимит,
		# отдельный лимит на загрузку медиа у не-Premium аккаунта
		# (FLOOD_PREMIUM_WAIT) и прочие «подождите N секунд».
		# Перечислять классы поимённо нельзя: семейство пополняется,
		# а в нём есть и бессрочные (FrozenMethodInvalidError) — срока
		# они не называют и уходят в общую ветку ниже
		seconds = getattr(exc, "seconds", None)
		if isinstance(seconds, int):
			return UserbotFloodError(
				f"Telegram просит подождать {seconds} с.", retry_after_s=seconds
			)
	if isinstance(exc, errors.ScheduleTooMuchError):
		return UserbotScheduleFullError(
			f"Все слоты отложенных сообщений канала заняты (лимит Telegram — "
			f"{TELEGRAM_MAX_SCHEDULED}) — пост подождёт освобождения слота."
		)
	if isinstance(exc, ValueError):
		# Telethon: «Could not find the input entity» — канал не в поле
		# зрения аккаунта (числовые ID валидируются до этой точки).
		# Прочие ValueError Telethon (битый аргумент, неразобранное имя) —
		# не про доступ: общий текст вместо ложного совета про права.
		if "entity" in str(exc).lower():
			return UserbotAccessError(
				"Userbot не видит этот канал — убедитесь, что аккаунт "
				"добавлен в канал администратором."
			)
		return UserbotUnavailableError(f"Telegram отклонил операцию: {exc}")
	if isinstance(exc, ConnectionError | OSError | TimeoutError):
		return UserbotNotConnectedError(
			"Нет связи с Telegram — проверьте сеть и попробуйте ещё раз."
		)
	return UserbotUnavailableError(f"Telegram отклонил операцию: {exc}")


@asynccontextmanager
async def _mtproto_errors() -> AsyncIterator[None]:
	"""Переводит исключения Telethon в доменные ошибки userbot.

	Единый маппер операций транспорта (парный ``_bot_errors`` в bot_api);
	уже доменные ошибки пропускает без изменений.
	"""
	try:
		yield
	except UserbotUnavailableError:
		raise
	except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
		raise _translate_error(exc) from exc


#: Единые тексты повторяющихся исходов. Три места «сессии» (перевод
#: исключения и две проверки is_user_authorized) и два места
#: «не администратор» не сводимы к одному raise, но текст у них обязан
#: быть общим — врозь формулировки уже начинали расходиться.
_SESSION_EXPIRED_TEXT = (
	"Сессия userbot недействительна — войдите в аккаунт заново: «Пользователи и боты»."
)
_NOT_ADMIN_TEXT = (
	"Userbot не администратор канала — добавьте аккаунт администратором с правом публиковать."
)


def has_admin_right(perms: Any, right: str) -> bool:
	"""Есть ли у аккаунта конкретное право администратора.

	Одно правило на все проверки прав userbot: владельцу сообщества
	можно всё, администратору — только то, что ему выдали поимённо.
	Роль «администратор» сама по себе не гарантирует ни удаления чужих
	сообщений, ни исключения участников (ADR-0026), поэтому каждое
	право спрашивается отдельно — но одним способом.

	У самой библиотеки есть одноимённые свойства (``delete_messages``
	и прочие), но они читают присланный набор флагов как есть — и для
	**владельца** отвечают ровно то, что прислал сервер. Проверено
	2026-09-13: владелец с неполным набором флагов получает от них
	«нельзя», хотя в Telegram владельцу нельзя урезать права в принципе.
	Поэтому владелец здесь — отдельная ветка, а не частный случай
	общего чтения флагов.

	Args:
		perms: ответ Telegram о правах аккаунта в сообществе.
		right: имя права в наборе ``admin_rights`` (``post_messages``,
			``delete_messages``, ``ban_users``).
	"""
	if getattr(perms, "is_creator", False):
		return True
	admin_rights = getattr(getattr(perms, "participant", None), "admin_rights", None)
	return bool(getattr(admin_rights, right, False))


def ensure_userbot_can_post(perms: Any) -> None:
	"""Требует права админа с публикацией (владельцу можно всё).

	Парная форма ``ensure_bot_can_post`` бот-пути: публичная функция
	модуля, тестируется по имени, а не через внутренности класса.

	Raises:
		UserbotAccessError: Прав не хватает (подтверждённый отказ —
			основание для сервисов менять привязку аккаунта, ADR-0019).
	"""
	if not perms.is_admin:
		raise UserbotAccessError(_NOT_ADMIN_TEXT)
	if not has_admin_right(perms, "post_messages"):
		raise UserbotAccessError("У userbot нет права публиковать сообщения в канале.")


def _forbids_sending(banned_rights: Any) -> bool:
	"""Запрещает ли набор ограничений отправку сообщений.

	Проверяются общий флаг ``send_messages`` и текстовый ``send_plain``
	(гранулярные права 2023 года). Медиа-права (``send_media``
	и подробнее) сознательно не проверяются: их сочетаний много,
	а отказ по конкретному типу вложения честно вернёт сама отправка.
	"""
	return bool(
		getattr(banned_rights, "send_messages", False)
		or getattr(banned_rights, "send_plain", False)
	)


def ensure_userbot_can_send(perms: Any, default_banned_rights: Any) -> None:
	"""Требует возможность писать в группе (ADR-0021).

	Групповая пара ``ensure_userbot_can_post``: права ``post_messages``
	в группах нет. Админам (и создателю) ограничения не мешают;
	ограниченный участник упирается в свои ограничения, обычный —
	в общие ограничения группы (в том числе гигагруппы: там писать
	могут только админы, что выражено теми же общими ограничениями).

	Args:
		perms: ``ParticipantPermissions`` самого аккаунта.
		default_banned_rights: общие ограничения группы
			(``entity.default_banned_rights``).

	Raises:
		UserbotAccessError: Аккаунт не участник или не может писать
			(подтверждённый отказ — основание менять привязку, ADR-0019).
	"""
	if perms.is_admin:
		return
	if perms.has_left:
		raise UserbotAccessError("Userbot не участник группы — вступите в неё с этого аккаунта.")
	if perms.is_banned and _forbids_sending(getattr(perms.participant, "banned_rights", None)):
		raise UserbotAccessError("Userbot ограничен в отправке сообщений в этой группе.")
	if _forbids_sending(default_banned_rights):
		raise UserbotAccessError(
			"В группе писать могут только администраторы — назначьте аккаунт администратором."
		)


def community_kind_from_entity(entity: Any) -> CommunityKind:
	"""Вид сообщества по сущности Telethon (ADR-0021).

	Каналы и супергруппы Telegram — один тип ``Channel`` с флагами:
	``broadcast`` — канал-вещалка, ``megagroup`` — супергруппа,
	``gigagroup`` — вещательная группа (считается группой: постинг
	в ней ограничен общими правами, а не правом ``post_messages``).
	Тип ``Chat`` — малая группа, всё прочее (пользователь) — не чат.

	Raises:
		UserbotAccessError: Сущность не подключается: малая группа —
			с подсказкой преобразовать в супергруппу, личный чат —
			с объяснением.
	"""
	if getattr(entity, "broadcast", False):
		return CommunityKind.CHANNEL
	if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
		return CommunityKind.GROUP
	from telethon.tl.types import Chat

	if isinstance(entity, Chat):
		raise UserbotAccessError(
			"Малые группы не подключаются — преобразуйте группу "
			"в супергруппу (в настройках группы) и повторите."
		)
	raise UserbotAccessError("Это личный чат — укажите канал или группу.")


def service_message_kind(action: Any) -> ServiceMessageKind:
	"""Вид служебной записи по её действию (ADR-0026).

	Действий в схеме Telegram больше шестидесяти, и перечислять их
	все незачем: человеку важны группы, а не отдельные конструкторы.
	Незнакомое действие попадает в «прочее служебное» — так новые
	виды (Telegram добавляет их регулярно) не теряются молча
	и не притворяются чем-то знакомым.

	Особый вид — ``PROTECTED``: записи, которые удалять нельзя.
	Главные среди них — корневые сообщения тем форума: идентификатор
	темы и есть идентификатор такого сообщения (на него мы отвечаем
	при публикации в тему), и удаление корня разрушило бы саму тему.
	Сюда же создание сообщества, переезд группы в супергруппу
	и передача владения — история, которую чистить нечего.
	"""
	from telethon.tl import types

	if isinstance(
		action,
		types.MessageActionChatAddUser
		| types.MessageActionChatDeleteUser
		| types.MessageActionChatJoinedByLink
		| types.MessageActionChatJoinedByRequest,
	):
		return ServiceMessageKind.MEMBERS
	if isinstance(action, types.MessageActionPinMessage):
		return ServiceMessageKind.PINS
	if isinstance(
		action,
		types.MessageActionChatEditTitle
		| types.MessageActionChatEditPhoto
		| types.MessageActionChatDeletePhoto
		| types.MessageActionSetChatTheme
		| types.MessageActionSetChatWallPaper
		| types.MessageActionSetMessagesTTL,
	):
		return ServiceMessageKind.APPEARANCE
	if isinstance(
		action,
		types.MessageActionGroupCall
		| types.MessageActionGroupCallScheduled
		| types.MessageActionInviteToGroupCall
		| types.MessageActionConferenceCall,
	):
		return ServiceMessageKind.CALLS
	if isinstance(
		action,
		types.MessageActionTopicCreate
		| types.MessageActionTopicEdit
		| types.MessageActionChatCreate
		| types.MessageActionChannelCreate
		| types.MessageActionChatMigrateTo
		| types.MessageActionChannelMigrateFrom
		| types.MessageActionChangeCreator
		| types.MessageActionNewCreatorPending,
	):
		return ServiceMessageKind.PROTECTED
	return ServiceMessageKind.OTHER


def media_kind_of(media: Any) -> MediaKind:
	"""Переводит вложение сообщения Telegram в вид вложения приложения.

	Чистая функция (тестируется без сети). Превью ссылки
	(``MessageMediaWebPage``) вложением не считается — это текст.
	Документ различается по атрибутам так же, как его различает
	Telethon (``Message.video``/``audio``): видео — атрибут видео,
	аудио и голосовое — атрибут аудио, остальное — файл. Всё, чего
	приложение не создаёт (опрос, геопозиция, контакт, стикер…), —
	``OTHER``: у такой записи правится только время.
	"""
	from telethon.tl import types

	if media is None or isinstance(media, types.MessageMediaWebPage):
		return MediaKind.NONE
	if isinstance(media, types.MessageMediaPhoto):
		return MediaKind.PHOTO
	if isinstance(media, types.MessageMediaDocument):
		document = getattr(media, "document", None)
		for attribute in getattr(document, "attributes", None) or ():
			if isinstance(attribute, types.DocumentAttributeVideo):
				return MediaKind.VIDEO
			if isinstance(attribute, types.DocumentAttributeAudio):
				return MediaKind.AUDIO
		return MediaKind.DOCUMENT
	return MediaKind.OTHER


def markup_button_count(markup: Any) -> int:
	"""Сколько кнопок стоит под постом (0 — клавиатуры нет).

	Чистая функция (тестируется без сети). Считаются кнопки всех рядов:
	человеку на карточке важно, стоят кнопки или нет, а не как они
	разложены. Чужие виды клавиатур (у своих постов их не бывает,
	но лента общая) считаются так же — по числу кнопок в рядах.
	"""
	rows = getattr(markup, "rows", None) or ()
	return sum(len(getattr(row, "buttons", None) or ()) for row in rows)


def markup_from(markup: Any) -> PostMarkup | None:
	"""Разбирает клавиатуру поста в наш тип (None — не наших видов).

	Чистая функция (тестируется без сети). Приложение умеет два вида
	кнопок — ссылку и «скопировать текст» (ADR-0031); всё остальное
	(callback, переход в бота, оплата) поставили не мы, и притворяться,
	что мы это правим, нельзя: форма правки показала бы кнопку одним
	видом, а сохранение подменило бы её другим. Поэтому у чужой
	клавиатуры возвращается None — человеку честно говорят, что заменить
	её можно только целиком.
	"""
	from telethon.tl import types

	rows_source = getattr(markup, "rows", None)
	if rows_source is None:
		return None
	rows: list[tuple[PostButton, ...]] = []
	for row in rows_source:
		buttons: list[PostButton] = []
		for button in getattr(row, "buttons", None) or ():
			if isinstance(button, types.KeyboardButtonUrl):
				buttons.append(PostButton(ButtonKind.LINK, button.text, button.url))
			elif isinstance(button, types.KeyboardButtonCopy):
				buttons.append(PostButton(ButtonKind.COPY, button.text, button.copy_text))
			else:
				return None  # кнопка не нашего вида — клавиатура чужая
		rows.append(tuple(buttons))
	return PostMarkup(tuple(rows))


def _topic_of(message: Any) -> int | None:
	"""Тема форума, в которую адресовано сообщение (None — общая лента).

	Тема Telegram — это её корневое сообщение (ADR-0021): сообщение
	в теме несёт заголовок ответа с признаком ``forum_topic``, где
	корень темы — ``reply_to_top_id`` (у ответа внутри темы) либо
	``reply_to_msg_id`` (у обычного сообщения темы).
	"""
	header = getattr(message, "reply_to", None)
	if header is None or not getattr(header, "forum_topic", False):
		return None
	top = getattr(header, "reply_to_top_id", None)
	return int(top) if top is not None else getattr(header, "reply_to_msg_id", None)


def _published_from(message: Any) -> PublishedMessage:
	"""Собирает вышедший пост границы из сообщения Telethon (у него есть дата)."""
	return PublishedMessage(
		id=int(message.id),
		text=getattr(message, "message", "") or "",
		date=message.date,
		media_kind=media_kind_of(getattr(message, "media", None)),
		topic_id=_topic_of(message),
		buttons=markup_button_count(getattr(message, "reply_markup", None)),
		markup=markup_from(getattr(message, "reply_markup", None)),
		views=_opt_int(getattr(message, "views", None)),
	)


def _scheduled_from(message: Any) -> ScheduledMessage:
	"""Собирает запись границы из сообщения Telethon (у него есть дата)."""
	return ScheduledMessage(
		id=int(message.id),
		text=getattr(message, "message", "") or "",
		scheduled_at=message.date,
		media_kind=media_kind_of(getattr(message, "media", None)),
		topic_id=_topic_of(message),
	)


def _can_delete_messages(perms: Any) -> bool:
	"""Может ли аккаунт удалять чужие сообщения (право админа).

	Владельцу можно всё; администратору — только при праве
	``delete_messages``. Роль сама по себе его не гарантирует
	(ADR-0026), поэтому обслуживание спрашивает именно это.
	"""
	return has_admin_right(perms, "delete_messages")


def _service_message_id(produced: Any) -> int | None:
	"""Идентификатор служебной записи из ответа Telethon на исключение.

	Ответ приходит в трёх видах, и это не наша прихоть, а устройство
	библиотеки: ``kick_participant`` разбирает обновления Telegram
	методом ``_get_response_message(None, …)``, а тот при пустом
	запросе возвращает **словарь** «id → сообщение» (так сказано в его
	же docstring). Служебную запись об исключении он отдаёт напрямую
	отдельной веткой, а когда записи нет вовсе — остаётся пустой
	словарь. Отсюда три случая: сообщение, словарь (возможно пустой)
	и None.

	Returns:
		Идентификатор записи или None, если Telegram её не создал.
	"""
	if produced is None:
		return None
	if isinstance(produced, dict):
		ids = [int(key) for key in produced]
		# несколько записей разом маловероятно, но если так — берём
		# последнюю: именно она об этом исключении
		return max(ids) if ids else None
	message_id = getattr(produced, "id", None)
	return int(message_id) if message_id is not None else None


def _can_ban_users(perms: Any) -> bool:
	"""Может ли аккаунт исключать участников (право админа).

	Чистка удалённых аккаунтов — это исключение участников, и права
	на неё у роли «админ» может не быть (ADR-0026).
	"""
	return has_admin_right(perms, "ban_users")


def _peer_id(chat_id: str) -> int:
	"""Числовой ID из строки БД (:func:`refs.numeric_chat_id` с нашим классом)."""
	return numeric_chat_id(chat_id, UserbotUnavailableError)


async def _fetch_premium(client: Any) -> bool:
	"""Статус Premium аккаунта (от него зависит лимит на файл).

	Запрос вспомогательный: его сбой не мешает подключению — статус
	считается False, действует меньший (безопасный) лимит.
	"""
	try:
		me = await client.get_me()
	except Exception:  # noqa: BLE001 — деградация к меньшему лимиту
		logger.warning("Не удалось узнать статус Premium.", exc_info=True)
		return False
	return bool(getattr(me, "premium", False))


async def _safe_disconnect(client: Any) -> None:
	"""Закрывает клиента; ошибки закрытия не роняют процесс."""
	try:
		await client.disconnect()
	except Exception:  # noqa: BLE001 — закрытие не должно ронять операцию
		logger.debug("Не удалось корректно закрыть клиента.", exc_info=True)


class MtprotoTransport:
	"""Подключённый userbot: отложенные посты и чтение каналов."""

	def __init__(
		self,
		client_factory: Callable[[int, str, str | None], Any] | None = None,
	) -> None:
		self._client_factory = client_factory or _default_client
		self._creds: tuple[int, str, str] | None = None
		self._client: Any | None = None
		self._premium = False
		# одно переподключение за раз: параллельные операции ждут его итога
		self._reconnect_lock = asyncio.Lock()

	@property
	def premium(self) -> bool:
		"""Есть ли у подключённого аккаунта подписка Premium.

		Определяет лимит на файл (2000/4000 МиБ). Обновляется при каждом
		подключении и переподключении; без подключения — False.
		"""
		return self._premium

	@property
	def connected(self) -> bool:
		"""Есть ли сейчас живое соединение с Telegram (снимок, без запросов).

		Для показа состояния аккаунта: клиент создан и соединение
		не потеряно. Само соединение это свойство не чинит — чинит
		первая операция (:meth:`_connected_client`).
		"""
		return self._client is not None and bool(self._client.is_connected())

	def configure(self, api_id: int, api_hash: str, session: str) -> None:
		"""Задаёт реквизиты подключения (из БД, ADR-0009)."""
		self._creds = (api_id, api_hash, session)

	async def start(self) -> None:
		"""Подключает клиента, если заданы реквизиты и ещё не подключён.

		Клиент считается подключённым только после успешного соединения
		и проверки, что сессия жива: неудачная попытка не оставляет
		«полуживого» клиента — повторный ``start()`` попробует заново.

		Raises:
			UserbotNotConnectedError: Соединение с Telegram не удалось.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
		"""
		if self._client is not None:
			return
		if self._creds is None:
			logger.info("Аккаунт MTProto не настроен — userbot отключён.")
			return
		# замок общий с переподключением: два одновременных старта не должны
		# создать двух клиентов (второй остался бы подключённым без владельца)
		async with self._reconnect_lock:
			if self._client is not None:
				return
			api_id, api_hash, session = self._creds
			client = self._client_factory(api_id, api_hash, session)
			try:
				await client.connect()
				# оговорка: is_user_authorized глотает RPC-отказы Telethon
				# и возвращает False — редкий флуд именно на этом шаге
				# покажется отзывом сессии (окно узкое, принято)
				authorized = bool(await client.is_user_authorized())
			except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
				await _safe_disconnect(client)
				translated = _translate_error(exc)
				if isinstance(translated, UserbotNotConnectedError):
					# сетевой сбой — текст с советом именно для подключения
					raise UserbotNotConnectedError(
						"Не удалось подключить userbot — нет связи с Telegram. "
						"Проверьте сеть и попробуйте ещё раз."
					) from exc
				# класс не зависит от момента ошибки: флуд остаётся флудом,
				# отзыв сессии — отзывом, а не ложным «нет связи»
				raise translated from exc
			if not authorized:
				await _safe_disconnect(client)
				raise UserbotSessionExpiredError(_SESSION_EXPIRED_TEXT)
			self._client = client
			self._premium = await _fetch_premium(client)
			logger.info(
				"MTProto клиент подключён (Premium: %s).",
				"да" if self._premium else "нет",
			)

	async def stop(self) -> None:
		"""Отключает клиента MTProto.

		Замок общий с подключением: остановка во время идущего
		переподключения иначе оставила бы подключённого клиента-сироту
		(переподключение вернуло бы клиента, которого уже никто
		не хранит), а остановка во время ``start()`` молча «проглотилась»
		бы — старт доподключил бы клиента после неё.
		"""
		async with self._reconnect_lock:
			if self._client is not None:
				await _safe_disconnect(self._client)
				self._client = None
			self._premium = False

	def _require_client(self) -> Any:
		"""Возвращает клиента (возможно, без соединения) или объясняет, чего не хватает."""
		if self._client is None:
			raise UserbotNotConnectedError(
				"Userbot не подключён — войдите в аккаунт: «Пользователи и боты»."
			)
		return self._client

	async def _client_and_entity(self, chat_id: str) -> tuple[Any, Any]:
		"""Подключённый клиент и сущность сообщества — общий пролог операций.

		Шесть операций транспорта начинались одинаковой четвёркой строк
		(подключиться → разобрать chat_id → спросить у Telegram сущность).
		Любое изменение правила — кэш сущностей, отдельный перевод ошибки
		разрешения — пришлось бы вносить в шесть мест.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		client = await self._connected_client()
		async with _mtproto_errors():
			entity = await client.get_input_entity(_peer_id(chat_id))
		return client, entity

	async def _connected_client(self) -> Any:
		"""Возвращает клиента с живым соединением, при обрыве — переподключает.

		Telethon, исчерпав свои попытки переподключения (например, сеть
		пропала на несколько минут), остаётся отключённым насовсем —
		без этой проверки разовый обрыв требовал бы перезапуска
		приложения. Тот же ремонт покрывает неудачный старт (приложение
		запустили до появления сети): клиента ещё нет, но реквизиты
		сохранены — операция сама пробует ``start()``. Замок пускает
		в переподключение одну операцию; остальные ждут и получают уже
		готового клиента.

		Raises:
			UserbotNotConnectedError: Userbot не настроен или связь
				восстановить не удалось.
			UserbotSessionExpiredError: Сессия отозвана за время простоя.
		"""
		if self._client is None and self._creds is not None:
			await self.start()  # запуск без сети — чиним повторным стартом
		client = self._require_client()
		if client.is_connected():
			return client
		async with self._reconnect_lock:
			client = self._require_client()  # клиент мог смениться за время ожидания
			if client.is_connected():
				return client
			logger.info("Соединение MTProto потеряно — переподключаю…")
			try:
				await client.connect()
				# та же оговорка про is_user_authorized, что и в start()
				authorized = bool(await client.is_user_authorized())
			except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
				translated = _translate_error(exc)
				if isinstance(translated, UserbotNotConnectedError):
					raise UserbotNotConnectedError(
						"Нет связи с Telegram — переподключить userbot не удалось. "
						"Проверьте сеть и повторите операцию."
					) from exc
				# класс не зависит от момента ошибки (см. start())
				raise translated from exc
			if not authorized:
				raise UserbotSessionExpiredError(_SESSION_EXPIRED_TEXT)
			# подписка могла кончиться/появиться за время простоя
			self._premium = await _fetch_premium(client)
			logger.info("MTProto клиент переподключён.")
			return client

	async def publish(
		self,
		chat_id: str,
		post: OutgoingPost,
		on_progress: Callable[[float], None] | None = None,
	) -> int:
		"""Публикует пост: текст или медиа с подписью, сразу или отложенно.

		Единый транспорт публикации — userbot (ADR-0011): лимит Bot API
		на файлы (50 МБ) мал для видео, отложенные (schedule_date) хранит
		и публикует сервер Telegram (ADR-0010). ``on_progress`` получает
		долю загрузки файла 0.0..1.0 (большие файлы — это минуты).
		Миниатюру Telegram принимает, только когда известны размеры
		видео — их извлекает hachoir.

		Returns:
			Номер отправленного сообщения (у отложенного — номер записи
			в очереди отложенных сервера): по нему бот дорисовывает
			кнопки (ADR-0031).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен новый вход.
			UserbotAccessError: Аккаунт не видит сообщество или не может
				в нём публиковать.
			UserbotScheduleFullError: Слоты отложенных заняты (ADR-0016).
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		client = await self._connected_client()
		peer = _peer_id(chat_id)

		def _progress(sent: int, total: int) -> None:
			if on_progress is not None and total > 0:
				on_progress(sent / total)

		async with _mtproto_errors():
			# тема форума адресуется ответом на её корневое сообщение
			if post.media_path is None:
				sent = await client.send_message(
					peer, post.text, schedule=post.when, reply_to=post.topic_id
				)
			else:
				sent = await client.send_file(
					peer,
					post.media_path,
					caption=post.text or None,
					schedule=post.when,
					supports_streaming=post.media_kind is MediaKind.VIDEO,
					force_document=post.media_kind is MediaKind.DOCUMENT,
					progress_callback=_progress,
					thumb=post.thumb_path,
					reply_to=post.topic_id,
				)
		message_id = int(getattr(sent, "id", 0))
		logger.info(
			"Пост id=%s отправлен в чат %s (%s, %s).",
			message_id,
			chat_id,
			post.media_kind if post.media_path else "текст",
			f"отложено на {post.when}" if post.when else "сразу",
		)
		return message_id

	async def me(self) -> UserbotProfile:
		"""Профиль владельца сессии: @имя и имя (живой запрос «кто я»).

		Из него актуализируются данные аккаунта в БД — владелец мог
		сменить имя или @имя в Telegram. Пустые строки Telethon
		нормализуются в None: полей у аккаунта просто нет.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotUnavailableError: Прочие отказы Telegram (включая флуд).
		"""
		client = await self._connected_client()
		async with _mtproto_errors():
			me = await client.get_me()
		if me is None:
			# библиотека отвечает пустотой, когда сессия не авторизована;
			# без этой проверки профиль аккаунта затёрся бы пустыми
			# полями, хотя ответа от Telegram не было вовсе
			raise UserbotSessionExpiredError(_SESSION_EXPIRED_TEXT)
		return UserbotProfile(
			username=getattr(me, "username", None) or None,
			first_name=getattr(me, "first_name", None) or None,
			last_name=getattr(me, "last_name", None) or None,
		)

	async def check_community(self, chat_ref: str) -> CommunityInfo:
		"""Проверяет сообщество и права userbot по его виду (ADR-0021).

		Канал: аккаунт — админ с правом публиковать. Группа
		(супергруппа): участник, не ограниченный в отправке. Малая
		группа и личный чат не подключаются. Принимает @имя, ссылку
		t.me/… или ID -100… (разбор общий с бот-путём —
		``normalize_chat_ref``).

		Raises:
			ChatRefError: Введённую ссылку/имя не удалось разобрать.
			UserbotAccessError: Прав не хватает или вид не подключается
				(малая группа, личный чат) — подтверждённый отказ.
			UserbotUnavailableError: Userbot не подключён или сообщество
				не найдено.
		"""
		from telethon import utils

		client = await self._connected_client()
		ref = normalize_chat_ref(chat_ref)
		async with _mtproto_errors():
			entity = await client.get_entity(ref)
			perms = await client.get_permissions(entity, "me")
		kind = community_kind_from_entity(entity)
		if kind is CommunityKind.CHANNEL:
			ensure_userbot_can_post(perms)
		else:
			ensure_userbot_can_send(perms, getattr(entity, "default_banned_rights", None))
		return CommunityInfo(
			chat_id=str(utils.get_peer_id(entity)),
			title=str(getattr(entity, "title", "") or chat_ref),
			username=getattr(entity, "username", None),
			kind=kind,
			forum=bool(getattr(entity, "forum", False)),
			# роль — бесплатный побочный продукт зонда (ADR-0022)
			role=UserbotRole.ADMIN if perms.is_admin else UserbotRole.MEMBER,
			# права, нужные обслуживанию, — тоже (ADR-0026)
			can_delete=_can_delete_messages(perms),
			can_ban=_can_ban_users(perms),
		)

	async def get_forum_topics(self, chat_id: str) -> list[ForumTopicInfo]:
		"""Читает темы форума (id и название), «General» — id 1.

		Одним запросом до 100 тем: больше на живых форумах — экзотика;
		если тем всё же больше, хвост не читается — об этом след в логе
		(правило «нет молчаливых обрезаний»).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotUnavailableError: Прочие отказы Telegram (не форум,
				чат не виден и т.п.).
		"""
		from telethon.tl.functions.messages import GetForumTopicsRequest

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			result = await client(
				GetForumTopicsRequest(
					peer=entity,
					offset_date=None,
					offset_id=0,
					offset_topic=0,
					limit=FORUM_TOPICS_PAGE,
				)
			)
		topics = [
			ForumTopicInfo(
				id=topic.id,
				title=topic.title,
				closed=bool(getattr(topic, "closed", False)),
			)
			for topic in result.topics
			if getattr(topic, "title", None) is not None  # ForumTopicDeleted — без названия
		]
		total = getattr(result, "count", len(topics))
		if total > len(result.topics):
			logger.warning(
				"Форум %s: тем больше лимита выборки (%s > %s) — хвост не показан.",
				chat_id,
				total,
				len(result.topics),
			)
		return topics

	async def community_stats(self, chat_id: str) -> CommunityStatsInfo:
		"""Читает подписчиков и онлайн сообщества (один запрос Telegram).

		``GetFullChannelRequest`` отдаёт оба поля разом; супергруппы
		в MTProto — те же каналы, малые группы не подключаются
		(ADR-0021), поэтому запрос един для обоих видов.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotFloodError: Флуд-лимит — вызывающий пропускает аккаунт.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.functions.channels import GetFullChannelRequest

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			result = await client(GetFullChannelRequest(channel=entity))
		full = result.full_chat
		linked = getattr(full, "linked_chat_id", None)
		return CommunityStatsInfo(
			participants=getattr(full, "participants_count", None),
			online=getattr(full, "online_count", None) or None,
			can_view_stats=bool(getattr(full, "can_view_stats", False)),
			# Telethon отдаёт голый id канала; в приложении сообщества
			# ходят в формате Bot API (-100…) — так связь найдётся по БД
			linked_chat_id=f"-100{linked}" if linked else None,
		)

	async def community_analytics(self, chat_id: str) -> CommunityAnalytics:
		"""Встроенная статистика Telegram: рост, приходы/уходы, часы, просмотры.

		Метод выбирается по виду сообщества (канал / супергруппа — как
		в ``client.get_stats`` Telethon); Telegram может ответить
		«мигрируй» (``STATS_MIGRATE``) — тогда и статистика, и все
		догрузки графиков «по токену» (``StatsGraphAsync``) идут через
		один одолженный канал в дата-центр статистики: токен выдан им,
		и домашний дата-центр отвечает на него не миграцией,
		а ``GRAPH_INVALID_RELOAD`` (ловилось живьём). Сырые объекты
		дальше транспорта не уходят: JSON графиков разбирает
		:mod:`stats_graph`.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Статистика недоступна (не админ, мало
				участников — Telegram отвечает «нужен админ»).
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			stats, dc = await _fetch_stats(client, entity)
			sender = await client._borrow_exported_sender(dc) if dc is not None else None  # noqa: SLF001 — приём самого Telethon
			try:
				send = sender.send if sender is not None else client
				growth = await _graph(send, getattr(stats, "growth_graph", None))
				# у канала ряд подписок/отписок — followers_graph, у группы — members_graph
				flow = await _graph(
					send,
					getattr(stats, "followers_graph", None)
					or getattr(stats, "members_graph", None),
				)
				hours = await _graph(send, getattr(stats, "top_hours_graph", None))
				# остальные графики — все, что отдал ответ: ряды по дням и доли
				daily_graphs = {
					field: await _graph(send, getattr(stats, attr, None))
					for field, attr in _DAILY_GRAPHS.items()
				}
				share_graphs = {
					field: await _graph(send, _first_attr(stats, attrs))
					for field, attrs in _SHARE_GRAPHS.items()
				}
			finally:
				if sender is not None:
					await client._return_exported_sender(sender)  # noqa: SLF001
		return _analytics_from(stats, growth, flow, hours, daily_graphs, share_graphs)

	async def history_marks(self, chat_id: str, *, with_created: bool) -> HistoryMarks:
		"""Момент последнего сообщения и — по запросу — создания сообщества.

		Последнее сообщение — одно чтение истории; создание — первое
		сообщение сообщества (служебная запись «создано», id 1); у группы
		после переезда в супергруппу её может не быть — тогда None.

		Raises: как у :meth:`community_stats`.
		"""
		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			latest = await client.get_messages(entity, limit=1)
			last_post_at = latest[0].date if latest else None
			created_at = None
			if with_created:
				first = await client.get_messages(entity, ids=1)
				created_at = getattr(first, "date", None)
		return HistoryMarks(last_post_at=last_post_at, created_at=created_at)

	async def download_avatar(self, chat_id: str, target: str) -> str | None:
		"""Скачивает аватар сообщества в файл ``target``.

		Returns:
			Путь скачанного файла или None — у сообщества нет аватара.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotSessionExpiredError: Сессия отозвана — нужен вход заново.
			UserbotFloodError: Флуд-лимит — вызывающий пропускает аккаунт.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			path = await client.download_profile_photo(entity, file=target)
		return str(path) if path else None

	async def find_published(
		self, chat_id: str, text: str, after: datetime, limit: int
	) -> int | None:
		"""Ищет вышедший пост по тексту среди свежих записей (ADR-0031, п. 9).

		Отложенную запись публикует сервер Telegram, и у вышедшего поста
		**новый** номер — поэтому его приходится опознавать. Совпадение
		считается доказанным только при точном равенстве текста (или
		подписи) и дате не раньше названной. Двусмысленность — не повод
		угадывать: два одинаковых поста дают None, и кнопки не ставятся
		вовсе. Промах здесь хуже отсутствия кнопок — клавиатура
		приклеилась бы к чужому посту.

		Args:
			chat_id: сообщество.
			text: текст или подпись поста, каким мы его отправляли.
			after: раньше этого момента пост появиться не мог.
			limit: сколько свежих записей просмотреть.

		Returns:
			Номер поста или None (не нашёлся либо нашёлся не один).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Флуд-лимит — дозор отступает и повторит.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			history = await client.get_messages(entity, limit=limit)
		found: list[int] = []
		for message in history:
			date = getattr(message, "date", None)
			if date is None or date < after:
				continue
			if (getattr(message, "message", None) or "") != text:
				continue
			found.append(int(message.id))
		if len(found) == 1:
			return found[0]
		if found:
			logger.warning(
				"В чате %s нашлось %d постов с одинаковым текстом — кнопки не ставлю.",
				chat_id,
				len(found),
			)
		return None

	async def history_page(self, chat_id: str, offset_id: int, limit: int) -> PublishedPage:
		"""Читает страницу ленты сообщества: вышедшие посты от новых к старым.

		Своей таблицы постов у приложения нет (ADR-0010) — истина живёт
		в самом сообществе, поэтому «Опубликовано» читает ленту. Механика
		та же, что у обслуживания (ADR-0026): одна страница — один
		запрос, дорожка аккаунта держит темп и между страницами
		пропускает вперёд публикацию.

		Служебные записи (вступил, закрепил, сменил фото) постами
		не считаются и отбрасываются: их показывает и чистит
		обслуживание.

		Args:
			chat_id: сообщество.
			offset_id: читать записи старше этого id (0 — с самых новых).
			limit: сколько сообщений прочитать (Telegram отдаёт до 100).

		Returns:
			Страницу ленты и id, с которого продолжать (None — лента
			кончилась).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Флуд-лимит — чтение прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.types import MessageService

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			history = await client.get_messages(entity, limit=limit, offset_id=offset_id)
		messages = [
			_published_from(message)
			for message in history
			if not isinstance(message, MessageService)
			and getattr(message, "date", None) is not None
		]
		# конец ленты — как у обслуживания: короткая страница концом
		# не считается (Telegram отдаёт меньше запрошенного и в середине
		# истории), честный признак — пустая страница или пост с номером 1
		oldest = next(
			(item for item in reversed(history) if getattr(item, "date", None) is not None),
			None,
		)
		return PublishedPage(
			messages=messages,
			next_offset_id=oldest.id if oldest is not None and oldest.id > 1 else None,
		)

	async def get_post(self, chat_id: str, message_id: int) -> PublishedMessage | None:
		"""Читает один вышедший пост целиком (свежее состояние с сервера).

		Форма правки открывается по этому чтению, а не по снимку ленты:
		пост могли изменить из другого клиента Telegram, а истина живёт
		в самом сообществе (ADR-0010).

		Returns:
			Пост или None — его в ленте больше нет (удалён).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			messages = await client.get_messages(entity, ids=[message_id])
		for message in messages or ():
			# удалённый пост приходит пустышкой без даты — это «его нет»
			if message is not None and getattr(message, "date", None) is not None:
				return _published_from(message)
		return None

	async def edit_post(self, chat_id: str, message_id: int, text: str) -> None:
		"""Меняет текст вышедшего поста (``messages.editMessage``).

		Тот же метод, что правит отложенные, но без ``schedule_date``:
		сервер ищет сообщение в ленте, а не в очереди отложенных.
		Клавиатуру правка публикателя не трогает — кнопки под постом
		остаются на месте (проверено живьём 15.09.2026, ADR-0031);
		снять или изменить их может только бот. Ответ «ничего
		не изменилось» (``MESSAGE_NOT_MODIFIED``) — не сбой: пост уже
		в запрошенном виде.

		Args:
			chat_id: сообщество.
			message_id: номер поста в ленте.
			text: новый текст (у поста с вложением — подпись; пустая
				строка снимает подпись).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Поста в ленте уже нет.
			UserbotAccessError: Нет права править (в группе правит только
				автор — подтверждённый отказ).
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon import errors

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			try:
				await client.edit_message(entity, message_id, text)
			except errors.MessageNotModifiedError:
				logger.info(
					"Пост %s в чате %s уже в запрошенном виде — правка не нужна.",
					message_id,
					chat_id,
				)
				return
		logger.info("Пост %s в чате %s изменён.", message_id, chat_id)

	async def service_messages_page(
		self, chat_id: str, offset_id: int, limit: int
	) -> ServiceMessagesPage:
		"""Читает страницу истории и отбирает из неё служебные записи.

		Серверного фильтра «только служебные» у Telegram нет
		(есть фильтры по типу вложения), поэтому история читается
		целиком и отбор идёт у нас. Одна страница — один запрос:
		дорожка аккаунта (ADR-0024) держит темп, а между страницами
		пропускает вперёд публикацию.

		Args:
			chat_id: сообщество.
			offset_id: читать записи старше этого id (0 — с самых новых).
			limit: сколько сообщений прочитать (Telegram отдаёт до 100).

		Returns:
			Страницу: служебные записи, число просмотренных сообщений
			и id, с которого продолжать (None — история кончилась).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.types import MessageService

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			history = await client.get_messages(entity, limit=limit, offset_id=offset_id)
		found = [
			ServiceMessageInfo(
				id=message.id,
				kind=service_message_kind(message.action),
				date=message.date,
			)
			for message in history
			if isinstance(message, MessageService)
		]
		# пустышки (удалённые сообщения) приходят без даты — для отчёта
		# «просмотрено до такого-то числа» годится последняя настоящая
		oldest = next(
			(item for item in reversed(history) if getattr(item, "date", None) is not None),
			None,
		)
		return ServiceMessagesPage(
			messages=found,
			scanned=len(history),
			# история кончилась только на пустой странице. Короткая
			# страница концом не считается: Telegram отдаёт меньше
			# запрошенного и в середине истории (скрытые по местным
			# законам записи, пропуски), а Telethon вдобавок выбрасывает
			# пустышки — счёт занижается. Второй честный признак конца:
			# самая старая запись с номером 1, раньше неё ничего нет
			next_offset_id=oldest.id if oldest is not None and oldest.id > 1 else None,
			oldest_date=oldest.date if oldest is not None else None,
		)

	async def delete_messages(self, chat_id: str, message_ids: list[int]) -> int:
		"""Удаляет сообщения сообщества; возвращает, сколько удалилось.

		Telegram отказывает в удалении части служебных записей
		(ошибка ``MESSAGE_DELETE_FORBIDDEN``) — например, сообщения
		о создании сообщества. Такой отказ не должен валить весь проход,
		поэтому пачка, которую сервер отверг целиком, считается
		пропущенной: 0 удалённых и след в логе (ADR-0026).

		Число удалённых — это число записей, которые сервер принял
		без отказа. Telethon отдаёт ``AffectedMessages`` со счётчиком
		изменений состояния, но он считает события обновления, а не
		сообщения, и подменять им ответ «принято» значило бы показывать
		человеку величину другой природы.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Нет права удалять (подтверждённый отказ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.

		Note:
			:class:`UserbotDeleteForbiddenError` наружу не выходит —
			это и есть пропуск пачки (0 удалённых).
		"""
		if not message_ids:
			return 0
		client = await self._connected_client()
		peer_id = _peer_id(chat_id)
		try:
			async with _mtproto_errors():
				await client.delete_messages(peer_id, message_ids)
		except UserbotDeleteForbiddenError:
			logger.info(
				"Удаление %d служебных записей чата %s отклонено Telegram — пропускаем.",
				len(message_ids),
				chat_id,
			)
			return 0
		return len(message_ids)

	async def participants_page(self, chat_id: str, offset: int, limit: int) -> ParticipantsPage:
		"""Читает страницу участников и отбирает удалённые аккаунты.

		Список участников Telegram отдаёт только администратору
		и порциями (до 200 за запрос). Одна страница — один запрос:
		темп держит дорожка аккаунта (ADR-0024).

		Returns:
			Страницу: идентификаторы удалённых учёток, число
			просмотренных участников и смещение для продолжения
			(None — список кончился).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Список участников недоступен (нужен админ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.functions.channels import GetParticipantsRequest
		from telethon.tl.types import ChannelParticipantsRecent

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			result = await client(
				GetParticipantsRequest(
					channel=entity,
					filter=ChannelParticipantsRecent(),
					offset=offset,
					limit=limit,
					hash=0,
				)
			)
		users = list(getattr(result, "users", []))
		# смещение двигают участники, а не карточки пользователей: у части
		# участников карточки нет (вышедшие, заблокированные каналы),
		# и счёт по `users` уводил бы смещение назад — часть списка
		# читалась бы дважды, часть не читалась бы вовсе
		step = len(getattr(result, "participants", users))
		return ParticipantsPage(
			deleted=[
				DeletedAccount(user.id, getattr(user, "access_hash", None))
				for user in users
				if getattr(user, "deleted", False)
			],
			scanned=len(users),
			# список кончился, когда страница пуста; короткая страница
			# концом не считается (см. `service_messages_page`)
			next_offset=offset + step if step else None,
			total=getattr(result, "count", None),
		)

	async def kick_participant(self, chat_id: str, account: DeletedAccount) -> int | None:
		"""Исключает участника; возвращает id порождённой служебной записи.

		Исключение — это блокировка со снятием: иначе учётка осела бы
		в списке заблокированных. В супергруппе Telegram пишет об этом
		служебную запись («X удалил Y») — её идентификатор и
		возвращается, чтобы чистка убрала за собой (ADR-0026).
		None — записи не было (так ведут себя каналы).

		Ссылка на пользователя собирается из пары «id + хеш доступа»,
		полученной вместе со списком участников: без хеша Telegram
		пользователя не опознаёт, а надеяться на кеш клиента нельзя —
		он живёт в памяти сессии, а между поиском и исключением
		проходит время.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Нет права исключать (подтверждённый отказ).
			UserbotFloodError: Флуд-лимит — обход прекращается.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.types import InputPeerUser

		client, entity = await self._client_and_entity(chat_id)
		peer: Any = (
			InputPeerUser(account.user_id, account.access_hash)
			if account.access_hash is not None
			else account.user_id
		)
		async with _mtproto_errors():
			produced = await client.kick_participant(entity, peer)
		return _service_message_id(produced)

	async def get_scheduled(self, chat_id: str) -> list[ScheduledMessage]:
		"""Читает отложенные записи сообщества (источник истины — Telegram).

		Returns:
			Записи в порядке, в котором их отдал сервер; пустой список —
			отложенных нет (это знание, а не «не удалось прочитать»:
			неудача приходит исключением).

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.functions.messages import GetScheduledHistoryRequest

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			result = await client(GetScheduledHistoryRequest(peer=entity, hash=0))
		# у ответа «ничего не изменилось» поля со списком нет вовсе,
		# а среди записей попадаются пустышки (удалённая отложка) —
		# у них нет даты, и дальше по коду она обещана как дата
		return [
			_scheduled_from(message)
			for message in getattr(result, "messages", [])
			if getattr(message, "date", None) is not None
		]

	async def get_scheduled_message(self, chat_id: str, message_id: int) -> ScheduledMessage | None:
		"""Читает одну отложенную запись целиком (свежее состояние с сервера).

		Форма правки открывается по этому чтению, а не по снимку списка:
		запись могли изменить из другого клиента Telegram (ADR-0010).

		Returns:
			Запись или None — её в очереди отложенных больше нет
			(опубликована или удалена): Telegram отвечает пустышкой
			без даты либо пустым списком.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotAccessError: Сообщество не видно аккаунту.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.functions.messages import GetScheduledMessagesRequest

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			result = await client(GetScheduledMessagesRequest(peer=entity, id=[message_id]))
		for message in getattr(result, "messages", []):
			if getattr(message, "date", None) is not None and message.id == message_id:
				return _scheduled_from(message)
		return None

	async def edit_scheduled(
		self, chat_id: str, message_id: int, text: str, when: datetime
	) -> None:
		"""Меняет текст и/или время отложенной записи (``messages.editMessage``).

		Тот же метод, что правит обычные сообщения, но с ``schedule_date``:
		так Telegram понимает, что правится запись из очереди отложенных
		(core.telegram.org/api/scheduled-messages). Вложение и тема
		здесь не меняются: у метода нет поля адресата, а замена файла —
		загрузка с прогрессом, то есть задание очереди, а не правка.
		Ответ «ничего не изменилось» (``MESSAGE_NOT_MODIFIED``) — не сбой:
		запись уже в запрошенном состоянии.

		Args:
			chat_id: сообщество.
			message_id: id записи в очереди отложенных.
			text: новый текст (у записи с вложением — подпись; пустая
				строка снимает подпись).
			when: новый момент публикации.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Записи в очереди уже нет.
			UserbotAccessError: Нет права править (подтверждённый отказ).
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Время отклонено и прочие отказы.
		"""
		from telethon import errors

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			try:
				await client.edit_message(entity, message_id, text, schedule=when)
			except errors.MessageNotModifiedError:
				logger.info(
					"Отложка %s в чате %s уже в запрошенном виде — правка не нужна.",
					message_id,
					chat_id,
				)
				return
		logger.info("Отложка %s в чате %s изменена (публикация %s).", message_id, chat_id, when)

	async def send_scheduled_now(self, chat_id: str, message_ids: list[int]) -> None:
		"""Публикует отложенные записи немедленно (``messages.sendScheduledMessages``).

		Записи уходят из очереди отложенных в ленту сообщества; в ленте
		у них будут другие id.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Записи в очереди уже нет.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.functions.messages import SendScheduledMessagesRequest

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			await client(SendScheduledMessagesRequest(peer=entity, id=message_ids))
		logger.info("Отложки %s в чате %s опубликованы сейчас.", message_ids, chat_id)

	async def delete_scheduled(self, chat_id: str, message_ids: list[int]) -> None:
		"""Удаляет отложенные записи, не публикуя (``messages.deleteScheduledMessages``).

		Обычное удаление сообщений (``delete_messages``) для очереди
		отложенных не годится — у неё собственный метод и собственные id.

		Raises:
			UserbotNotConnectedError: Аккаунт не активирован или нет связи.
			UserbotMessageGoneError: Записи в очереди уже нет.
			UserbotFloodError: Telegram просит подождать.
			UserbotUnavailableError: Прочие отказы Telegram.
		"""
		from telethon.tl.functions.messages import DeleteScheduledMessagesRequest

		client, entity = await self._client_and_entity(chat_id)
		async with _mtproto_errors():
			await client(DeleteScheduledMessagesRequest(peer=entity, id=message_ids))
		logger.info("Отложки %s в чате %s удалены без публикации.", message_ids, chat_id)


async def _fetch_stats(client: Any, entity: Any) -> tuple[Any, int | None]:
	"""Ответ статистики и дата-центр, куда Telegram отправил за ней.

	Повторяет выбор метода ``client.get_stats`` Telethon (сначала канал,
	по ``BROADCAST_REQUIRED`` — супергруппа), но дата-центр из ошибки
	миграции возвращает наружу: он нужен и догрузкам графиков. None —
	миграции не было, всё идёт обычным путём.
	"""
	from telethon.errors import BroadcastRequiredError, StatsMigrateError
	from telethon.tl.functions.stats import GetBroadcastStatsRequest, GetMegagroupStatsRequest

	request: Any = GetBroadcastStatsRequest(entity)
	try:
		return await client(request), None
	except StatsMigrateError as exc:
		dc = exc.dc
	except BroadcastRequiredError:
		request = GetMegagroupStatsRequest(entity)
		try:
			return await client(request), None
		except StatsMigrateError as exc:
			dc = exc.dc
	sender = await client._borrow_exported_sender(dc)  # noqa: SLF001 — приём самого Telethon
	try:
		return await sender.send(request), dc
	finally:
		await client._return_exported_sender(sender)  # noqa: SLF001


async def _graph(send: Any, graph: Any) -> list[GraphSeries]:
	"""Ряды графика: готовый JSON или догрузка по токену через ``send``.

	``send`` — клиент или ``sender.send`` одолженного канала в дата-центр
	статистики. График с ошибкой построения (``StatsGraphError``),
	отсутствующий и не догрузившийся (``GRAPH_INVALID_RELOAD`` — токен
	протух или чужой) — пустой список: вкладка покажет «нет данных»,
	а остальные графики прохода не пострадают.
	"""
	from telethon.errors import GraphInvalidReloadError
	from telethon.tl.functions.stats import LoadAsyncGraphRequest
	from telethon.tl.types import StatsGraph, StatsGraphAsync

	if isinstance(graph, StatsGraphAsync):
		try:
			graph = await send(LoadAsyncGraphRequest(token=graph.token))
		except GraphInvalidReloadError:
			logger.warning("График статистики Telegram не догрузился: токен отклонён.")
			return []
	if not isinstance(graph, StatsGraph):
		return []
	payload = getattr(getattr(graph, "json", None), "data", None)
	return parse_graph(payload) if isinstance(payload, str) else []


#: Графики «ряды по дням»: поле границы → атрибут ответа Telegram.
_DAILY_GRAPHS = {
	"interactions": "interactions_graph",
	"iv_interactions": "iv_interactions_graph",
	"mute": "mute_graph",
	"story_interactions": "story_interactions_graph",
	"messages_daily": "messages_graph",
	"actions": "actions_graph",
}
#: Графики долей: поле границы → атрибуты ответа (первый непустой; у канала
#: и группы источники новых участников названы по-разному).
_SHARE_GRAPHS = {
	"views_by_source": ("views_by_source_graph",),
	"members_by_source": ("new_followers_by_source_graph", "new_members_by_source_graph"),
	"languages": ("languages_graph",),
	"reactions_by_emotion": ("reactions_by_emotion_graph",),
	"story_reactions": ("story_reactions_by_emotion_graph",),
	"weekdays": ("weekdays_graph",),
}


def _first_attr(obj: Any, attrs: tuple[str, ...]) -> Any:
	"""Первый непустой атрибут из перечисленных (None — ни одного)."""
	for attr in attrs:
		value = getattr(obj, attr, None)
		if value is not None:
			return value
	return None


def _analytics_from(
	stats: Any,
	growth: list[GraphSeries],
	flow: list[GraphSeries],
	hours: list[GraphSeries],
	daily_graphs: dict[str, list[GraphSeries]] | None = None,
	share_graphs: dict[str, list[GraphSeries]] | None = None,
) -> CommunityAnalytics:
	"""Собирает границу из ответа статистики и разобранных графиков.

	Ряды приходов и уходов опознаются по именам («Joined» / «Left»
	в клиентах Telegram), при промахе — по позиции: первый ряд —
	пришли, второй — ушли. Числа за период: у канала ``followers``,
	у группы ``members``; просмотры на пост есть только у канала.
	Остальное берётся как есть: пары «сейчас, раньше» по именам полей
	ответа, ряды по дням с именами Telegram, доли — суммой за период;
	имена людей в списках самых активных — из ``users`` ответа.
	"""
	period = getattr(stats, "period", None)
	period_from = getattr(period, "min_date", None)
	period_to = getattr(period, "max_date", None)
	members_value = getattr(stats, "followers", None) or getattr(stats, "members", None)
	views = getattr(stats, "views_per_post", None)
	recent_posts = tuple(
		RecentPost(
			int(getattr(item, "msg_id", 0)),
			_opt_int(getattr(item, "views", None)),
			_opt_int(getattr(item, "forwards", None)),
			_opt_int(getattr(item, "reactions", None)),
		)
		for item in getattr(stats, "recent_posts_interactions", None) or []
		if getattr(item, "msg_id", None) is not None
	)
	names = _user_names(getattr(stats, "users", None) or [])
	graphs: dict[str, Any] = {
		field: tuple(
			NamedSeries(name, tuple(DayPoint(*point) for point in points))
			for name, points in named_daily(series)
			if points
		)
		for field, series in (daily_graphs or {}).items()
	}
	graphs.update(
		{
			field: tuple(Share(name, value) for name, value in shares(series))
			for field, series in (share_graphs or {}).items()
		}
	)
	return CommunityAnalytics(
		period_from=period_from.date() if period_from else datetime.now(UTC).date(),
		period_to=period_to.date() if period_to else datetime.now(UTC).date(),
		members=_abs_pair(members_value),
		growth=tuple(DayPoint(*point) for point in daily(pick_series(growth, position=0))),
		joined=tuple(DayPoint(*point) for point in daily(pick_series(flow, "join", position=0))),
		left=tuple(
			DayPoint(*point) for point in daily(pick_series(flow, "left", "leav", position=1))
		),
		hours=tuple(profile) if (profile := hourly(pick_series(hours, position=0))) else None,
		views_per_post=_abs_pair(views),
		recent_post_views=tuple(post.views for post in recent_posts if post.views is not None),
		shares_per_post=_abs_pair(getattr(stats, "shares_per_post", None)),
		reactions_per_post=_abs_pair(getattr(stats, "reactions_per_post", None)),
		views_per_story=_abs_pair(getattr(stats, "views_per_story", None)),
		shares_per_story=_abs_pair(getattr(stats, "shares_per_story", None)),
		reactions_per_story=_abs_pair(getattr(stats, "reactions_per_story", None)),
		notifications=_percent_pair(getattr(stats, "enabled_notifications", None)),
		messages=_abs_pair(getattr(stats, "messages", None)),
		viewers=_abs_pair(getattr(stats, "viewers", None)),
		posters=_abs_pair(getattr(stats, "posters", None)),
		recent_posts=recent_posts,
		top_posters=tuple(
			TopPoster(
				names.get(int(item.user_id), str(item.user_id)),
				int(item.messages),
				int(item.avg_chars),
			)
			for item in getattr(stats, "top_posters", None) or []
		),
		top_admins=tuple(
			TopAdmin(
				names.get(int(item.user_id), str(item.user_id)),
				int(item.deleted),
				int(item.kicked),
				int(item.banned),
			)
			for item in getattr(stats, "top_admins", None) or []
		),
		top_inviters=tuple(
			TopInviter(names.get(int(item.user_id), str(item.user_id)), int(item.invitations))
			for item in getattr(stats, "top_inviters", None) or []
		),
		**graphs,  # ключи — поля границы
	)


def _opt_int(value: Any) -> int | None:
	return None if value is None else int(value)


def _percent_pair(value: Any) -> tuple[int, int] | None:
	"""«Часть и всего» из ``StatsPercentValue``; None — поля нет."""
	part = getattr(value, "part", None)
	total = getattr(value, "total", None)
	if part is None or total is None:
		return None
	return int(round(part)), int(round(total))


def _user_names(users: list[Any]) -> dict[int, str]:
	"""Имена людей из ответа статистики: «Имя Фамилия», иначе @имя, иначе id."""
	names: dict[int, str] = {}
	for user in users:
		user_id = getattr(user, "id", None)
		if user_id is None:
			continue
		full = " ".join(
			part
			for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
			if part
		)
		username = getattr(user, "username", None)
		names[int(user_id)] = full or (f"@{username}" if username else str(user_id))
	return names


def _abs_pair(value: Any) -> tuple[int, int] | None:
	"""«Сейчас и раньше» из ``StatsAbsValueAndPrev``; None — поля нет."""
	current = getattr(value, "current", None)
	previous = getattr(value, "previous", None)
	if current is None:
		return None
	return int(round(current)), int(round(previous or 0))


class MtprotoLoginManager:
	"""Пошаговый вход userbot. Держит незавершённые входы по id аккаунта."""

	def __init__(self, client_factory: Callable[[int, str], Any] | None = None) -> None:
		self._client_factory = client_factory or (
			lambda api_id, api_hash: _default_client(api_id, api_hash, None)
		)
		self._pending: dict[int, tuple[Any, str, str]] = {}

	async def start(self, account_id: int, api_id: int, api_hash: str, phone: str) -> None:
		"""Подключается и просит Telegram отправить код на телефон.

		Raises:
			LoginError: Telegram отклонил запрос (номер, лимиты и т.п.).
		"""
		await self.cancel(account_id)  # висел незавершённый вход — закрываем
		client = self._client_factory(api_id, api_hash)
		try:
			await client.connect()
			sent = await client.send_code_request(phone)
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			await _safe_disconnect(client)
			raise LoginError(_map_login_error(exc)) from exc
		# пока ждали сеть, параллельный «Войти» мог начать другой вход —
		# закрываем его клиента, иначе тот останется подключённым без владельца
		stale = self._pending.pop(account_id, None)
		if stale is not None:
			await _safe_disconnect(stale[0])
		self._pending[account_id] = (client, sent.phone_code_hash, phone)
		logger.info("Userbot id=%s: код отправлен.", account_id)

	async def confirm_code(self, account_id: int, code: str) -> str | None:
		"""Подтверждает код из Telegram.

		Returns:
			Строку сессии, либо ``None``, если дальше нужен пароль 2FA.

		Raises:
			LoginError: Код неверный/устарел или вход не был начат.
		"""
		from telethon.errors import SessionPasswordNeededError

		client, code_hash, phone = self._require(account_id)
		try:
			await client.sign_in(phone, code, phone_code_hash=code_hash)
		except SessionPasswordNeededError:
			return None  # клиент остаётся жить до ввода пароля
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			await self.cancel(account_id)
			raise LoginError(_map_login_error(exc)) from exc
		return await self._finish(account_id, client)

	async def confirm_password(self, account_id: int, password: str) -> str:
		"""Подтверждает пароль двухфакторной защиты и завершает вход.

		Raises:
			LoginError: Пароль неверный или вход не был начат.
		"""
		client, _hash, _phone = self._require(account_id)
		try:
			await client.sign_in(password=password)
		except Exception as exc:  # noqa: BLE001 — переводим в понятный текст
			# как и confirm_code: неудачный шаг закрывает незавершённый вход
			await self.cancel(account_id)
			raise LoginError(_map_login_error(exc)) from exc
		return await self._finish(account_id, client)

	async def cancel(self, account_id: int) -> None:
		"""Прерывает незавершённый вход и закрывает его клиента."""
		entry = self._pending.pop(account_id, None)
		if entry is not None:
			await _safe_disconnect(entry[0])

	async def cancel_all(self) -> None:
		"""Закрывает все незавершённые входы (при остановке движка)."""
		for account_id in list(self._pending):
			await self.cancel(account_id)

	def _require(self, account_id: int) -> tuple[Any, str, str]:
		"""Возвращает состояние входа или объясняет, что вход не начат."""
		entry = self._pending.get(account_id)
		if entry is None:
			raise LoginError("Вход не начат — нажмите «Войти» ещё раз.")
		return entry

	async def _finish(self, account_id: int, client: Any) -> str:
		"""Забирает строку сессии и закрывает клиента входа."""
		session_string = str(client.session.save())
		await _safe_disconnect(client)
		self._pending.pop(account_id, None)
		logger.info("Userbot id=%s: вход завершён.", account_id)
		return session_string
