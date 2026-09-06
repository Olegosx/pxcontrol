"""Страница «Каналы и группы»: подключение сообществ и их список."""

from __future__ import annotations

from collections.abc import Callable
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
from pxcontrol.engine.services.communities import CommunityAccess, CommunityDto, MemberDto
from pxcontrol.engine.services.settings import (
	COMMUNITY_DEFAULT_PRESET,
	COMMUNITY_ENABLED,
	PUBLISH_TIMES,
	SettingKey,
)
from pxcontrol.engine.services.video import PresetDto
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	account_caption,
	bind,
	bot_caption,
	clear_layout,
	community_kind_caption,
	confirm_delete,
	error_reporter,
	exec_dialog,
	noop,
	page_layout,
	parse_hhmm,
	role_caption,
	row_card,
	show_warning,
)

#: Фильтр списка по виду: подпись пункта → правило показа.
_KIND_FILTERS: list[tuple[str, Callable[[CommunityDto], bool]]] = [
	("Все", lambda community: True),
	("Каналы", lambda community: community.kind is CommunityKind.CHANNEL),
	("Группы", lambda community: community.kind is CommunityKind.GROUP),
]


class _ConnectDialog(MessageBoxBase):
	"""Диалог подключения: способ (userbot/бот), исполнитель, ссылка.

	Для userbot-способа аккаунт выбирается явно (ADR-0019): его права
	проверяются, и именно он привязывается к каналу как публикатор.
	"""

	_HINTS = {
		"userbot": (
			"Каналу аккаунт нужен администратором с правом публиковать;\n"
			"группе достаточно участника без ограничений (бот не нужен)."
		),
		"bot": (
			"В канал добавьте бота администратором с правом публиковать;\n"
			"в группу — участником (админство не требуется)."
		),
	}

	def __init__(self, bots: list[BotDto], accounts: list[TgAccountDto], parent: QWidget) -> None:
		"""``accounts`` — вошедшие userbot-аккаунты (кандидаты в админы)."""
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Подключить канал или группу", self))
		self._way = ComboBox(self)
		self._way.addItem("Через userbot (приоритетный способ)")
		self._way.addItem("Через бота")
		self._way.currentIndexChanged.connect(self._on_way_changed)
		self.viewLayout.addWidget(self._way)
		self._hint = BodyLabel("", self)
		self.viewLayout.addWidget(self._hint)
		self._account_combo: DtoComboBox[TgAccountDto] = DtoComboBox(self)
		self._account_combo.set_items(
			accounts, label=lambda acc: account_caption(acc.label, acc.phone)
		)
		self.viewLayout.addWidget(self._account_combo)
		self._combo: DtoComboBox[BotDto] = DtoComboBox(self)
		self._combo.set_items(bots, label=lambda bot: bot_caption(bot.label, bot.username))
		self.viewLayout.addWidget(self._combo)
		self._ref = LineEdit(self)
		self._ref.setPlaceholderText("@имя, ссылка t.me/… или ID -100…")
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
			return self._error.fail("Укажите @имя, ссылку или ID канала либо группы.")
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
		"""Введённая ссылка на сообщество."""
		return str(self._ref.text()).strip()


