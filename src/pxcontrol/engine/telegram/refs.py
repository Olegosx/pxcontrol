"""Разбор пользовательского ввода «какое сообщество» — общий для обоих транспортов.

Ввод пользователя (@имя, ссылка t.me/…, числовой ID) не принадлежит ни Bot API,
ни MTProto: транспорты зависят от этого модуля, но не друг от друга.
"""

from __future__ import annotations

from pxcontrol.engine.errors import EngineError


class ChatRefError(EngineError):
	"""Ссылку или имя сообщества не удалось разобрать (с понятным текстом)."""


#: Префикс числовых ID каналов и супергрупп в формате Bot API: Telegram
#: дополняет внутреннюю нумерацию маркером «-100» (конвенция Bot API;
#: в ссылках t.me/c/<внутренний id> префикса нет — его дописываем мы).
#: Что просить у человека, когда ввод не разобрался. Один текст на все
#: точки отказа: подключить можно и канал, и группу (ADR-0021), и
#: диалог подключения просит именно так.
_ASK_REF = "Укажите @имя, ссылку t.me/… или ID сообщества."

CHANNEL_ID_PREFIX = "-100"


def numeric_chat_id(chat_id: str, error: type[Exception]) -> int:
	"""Числовой ID канала из строки БД (контракт ``CommunityInfo.chat_id``).

	Общий помощник обоих транспортов: нечисловая строка — повреждённая
	запись БД, а не сетевой сбой, и заслуживает понятного текста. Класс
	ошибки — параметр, потому что таксономии транспортов разные
	(бот — ``BotError``, userbot — ``UserbotUnavailableError``).

	Raises:
		error: В БД оказался нечисловой ID.
	"""
	try:
		return int(chat_id)
	except ValueError as exc:
		raise error(
			f"Некорректный ID сообщества в базе: {chat_id!r} — переподключите "
			"сообщество на его странице."
		) from exc


def normalize_chat_ref(chat_ref: str) -> str | int:
	"""Приводит ввод пользователя к виду для API Telegram.

	Принимает ``@имя``, ``имя``, ссылки ``t.me/имя`` и ``t.me/c/<число>/…``,
	числовой ID (в том числе с пробелами внутри). Возвращает ``@имя``
	или число.

	Raises:
		ChatRefError: Пустая, инвайт- или неразборчивая ссылка.
	"""
	ref = chat_ref.strip()
	for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
		if ref.lower().startswith(prefix):
			ref = ref[len(prefix) :]
			break
	ref = ref.strip("/")
	# оба формата инвайт-ссылок: новый t.me/+… и старый t.me/joinchat/…
	# (без этой ветки старый формат превратился бы в кривое @имя и ушёл
	# бы в API, а человек получил бы общее «сообщество не найдено»)
	if ref.startswith("+") or ref.lower().startswith("joinchat/"):
		raise ChatRefError(
			"Инвайт-ссылка (t.me/+… или t.me/joinchat/…) не подходит — "
			"укажите @имя сообщества или его ID (начинается с -100)."
		)
	if ref.lower().startswith("s/"):
		# веб-превью сообщества (браузерный Telegram даёт t.me/s/имя):
		# без среза префикс превратился бы в кривое «@s/имя» и ушёл
		# в API с общим «сообщество не найдено» — как у инвайт-ссылок выше
		ref = ref[2:]
	if ref.lower().startswith("c/"):
		internal = ref[2:].split("/", 1)[0]
		if internal.isdigit():
			return int(f"{CHANNEL_ID_PREFIX}{internal}")
		raise ChatRefError("Не удалось разобрать ссылку t.me/c/… — укажите ID сообщества (-100…).")
	ref = ref.lstrip("@")
	digits = ref.replace(" ", "")
	if digits.lstrip("-").isdigit():
		try:
			return int(digits)
		except ValueError as exc:  # ввод вида «--123»: минусов больше одного
			raise ChatRefError(_ASK_REF) from exc
	if not ref:
		raise ChatRefError(_ASK_REF)
	return f"@{ref}"


def invite_hash(link: str) -> str:
	"""Достаёт хеш из ссылки-приглашения (ADR-0035).

	Приглашение адресуется не именем, а хешем: по нему вступают
	(``messages.importChatInvite``) в сообщество, которого аккаунт
	ещё не видит. Принимает оба формата Telegram — новый ``t.me/+хеш``
	и старый ``t.me/joinchat/хеш``, — а также голый хеш: человек может
	скопировать его из настроек сообщества без адреса.

	Raises:
		ChatRefError: Ссылка пустая или это не приглашение (например,
			обычная ссылка на публичное сообщество — по ней вступают
			иначе).
	"""
	ref = link.strip()
	for prefix in ("https://", "http://"):
		if ref.lower().startswith(prefix):
			ref = ref[len(prefix) :]
	with_host = False
	for host in ("t.me/", "telegram.me/", "telegram.dog/"):
		if ref.lower().startswith(host):
			ref = ref[len(host) :]
			with_host = True
			break
	ref = ref.strip("/")
	# ссылка с адресом обязана быть помечена как приглашение: без «+»
	# или «joinchat/» это обычная ссылка на публичное сообщество,
	# и «хеш» из неё Telegram не примет. Голый ввод без адреса —
	# другое дело: там хеш и есть весь ввод
	marked = ref.startswith("+") or ref.lower().startswith("joinchat/")
	if with_host and not marked:
		raise ChatRefError(
			"Это ссылка на публичное сообщество, а не приглашение. "
			"Нужна ссылка вида t.me/+… или t.me/joinchat/…"
		)
	if ref.lower().startswith("joinchat/"):
		ref = ref[len("joinchat/") :]
	ref = ref.lstrip("+")
	if not ref or "/" in ref or ref.startswith("@"):
		raise ChatRefError(
			"Это не ссылка-приглашение. Нужна ссылка вида t.me/+… "
			"или t.me/joinchat/… — её можно взять в настройках сообщества."
		)
	return ref
