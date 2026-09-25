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

from pxcontrol.engine.telegram.rights import AdminRights, ExecutorRights, ParticipantStatus
from pxcontrol.engine.telegram.types import CommunityKind


class ExecutorAction(StrEnum):
	"""Предметное действие, на которое проверяют исполнителя (ADR-0035).

	Перечень ровно тот, у которого есть потребитель: публикация и кнопки
	(маршруты постов), удаление и исключение (обслуживание), приглашение,
	чтение ссылки-приглашения и назначение (ввод исполнителя), чтение
	истории (обслуживание же), реакции (задача реакций, ADR-0039),
	приём заявок на вступление (ADR-0040), правка настроек сообщества
	в Telegram (ADR-0043): оформление, ограничения, решения владельца.
	Новое действие — одна ветка в :func:`can` и один потребитель; заводить
	их про запас в проекте не принято.
	"""

	PUBLISH = "publish"  # опубликовать пост
	PUBLISH_AS_COMMUNITY = "publish_as_community"  # опубликовать от имени сообщества (ADR-0036)
	EDIT_OTHERS = "edit_others"  # править чужие сообщения (кнопки, ADR-0031)
	DELETE_OTHERS = "delete_others"  # удалять чужие сообщения (ADR-0026)
	BAN = "ban"  # исключать участников (чистка удалённых аккаунтов)
	INVITE = "invite"  # приглашать в сообщество
	INVITE_LINK = "invite_link"  # видеть основную ссылку-приглашение сообщества
	PROMOTE = "promote"  # назначать администраторов (так входит бот в канал)
	READ_HISTORY = "read_history"  # читать ленту и список участников
	REACT = "react"  # ставить реакции на записи (задача реакций, ADR-0039)
	APPROVE_REQUESTS = "approve_requests"  # принимать заявки на вступление (ADR-0040)
	CHANGE_INFO = "change_info"  # менять информацию и оформление сообщества (ADR-0043)
	RESTRICT_MEMBERS = "restrict_members"  # ограничивать участников: разрешения, медленный режим
	OWN = "own"  # то, что Telegram оставляет только владельцу (ADR-0043)


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
	if action is ExecutorAction.PUBLISH_AS_COMMUNITY:
		return _can_publish_as_community(rights, kind)
	if action is ExecutorAction.READ_HISTORY:
		# ленту и участников видит тот, кто состоит: отдельного права
		# на чтение Telegram не выдаёт
		return rights.status.in_community
	if action is ExecutorAction.OWN:
		# @имя, запрет копирования, темы: по TDLib — «owner privileges»,
		# никакое право администратора их не открывает
		return rights.status is ParticipantStatus.CREATOR
	if action is ExecutorAction.REACT:
		# реакции — разрешение участника (chatBannedRights.send_reactions);
		# администратору ограничения не мешают
		if rights.status.administers:
			return True
		return rights.status.in_community and rights.allowed.send_reactions
	if action is ExecutorAction.INVITE:
		# приглашать может и обычный участник, если сообщество ему это
		# оставило, — в группах настройка частая
		if rights.status.administers:
			return rights.admin.invite_users
		return rights.status.in_community and rights.allowed.invite_users
	# остальное — права администратора поимённо: роль сама по себе
	# не гарантирует ни удаления чужих сообщений, ни исключений.
	# Ссылку-приглашение — в отличие от самого приглашения — Telegram
	# показывает только администратору (живая проверка 20.09.2026)
	if not rights.status.administers:
		return False
	return {
		ExecutorAction.EDIT_OTHERS: rights.admin.edit_messages,
		ExecutorAction.DELETE_OTHERS: rights.admin.delete_messages,
		ExecutorAction.BAN: rights.admin.ban_users,
		ExecutorAction.INVITE_LINK: rights.admin.invite_users,
		# заявки одобряет администратор с правом приглашать: в MTProto
		# право не названо словами, у Bot API для тех же методов —
		# can_invite_users (ADR-0040; проверяется живьём)
		ExecutorAction.APPROVE_REQUESTS: rights.admin.invite_users,
		ExecutorAction.PROMOTE: rights.admin.add_admins,
		# настройки сообщества (ADR-0043). Участнику группы Telegram
		# тоже может оставить право менять информацию, но экран настроек
		# на него не рассчитывает: какие из настроек оно открывает,
		# документация не говорит, а угадывать правила нельзя
		ExecutorAction.CHANGE_INFO: rights.admin.change_info,
		# TDLib называет его can_restrict_members, MTProto — ban_users
		ExecutorAction.RESTRICT_MEMBERS: rights.admin.ban_users,
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


def _can_publish_as_community(rights: ExecutorRights, kind: CommunityKind) -> bool:
	"""Публикация **от имени сообщества** (ADR-0036).

	В **канале** это обычная публикация: пост там всегда от имени канала.
	В **группе** от имени группы публикует только администратор с правом
	``anonymous`` — по правилам Telegram он «может публиковать только
	от имени группы или своих каналов»; участник и неанонимный
	администратор публикуют от своего имени, и группу в ``send_as``
	им не дают (core.telegram.org/api/rights, сверено 21.09.2026).
	"""
	if kind is CommunityKind.CHANNEL:
		return _can_publish(rights, kind)
	# правило о правах пользователя: бот с тем же флагом «анонимность»
	# всё равно пишет от своего имени (живая проверка 21.09.2026),
	# поэтому кандидатов на лицо «сообщество» подбор ограничивает
	# пользователями
	return rights.status.administers and rights.admin.anonymous


#: Что сказать человеку, когда способного исполнителя в пуле нет.
#: Текст один на все действия: отличается только само действие, а совет
#: одинаков — выдать право в Telegram и перепроверить доступы.
ACTION_WORDS: dict[ExecutorAction, str] = {
	ExecutorAction.PUBLISH: "публиковать",
	ExecutorAction.PUBLISH_AS_COMMUNITY: "публиковать от имени сообщества",
	ExecutorAction.EDIT_OTHERS: "править чужие сообщения",
	ExecutorAction.DELETE_OTHERS: "удалять чужие сообщения",
	ExecutorAction.BAN: "исключать участников",
	ExecutorAction.INVITE: "приглашать",
	ExecutorAction.INVITE_LINK: "видеть ссылку-приглашение",
	ExecutorAction.PROMOTE: "назначать администраторов",
	ExecutorAction.READ_HISTORY: "читать историю и участников",
	ExecutorAction.REACT: "ставить реакции",
	ExecutorAction.APPROVE_REQUESTS: "принимать заявки на вступление",
	ExecutorAction.CHANGE_INFO: "менять информацию сообщества",
	ExecutorAction.RESTRICT_MEMBERS: "ограничивать участников",
	ExecutorAction.OWN: "распоряжаться как владелец",
}


#: Права, которые приложение выдаёт боту, вводя его в **канал** (ADR-0035).
#: Участником бот в канале не бывает, поэтому ввод — это назначение
#: администратором, и набор прав приходится называть заранее. Названы
#: ровно те два, ради которых бот и нужен: опубликовать пост запасным
#: путём и поставить кнопки под постом публикатора (ADR-0031). Больше
#: не просим: чего приложение не делает, того ему и не нужно, а лишнее
#: право у бота — лишний риск для канала.
BOT_ADMIN_RIGHTS = AdminRights(post_messages=True, edit_messages=True)
