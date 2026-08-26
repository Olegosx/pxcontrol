"""Разбор пользовательского ввода «какой канал» — общий для обоих транспортов.

Ввод пользователя (@имя, ссылка t.me/…, числовой ID) не принадлежит ни Bot API,
ни MTProto: транспорты зависят от этого модуля, но не друг от друга.
"""

from __future__ import annotations

from pxcontrol.engine.errors import EngineError


class ChatRefError(EngineError):
	"""Ссылку/имя канала не удалось разобрать (с понятным человеку текстом)."""


#: Префикс числовых ID каналов и супергрупп в формате Bot API: Telegram
#: дополняет внутреннюю нумерацию маркером «-100» (конвенция Bot API;
#: в ссылках t.me/c/<внутренний id> префикса нет — его дописываем мы).
CHANNEL_ID_PREFIX = "-100"


def numeric_chat_id(chat_id: str, error: type[Exception]) -> int:
	"""Числовой ID канала из строки БД (контракт ``ChannelInfo.chat_id``).

	Общий помощник обоих транспортов: нечисловая строка — повреждённая
	запись БД, а не сетевой сбой, и заслуживает понятного текста. Класс
	ошибки — параметр, потому что таксономии транспортов разные
	(бот — ``ChannelCheckError``, userbot — ``UserbotUnavailableError``).

	Raises:
		error: В БД оказался нечисловой ID.
	"""
	try:
		return int(chat_id)
	except ValueError as exc:
		raise error(
			f"Некорректный ID канала в базе: {chat_id!r} — переподключите "
			"канал на странице «Каналы»."
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
	# бы в API, а пользователь получил бы общее «канал не найден»)
	if ref.startswith("+") or ref.lower().startswith("joinchat/"):
		raise ChatRefError(
			"Инвайт-ссылка (t.me/+… или t.me/joinchat/…) не подходит — "
			"укажите @имя канала или его ID (начинается с -100)."
		)
	if ref.lower().startswith("c/"):
		internal = ref[2:].split("/", 1)[0]
		if internal.isdigit():
			return int(f"{CHANNEL_ID_PREFIX}{internal}")
		raise ChatRefError("Не удалось разобрать ссылку t.me/c/… — укажите ID канала (-100…).")
	ref = ref.lstrip("@")
	digits = ref.replace(" ", "")
	if digits.lstrip("-").isdigit():
		try:
			return int(digits)
		except ValueError as exc:  # ввод вида «--123»: минусов больше одного
			raise ChatRefError("Укажите @имя, ссылку t.me/… или ID канала.") from exc
	if not ref:
		raise ChatRefError("Укажите @имя, ссылку t.me/… или ID канала.")
	return f"@{ref}"
