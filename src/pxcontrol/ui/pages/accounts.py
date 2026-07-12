"""Страница «Аккаунты»: боты, userbot-аккаунты MTProto, ключи ИИ."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	CaptionLabel,
	CardWidget,
	FluentIcon,
	InfoBar,
	MessageBox,
	PushButton,
	ScrollArea,
	StrongBodyLabel,
	SubtitleLabel,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import AiKeyDto, BotDto, TgAccountDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import FormDialog, bind, clear_layout, confirm_delete


class _Section(QWidget):
	"""Группа страницы: заголовок, кнопка «Добавить», список карточек."""

	def __init__(
		self, title: str, on_add: Callable[[], None], parent: QWidget | None = None
	) -> None:
		super().__init__(parent)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel(title, self))
		header.addStretch()
		add_button = PushButton(FluentIcon.ADD, "Добавить", self)
		add_button.clicked.connect(on_add)
		header.addWidget(add_button)
		layout.addLayout(header)
		self._rows = QVBoxLayout()
		self._rows.setSpacing(8)
		layout.addLayout(self._rows)

	def set_rows(self, rows: list[QWidget], empty_hint: str) -> None:
		"""Заменяет список карточек; если пусто — показывает подсказку."""
		clear_layout(self._rows)
		if not rows:
			self._rows.addWidget(CaptionLabel(empty_hint, self))
			return
		for row in rows:
			self._rows.addWidget(row)


class AccountsPage(ScrollArea):
	"""Управление ботами, userbot-аккаунтами и ключами ИИ (ADR-0009)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("accounts")
		self._worker = worker
		self._build()
		self._reload_all()

	# --- сборка страницы ----------------------------------------------------

	def _build(self) -> None:
		"""Собирает три группы в прокручиваемом контейнере."""
		container = QWidget(self)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.setSpacing(24)
		self._bots = _Section("Боты", self._on_add_bot, container)
		self._accounts = _Section("Userbot (MTProto)", self._on_add_account, container)
		self._keys = _Section("Ключи ИИ", self._on_add_key, container)
		for section in (self._bots, self._accounts, self._keys):
			layout.addWidget(section)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()

	def _row(
		self,
		title: str,
		subtitle: str,
		on_delete: Callable[[], None],
		trailing: QWidget | None = None,
	) -> CardWidget:
		"""Карточка-строка списка: название, подпись, удаление."""
		card = CardWidget(self)
		layout = QHBoxLayout(card)
		layout.setContentsMargins(16, 10, 10, 10)
		column = QVBoxLayout()
		column.setSpacing(2)
		column.addWidget(StrongBodyLabel(title, card))
		column.addWidget(CaptionLabel(subtitle, card))
		layout.addLayout(column)
		layout.addStretch()
		if trailing is not None:
			layout.addWidget(trailing)
		delete_button = TransparentToolButton(FluentIcon.DELETE, card)
		delete_button.clicked.connect(on_delete)
		layout.addWidget(delete_button)
		return card

	# --- общие помощники ------------------------------------------------------

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку всплывающей плашкой."""
		InfoBar.error("Ошибка", message, parent=self, duration=6000)

	def _reload_all(self) -> None:
		"""Перезагружает все три списка из движка."""
		self._reload_bots()
		self._reload_accounts()
		self._reload_keys()

	# --- боты ---------------------------------------------------------------

	def _reload_bots(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.accounts.list_bots(),
			self, self._show_bots, self._show_error,
		)

	def _show_bots(self, bots: list[BotDto]) -> None:
		rows = [
			self._row(
				bot.label,
				f"@{bot.username or '—'} · {bot.token_masked}",
				bind(self._delete_bot, bot),
				trailing=self._diag_button(bot),
			)
			for bot in bots
		]
		self._bots.set_rows(rows, "Пока нет ботов — добавьте токен от @BotFather.")

	def _diag_button(self, bot: BotDto) -> TransparentToolButton:
		"""Кнопка диагностики «где состоит бот»."""
		button = TransparentToolButton(FluentIcon.SEARCH, self)
		button.setToolTip("Где состоит бот? События Telegram за 24 часа")
		button.clicked.connect(bind(self._diagnose_bot, bot))
		return button

	def _diagnose_bot(self, bot: BotDto) -> None:
		"""Запрашивает события бота и показывает результат."""
		InfoBar.info("Диагностика", "Читаю события бота…", parent=self)
		run_in_engine(
			self._worker, self._worker.engine.accounts.bot_whereabouts(bot.id),
			self, partial(self._show_diagnosis, bot), self._show_error,
		)

	def _show_diagnosis(self, bot: BotDto, lines: object) -> None:
		"""Показывает, где состоит бот (или что событий не было)."""
		text = "\n".join(lines) if lines else (  # type: ignore[arg-type]
			"Событий за последние 24 часа нет — Telegram хранит их сутки.\n"
			"Добавьте бота администратором канала и проверьте снова."
		)
		box = MessageBox(f"Где состоит @{bot.username or bot.label}", text, self.window())
		box.yesButton.setText("Понятно")
		box.cancelButton.hide()
		box.exec()

	def _on_add_bot(self) -> None:
		dialog = FormDialog(
			"Новый бот",
			[("label", "Название (для себя)"), ("token", "Токен от @BotFather")],
			self.window(),
		)
		if not dialog.exec():
			return
		label, token = dialog.value("label"), dialog.value("token")
		if not (label and token):
			self._show_error("Заполните оба поля.")
			return
		InfoBar.info("Проверка", "Проверяю токен через Telegram…", parent=self)
		run_in_engine(
			self._worker, self._worker.engine.accounts.add_bot(label, token),
			self, self._on_bot_added, self._show_error,
		)

	def _on_bot_added(self, bot: BotDto) -> None:
		InfoBar.success("Бот добавлен", f"@{bot.username}", parent=self)
		self._reload_bots()

	def _delete_bot(self, bot: BotDto) -> None:
		if not confirm_delete(self, f"Удалить бота «{bot.label}»?"):
			return
		run_in_engine(
			self._worker, self._worker.engine.accounts.delete_bot(bot.id),
			self, lambda _r: self._reload_bots(), self._show_error,
		)

	# --- userbot (MTProto) ----------------------------------------------------

	def _reload_accounts(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.accounts.list_tg_accounts(),
			self, self._show_accounts, self._show_error,
		)

	def _show_accounts(self, accounts: list[TgAccountDto]) -> None:
		rows = [self._account_row(account) for account in accounts]
		self._accounts.set_rows(
			rows, "Пока нет аккаунтов — понадобятся api_id и api_hash с my.telegram.org."
		)

	def _account_row(self, account: TgAccountDto) -> CardWidget:
		status = "вход выполнен ✓" if account.logged_in else "вход не выполнен"
		trailing: QWidget | None = None
		if not account.logged_in:
			login_button = PushButton("Войти", self)
			login_button.clicked.connect(bind(self._start_login, account))
			trailing = login_button
		subtitle = (
			f"{account.phone or 'без телефона'} · api_id {account.api_id} · {status}"
		)
		return self._row(
			account.label, subtitle,
			bind(self._delete_account, account),
			trailing=trailing,
		)

	# --- вход userbot: телефон → код → (пароль 2FA) ---------------------------

	def _start_login(self, account: TgAccountDto) -> None:
		"""Шаг 1: просим Telegram отправить код на телефон аккаунта."""
		InfoBar.info(
			"Вход", f"Отправляю код на {account.phone or 'номер аккаунта'}…",
			parent=self,
		)
		run_in_engine(
			self._worker, self._worker.engine.accounts.start_login(account.id),
			self, partial(self._ask_code, account), self._show_error,
		)

	def _ask_code(self, account: TgAccountDto, _phone: object = None) -> None:
		"""Шаг 2: спрашиваем код, присланный Telegram."""
		dialog = FormDialog(
			f"Код отправлен ({account.phone})",
			[("code", "Код из Telegram")],
			self.window(), accept_text="Подтвердить",
		)
		if not dialog.exec():
			self._cancel_login(account)
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.confirm_login_code(
				account.id, dialog.value("code")
			),
			self, partial(self._after_code, account), self._show_error,
		)

	def _after_code(self, account: TgAccountDto, done: object) -> None:
		"""После кода: вход завершён (``done``) или нужен пароль 2FA."""
		if done:
			InfoBar.success("Вход выполнен", account.label, parent=self)
			self._reload_accounts()
			return
		self._ask_password(account)

	def _ask_password(self, account: TgAccountDto) -> None:
		"""Шаг 3 (если включён): пароль двухфакторной защиты."""
		dialog = FormDialog(
			"Двухфакторная защита",
			[("password", "Пароль 2FA")],
			self.window(), accept_text="Войти", password_fields=("password",),
		)
		if not dialog.exec():
			self._cancel_login(account)
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.confirm_login_password(
				account.id, dialog.value("password")
			),
			self, partial(self._after_password, account), self._show_error,
		)

	def _after_password(self, account: TgAccountDto, _result: object = None) -> None:
		"""Пароль принят — вход завершён."""
		InfoBar.success("Вход выполнен", account.label, parent=self)
		self._reload_accounts()

	def _cancel_login(self, account: TgAccountDto) -> None:
		"""Пользователь закрыл диалог — прерываем незавершённый вход."""
		run_in_engine(
			self._worker, self._worker.engine.accounts.cancel_login(account.id),
			self, lambda _r: None, self._show_error,
		)

	def _on_add_account(self) -> None:
		fields = [
			("label", "Название"), ("phone", "Телефон (справочно)"),
			("api_id", "api_id с my.telegram.org"), ("api_hash", "api_hash"),
		]
		dialog = FormDialog("Новый userbot-аккаунт", fields, self.window())
		if not dialog.exec():
			return
		label, api_hash = dialog.value("label"), dialog.value("api_hash")
		if not (label and api_hash and dialog.value("api_id").isdigit()):
			self._show_error("Обязательны: название, числовой api_id и api_hash.")
			return
		coro = self._worker.engine.accounts.add_tg_account(
			label, dialog.value("phone") or None, int(dialog.value("api_id")), api_hash
		)
		run_in_engine(self._worker, coro, self, self._on_account_added, self._show_error)

	def _on_account_added(self, account: TgAccountDto) -> None:
		InfoBar.success("Аккаунт сохранён", account.label, parent=self)
		self._reload_accounts()

	def _delete_account(self, account: TgAccountDto) -> None:
		if not confirm_delete(self, f"Удалить аккаунт «{account.label}»?"):
			return
		run_in_engine(
			self._worker, self._worker.engine.accounts.delete_tg_account(account.id),
			self, lambda _r: self._reload_accounts(), self._show_error,
		)

	# --- ключи ИИ --------------------------------------------------------------

	def _reload_keys(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.accounts.list_ai_keys(),
			self, self._show_keys, self._show_error,
		)

	def _show_keys(self, keys: list[AiKeyDto]) -> None:
		rows = [
			self._row(
				key.label or key.provider,
				f"{key.provider} · {key.key_masked}",
				bind(self._delete_key, key),
			)
			for key in keys
		]
		self._keys.set_rows(rows, "Пока нет ключей — нужны для генерации контента.")

	def _on_add_key(self) -> None:
		dialog = FormDialog(
			"Новый ключ ИИ (Anthropic)",
			[("label", "Название"), ("api_key", "Ключ API (sk-ant-…)")],
			self.window(),
		)
		if not dialog.exec():
			return
		label, api_key = dialog.value("label"), dialog.value("api_key")
		if not (label and api_key):
			self._show_error("Заполните оба поля.")
			return
		run_in_engine(
			self._worker, self._worker.engine.accounts.add_ai_key(label, api_key),
			self, self._on_key_added, self._show_error,
		)

	def _on_key_added(self, key: AiKeyDto) -> None:
		InfoBar.success("Ключ сохранён", key.label, parent=self)
		self._reload_keys()

	def _delete_key(self, key: AiKeyDto) -> None:
		if not confirm_delete(self, f"Удалить ключ «{key.label}»?"):
			return
		run_in_engine(
			self._worker, self._worker.engine.accounts.delete_ai_key(key.id),
			self, lambda _r: self._reload_keys(), self._show_error,
		)
