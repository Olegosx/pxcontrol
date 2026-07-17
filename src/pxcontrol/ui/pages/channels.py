"""Страница «Каналы»: подключение каналов и их список."""

from __future__ import annotations

from functools import partial

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
from pxcontrol.engine.services.accounts import BotDto
from pxcontrol.engine.services.channels import ChannelAccess, ChannelDto
from pxcontrol.engine.services.settings import (
	CHANNEL_DEFAULT_PRESET,
	CHANNEL_ENABLED,
	PUBLISH_TIMES,
)
from pxcontrol.engine.services.video import PresetDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	TOAST_DURATION_MS,
	DtoComboBox,
	bind,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
	noop,
	page_layout,
	parse_hhmm,
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
		self.viewLayout.addWidget(SubtitleLabel("Подключить канал", self))
		self._way = ComboBox(self)
		self._way.addItem("Через userbot (аккаунт — админ канала)")
		self._way.addItem("Через бота")
		self._way.currentIndexChanged.connect(self._on_way_changed)
		self.viewLayout.addWidget(self._way)
		self._hint = BodyLabel("", self)
		self.viewLayout.addWidget(self._hint)
		self._combo: DtoComboBox[BotDto] = DtoComboBox(self)
		self._combo.set_items(
			bots, label=lambda bot: f"{bot.label} (@{bot.username or '—'})"
		)
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
		bot = self._combo.selected()
		return bot.id if bot is not None else None

	def chat_ref(self) -> str:
		"""Введённая ссылка на канал."""
		return str(self._ref.text()).strip()


class _AssignBotDialog(MessageBoxBase):
	"""Выбор бота для назначения каналу."""

	def __init__(self, bots: list[BotDto], parent: QWidget) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Назначить бота", self))
		self.viewLayout.addWidget(BodyLabel(
			"Бот должен быть администратором канала\n"
			"с правом публиковать сообщения.", self,
		))
		self._combo: DtoComboBox[BotDto] = DtoComboBox(self)
		self._combo.set_items(
			bots, label=lambda bot: f"{bot.label} (@{bot.username or '—'})"
		)
		self.viewLayout.addWidget(self._combo)
		self.yesButton.setText("Назначить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def bot_id(self) -> int | None:
		"""Идентификатор выбранного бота (None — ботов нет)."""
		bot = self._combo.selected()
		return bot.id if bot is not None else None


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
		self._combo: DtoComboBox[PresetDto] = DtoComboBox(
			self, placeholder="(не задан)"
		)
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
		prefs_action = PushButton("Настройки…", buttons)
		prefs_action.setToolTip(
			"Пресет видео по умолчанию и времена публикации"
		)
		prefs_action.clicked.connect(bind(self._on_open_prefs, channel))
		row.addWidget(prefs_action)
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

	def _on_open_prefs(self, channel: ChannelDto) -> None:
		"""Открывает настройки канала (цепочка: пресеты → пресет → времена)."""
		run_in_engine(
			self._worker, self._worker.engine.video.list_presets(),
			self, partial(self._on_presets_loaded, channel), self._show_error,
		)

	def _on_presets_loaded(
		self, channel: ChannelDto, presets: list[PresetDto]
	) -> None:
		"""Пресеты получены — узнаём текущий выбор канала."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(CHANNEL_DEFAULT_PRESET, channel.id),
			self, partial(self._on_current_preset_loaded, channel, presets),
			self._show_error,
		)

	def _on_current_preset_loaded(
		self, channel: ChannelDto, presets: list[PresetDto], current_id: int | None
	) -> None:
		"""Текущий пресет получен — узнаём времена публикации."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, channel.id),
			self, partial(self._open_prefs_dialog, channel, presets, current_id),
			self._show_error,
		)

	def _open_prefs_dialog(
		self,
		channel: ChannelDto,
		presets: list[PresetDto],
		current_id: int | None,
		times: list[str],
	) -> None:
		"""Диалог настроек; сохранение — двумя настройками канала."""
		dialog = _ChannelPrefsDialog(
			channel.title, presets, current_id, times, self.window()
		)
		if not exec_dialog(dialog):
			return
		preset_id, times_value = dialog.preset_id(), dialog.times()
		# записи последовательно: успех сообщается только после обеих,
		# ошибка первой не даёт ложной плашки «сохранено»
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(
				CHANNEL_DEFAULT_PRESET, channel.id, preset_id
			),
			self,
			partial(self._save_publish_times, channel, times_value),
			self._show_error,
		)

	def _save_publish_times(
		self, channel: ChannelDto, times: list[str], _result: object = None
	) -> None:
		"""Вторая запись настроек канала (после успешной первой)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(
				PUBLISH_TIMES, channel.id, times
			),
			self, partial(self._on_prefs_saved, channel), self._show_error,
		)

	def _on_prefs_saved(self, channel: ChannelDto, _result: object = None) -> None:
		InfoBar.success(
			"Готово", f"Настройки канала «{channel.title}» сохранены.", parent=self
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
		if access.userbot_ok is None:
			userbot_text = "не удалось проверить (нет связи или userbot отключён)"
		else:
			userbot_text = "админ" if access.userbot_ok else "не админ"
		parts = [f"userbot: {userbot_text}"]
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
		if not exec_dialog(dialog):
			return
		bot_id = dialog.bot_id()
		if bot_id is None:
			return
		InfoBar.info("Проверка", "Проверяю права бота в канале…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.assign_bot(channel.id, bot_id),
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
		if not exec_dialog(dialog):
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
