"""Разбор пользовательского ввода «какой канал» — общий для обоих транспортов.

Ввод пользователя (@имя, ссылка t.me/…, числовой ID) не принадлежит ни Bot API,
ни MTProto: транспорты зависят от этого модуля, но не друг от друга.
"""

from __future__ import annotations

from pxcontrol.engine.errors import EngineError


class ChatRefError(EngineError):
	"""Ссылку/имя канала не удалось разобрать (с понятным человеку текстом)."""


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
			ref = ref[len(prefix):]
			break
	ref = ref.strip("/")
	if ref.startswith("+"):
		raise ChatRefError(
			"Инвайт-ссылка (t.me/+…) не подходит — укажите @имя канала "
			"или его ID (начинается с -100)."
		)
	if ref.lower().startswith("c/"):
		internal = ref[2:].split("/", 1)[0]
		if internal.isdigit():
			return int(f"-100{internal}")
		raise ChatRefError(
			"Не удалось разобрать ссылку t.me/c/… — укажите ID канала (-100…)."
		)
	ref = ref.lstrip("@")
	digits = ref.replace(" ", "")
	if digits.lstrip("-").isdigit() and digits.lstrip("-"):
		return int(digits)
	if not ref:
		raise ChatRefError("Укажите @имя, ссылку t.me/… или ID канала.")
	return f"@{ref}"
