"""Страница «Каналы»: подключение каналов и их список."""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	ComboBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	SwitchButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.channels import ChannelAccess, ChannelDto
from pxcontrol.engine.services.settings import (
	CHANNEL_DEFAULT_PRESET,
	CHANNEL_ENABLED,
	PUBLISH_TIMES,
	SettingKey,
)
from pxcontrol.engine.services.video import PresetDto
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	bind,
	bot_caption,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
	noop,
	page_layout,
	parse_hhmm,
	row_card,
	show_warning,
)


class _ConnectDialog(MessageBoxBase):
	"""Диалог подключения: способ (userbot/бот), исполнитель, ссылка.

	Для userbot-способа аккаунт выбирается явно (ADR-0019): его права
	проверяются, и именно он привязывается к каналу как публикатор.
	"""

	_HINTS = {
		"userbot": (
			"Выбранный аккаунт должен быть администратором канала\n"
			"с правом публиковать сообщения (бот не нужен)."
		),
		"bot": (
			"Перед подключением добавьте бота администратором канала\n"
			"с правом публиковать сообщения."
		),
	}

	def __init__(self, bots: list[BotDto], accounts: list[TgAccountDto], parent: QWidget) -> None:
		"""``accounts`` — вошедшие userbot-аккаунты (кандидаты в админы)."""
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Подключить канал", self))
		self._way = ComboBox(self)
		self._way.addItem("Через userbot (аккаунт — админ канала)")
		self._way.addItem("Через бота")
		self._way.currentIndexChanged.connect(self._on_way_changed)
		self.viewLayout.addWidget(self._way)
		self._hint = BodyLabel("", self)
		self.viewLayout.addWidget(self._hint)
		self._account_combo: DtoComboBox[TgAccountDto] = DtoComboBox(self)
		self._account_combo.set_items(accounts, label=lambda acc: f"{acc.label} ({acc.phone})")
		self.viewLayout.addWidget(self._account_combo)
		self._combo: DtoComboBox[BotDto] = DtoComboBox(self)
		self._combo.set_items(bots, label=lambda bot: bot_caption(bot.label, bot.username))
		self.viewLayout.addWidget(self._combo)
		self._ref = LineEdit(self)
		self._ref.setPlaceholderText("@имя, ссылка t.me/… или ID -100… (приватный канал)")
		self._ref.setClearButtonEnabled(True)
		self.viewLayout.addWidget(self._ref)
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("Подключить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(460)
		self._on_way_changed(0)

	def validate(self) -> bool:
		"""Крючок MessageBoxBase: при ошибке диалог не закрывается —
		введённая ссылка не пропадает."""
		if not self.chat_ref():
			return self._error.fail("Укажите @имя, ссылку или ID канала.")
		if self.way() == "bot" and self.bot_id() is None:
			return self._error.fail("Сначала добавьте бота: Настройки → Аккаунты.")
		if self.way() == "userbot" and self.account_id() is None:
			return self._error.fail(
				"Нет вошедших userbot-аккаунтов — войдите: Настройки → Аккаунты."
			)
		return self._error.succeed()

	def _on_way_changed(self, index: int) -> None:
		"""Показывает выбор исполнителя своего способа."""
		self._combo.setVisible(index == 1)
		self._account_combo.setVisible(index == 0)
		self._hint.setText(self._HINTS["bot" if index == 1 else "userbot"])

	def way(self) -> str:
		"""Способ подключения: 'userbot' или 'bot'."""
		return "bot" if int(self._way.currentIndex()) == 1 else "userbot"

	def bot_id(self) -> int | None:
		"""Идентификатор выбранного бота (None — ботов нет)."""
		bot = self._combo.selected()
		return bot.id if bot is not None else None

	def account_id(self) -> int | None:
		"""Идентификатор выбранного userbot-аккаунта (None — вошедших нет)."""
		account = self._account_combo.selected()
		return account.id if account is not None else None

	def chat_ref(self) -> str:
		"""Введённая ссылка на канал."""
		return str(self._ref.text()).strip()


class _AssignBotDialog(MessageBoxBase):
	"""Выбор бота для назначения каналу."""

	def __init__(self, bots: list[BotDto], parent: QWidget) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Назначить бота", self))
		self.viewLayout.addWidget(
			BodyLabel(
				"Бот должен быть администратором канала\nс правом публиковать сообщения.",
				self,
			)
		)
		self._combo: DtoComboBox[BotDto] = DtoComboBox(self)
		self._combo.set_items(bots, label=lambda bot: bot_caption(bot.label, bot.username))
		self.viewLayout.addWidget(self._combo)
		self.yesButton.setText("Назначить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def bot_id(self) -> int | None:
		"""Идентификатор выбранного бота (None — ботов нет)."""
		bot = self._combo.selected()
		return bot.id if bot is not None else None


class _AssignUserbotDialog(MessageBoxBase):
	"""Выбор userbot-аккаунта для привязки к каналу (ADR-0019)."""

	def __init__(self, accounts: list[TgAccountDto], parent: QWidget) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Привязать userbot", self))
		self.viewLayout.addWidget(
			BodyLabel(
				"Аккаунт должен быть администратором канала с правом\n"
				"публиковать — посты пойдут из его сессии.",
				self,
			)
		)
		self._combo: DtoComboBox[TgAccountDto] = DtoComboBox(self)
		self._combo.set_items(accounts, label=lambda acc: f"{acc.label} ({acc.phone})")
		self.viewLayout.addWidget(self._combo)
		self.yesButton.setText("Привязать")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def account_id(self) -> int | None:
		"""Идентификатор выбранного аккаунта (None — вошедших нет)."""
		account = self._combo.selected()
		return account.id if account is not None else None


class _ChannelPrefsDialog(MessageBoxBase):
	"""Настройки канала: пресет видео по умолчанию и времена публикации."""

	_TIMES_HINT = "Через запятую, первое — по умолчанию; пусто — без стандартных."

	def __init__(
		self,
		channel_title: str,
		presets: list[PresetDto],
		current_id: int | None,
		times: list[str],
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Настройки канала", self))
		self.viewLayout.addWidget(BodyLabel(f"«{channel_title}»", self))
		self.viewLayout.addWidget(BodyLabel("Пресет видео по умолчанию:", self))
		self._combo: DtoComboBox[PresetDto] = DtoComboBox(self, placeholder="(не задан)")
		self._combo.set_items(presets, label=lambda preset: preset.name)
		if current_id is not None:
			self._combo.select(lambda preset: preset.id == current_id)
		self.viewLayout.addWidget(self._combo)
		self.viewLayout.addWidget(BodyLabel("Времена публикации (ЧЧ:ММ):", self))
		self._times_edit = LineEdit(self)
		self._times_edit.setPlaceholderText("10:00, 18:30…")
		self._times_edit.setText(", ".join(str(t) for t in times))
		self.viewLayout.addWidget(self._times_edit)
		self._times_hint = CaptionLabel(self._TIMES_HINT, self)
		self.viewLayout.addWidget(self._times_hint)
		self.yesButton.setText("Сохранить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def validate(self) -> bool:  # noqa: N802 — API MessageBoxBase
		"""Не даёт сохранить времена в неверном формате (диалог открыт)."""
		try:
			self.times()
		except ValueError as exc:
			self._times_hint.setText(f"⚠ {exc}")
			return False
		return True

	def preset_id(self) -> int | None:
		"""Идентификатор выбранного пресета (None — «не задан»)."""
		preset = self._combo.selected()
		return preset.id if preset is not None else None

	def times(self) -> list[str]:
		"""Времена публикации из поля — нормализованные «ЧЧ:ММ».

		Raises:
			ValueError: Какое-то из времён не в формате «ЧЧ:ММ».
		"""
		raw = str(self._times_edit.text()).strip()
		if not raw:
			return []
		result = []
		for token in raw.split(","):
			hours, minutes = parse_hhmm(token)
			result.append(f"{hours:02d}:{minutes:02d}")
		return result


class ChannelsPage(ScrollArea):
	"""Список подключённых каналов; подключение через проверку прав бота."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("channels")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._build()
		self._reload()

	def _build(self) -> None:
		"""Собирает шапку с кнопкой и область списка."""
		layout = page_layout(self)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Подключённые каналы", self))
		header.addStretch()
		connect_button = PrimaryPushButton(FluentIcon.ADD, "Подключить канал", self)
		connect_button.clicked.connect(self._on_connect)
		header.addWidget(connect_button)
		layout.addLayout(header)
		self._list = QVBoxLayout()
		self._list.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._list)
		layout.addStretch()

	# --- список ---------------------------------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.channels.list_channels(),
			self,
			self._show_channels,
			self._show_error,
		)

	def _show_channels(self, channels: list[ChannelDto]) -> None:
		clear_layout(self._list)
		if not channels:
			self._list.addWidget(self._empty_state())
			return
		for channel in channels:
			self._list.addWidget(self._channel_row(channel))

	def _empty_state(self) -> QWidget:
		"""Пустое состояние с подсказкой."""
		box = QWidget(self)
		layout = QVBoxLayout(box)
		layout.setContentsMargins(0, 48, 0, 0)
		title = SubtitleLabel("Пока нет подключённых каналов", box)
		title.setAlignment(Qt.AlignmentFlag.AlignCenter)
		hint = BodyLabel(
			"Нажмите «Подключить канал»: через userbot (аккаунт — админ) или через бота.",
			box,
		)
		hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title)
		layout.addWidget(hint)
		return box

	def _channel_row(self, channel: ChannelDto) -> CardWidget:
		"""Карточка канала: название, публикаторы, действия."""
		ways = []
		if channel.tg_account_label:
			ways.append(f"userbot {channel.tg_account_label}")
		if channel.bot_label:
			ways.append(f"бот {channel.bot_label}")
		subtitle = f"@{channel.username or '—'} · админ: {' + '.join(ways) or '—'}"
		buttons = QWidget(self)
		row = QHBoxLayout(buttons)
		row.setContentsMargins(0, 0, 0, 0)
		enabled_switch = SwitchButton(buttons)
		enabled_switch.setChecked(channel.enabled)
		enabled_switch.setToolTip("Канал активен: участвует в публикации и опросе расписания")
		enabled_switch.checkedChanged.connect(partial(self._on_toggle_enabled, channel))
		row.addWidget(enabled_switch)
		recheck = PushButton("Проверить доступы", buttons)
		recheck.clicked.connect(bind(self._recheck_channel, channel))
		row.addWidget(recheck)
		prefs_action = PushButton("Настройки…", buttons)
		prefs_action.setToolTip("Пресет видео по умолчанию и времена публикации")
		prefs_action.clicked.connect(bind(self._on_open_prefs, channel))
		row.addWidget(prefs_action)
		if channel.tg_account_id is None:
			userbot_action = PushButton("Привязать userbot…", buttons)
			userbot_action.setToolTip("Постинг пойдёт из сессии привязанного аккаунта")
			userbot_action.clicked.connect(bind(self._on_assign_userbot, channel))
		else:
			userbot_action = PushButton("Отвязать userbot", buttons)
			userbot_action.clicked.connect(bind(self._on_unassign_userbot, channel))
		row.addWidget(userbot_action)
		if channel.bot_id is None:
			bot_action = PushButton("Назначить бота…", buttons)
			bot_action.clicked.connect(bind(self._on_assign_bot, channel))
		else:
			bot_action = PushButton("Отвязать бота", buttons)
			bot_action.clicked.connect(bind(self._on_unassign_bot, channel))
		row.addWidget(bot_action)
		return row_card(
			self,
			channel.title,
			subtitle,
			trailing=buttons,
			on_delete=bind(self._delete_channel, channel),
		)

	# --- настройки канала (активность, пресет) -----------------------------------

	def _on_toggle_enabled(self, channel: ChannelDto, checked: bool) -> None:
		"""Включает/выключает канал (публикация и расписание)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(CHANNEL_ENABLED, channel.id, checked),
			self,
			noop,
			self._on_toggle_failed,
		)

	def _on_toggle_failed(self, message: str) -> None:
		"""Ошибка записи флага: показать и вернуть карточкам правду из БД."""
		self._show_error(message)
		self._reload()

	def _on_open_prefs(self, channel: ChannelDto) -> None:
		"""Открывает настройки канала (цепочка: пресеты → пресет → времена)."""
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_presets(),
			self,
			partial(self._on_presets_loaded, channel),
			self._show_error,
		)

	def _on_presets_loaded(self, channel: ChannelDto, presets: list[PresetDto]) -> None:
		"""Пресеты получены — узнаём текущий выбор канала."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(CHANNEL_DEFAULT_PRESET, channel.id),
			self,
			partial(self._on_current_preset_loaded, channel, presets),
			self._show_error,
		)

	def _on_current_preset_loaded(
		self, channel: ChannelDto, presets: list[PresetDto], current_id: int | None
	) -> None:
		"""Текущий пресет получен — узнаём времена публикации."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, channel.id),
			self,
			partial(self._open_prefs_dialog, channel, presets, current_id),
			self._show_error,
		)

	def _open_prefs_dialog(
		self,
		channel: ChannelDto,
		presets: list[PresetDto],
		current_id: int | None,
		times: list[str],
	) -> None:
		"""Диалог настроек; сохранение — одной транзакцией движка."""
		dialog = _ChannelPrefsDialog(channel.title, presets, current_id, times, self.window())
		if not exec_dialog(dialog):
			return
		# обе настройки — одна пользовательская операция: движок пишет их
		# одной транзакцией (set_for_many), успех сообщается по факту записи
		items: list[tuple[SettingKey[Any], Any]] = [
			(CHANNEL_DEFAULT_PRESET, dialog.preset_id()),
			(PUBLISH_TIMES, dialog.times()),
		]
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for_many(channel.id, items),
			self,
			partial(self._on_prefs_saved, channel),
			self._show_error,
		)

	def _on_prefs_saved(self, channel: ChannelDto, _result: object = None) -> None:
		InfoBar.success("Готово", f"Настройки канала «{channel.title}» сохранены.", parent=self)

	# --- доступы и бот -----------------------------------------------------------

	def _recheck_channel(self, channel: ChannelDto) -> None:
		"""Перепроверяет оба способа администрирования канала."""
		InfoBar.info("Проверка", f"Проверяю доступы «{channel.title}»…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.recheck_channel(channel.id),
			self,
			self._on_rechecked,
			self._show_error,
		)

	def _on_rechecked(self, access: ChannelAccess) -> None:
		"""Показывает итог перепроверки и обновляет список."""
		if access.userbot_ok is None:
			userbot_text = "не удалось проверить (нет связи или аккаунт не подключён)"
		elif access.userbot_ok:
			userbot_text = f"админ — {access.channel.tg_account_label or '—'}"
		else:
			userbot_text = "не админ — привязка снята"
		parts = [f"userbot: {userbot_text}"]
		if access.bot_ok is not None:
			parts.append(f"бот: {'права на месте' if access.bot_ok else 'права потеряны'}")
		summary = " · ".join(parts)
		if access.userbot_ok and access.bot_ok is not False:
			InfoBar.success(access.channel.title, summary, parent=self)
		else:
			show_warning(self, access.channel.title, summary)
		self._reload()

	def _on_assign_bot(self, channel: ChannelDto) -> None:
		"""Открывает выбор бота для назначения каналу."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_bots(),
			self,
			partial(self._open_assign_dialog, channel),
			self._show_error,
		)

	def _open_assign_dialog(self, channel: ChannelDto, bots: list[BotDto]) -> None:
		"""Диалог выбора бота; после выбора — проверка его прав в канале."""
		if not bots:
			self._show_error("Сначала добавьте бота: Настройки → Аккаунты.")
			return
		dialog = _AssignBotDialog(bots, self.window())
		if not exec_dialog(dialog):
			return
		bot_id = dialog.bot_id()
		if bot_id is None:
			return
		InfoBar.info("Проверка", "Проверяю права бота в канале…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.assign_bot(channel.id, bot_id),
			self,
			self._on_bot_changed,
			self._show_error,
		)

	def _on_unassign_bot(self, channel: ChannelDto) -> None:
		if not confirm_delete(
			self,
			f"Отвязать бота от канала «{channel.title}»?",
			accept_text="Отвязать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.channels.unassign_bot(channel.id),
			self,
			self._on_bot_changed,
			self._show_error,
		)

	def _on_bot_changed(self, channel: ChannelDto) -> None:
		InfoBar.success("Готово", channel.title, parent=self)
		self._reload()

	# --- привязка userbot (ADR-0019) --------------------------------------------

	def _on_assign_userbot(self, channel: ChannelDto) -> None:
		"""Открывает выбор аккаунта для привязки к каналу."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_tg_accounts(),
			self,
			partial(self._open_assign_userbot_dialog, channel),
			self._show_error,
		)

	def _open_assign_userbot_dialog(
		self, channel: ChannelDto, accounts: list[TgAccountDto]
	) -> None:
		"""Диалог выбора аккаунта; после выбора — проверка его прав в канале."""
		logged_in = [account for account in accounts if account.logged_in]
		if not logged_in:
			self._show_error("Нет вошедших userbot-аккаунтов — войдите: Настройки → Аккаунты.")
			return
		dialog = _AssignUserbotDialog(logged_in, self.window())
		if not exec_dialog(dialog):
			return
		account_id = dialog.account_id()
		if account_id is None:
			return
		InfoBar.info("Проверка", "Проверяю права аккаунта в канале…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.assign_userbot(channel.id, account_id),
			self,
			self._on_bot_changed,
			self._show_error,
		)

	def _on_unassign_userbot(self, channel: ChannelDto) -> None:
		if not confirm_delete(
			self,
			f"Отвязать userbot от канала «{channel.title}»? Отложенные посты "
			"и большие файлы станут ему недоступны.",
			accept_text="Отвязать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.channels.unassign_userbot(channel.id),
			self,
			self._on_bot_changed,
			self._show_error,
		)

	# --- подключение -----------------------------------------------------------

	def _on_connect(self) -> None:
		"""Загружает ботов и аккаунты, затем открывает диалог подключения."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_bots(),
			self,
			self._on_connect_bots_loaded,
			self._show_error,
		)

	def _on_connect_bots_loaded(self, bots: list[BotDto]) -> None:
		"""Боты получены — вторым шагом список userbot-аккаунтов."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_tg_accounts(),
			self,
			partial(self._open_connect_dialog, bots),
			self._show_error,
		)

	def _open_connect_dialog(self, bots: list[BotDto], accounts: list[TgAccountDto]) -> None:
		logged_in = [account for account in accounts if account.logged_in]
		dialog = _ConnectDialog(bots, logged_in, self.window())
		if not exec_dialog(dialog):
			return
		# пригодность ввода проверил validate() диалога — здесь только сборка
		if dialog.way() == "bot":
			bot_id = dialog.bot_id()
			if bot_id is None:  # недостижимо после validate(), страховка типа
				self._show_error("Сначала добавьте бота: Настройки → Аккаунты.")
				return
			coro = self._worker.engine.channels.add_channel(bot_id, dialog.chat_ref())
		else:
			account_id = dialog.account_id()
			if account_id is None:  # недостижимо после validate(), страховка типа
				self._show_error("Войдите в userbot-аккаунт: Настройки → Аккаунты.")
				return
			coro = self._worker.engine.channels.add_channel_via_userbot(
				account_id, dialog.chat_ref()
			)
		InfoBar.info("Проверка", "Проверяю канал и права…", parent=self)
		run_in_engine(self._worker, coro, self, self._on_connected, self._show_error)

	def _on_connected(self, channel: ChannelDto) -> None:
		InfoBar.success("Канал подключён", channel.title, parent=self)
		self._reload()

	def _delete_channel(self, channel: ChannelDto) -> None:
		if not confirm_delete(self, f"Удалить канал «{channel.title}» из приложения?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.delete_channel(channel.id),
			self,
			self._reload,
			self._show_error,
		)