class _AssignBotDialog(MessageBoxBase):
	"""Выбор бота для назначения каналу."""

	def __init__(self, bots: list[BotDto], parent: QWidget) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Назначить бота", self))
		self.viewLayout.addWidget(
			BodyLabel(
				"Каналу бот нужен администратором с правом публиковать;\nгруппе — участником.",
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


class _MembersDialog(MessageBoxBase):
	"""Участники сообщества (ADR-0022): роли, умолчание, состав.

	Живой диалог: операции выполняются сразу (движком), список
	перечитывается после каждой; страница перегружает карточки
	по закрытии.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		community: CommunityDto,
		accounts: list[TgAccountDto],
		parent: QWidget,
	) -> None:
		"""``accounts`` — вошедшие userbot-аккаунты (кандидаты)."""
		super().__init__(parent)
		self._worker = worker
		self._community = community
		self._accounts = accounts
		self._show_error = error_reporter(self)
		self.viewLayout.addWidget(SubtitleLabel(f"Участники — {community.title}", self))
		self.viewLayout.addWidget(
			BodyLabel(
				"Публикует аккаунт по умолчанию; остальные — пул сообщества.\n"
				"Каналу нужен админ с правом публиковать, группе — участник.",
				self,
			)
		)
		self._rows = QVBoxLayout()
		self._rows.setSpacing(density.spacing().list_spacing)
		self.viewLayout.addLayout(self._rows)
		add_row = QHBoxLayout()
		self._add_combo: DtoComboBox[TgAccountDto] = DtoComboBox(self)
		add_row.addWidget(self._add_combo, stretch=1)
		add_button = PushButton("Добавить", self)
		add_button.clicked.connect(self._on_add)
		add_row.addWidget(add_button)
		self.viewLayout.addLayout(add_row)
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("Готово")
		self.cancelButton.hide()
		self.widget.setMinimumWidth(520)
		self._members: list[MemberDto] = []
		self._reload()

	def _reload(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_members(self._community.id),
			self,
			self._show_members,
			self._show_error,
		)

	def _show_members(self, members: list[MemberDto]) -> None:
		"""Перестраивает строки участников и список кандидатов."""
		self._members = members
		clear_layout(self._rows)
		if not members:
			self._rows.addWidget(BodyLabel("Участников нет — добавьте вошедший аккаунт.", self))
		for member in members:
			self._rows.addWidget(self._member_row(member))
		taken = {member.account_id for member in members}
		self._add_combo.set_items(
			[account for account in self._accounts if account.id not in taken],
			label=lambda acc: account_caption(acc.label, acc.phone),
			key=lambda acc: acc.id,
		)

	def _member_row(self, member: MemberDto) -> QWidget:
		"""Строка участника: имя, роль, умолчание, удаление."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.addWidget(BodyLabel(f"{member.label} — {role_caption(member.role)}", box))
		row.addStretch()
		if member.is_default:
			row.addWidget(CaptionLabel("публикатор по умолчанию", box))
		else:
			make_default = PushButton("Сделать публикатором", box)
			make_default.clicked.connect(bind(self._on_set_default, member))
			row.addWidget(make_default)
		remove = PushButton("Удалить", box)
		remove.clicked.connect(bind(self._on_remove, member))
		row.addWidget(remove)
		return box

	def _on_add(self) -> None:
		account = self._add_combo.selected()
		if account is None:
			self._error.fail("Нет свободных вошедших аккаунтов — войдите: Настройки → Аккаунты.")
			return
		self._error.succeed()
		InfoBar.info("Проверка", "Проверяю права аккаунта…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.communities.add_member(self._community.id, account.id),
			self,
			self._show_members,
			self._show_error,
		)

	def _on_set_default(self, member: MemberDto) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.communities.set_default(self._community.id, member.account_id),
			self,
			lambda _dto: self._reload(),
			self._show_error,
		)

	def _on_remove(self, member: MemberDto) -> None:
		warning = (
			" Это публикатор по умолчанию: публикация через userbot остановится до выбора нового."
			if member.is_default
			else ""
		)
		if not confirm_delete(
			self,
			f"Удалить «{member.label}» из участников?{warning}",
			accept_text="Удалить",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.communities.remove_member(self._community.id, member.account_id),
			self,
			self._show_members,
			self._show_error,
		)


class _CommunityPrefsDialog(MessageBoxBase):
	"""Настройки сообщества: пресет видео по умолчанию и времена публикации."""

	_TIMES_HINT = "Через запятую, первое — по умолчанию; пусто — без стандартных."

	def __init__(
		self,
		community_title: str,
		presets: list[PresetDto],
		current_id: int | None,
		times: list[str],
		parent: QWidget,
	) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel("Настройки", self))
		self.viewLayout.addWidget(BodyLabel(f"«{community_title}»", self))
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
		# ошибка валидации — единой красной подписью (как у всех диалогов),
		# подсказка о формате при этом остаётся на месте
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("Сохранить")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def validate(self) -> bool:  # noqa: N802 — API MessageBoxBase
		"""Не даёт сохранить времена в неверном формате (диалог открыт)."""
		try:
			self.times()
		except ValueError as exc:
			return self._error.fail(str(exc))
		return self._error.succeed()

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


