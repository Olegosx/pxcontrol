"""Правила показа исполнителя сообщества: строка, плашка, права, исходы ввода (ADR-0035).

Чистые функции без виджетов — как :mod:`community_state` для сообщества,
только предмет другой: не «что с сообществом», а «кто в нём работает
и что ему можно». Единственное, что берётся у библиотеки, — перечисление
уровней плашки (``InfoLevel``): это данные о том, каким цветом говорить,
а не элемент интерфейса. Здесь же словарь прав по-русски: снимок хранит
телеграмные имена (``post_messages``, ``send_reactions``), а человеку
нужны слова.

Перечень прав показывается целиком и **только выданное**: список
из тридцати с лишним строк, где половина «нельзя», читать невозможно,
а отсутствие права и означает «не выдано».
"""

from __future__ import annotations

from datetime import datetime

from qfluentwidgets import InfoLevel

from pxcontrol.engine.services.communities import (
	CommunityDto,
	ExecutorDto,
	JoinResult,
)
from pxcontrol.engine.services.executor_join import JoinOutcome
from pxcontrol.engine.telegram.rights import AdminRights, ExecutorRights, MemberRights
from pxcontrol.engine.telegram.types import OwnerKind
from pxcontrol.ui.pages.common import format_local, status_caption


def member_caption(executor: ExecutorDto, username: str | None = None) -> str:
	"""Подпись под именем в карточке пула: роль; у бота — «@имя · роль».

	Коротко: назначение и помехи работе говорит плашка рядом с именем,
	и повторять их словами незачем.
	"""
	role = status_caption(executor.status)
	if username:
		return f"@{username} · {role}"
	return role


def member_badge(executor: ExecutorDto) -> tuple[str, InfoLevel] | None:
	"""Плашка карточки исполнителя: текст и уровень; None — плашки нет.

	Плашка одна, приоритет сверху вниз: назначение важнее всего (по нему
	человек находит того, кем публикуют), затем пауза — приложение его
	не использует вовсе, и говорить про его права было бы обманом, затем
	нехватка прав. Уровни те же, что у сообщества (``communities.md``,
	6.1): акцент — назначению, серое — паузе (это решение человека,
	а не беда), предупреждение — тому, что мешает публиковать.
	"""
	if executor.is_default:
		return "по умолчанию", InfoLevel.ATTENTION
	if executor.paused:
		return "приостановлен", InfoLevel.INFOAMTION
	if not executor.can_publish:
		return "не может публиковать", InfoLevel.WARNING
	return None


def default_candidates(executors: list[ExecutorDto], kind: OwnerKind) -> list[ExecutorDto]:
	"""Кого предлагать публикатором по умолчанию: свой вид, может, не на паузе.

	Назначить можно и того, кто сейчас публиковать не может (ADR-0035:
	права — знание Telegram, назначение — решение человека), но
	**предлагать** такого в списке смены незачем: человек выбирает
	из тех, кем публикация пойдёт прямо сейчас.
	"""
	return [
		executor
		for executor in executors
		if executor.owner.kind is kind and executor.can_publish and not executor.paused
	]


#: Что сказать человеку про исход ввода исполнителя (ADR-0035). Ввод
#: меняет состояние в Telegram, и молчаливое «готово» тут неуместно:
#: человек должен знать, вступил ли аккаунт, приглашён ли, ждёт ли
#: заявка одобрения — от этого зависит, работает исполнитель или нет.
_JOIN_WORDS = {
	JoinOutcome.ALREADY_IN: "уже состоял — записаны его права",
	JoinOutcome.JOINED: "вступил в сообщество",
	JoinOutcome.INVITED: "приглашён и добавлен",
	JoinOutcome.PROMOTED: "принят администратором канала",
	JoinOutcome.REQUESTED: "заявка на вступление отправлена — ждёт одобрения",
}


def join_result_text(result: JoinResult, label: str) -> str:
	"""Человеческий итог ввода исполнителя в сообщество."""
	return f"{label}: {_JOIN_WORDS.get(result.outcome, str(result.outcome))}"


#: Чего просить у человека, когда автоматических путей не осталось.
INVITE_LINK_PROMPT = (
	"Это приватное сообщество: вступить по имени нельзя, а готовую "
	"ссылку Telegram не отдал. Вставьте ссылку-приглашение — её видно "
	"в настройках сообщества (приложение своих ссылок не создаёт)."
)


