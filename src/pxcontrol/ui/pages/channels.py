"""Страница «Каналы»: подключение каналов и их список."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
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
from pxcontrol.engine.services.accounts import BotDto
from pxcontrol.engine.services.channels import ChannelAccess, ChannelDto
from pxcontrol.engine.services.settings import CHANNEL_DEFAULT_PRESET, CHANNEL_ENABLED
from pxcontrol.engine.services.video import PresetDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	TOAST_DURATION_MS,
	bind,
	clear_layout,
	confirm_delete,
	error_reporter,
	noop,
	page_layout,
	row_card,
)


class _ConnectDialog(MessageBoxBase):
	"""Диалог подключения: способ (userbot/бот), бот для бот-способа, ссылка."""

	_HINTS = {
		"userbot": (
			"Аккаунт userbot должен быть администратором канала\n"
			"с правом публиковать сообщения (бот не нужен)."
		),
		"bot": (
			"Перед подключением добавьте бота администратором канала\n"
			"с правом публиковать сообщения."
		),
	}

	def __init__(self, bots: list[BotDto], parent: QWidget) -> None:
		super().__init__(parent)
		self._bots = bots
		self.viewLayout.addWidget(SubtitleLabel("Подключить канал", self))
		self._way = ComboBox(self)
		self._way.addItem("Через userbot (аккаунт — админ канала)")
		self._way.addItem("Через бота")
		self._way.currentIndexChanged.connect(self._on_way_changed)
		self.viewLayout.addWidget(self._way)
		self._hint = BodyLabel("", self)
		self.viewLayout.addWidget(self._hint)
		self._combo = ComboBox(self)
		for bot in bots:
			self._combo.addItem(f"{bot.label} (@{bot.username or '—'})")
		self.viewLayout.addWidget(self._combo)
		self._ref = LineEdit(self)
		self._ref.setPlaceholderText(
			"@имя, ссылка t.me/… или ID -100… (приватный канал)"
		)
		self._ref.setClearButtonEnabled(True)
		self.viewLayout.addWidget(self._ref)
		self.yesButton.setText("Подключить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(460)
		self._on_way_changed(0)

	def _on_way_changed(self, index: int) -> None:
		"""Показывает выбор бота только для бот-способа."""
		self._combo.setVisible(index == 1)
		self._hint.setText(self._HINTS["bot" if index == 1 else "userbot"])

	def way(self) -> str:
		"""Способ подключения: 'userbot' или 'bot'."""
		return "bot" if int(self._way.currentIndex()) == 1 else "userbot"

	def bot_id(self) -> int | None:
		"""Идентификатор выбранного бота (None — ботов нет)."""
		index = int(self._combo.currentIndex())
		if 0 <= index < len(self._bots):
			return self._bots[index].id
		return None

	def chat_ref(self) -> str:
		"""Введённая ссылка на канал."""
		return str(self._ref.text()).strip()


class _AssignBotDialog(MessageBoxBase):
	"""Выбор бота для назначения каналу."""

	def __init__(self, bots: list[BotDto], parent: QWidget) -> None:
		super().__init__(parent)
		self._bots = bots
		self.viewLayout.addWidget(SubtitleLabel("Назначить бота", self))
		self.viewLayout.addWidget(BodyLabel(
			"Бот должен быть администратором канала\n"
			"с правом публиковать сообщения.", self,
		))
		self._combo = ComboBox(self)
		for bot in bots:
			self._combo.addItem(f"{bot.label} (@{bot.username or '—'})")
		self.viewLayout.addWidget(self._combo)
		self.yesButton.setText("Назначить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def bot_id(self) -> int:
		"""Идентификатор выбранного бота."""
		return self._bots[int(self._combo.currentIndex())].id


class _ChannelPresetDialog(MessageBoxBase):
	"""Выбор пресета обработки видео по умолчанию для канала."""

	def __init__(
		self,
		channel_title: str,
		presets: list[PresetDto],
		current_id: int | None,
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self._presets = presets
		self.viewLayout.addWidget(SubtitleLabel("Пресет по умолчанию", self))
		self.viewLayout.addWidget(BodyLabel(
			f"Канал «{channel_title}»: пресет подставляется\n"
			"на странице «Видео» при выборе канала.", self,
		))
		self._combo = ComboBox(self)
		self._combo.addItem("(не задан)")
		for preset in presets:
			self._combo.addItem(preset.name)
		ids = [preset.id for preset in presets]
		if current_id in ids:
			self._combo.setCurrentIndex(ids.index(current_id) + 1)
		self.viewLayout.addWidget(self._combo)
		self.yesButton.setText("Сохранить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def preset_id(self) -> int | None:
		"""Идентификатор выбранного пресета (None — «не задан»)."""
		index = int(self._combo.currentIndex())
		if index <= 0:
			return None
		return self._presets[index - 1].id


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
		self._list.setSpacing(8)
		layout.addLayout(self._list)
		layout.addStretch()

	# --- список ---------------------------------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker, self._worker.engine.channels.list_channels(),
			self, self._show_channels, self._show_error,
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
			"Нажмите «Подключить канал»: через userbot (аккаунт — админ) "
			"или через бота.",
			box,
		)
		hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title)
		layout.addWidget(hint)
		return box

	def _channel_row(self, channel: ChannelDto) -> CardWidget:
		"""Карточка канала: название, способы администрирования, действия."""
		ways = []
		if channel.bot_label:
			ways.append(f"бот {channel.bot_label}")
		if channel.userbot_admin:
			ways.append("userbot")
		subtitle = (
			f"@{channel.username or '—'} · админ: {' + '.join(ways) or '—'}"
		)
		buttons = QWidget(self)
		row = QHBoxLayout(buttons)
		row.setContentsMargins(0, 0, 0, 0)
		enabled_switch = SwitchButton(buttons)
		enabled_switch.setChecked(channel.enabled)
		enabled_switch.setToolTip(
			"Канал активен: участвует в публикации и опросе расписания"
		)
		enabled_switch.checkedChanged.connect(
			partial(self._on_toggle_enabled, channel)
		)
		row.addWidget(enabled_switch)
		recheck = PushButton("Проверить доступы", buttons)
		recheck.clicked.connect(bind(self._recheck_channel, channel))
		row.addWidget(recheck)
		preset_action = PushButton("Пресет…", buttons)
		preset_action.setToolTip("Пресет обработки видео по умолчанию")
		preset_action.clicked.connect(bind(self._on_choose_preset, channel))
		row.addWidget(preset_action)
		if channel.bot_id is None:
			bot_action = PushButton("Назначить бота…", buttons)
			bot_action.clicked.connect(bind(self._on_assign_bot, channel))
		else:
			bot_action = PushButton("Отвязать бота", buttons)
			bot_action.clicked.connect(bind(self._on_unassign_bot, channel))
		row.addWidget(bot_action)
		return row_card(
			self, channel.title, subtitle, trailing=buttons,
			on_delete=bind(self._delete_channel, channel),
		)

	# --- настройки канала (активность, пресет) -----------------------------------

	def _on_toggle_enabled(self, channel: ChannelDto, checked: bool) -> None:
		"""Включает/выключает канал (публикация и расписание)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(
				CHANNEL_ENABLED, channel.id, checked
			),
			self, noop, self._on_toggle_failed,
		)

	def _on_toggle_failed(self, message: str) -> None:
		"""Ошибка записи флага: показать и вернуть карточкам правду из БД."""
		self._show_error(message)
		self._reload()

	def _on_choose_preset(self, channel: ChannelDto) -> None:
		"""Открывает выбор пресета по умолчанию (сначала — список пресетов)."""
		run_in_engine(
			self._worker, self._worker.engine.video.list_presets(),
			self, partial(self._on_presets_loaded, channel), self._show_error,
		)

	def _on_presets_loaded(
		self, channel: ChannelDto, presets: list[PresetDto]
	) -> None:
		"""Пресеты получены — узнаём текущий выбор канала."""
		if not presets:
			self._show_error(
				"Пресетов пока нет — сохраните хотя бы один на странице «Видео»."
			)
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(CHANNEL_DEFAULT_PRESET, channel.id),
			self, partial(self._open_preset_dialog, channel, presets),
			self._show_error,
		)

	def _open_preset_dialog(
		self, channel: ChannelDto, presets: list[PresetDto], current_id: int | None
	) -> None:
		"""Диалог выбора; сохранение — настройкой канала."""
		dialog = _ChannelPresetDialog(channel.title, presets, current_id, self.window())
		if not dialog.exec():
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(
				CHANNEL_DEFAULT_PRESET, channel.id, dialog.preset_id()
			),
			self, partial(self._on_preset_saved, channel), self._show_error,
		)

	def _on_preset_saved(self, channel: ChannelDto, _result: object = None) -> None:
		InfoBar.success(
			"Готово", f"Пресет по умолчанию «{channel.title}» сохранён.", parent=self
		)

	# --- доступы и бот -----------------------------------------------------------

	def _recheck_channel(self, channel: ChannelDto) -> None:
		"""Перепроверяет оба способа администрирования канала."""
		InfoBar.info("Проверка", f"Проверяю доступы «{channel.title}»…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.recheck_channel(channel.id),
			self, self._on_rechecked, self._show_error,
		)

	def _on_rechecked(self, access: ChannelAccess) -> None:
		"""Показывает итог перепроверки и обновляет список."""
		parts = [f"userbot: {'админ' if access.userbot_ok else 'не админ'}"]
		if access.bot_ok is not None:
			parts.append(f"бот: {'права на месте' if access.bot_ok else 'права потеряны'}")
		summary = " · ".join(parts)
		if access.userbot_ok and access.bot_ok is not False:
			InfoBar.success(access.channel.title, summary, parent=self)
		else:
			InfoBar.warning(
				access.channel.title, summary, parent=self,
				duration=TOAST_DURATION_MS,
			)
		self._reload()

	def _on_assign_bot(self, channel: ChannelDto) -> None:
		"""Открывает выбор бота для назначения каналу."""
		run_in_engine(
			self._worker, self._worker.engine.accounts.list_bots(),
			self, partial(self._open_assign_dialog, channel), self._show_error,
		)

	def _open_assign_dialog(self, channel: ChannelDto, bots: list[BotDto]) -> None:
		"""Диалог выбора бота; после выбора — проверка его прав в канале."""
		if not bots:
			self._show_error("Сначала добавьте бота: Настройки → Аккаунты.")
			return
		dialog = _AssignBotDialog(bots, self.window())
		if not dialog.exec():
			return
		InfoBar.info("Проверка", "Проверяю права бота в канале…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.assign_bot(channel.id, dialog.bot_id()),
			self, self._on_bot_changed, self._show_error,
		)

	def _on_unassign_bot(self, channel: ChannelDto) -> None:
		if not confirm_delete(
			self, f"Отвязать бота от канала «{channel.title}»?",
			accept_text="Отвязать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.channels.unassign_bot(channel.id),
			self, self._on_bot_changed, self._show_error,
		)

	def _on_bot_changed(self, channel: ChannelDto) -> None:
		InfoBar.success("Готово", channel.title, parent=self)
		self._reload()

	# --- подключение -----------------------------------------------------------

	def _on_connect(self) -> None:
		"""Загружает список ботов и открывает диалог подключения."""
		run_in_engine(
			self._worker, self._worker.engine.accounts.list_bots(),
			self, self._open_connect_dialog, self._show_error,
		)

	def _open_connect_dialog(self, bots: list[BotDto]) -> None:
		dialog = _ConnectDialog(bots, self.window())
		if not dialog.exec():
			return
		if not dialog.chat_ref():
			self._show_error("Укажите @имя, ссылку или ID канала.")
			return
		if dialog.way() == "bot":
			bot_id = dialog.bot_id()
			if bot_id is None:
				self._show_error("Сначала добавьте бота: Настройки → Аккаунты.")
				return
			coro = self._worker.engine.channels.add_channel(
				bot_id, dialog.chat_ref()
			)
		else:
			coro = self._worker.engine.channels.add_channel_via_userbot(
				dialog.chat_ref()
			)
		InfoBar.info("Проверка", "Проверяю канал и права…", parent=self)
		run_in_engine(self._worker, coro, self, self._on_connected, self._show_error)

	def _on_connected(self, channel: ChannelDto) -> None:
		InfoBar.success("Канал подключён", channel.title, parent=self)
		self._reload()

	def _delete_channel(self, channel: ChannelDto) -> None:
		if not confirm_delete(
			self, f"Удалить канал «{channel.title}» из приложения?"
		):
			return
		run_in_engine(
			self._worker, self._worker.engine.channels.delete_channel(channel.id),
			self, self._reload, self._show_error,
		)
