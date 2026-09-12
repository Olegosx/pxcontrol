"""Страница одного сообщества: сведения, управление, действия.

Открывается из подменю «Каналы и группы» (или кликом по карточке
дашборда). Здесь живут все действия с сообществом: активность,
проверка доступов, настройки, участники, привязка бота, удаление.
Будущие блоки (приборка, плановые работы) добавляются новыми секциями.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBoxBase,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	SwitchButton,
	TitleLabel,
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
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	WorkDialog,
	account_caption,
	bind,
	bot_caption,
	clear_layout,
	community_kind_caption,
	confirm_delete,
	error_reporter,
	exec_dialog,
	list_area,
	page_layout,
	parse_hhmm,
	role_caption,
	show_warning,
)
from pxcontrol.ui.pages.maintenance import open_maintenance


def community_route_key(community_id: int) -> str:
	"""Ключ маршрута страницы сообщества в навигации (objectName)."""
	return f"community_{community_id}"


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


class _MembersDialog(WorkDialog):
	"""Участники сообщества (ADR-0022): роли, умолчание, состав.

	Живой диалог: операции выполняются сразу (движком), список
	перечитывается после каждой; страница обновляется по закрытии.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		community: CommunityDto,
		accounts: list[TgAccountDto],
		parent: QWidget,
	) -> None:
		"""``accounts`` — вошедшие userbot-аккаунты (кандидаты)."""
		super().__init__(f"Участники — {community.title}", parent, size=(560, 520))
		self._worker = worker
		self._community = community
		self._accounts = accounts
		self._show_error = error_reporter(self)
		self.content.addWidget(
			BodyLabel(
				"Публикует аккаунт по умолчанию; остальные — пул сообщества.\n"
				"Каналу нужен админ с правом публиковать, группе — участник.",
				self,
			)
		)
		area, self._rows = list_area(self, spacing=density.spacing().list_spacing)
		self.content.addWidget(area, stretch=1)
		add_row = QHBoxLayout()
		self._add_combo: DtoComboBox[TgAccountDto] = DtoComboBox(self)
		add_row.addWidget(self._add_combo, stretch=1)
		add_button = PushButton("Добавить", self)
		add_button.clicked.connect(self._on_add)
		add_row.addWidget(add_button)
		self.content.addLayout(add_row)
		self._error = ErrorLabel(self)
		self.content.addWidget(self._error)
		self.add_close_button("Готово")
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
		self._rows.addStretch()
		taken = {member.account_id for member in members}
		self._add_combo.set_items(
			[account for account in self._accounts if account.id not in taken],
			label=lambda acc: account_caption(acc.display, acc.phone),
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


class CommunityPage(ScrollArea):
	"""Страница сообщества: шапка со сведениями и секции действий.

	Сигнал ``changed`` уходит после каждой операции, меняющей данные
	(активность, доступы, участники, бот, настройки, удаление) — главное
	окно по нему обновляет дашборд и подменю навигации.
	"""

	changed = Signal()

	def __init__(
		self, worker: EngineWorker, community: CommunityDto, parent: QWidget | None = None
	) -> None:
		super().__init__(parent)
		self.setObjectName(community_route_key(community.id))
		self._worker = worker
		self._community = community
		self._show_error = error_reporter(self)
		self._build()
		self._render()

	@property
	def community_id(self) -> int:
		"""Идентификатор сообщества этой страницы."""
		return self._community.id

	def update_community(self, community: CommunityDto) -> None:
		"""Обновляет страницу свежим снимком (синхронизация главного окна)."""
		self._community = community
		self._render()

	# --- сборка -----------------------------------------------------------------

	def _build(self) -> None:
		"""Каркас: шапка и секции; наполнение — в ``_render``."""
		layout = page_layout(self)
		self._title = TitleLabel("", self)
		layout.addWidget(self._title)
		self._subtitle = BodyLabel("", self)
		layout.addWidget(self._subtitle)
		layout.addSpacing(density.spacing().wide_spacing)
		layout.addWidget(SubtitleLabel("Публикация", self))
		self._publish_rows = QVBoxLayout()
		self._publish_rows.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._publish_rows)
		layout.addSpacing(density.spacing().wide_spacing)
		layout.addWidget(SubtitleLabel("Обслуживание", self))
		self._service_rows = QVBoxLayout()
		self._service_rows.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._service_rows)
		layout.addStretch()

	def _render(self) -> None:
		"""Перерисовывает шапку и секции по текущему снимку."""
		community = self._community
		self._title.setText(community.title)
		details = [community_kind_caption(community), f"@{community.username or '—'}"]
		if not community.enabled:
			details.append("выключено")
		self._subtitle.setText(" · ".join(details))
		clear_layout(self._publish_rows)
		self._publish_rows.addWidget(self._enabled_row())
		self._publish_rows.addWidget(self._userbot_row())
		self._publish_rows.addWidget(self._bot_row())
		self._publish_rows.addWidget(self._prefs_row())
		clear_layout(self._service_rows)
		self._service_rows.addWidget(self._recheck_row())
		self._service_rows.addWidget(self._maintenance_row())
		self._service_rows.addWidget(self._delete_row())

	def _action_row(self, text: str, actions: list[QWidget]) -> QWidget:
		"""Строка секции: описание слева, действия справа."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 2, 0, 2)
		label = BodyLabel(text, box)
		label.setWordWrap(True)
		row.addWidget(label, stretch=1)
		for widget in actions:
			row.addWidget(widget)
		return box

	def _enabled_row(self) -> QWidget:
		"""Активность: участие в публикации и опросе расписания."""
		switch = SwitchButton(self)
		switch.setChecked(self._community.enabled)
		switch.setToolTip("Активно: участвует в публикации и опросе расписания")
		switch.checkedChanged.connect(self._on_toggle_enabled)
		return self._action_row("Активность (публикация и расписание)", [switch])

	def _userbot_row(self) -> QWidget:
		"""Публикатор userbot: умолчание, роль, прочие участники."""
		community = self._community
		if community.default_account_label:
			role = f" ({role_caption(community.default_role)})" if community.default_role else ""
			text = f"Userbot-публикатор: {community.default_account_label}{role}"
		else:
			text = "Userbot-публикатор не выбран"
		extras = max(community.members_count - (1 if community.default_account_id else 0), 0)
		if extras:
			text += f" · ещё участников: {extras}"
		members = PushButton("Участники…", self)
		members.setToolTip("Пул userbot-аккаунтов сообщества: роли и публикатор по умолчанию")
		members.clicked.connect(self._on_open_members)
		return self._action_row(text, [members])

	def _bot_row(self) -> QWidget:
		"""Бот-публикатор: запасной путь (до 50 МБ, только «сейчас»)."""
		community = self._community
		if community.bot_id is None:
			action = PushButton("Назначить бота…", self)
			action.clicked.connect(self._on_assign_bot)
			return self._action_row("Бот-публикатор не назначен (запасной путь)", [action])
		action = PushButton("Отвязать бота", self)
		action.clicked.connect(self._on_unassign_bot)
		return self._action_row(f"Бот-публикатор: {community.bot_label}", [action])

	def _prefs_row(self) -> QWidget:
		"""Настройки публикации: пресет видео и времена."""
		action = PushButton("Настройки…", self)
		action.setToolTip("Пресет видео по умолчанию и времена публикации")
		action.clicked.connect(self._on_open_prefs)
		return self._action_row("Пресет видео по умолчанию и времена публикации", [action])

	def _recheck_row(self) -> QWidget:
		"""Перепроверка доступов обоих публикаторов."""
		action = PushButton("Проверить доступы", self)
		action.clicked.connect(self._recheck)
		return self._action_row(
			"Доступы публикаторов: роли участников, права бота, свойства сообщества",
			[action],
		)

	def _maintenance_row(self) -> QWidget:
		"""Уборка в сообществе: чистка служебных записей (ADR-0026).

		Без userbot-публикатора недоступна: списка участников и чужой
		истории Bot API не отдаёт — и это сказано прямо, а не показано
		пустым окном.
		"""
		action = PushButton("Служебные записи…", self)
		action.setToolTip(
			"«Такой-то вступил», «сообщение закреплено» — посмотреть, сколько их, и убрать"
		)
		if self._community.userbot_assigned:
			action.clicked.connect(self._on_open_maintenance)
			text = "Чистка служебных записей в ленте"
		else:
			action.setEnabled(False)
			text = "Чистка служебных записей — нужен userbot-публикатор (боту история недоступна)"
		return self._action_row(text, [action])

	def _delete_row(self) -> QWidget:
		"""Удаление сообщества из приложения (не из Telegram)."""
		action = PushButton(FluentIcon.DELETE, "Удалить…", self)
		action.clicked.connect(self._on_delete)
		return self._action_row(
			"Удалить из приложения (сам канал или группа в Telegram не трогается)",
			[action],
		)

	# --- операции ---------------------------------------------------------------

	def _refresh(self, *_args: object) -> None:
		"""Перечитывает снимок сообщества и сообщает об изменениях.

		Сообщество могло быть удалено (ошибка «не найден») — страница
		всё равно шлёт ``changed``: синхронизация главного окна снимет
		её вместе с пунктом подменю.
		"""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.get_community(self._community.id),
			self,
			self._on_refreshed,
			lambda _message: self.changed.emit(),
		)

	def _on_refreshed(self, community: CommunityDto) -> None:
		self._community = community
		self._render()
		self.changed.emit()

	def _on_toggle_enabled(self, checked: bool) -> None:
		"""Включает/выключает сообщество (публикация и расписание)."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set_for(COMMUNITY_ENABLED, self._community.id, checked),
			self,
			self._refresh,
			self._on_toggle_failed,
		)

	def _on_toggle_failed(self, message: str) -> None:
		"""Ошибка записи флага: показать и вернуть странице правду из БД."""
		self._show_error(message)
		self._refresh()

	def _on_open_maintenance(self) -> None:
		"""Открывает окно обслуживания сообщества."""
		open_maintenance(self._worker, self._community, self)

	def _recheck(self) -> None:
		"""Перепроверяет оба способа администрирования."""
		InfoBar.info("Проверка", f"Проверяю доступы «{self._community.title}»…", parent=self)
		run_in_engine(
			self._worker,
			self._worker.engine.communities.recheck_community(self._community.id),
			self,
			self._on_rechecked,
			self._show_error,
		)

	def _on_rechecked(self, access: CommunityAccess) -> None:
		"""Показывает итог перепроверки и обновляет страницу."""
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
		self._refresh()

	# --- настройки (пресет, времена) ---------------------------------------------

	def _on_open_prefs(self) -> None:
		"""Открывает настройки (цепочка: пресеты → пресет → времена)."""
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_presets(),
			self,
			self._on_presets_loaded,
			self._show_error,
		)

	def _on_presets_loaded(self, presets: list[PresetDto]) -> None:
		"""Пресеты получены — узнаём текущий выбор сообщества."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(COMMUNITY_DEFAULT_PRESET, self._community.id),
			self,
			partial(self._on_current_preset_loaded, presets),
			self._show_error,
		)

	def _on_current_preset_loaded(self, presets: list[PresetDto], current_id: int | None) -> None:
		"""Текущий пресет получен — узнаём времена публикации."""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(PUBLISH_TIMES, self._community.id),
			self,
			partial(self._open_prefs_dialog, presets, current_id),
			self._show_error,
		)

	def _open_prefs_dialog(
		self, presets: list[PresetDto], current_id: int | None, times: list[str]
	) -> None:
		"""Диалог настроек; сохранение — одной транзакцией движка."""
		dialog = _CommunityPrefsDialog(
			self._community.title, presets, current_id, times, self.window()
		)
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
			self._worker.engine.settings.set_for_many(self._community.id, items),
			self,
			self._on_prefs_saved,
			self._show_error,
		)

	def _on_prefs_saved(self, _result: object = None) -> None:
		InfoBar.success("Готово", f"Настройки «{self._community.title}» сохранены.", parent=self)

	# --- бот ---------------------------------------------------------------------

	def _on_assign_bot(self) -> None:
		"""Открывает выбор бота для назначения."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_bots(),
			self,
			self._open_assign_dialog,
			self._show_error,
		)

	def _open_assign_dialog(self, bots: list[BotDto]) -> None:
		"""Диалог выбора бота; после выбора — проверка его прав."""
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
			self._worker.engine.communities.assign_bot(self._community.id, bot_id),
			self,
			self._on_publisher_changed,
			self._show_error,
		)

	def _on_unassign_bot(self) -> None:
		if not confirm_delete(
			self,
			f"Отвязать бота от «{self._community.title}»?",
			accept_text="Отвязать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.communities.unassign_bot(self._community.id),
			self,
			self._on_publisher_changed,
			self._show_error,
		)

	def _on_publisher_changed(self, community: CommunityDto) -> None:
		InfoBar.success("Готово", community.title, parent=self)
		self._refresh()

	# --- участники (ADR-0022) ----------------------------------------------------

	def _on_open_members(self) -> None:
		"""Открывает диалог участников (нужны вошедшие аккаунты-кандидаты)."""
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_tg_accounts(),
			self,
			self._open_members_dialog,
			self._show_error,
		)

	def _open_members_dialog(self, accounts: list[TgAccountDto]) -> None:
		"""Живой диалог участников; по закрытии — обновление страницы."""
		logged_in = [account for account in accounts if account.logged_in]
		dialog = _MembersDialog(self._worker, self._community, logged_in, self.window())
		exec_dialog(dialog)
		self._refresh()

	# --- удаление ----------------------------------------------------------------

	def _on_delete(self) -> None:
		if not confirm_delete(self, f"Удалить «{self._community.title}» из приложения?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.delete_community(self._community.id),
			self,
			lambda *_a: self.changed.emit(),
			self._show_error,
		)