def remove_executor_text(executor: ExecutorDto, community: CommunityDto) -> str:
	"""Подтверждение: что потеряет сообщество, если убрать исполнителя.

	Исполнитель убирается **из приложения**, а не из Telegram: сообщество
	перестаёт им пользоваться, но в самом Telegram он остаётся там, где был.
	"""
	text = f"Убрать «{executor.label}» из пула «{community.title}»?"
	if not executor.is_default:
		return text
	if executor.owner.kind is OwnerKind.USER:
		return (
			f"{text} Это публикатор по умолчанию: публикация через userbot "
			"остановится до выбора нового."
		)
	return (
		f"{text} Это публикатор-бот: кнопки под постами и запасной путь "
		"публикации станут недоступны."
	)


#: Права администратора по-русски. Снимок хранит телеграмные имена —
#: они верны рядом с документацией, но человеку нужны слова. Порядок
#: словаря и есть порядок показа: сперва то, ради чего исполнителя
#: заводят (публикация, правка, удаление), потом всё прочее.
_ADMIN_WORDS: dict[str, str] = {
	"post_messages": "публиковать",
	"edit_messages": "править чужие сообщения",
	"delete_messages": "удалять чужие сообщения",
	"invite_users": "приглашать",
	"ban_users": "исключать участников",
	"pin_messages": "закреплять",
	"add_admins": "назначать администраторов",
	"manage_topics": "управлять темами",
	"change_info": "менять описание",
	"manage_call": "вести видеочаты",
	"anonymous": "выступать анонимно",
	"other": "видеть журнал и статистику",
	"post_stories": "публиковать истории",
	"edit_stories": "править истории",
	"delete_stories": "удалять истории",
	"manage_direct_messages": "вести личные сообщения канала",
	"manage_ranks": "менять теги участников",
}

#: Разрешения участника по-русски. Тот же порядок «сперва главное»:
#: текст и вложения, затем взаимодействие, затем права по сообществу.
_MEMBER_WORDS: dict[str, str] = {
	"send_plain": "писать текст",
	"send_photos": "фото",
	"send_videos": "видео",
	"send_roundvideos": "кружки",
	"send_audios": "аудио",
	"send_voices": "голосовые",
	"send_docs": "файлы",
	"send_stickers": "стикеры",
	"send_gifs": "гифки",
	"send_games": "игры",
	"send_inline": "встроенные боты",
	"embed_links": "превью ссылок",
	"send_polls": "опросы",
	"send_reactions": "реакции",
	"invite_users": "приглашать",
	"pin_messages": "закреплять",
	"change_info": "менять описание",
	"manage_topics": "управлять темами",
	"edit_rank": "менять свой тег",
}

#: Что показать вместо перечня, когда выданного нет вовсе.
_NOTHING = "нет"


def _granted_words(rights: AdminRights | MemberRights, words: dict[str, str]) -> str:
	"""Выданные права словами, в порядке словаря (пусто — «нет»)."""
	granted = [word for name, word in words.items() if getattr(rights, name, False)]
	return " · ".join(granted) if granted else _NOTHING


def snapshot_caption(checked_at: datetime | None) -> str:
	"""Когда снимали снимок прав — или честное «снимка не было».

	Пустая отметка значит, что права перенесены из прежней модели
	(ADR-0035, этап B): им можно верить в том, что прежняя модель
	подтверждала, но полным снимком они не были. Первая перепроверка
	доступов заменит их целиком — и это стоит сказать прямо.
	"""
	if checked_at is None:
		return "снимка не было — права перенесены из прежней модели, перепроверьте доступы"
	return f"снимок от {format_local(checked_at)}"


def executor_rights_rows(executor: ExecutorDto) -> list[tuple[str, str]]:
	"""Перечень прав для раскрытой карточки: «подпись — значение».

	Администратору ограничения участников не мешают вовсе (правило
	Telegram, а не наше послабление), поэтому перечислять ему разрешения
	участника незачем — об этом говорится словами.
	"""
	rights: ExecutorRights = executor.rights
	rows = [("Права администратора", _granted_words(rights.admin, _ADMIN_WORDS))]
	if rights.status.administers:
		rows.append(("Как участник", "ограничения на администратора не действуют"))
	else:
		rows.append(("Как участник", _granted_words(rights.allowed, _MEMBER_WORDS)))
	rows.append(("Права прочитаны", snapshot_caption(executor.checked_at)))
	return rows