class CommunitiesPage(ScrollArea):
	"""Каналы и группы: список с фильтром вида, подключение и привязки."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("communities")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._build()
		self._reload()

	def _build(self) -> None:
		"""Собирает шапку с фильтром вида, кнопкой и областью списка."""
		layout = page_layout(self)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Каналы и группы", self))
		header.addStretch()
		self._kind_filter = ComboBox(self)
		self._kind_filter.addItems([label for label, _pred in _KIND_FILTERS])
		self._kind_filter.setToolTip("Показывать все сообщества или только один вид")
		self._kind_filter.currentIndexChanged.connect(lambda _i: self._render())
		header.addWidget(self._kind_filter)
		connect_button = PrimaryPushButton(FluentIcon.ADD, "Подключить…", self)
		connect_button.clicked.connect(self._on_connect)
		header.addWidget(connect_button)
		layout.addLayout(header)
		self._communities: list[CommunityDto] = []
		self._list = QVBoxLayout()
		self._list.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._list)
		layout.addStretch()

	# --- список ---------------------------------------------------------------

	def _reload(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._show_communities,
			self._show_error,
		)

	def _show_communities(self, communities: list[CommunityDto]) -> None:
		self._communities = communities
		self._render()

	def _render(self) -> None:
		"""Перерисовывает список по текущему фильтру вида."""
		clear_layout(self._list)
		_label, predicate = _KIND_FILTERS[int(self._kind_filter.currentIndex())]
		shown = [community for community in self._communities if predicate(community)]
		if not shown:
			self._list.addWidget(self._empty_state(filtered=bool(self._communities)))
			return
		for community in shown:
			self._list.addWidget(self._community_row(community))

	def _empty_state(self, filtered: bool = False) -> QWidget:
		"""Пустое состояние: ничего не подключено или фильтр всё скрыл."""
		box = QWidget(self)
		layout = QVBoxLayout(box)
		layout.setContentsMargins(0, 48, 0, 0)
		if filtered:
			title = SubtitleLabel("Под фильтр ничего не попало", box)
			hint = BodyLabel("Выберите «Все» в фильтре справа вверху.", box)
		else:
			title = SubtitleLabel("Пока нет подключённых каналов и групп", box)
			hint = BodyLabel(
				"Нажмите «Подключить…»: через userbot или через бота.",
				box,
			)
		title.setAlignment(Qt.AlignmentFlag.AlignCenter)
		hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
		layout.addWidget(title)
		layout.addWidget(hint)
		return box

	def _community_row(self, community: CommunityDto) -> CardWidget:
		"""Карточка сообщества: вид, название, публикаторы, действия."""
		ways = []
		if community.default_account_label:
			role = f" ({role_caption(community.default_role)})" if community.default_role else ""
			ways.append(f"userbot {community.default_account_label}{role}")
		if community.bot_label:
			ways.append(f"бот {community.bot_label}")
		extras = max(community.members_count - (1 if community.default_account_id else 0), 0)
		members_note = f" · ещё участников: {extras}" if extras else ""
		subtitle = (
			f"{community_kind_caption(community)} · @{community.username or '—'} "
			f"· публикатор: {' + '.join(ways) or '—'}{members_note}"
		)
		buttons = QWidget(self)
		row = QHBoxLayout(buttons)
		row.setContentsMargins(0, 0, 0, 0)
		enabled_switch = SwitchButton(buttons)
		enabled_switch.setChecked(community.enabled)
		enabled_switch.setToolTip("Активно: участвует в публикации и опросе расписания")
		enabled_switch.checkedChanged.connect(partial(self._on_toggle_enabled, community))
		row.addWidget(enabled_switch)
		recheck = PushButton("Проверить доступы", buttons)
		recheck.clicked.connect(bind(self._recheck_community, community))
		row.addWidget(recheck)
		prefs_action = PushButton("Настройки…", buttons)
		prefs_action.setToolTip("Пресет видео по умолчанию и времена публикации")
		prefs_action.clicked.connect(bind(self._on_open_prefs, community))
		row.addWidget(prefs_action)
		members_action = PushButton("Участники…", buttons)
		members_action.setToolTip(
			"Пул userbot-аккаунтов сообщества: роли и публикатор по умолчанию"
		)
		members_action.clicked.connect(bind(self._on_open_members, community))
		row.addWidget(members_action)
		if community.bot_id is None:
			bot_action = PushButton("Назначить бота…", buttons)
			bot_action.clicked.connect(bind(self._on_assign_bot, community))
		else:
			bot_action = PushButton("Отвязать бота", buttons)
			bot_action.clicked.connect(bind(self._on_unassign_bot, community))
		row.addWidget(bot_action)
		return row_card(
			self,
			community.title,
			subtitle,
			trailing=buttons,
			on_delete=bind(self._delete_community, community),
		)

	# --- настройки канала (активность, пресет) -----------------------------------

	def _on_toggle_enabled(self, community: CommunityDto, checked: bool) -> None:
		"""Включает/выключает канал (публикация и расписание)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(COMMUNITY_ENABLED, community.id, checked),
			self,
			noop,
			self._on_toggle_failed,
		)

	def _on_toggle_failed(self, message: str) -> None:
		"""Ошибка записи флага: показать и вернуть карточкам правду из БД."""
		self._show_error(message)
		self._reload()

	def _on_open_prefs(self, community: CommunityDto) -> None:
		"""Открывает настройки канала (цепочка: пресеты → пресет → времена)."""
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_presets(),
			self,
			partial(self._on_presets_loaded, community),
			self._show_error,
		)

	def _on_presets_loaded(self, community: CommunityDto, presets: list[PresetDto]) -> None:
		"""Пресеты получены — узнаём текущий выбор канала."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(COMMUNITY_DEFAULT_PRESET, community.id),
			self,
			partial(self._on_current_preset_loaded, community, presets),
			self._show_error,
		)

	def _on_current_preset_loaded(
		self, community: CommunityDto, presets: list[PresetDto], current_id: int | None
	) -> None:
		"""Текущий пресет получен — узнаём времена публикации."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, community.id),
			self,
			partial(self._open_prefs_dialog, community, presets, current_id),
			self._show_error,
		)

	def _open_prefs_dialog(
		self,
		community: CommunityDto,
		presets: list[PresetDto],
		current_id: int | None,
		times: list[str],
	) -> None:
		"""Диалог настроек; сохранение — одной транзакцией движка."""
		dialog = _CommunityPrefsDialog(community.title, presets, current_id, times, self.window())
		if not exec_dialog(dialog):
			return
		# обе настройки — одна пользовательская операция: движок пишет их
		# одной транзакцией (set_for_many), успех сообщается по факту записи
		items: list[tuple[SettingKey[Any], Any]] = [
			(COMMUNITY_DEFAULT_PRESET, dialog.preset_id()),
			(PUBLISH_TIMES, dialog.times()),
		]
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for_many(community.id, items),
			self,
			partial(self._on_prefs_saved, community),
			self._show_error,
		)

	def _on_prefs_saved(self, community: CommunityDto, _result: object = None) -> None:
		InfoBar.success("Готово", f"Настройки «{community.title}» сохранены.", parent=self)

	# --- доступы и бот -----------------------------------------------------------

	def _recheck_community(self, community: CommunityDto) -> None:
		"""Перепроверяет оба способа администрирования канала."""
		InfoBar.info("Проверка", f"Проверяю доступы «{community.title}»…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.communities.recheck_community(community.id),
			self,
			self._on_rechecked,
			self._show_error,
		)

	def _on_rechecked(self, access: CommunityAccess) -> None:
		"""Показывает итог перепроверки и обновляет список."""
		if access.userbot_ok is None:
			userbot_text = "не удалось проверить (нет связи или аккаунт не подключён)"
		elif access.userbot_ok:
			userbot_text = f"публикатор — {access.community.default_account_label or '—'}"
		else:
			userbot_text = "не админ — привязка снята"
		parts = [f"userbot: {userbot_text}"]
		if access.bot_ok is not None:
			parts.append(f"бот: {'права на месте' if access.bot_ok else 'права потеряны'}")
		summary = " · ".join(parts)
		if access.userbot_ok and access.bot_ok is not False:
			InfoBar.success(access.community.title, summary, parent=self)
		else:
			show_warning(self, access.community.title, summary)
		self._reload()

	def _on_assign_bot(self, community: CommunityDto) -> None:
		"""Открывает выбор бота для назначения каналу."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_bots(),
			self,
			partial(self._open_assign_dialog, community),
			self._show_error,
		)

	def _open_assign_dialog(self, community: CommunityDto, bots: list[BotDto]) -> None:
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
		InfoBar.info("Проверка", "Проверяю права бота…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.communities.assign_bot(community.id, bot_id),
			self,
			self._on_publisher_changed,
			self._show_error,
		)

	def _on_unassign_bot(self, community: CommunityDto) -> None:
		if not confirm_delete(
			self,
			f"Отвязать бота от «{community.title}»?",
			accept_text="Отвязать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.communities.unassign_bot(community.id),
			self,
			self._on_publisher_changed,
			self._show_error,
		)

	def _on_publisher_changed(self, community: CommunityDto) -> None:
		InfoBar.success("Готово", community.title, parent=self)
		self._reload()

	# --- участники (ADR-0022) ----------------------------------------------------

	def _on_open_members(self, community: CommunityDto) -> None:
		"""Открывает диалог участников (нужны вошедшие аккаунты-кандидаты)."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_tg_accounts(),
			self,
			partial(self._open_members_dialog, community),
			self._show_error,
		)

	def _open_members_dialog(self, community: CommunityDto, accounts: list[TgAccountDto]) -> None:
		"""Живой диалог участников; по закрытии — перезагрузка карточек."""
		logged_in = [account for account in accounts if account.logged_in]
		dialog = _MembersDialog(self._worker, community, logged_in, self.window())
		exec_dialog(dialog)
		self._reload()

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
			coro = self._worker.engine.communities.add_community(bot_id, dialog.chat_ref())
		else:
			account_id = dialog.account_id()
			if account_id is None:  # недостижимо после validate(), страховка типа
				self._show_error("Войдите в userbot-аккаунт: Настройки → Аккаунты.")
				return
			coro = self._worker.engine.communities.add_community_via_userbot(
				account_id, dialog.chat_ref()
			)
		InfoBar.info("Проверка", "Проверяю сообщество и права…", parent=self)
		run_in_engine(self._worker, coro, self, self._on_connected, self._show_error)

	def _on_connected(self, community: CommunityDto) -> None:
		InfoBar.success("Канал подключён", community.title, parent=self)
		self._reload()

	def _delete_community(self, community: CommunityDto) -> None:
		if not confirm_delete(self, f"Удалить «{community.title}» из приложения?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.delete_community(community.id),
			self,
			self._reload,
			self._show_error,
		)
