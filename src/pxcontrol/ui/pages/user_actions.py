"""Действия с пользователем и ботом — общие для дашборда и страницы аккаунта.

Вход (код и 2FA), пауза и возобновление, пометка, диагностика бота,
удаление с последствиями. Каждая функция получает владельца-виджет
(для диалогов, плашек и моста к движку) и колбэк «готово» — что делать
после успеха (дашборд перечитывает список, страница — свой снимок).
Ошибки показываются владельцу через :func:`error_reporter`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtWidgets import QWidget
from qfluentwidgets import MessageBox

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	FormDialog,
	confirm_delete,
	error_reporter,
	exec_dialog,
	noop,
	show_info,
	show_success,
)
from pxcontrol.ui.pages.user_state import (
	UserState,
	delete_bot_text,
	delete_user_text,
	user_state,
)

Done = Callable[[], None]


# --- пользователь -----------------------------------------------------------------------


def set_user_paused(
	worker: EngineWorker, owner: QWidget, account: TgAccountDto, paused: bool, done: Done
) -> None:
	"""Пауза или возобновление (ADR-0029); подтверждения нет — обратимо."""

	def _finished(updated: TgAccountDto) -> None:
		if paused:
			show_info(
				owner,
				"Приостановлен",
				f"{updated.display}: посты его сообществ ждут возобновления.",
			)
		elif user_state(updated) is UserState.OFFLINE:
			show_info(owner, "Возобновлён", f"{updated.display}: соединение появится при связи.")
		else:
			show_success(owner, "Возобновлён", updated.display)
		done()

	run_in_engine(
		worker,
		worker.engine.accounts.set_tg_account_paused(account.id, paused),
		owner,
		_finished,
		error_reporter(owner),
	)


def save_user_label(
	worker: EngineWorker, owner: QWidget, account: TgAccountDto, label: str, done: Done
) -> None:
	"""Сохраняет пометку, введённую на месте (пусто — снять: имя из Telegram).

	Тот же текст, что и был, не сохраняется: правка отменена уходом
	фокуса без изменений.
	"""
	if label == (account.label or ""):
		return
	run_in_engine(
		worker,
		worker.engine.accounts.set_account_label(account.id, label),
		owner,
		lambda *_a: done(),
		error_reporter(owner),
	)


def save_bot_label(
	worker: EngineWorker, owner: QWidget, bot: BotDto, label: str, done: Done
) -> None:
	"""Сохраняет название бота, введённое на месте; пустое отклоняет движок."""
	if label == bot.label:
		return
	run_in_engine(
		worker,
		worker.engine.accounts.set_bot_label(bot.id, label),
		owner,
		lambda *_a: done(),
		error_reporter(owner),
	)


def delete_user(worker: EngineWorker, owner: QWidget, account: TgAccountDto, done: Done) -> None:
	"""Удаление: сначала — какие сообщества останутся без публикатора."""

	def _confirm(communities: list[CommunityDto]) -> None:
		bound = [c.title for c in communities if c.default_account_id == account.id]
		if not confirm_delete(owner, delete_user_text(account, bound)):
			return
		run_in_engine(
			worker,
			worker.engine.accounts.delete_tg_account(account.id),
			owner,
			done,
			error_reporter(owner),
		)

	run_in_engine(
		worker,
		worker.engine.communities.list_communities(),
		owner,
		_confirm,
		error_reporter(owner),
	)


# --- вход: телефон → код → (пароль 2FA) -------------------------------------------------


def start_login(worker: EngineWorker, owner: QWidget, account: TgAccountDto, done: Done) -> None:
	"""Шаг 1: просим Telegram отправить код на телефон."""
	show_info(owner, "Вход", f"Отправляю код на {account.phone or 'номер аккаунта'}…")
	run_in_engine(
		worker,
		worker.engine.accounts.start_login(account.id),
		owner,
		lambda *_a: _ask_code(worker, owner, account, done),
		error_reporter(owner),
	)


def _ask_code(worker: EngineWorker, owner: QWidget, account: TgAccountDto, done: Done) -> None:
	"""Шаг 2: код, присланный Telegram."""
	dialog = FormDialog(
		f"Код отправлен ({account.phone})",
		[("code", "Код из Telegram")],
		owner.window(),
		accept_text="Подтвердить",
	)
	if not exec_dialog(dialog):
		_cancel_login(worker, owner, account)
		return
	run_in_engine(
		worker,
		worker.engine.accounts.confirm_login_code(account.id, dialog.value("code")),
		owner,
		partial(_after_code, worker, owner, account, done),
		error_reporter(owner),
	)


def _after_code(
	worker: EngineWorker, owner: QWidget, account: TgAccountDto, done: Done, finished: bool
) -> None:
	"""После кода: вход завершён или нужен пароль 2FA."""
	if finished:
		_logged_in(owner, account, done)
		return
	_ask_password(worker, owner, account, done)


def _ask_password(worker: EngineWorker, owner: QWidget, account: TgAccountDto, done: Done) -> None:
	"""Шаг 3 (если включён): пароль двухфакторной защиты."""
	dialog = FormDialog(
		"Двухфакторная защита",
		[("password", "Пароль 2FA")],
		owner.window(),
		accept_text="Войти",
		password_fields=("password",),
	)
	if not exec_dialog(dialog):
		_cancel_login(worker, owner, account)
		return
	run_in_engine(
		worker,
		worker.engine.accounts.confirm_login_password(account.id, dialog.value("password")),
		owner,
		lambda *_a: _logged_in(owner, account, done),
		error_reporter(owner),
	)


def _logged_in(owner: QWidget, account: TgAccountDto, done: Done) -> None:
	show_success(owner, "Вход выполнен", account.display)
	done()


def _cancel_login(worker: EngineWorker, owner: QWidget, account: TgAccountDto) -> None:
	"""Диалог закрыт — прерываем незавершённый вход."""
	run_in_engine(
		worker,
		worker.engine.accounts.cancel_login(account.id),
		owner,
		noop,
		error_reporter(owner),
	)


# --- бот ----------------------------------------------------------------------------------


def set_bot_paused(
	worker: EngineWorker, owner: QWidget, bot: BotDto, paused: bool, done: Done
) -> None:
	"""Пауза или возобновление бота (ADR-0029)."""

	def _finished(updated: BotDto) -> None:
		if paused:
			show_info(owner, "Приостановлен", f"Бот «{updated.label}» больше не используется.")
		else:
			show_success(owner, "Возобновлён", f"Бот «{updated.label}»")
		done()

	run_in_engine(
		worker,
		worker.engine.accounts.set_bot_paused(bot.id, paused),
		owner,
		_finished,
		error_reporter(owner),
	)


def diagnose_bot(worker: EngineWorker, owner: QWidget, bot: BotDto) -> None:
	"""Диагностика «где состоит бот» по событиям Telegram за сутки."""
	show_info(owner, "Диагностика", "Читаю события бота…")

	def _show(lines: list[str]) -> None:
		text = "\n".join(lines) or (
			"Событий за последние 24 часа нет — Telegram хранит их сутки.\n"
			"Добавьте бота администратором сообщества и проверьте снова."
		)
		box = MessageBox(f"Где состоит @{bot.username or bot.label}", text, owner.window())
		box.yesButton.setText("Понятно")
		box.cancelButton.hide()
		exec_dialog(box)

	run_in_engine(
		worker,
		worker.engine.accounts.bot_whereabouts(bot.id),
		owner,
		_show,
		error_reporter(owner),
	)


def delete_bot(worker: EngineWorker, owner: QWidget, bot: BotDto, done: Done) -> None:
	"""Удаление: сначала — какие сообщества останутся без бота."""

	def _confirm(communities: list[CommunityDto]) -> None:
		bound = [c.title for c in communities if c.bot_id == bot.id]
		if not confirm_delete(owner, delete_bot_text(bot, bound)):
			return
		run_in_engine(
			worker,
			worker.engine.accounts.delete_bot(bot.id),
			owner,
			done,
			error_reporter(owner),
		)

	run_in_engine(
		worker,
		worker.engine.communities.list_communities(),
		owner,
		_confirm,
		error_reporter(owner),
	)
