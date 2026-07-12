"""Страница «Каналы»: подключение каналов и их список."""

from __future__ import annotations

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
	ScrollArea,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	bind,
	clear_layout,
	confirm_delete,
	row_card,
	show_error,
)


class _ConnectDialog(MessageBoxBase):
	"""Диалог подключения: выбор бота и ссылка на канал."""

	def __init__(self, bots: list[BotDto], parent: QWidget) -> None:
		super().__init__(parent)
		self._bots = bots
		self.viewLayout.addWidget(SubtitleLabel("Подключить канал", self))
		hint = BodyLabel(
			"Перед подключением добавьте бота администратором канала\n"
			"с правом публиковать сообщения.\n"
			"Приватный канал: укажите ID вида -100… (его покажет бот\n"
			"@getidsbot, если переслать ему любой пост канала).", self,
		)
		self.viewLayout.addWidget(hint)
		self._combo = ComboBox(self)
		for bot in bots:
			self._combo.addItem(f"{bot.label} (@{bot.username or '—'})")
		self.viewLayout.addWidget(self._combo)
		self._ref = LineEdit(self)
		self._ref.setPlaceholderText("@имя, ссылка t.me/… или ID канала")
		self._ref.setClearButtonEnabled(True)
		self.viewLayout.addWidget(self._ref)
		self.yesButton.setText("Подключить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(440)

	def bot_id(self) -> int:
		"""Идентификатор выбранного бота."""
		return self._bots[int(self._combo.currentIndex())].id

	def chat_ref(self) -> str:
		"""Введённая ссылка на канал."""
		return str(self._ref.text()).strip()


class ChannelsPage(ScrollArea):
	"""Список подключённых каналов; подключение через проверку прав бота."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("channels")
		self._worker = worker
		self._build()
		self._reload()

	def _build(self) -> None:
		"""Собирает шапку с кнопкой и область списка."""
		container = QWidget(self)
		layout = QVBoxLayout(container)
		layout.setContentsMargins(28, 24, 28, 24)
		layout.setSpacing(16)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Подключённые каналы", container))
		header.addStretch()
		connect_button = PrimaryPushButton(
			FluentIcon.ADD, "Подключить канал", container
		)
		connect_button.clicked.connect(self._on_connect)
		header.addWidget(connect_button)
		layout.addLayout(header)
		self._list = QVBoxLayout()
		self._list.setSpacing(8)
		layout.addLayout(self._list)
		layout.addStretch()
		self.setWidget(container)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()

	def _show_error(self, message: str) -> None:
		"""Показывает ошибку всплывающей плашкой."""
		show_error(self, message)

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
			"Добавьте бота администратором канала и нажмите «Подключить канал».",
			box,
		)
		hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title)
		layout.addWidget(hint)
		return box

	def _channel_row(self, channel: ChannelDto) -> CardWidget:
		"""Карточка канала: название, @имя, бот, удаление."""
		subtitle = f"@{channel.username or '—'} · бот: {channel.bot_label or '—'}"
		return row_card(
			self, channel.title, subtitle,
			on_delete=bind(self._delete_channel, channel),
		)

	# --- подключение -----------------------------------------------------------

	def _on_connect(self) -> None:
		"""Загружает список ботов и открывает диалог подключения."""
		run_in_engine(
			self._worker, self._worker.engine.accounts.list_bots(),
			self, self._open_connect_dialog, self._show_error,
		)

	def _open_connect_dialog(self, bots: list[BotDto]) -> None:
		if not bots:
			self._show_error("Сначала добавьте бота: Настройки → Аккаунты.")
			return
		dialog = _ConnectDialog(bots, self.window())
		if not dialog.exec():
			return
		if not dialog.chat_ref():
			self._show_error("Укажите @имя, ссылку или ID канала.")
			return
		InfoBar.info("Проверка", "Проверяю канал и права бота…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.channels.add_channel(
				dialog.bot_id(), dialog.chat_ref()
			),
			self, self._on_connected, self._show_error,
		)

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
