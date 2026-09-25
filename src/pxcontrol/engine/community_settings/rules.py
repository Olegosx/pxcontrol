"""Правила настроек сообщества: доступность, проверка значения, изменения (ADR-0043).

Три чистых вопроса, на которые отвечают и экран, и движок:

- **можно ли править** настройку этому исполнителю (:func:`availability`):
  умеет ли транспорт, есть ли право, нет ли условия Telegram;
- **годится ли значение** (:func:`value_problem`): вид, длина, вариант;
- **что изменилось** между снимком и правкой (:func:`changes`).

Снимок прав стареет, поэтому «можно» здесь означает «по последнему
ответу Telegram можно»; отказ сервера при записи — последнее слово.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from pxcontrol.engine.community_settings.catalog import CATALOG, SettingSpec
from pxcontrol.engine.community_settings.model import (
	CommunitySettings,
	LinkedChat,
	PhotoValue,
	ReactionsValue,
	SettingChange,
	SettingValue,
	ValueKind,
)
from pxcontrol.engine.services.abilities import ACTION_WORDS, can
from pxcontrol.engine.telegram.rights import ExecutorRights, MemberRights
from pxcontrol.engine.telegram.types import ChatReactionsMode

#: Чем объяснить, что настройку не меняет бот (его транспорт её не пишет).
BOT_CANNOT = "Бот эту настройку не меняет — нужен userbot-публикатор."


@dataclass(frozen=True)
class Availability:
	"""Можно ли править настройку, а если нет — почему (текст для человека)."""

	editable: bool
	reason: str | None = None


def availability(
	spec: SettingSpec,
	settings: CommunitySettings,
	rights: ExecutorRights,
	writable: Collection[str],
) -> Availability:
	"""Можно ли этому исполнителю править настройку.

	Порядок проверок — от устройства к обстоятельствам: транспорт не
	пишет → права нет → условие Telegram. Первая же преграда и есть
	причина: человеку важнее одна понятная причина, чем перечень.

	Args:
		spec: настройка из каталога.
		settings: снимок (контекст сообщества и значения соседних настроек).
		rights: снимок прав исполнителя, который будет писать.
		writable: ключи, которые транспорт исполнителя умеет записывать.
	"""
	if spec.key not in writable:
		return Availability(False, BOT_CANNOT)
	if not can(rights, spec.action, settings.context.kind):
		return Availability(False, f"Нужно право «{ACTION_WORDS[spec.action]}».")
	if spec.blocker is not None:
		reason = spec.blocker(settings)
		if reason is not None:
			return Availability(False, reason)
	return Availability(True)


def value_problem(spec: SettingSpec, value: SettingValue) -> str | None:
	"""Претензия к значению настройки (None — значение годное).

	Проверка вида и рамок каталога: сервер проверит всё остальное сам,
	а здесь отсекается то, с чем к нему идти незачем.
	"""
	expected: dict[ValueKind, type | tuple[type, ...]] = {
		ValueKind.TOGGLE: bool,
		ValueKind.TEXT: str,
		ValueKind.CHOICE: (int, str),
		ValueKind.PERMISSIONS: MemberRights,
		ValueKind.REACTIONS: ReactionsValue,
		ValueKind.PHOTO: PhotoValue,
		ValueKind.LINKED_CHAT: LinkedChat,
	}
	if not isinstance(value, expected[spec.kind]) or (
		spec.kind is not ValueKind.TOGGLE and isinstance(value, bool)
	):
		return f"«{spec.label}»: значение не того вида."
	if isinstance(value, str) and spec.kind is ValueKind.TEXT:
		return _text_problem(spec, value)
	if spec.kind is ValueKind.CHOICE and value not in {choice.value for choice in spec.choices}:
		return f"«{spec.label}»: такого варианта нет."
	if (
		isinstance(value, ReactionsValue)
		and value.mode is ChatReactionsMode.SOME
		and not value.emojis
	):
		return f"«{spec.label}»: выберите хотя бы одну реакцию или запретите реакции."
	return None


def _text_problem(spec: SettingSpec, value: str) -> str | None:
	"""Текст: предел длины каталога и обязательность."""
	if spec.max_length is not None and len(value) > spec.max_length:
		return f"«{spec.label}»: не длиннее {spec.max_length} символов (сейчас {len(value)})."
	if spec.required and not value.strip():
		return f"«{spec.label}»: не может быть пустым."
	return None


def changes(before: CommunitySettings, after: Mapping[str, SettingValue]) -> list[SettingChange]:
	"""Изменения правки относительно снимка — в порядке каталога.

	В изменения попадают только настройки каталога, которые были
	в снимке и отличаются от него. Порядок каталога важен: он же
	порядок применения (например, фото раньше названия — как на экране).
	Новое фото — всегда изменение: одинаковость картинок сравнивать
	не по чему.
	"""
	result: list[SettingChange] = []
	for spec in CATALOG:
		if spec.key not in after or spec.key not in before.values:
			continue
		value = after[spec.key]
		new_photo = isinstance(value, PhotoValue) and value.upload is not None
		if new_photo or value != before.values[spec.key]:
			result.append(SettingChange(spec.key, value))
	return result
