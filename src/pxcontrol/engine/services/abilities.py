"""Что исполнитель может в сообществе — чистые предикаты над снимком прав (ADR-0035).

Права приходят от Telegram снимком (:mod:`pxcontrol.engine.telegram.rights`),
а здесь на них отвечают предметными вопросами приложения: может ли этот
исполнитель опубликовать пост, дорисовать кнопки к чужому, пригласить
в сообщество, принять туда бота. Вопросы разные, правило одно: вид
сообщества и вид права решает Telegram, а не мы.

Модуль чистый — ни сети, ни базы, ни моделей: так правило проверяется
тестами целиком и живёт одной точкой. Предикаты нужны и движку
(возможности сообщества, выбор того, кто введёт нового исполнителя),
и интерфейсу (что показать человеку вместо молчаливо серой кнопки).

Снимок стареет, и «да» здесь означает «по последнему ответу Telegram
мог». Отказ сервера при самой операции остаётся последним словом
(ADR-0022, п. 8) — правам слепо не доверяют.
"""

from __future__ import annotations

from enum import StrEnum

from pxcontrol.engine.telegram.rights import AdminRights, ExecutorRights
from pxcontrol.engine.telegram.types import CommunityKind


class ExecutorAction(StrEnum):
	"""Предметное действие, на которое проверяют исполнителя (ADR-0035).

	Перечень ровно тот, у которого есть потребитель: публикация и кнопки
	(маршруты постов), удаление и исключение (обслуживание), приглашение
	и назначение (ввод исполнителя), чтение истории (обслуживание же).
	Новое действие — одна ветка в :func:`can` и один потребитель; заводить
	их про запас в проекте не принято.
	"""

	PUBLISH = "publish"  # опубликовать пост
	EDIT_OTHERS = "edit_others"  # править чужие сообщения (кнопки, ADR-0031)
	DELETE_OTHERS = "delete_others"  # удалять чужие сообщения (ADR-0026)
	BAN = "ban"  # исключать участников (чистка удалённых аккаунтов)
	INVITE = "invite"  # приглашать в сообщество
	PROMOTE = "promote"  # назначать администраторов (так входит бот в канал)
	READ_HISTORY = "read_history"  # читать ленту и список участников


def can(rights: ExecutorRights, action: ExecutorAction, kind: CommunityKind) -> bool:
	"""Может ли исполнитель с такими правами сделать это здесь (ADR-0035).

	Одна точка на весь движок и интерфейс: возможности сообщества,
	подбор того, кто введёт нового исполнителя, выбор исполнителя
	обслуживания, подсказки форм. Пока правило жило россыпью предикатов,
	каждый новый вопрос («а кто может исключать?») заводил свой.

	Права — снимок, и он стареет: «да» здесь означает «по последнему
	ответу Telegram мог». Отказ сервера при самой операции остаётся
	последним словом (ADR-0022, п. 8) — правам слепо не доверяют.

	Args:
		rights: снимок прав исполнителя в этом сообществе.
		kind: вид сообщества (ADR-0021) — от него зависит публикация.
	"""
	if action is ExecutorAction.PUBLISH:
		return _can_publish(rights, kind)
	if action is ExecutorAction.READ_HISTORY:
		# ленту и участников видит тот, кто состоит: отдельного права
		# на чтение Telegram не выдаёт
		return rights.status.in_community
	if action is ExecutorAction.INVITE:
		# приглашать может и обычный участник, если сообщество ему это
		# оставило, — в группах настройка частая
		if rights.status.administers:
			return rights.admin.invite_users
		return rights.status.in_community and rights.allowed.invite_users
	# остальное — права администратора поимённо: роль сама по себе
	# не гарантирует ни удаления чужих сообщений, ни исключений
	if not rights.status.administers:
		return False
	return {
		ExecutorAction.EDIT_OTHERS: rights.admin.edit_messages,
		ExecutorAction.DELETE_OTHERS: rights.admin.delete_messages,
		ExecutorAction.BAN: rights.admin.ban_users,
		ExecutorAction.PROMOTE: rights.admin.add_admins,
	}[action]


def _can_publish(rights: ExecutorRights, kind: CommunityKind) -> bool:
	"""Публикация: различает не приложение, а Telegram.

	В **канале** публикует только администратор с правом ``post_messages``
	(у бота оно называется иначе, но значит то же), в **группе** — любой
	участник, которого не ограничили в отправке; администратору там
	ограничения не мешают.
	"""
	if kind is CommunityKind.CHANNEL:
		return rights.status.administers and rights.admin.post_messages
	if not rights.status.in_community:
		return False
	return rights.status.administers or rights.allowed.send_plain


#: Что сказать человеку, когда способного исполнителя в пуле нет.
#: Текст один на все действия: отличается только само действие, а совет
#: одинаков — выдать право в Telegram и перепроверить доступы.
ACTION_WORDS: dict[ExecutorAction, str] = {
	ExecutorAction.PUBLISH: "публиковать",
	ExecutorAction.EDIT_OTHERS: "править чужие сообщения",
	ExecutorAction.DELETE_OTHERS: "удалять чужие сообщения",
	ExecutorAction.BAN: "исключать участников",
	ExecutorAction.INVITE: "приглашать",
	ExecutorAction.PROMOTE: "назначать администраторов",
	ExecutorAction.READ_HISTORY: "читать историю и участников",
}


#: Права, которые приложение выдаёт боту, вводя его в **канал** (ADR-0035).
#: Участником бот в канале не бывает, поэтому ввод — это назначение
#: администратором, и набор прав приходится называть заранее. Названы
#: ровно те два, ради которых бот и нужен: опубликовать пост запасным
#: путём и поставить кнопки под постом публикатора (ADR-0031). Больше
#: не просим: чего приложение не делает, того ему и не нужно, а лишнее
#: право у бота — лишний риск для канала.
BOT_ADMIN_RIGHTS = AdminRights(post_messages=True, edit_messages=True)
