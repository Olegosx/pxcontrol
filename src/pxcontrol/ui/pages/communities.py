"""Дашборд «Каналы и группы»: сводка, карточки сообществ, подключение.

Карточка — обзорная плитка (клик открывает страницу сообщества
в подменю); действия с сообществом живут на его странице
(:mod:`pxcontrol.ui.pages.community_page`). Каркас карточки — три зоны
(шапка с местом под логотип, ряд метрик, нижняя строка) — рассчитан
на постепенное добавление данных: подписчики, сообщения, активность
встанут в ряд метрик, аватар — в заготовленное место в шапке.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	ComboBox,
	FlowLayout,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBoxBase,
	PrimaryPushButton,
	ScrollArea,
	SimpleCardWidget,
	StrongBodyLabel,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import BotDto, TgAccountDto
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	account_caption,
	bot_caption,
	community_kind_caption,
	error_reporter,
	exec_dialog,
	page_layout,
)

#: Фильтр списка по виду: подпись пункта → правило показа.
_KIND_FILTERS: list[tuple[str, Callable[[CommunityDto], bool]]] = [
	("Все", lambda community: True),
	("Каналы", lambda community: community.kind is CommunityKind.CHANNEL),
	("Группы", lambda community: community.kind is CommunityKind.GROUP),
]

#: Палитра подложек логотипа-заглушки (в духе цветов аватаров Telegram);
#: цвет выбирается по id сообщества — стабилен между перерисовками.
_LOGO_COLORS = ("#e17076", "#eda86c", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae")

#: Размер квадрата логотипа в шапке карточки (пиксели).
_LOGO_SIZE = 44

#: Размер карточки сообщества в сетке дашборда (пиксели). Высота
#: фиксирована: карточки в сетке обязаны быть одинаковыми, содержимое
#: подгоняется (длинные тексты сокращаются многоточием).
_CARD_WIDTH = 330
_CARD_HEIGHT = 150

#: Внутренние отступы карточки и ширина текста шапки (пиксели).
_CARD_MARGIN = 16
_TITLE_WIDTH = _CARD_WIDTH - 2 * _CARD_MARGIN - _LOGO_SIZE - 12

#: Размер плитки сводки (пиксели): одинаковый у всех — ряд ровный,
#: ширины хватает самой длинной подписи («в очереди отправки»).
_STAT_TILE_HEIGHT = 72
_STAT_TILE_WIDTH = 160


def _elide(label: QLabel, text: str, width: int) -> None:
	"""Укладывает текст в одну строку заданной ширины (хвост — «…»).

	Обрезанный текст остаётся доступным во всплывающей подсказке.
	"""
	metrics = label.fontMetrics()
	label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, width))
	if metrics.horizontalAdvance(text) > width:
		label.setToolTip(text)


def _logo_placeholder(parent: QWidget, community: CommunityDto) -> QLabel:
	"""Логотип-заглушка: первая буква названия на цветной подложке.

	Когда появится загрузка аватаров из Telegram, картинка встанет
	в этот же квадрат — вёрстка шапки не изменится.
	"""
	label = QLabel(community.title[:1].upper() or "?", parent)
	label.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
	label.setAlignment(Qt.AlignmentFlag.AlignCenter)
	color = _LOGO_COLORS[community.id % len(_LOGO_COLORS)]
	label.setStyleSheet(
		f"background: {color}; color: white; border-radius: {_LOGO_SIZE // 2}px;"
		"font-size: 18px; font-weight: 600;"
	)
	return label


class CommunityCard(CardWidget):
	"""Плитка сообщества: шапка, ряд метрик, нижняя строка.

	Метрики задаются данными (:meth:`_metric`) — новые показатели
	(подписчики, сообщения) добавляются в ряд без перестройки каркаса.
	Нижняя строка зарезервирована под индикаторы и быстрые действия.
	"""

	def __init__(self, community: CommunityDto, queued: int, parent: QWidget) -> None:
		super().__init__(parent)
		# высота фиксирована наравне с шириной: сетка из одинаковых плиток
		self.setFixedSize(_CARD_WIDTH, _CARD_HEIGHT)
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(_CARD_MARGIN, 12, _CARD_MARGIN, 12)
		layout.setSpacing(10)
		layout.addWidget(self._header(community))
		layout.addWidget(self._metrics(community, queued))
		layout.addStretch()
		layout.addWidget(self._bottom(community))

	def _header(self, community: CommunityDto) -> QWidget:
		"""Шапка: логотип (пока заглушка), название, вид и @имя.

		Название и детали — по одной строке с многоточием: высота
		шапки одинакова у всех карточек.
		"""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(12)
		row.addWidget(_logo_placeholder(box, community))
		column = QVBoxLayout()
		column.setSpacing(2)
		title = StrongBodyLabel(box)
		_elide(title, community.title, _TITLE_WIDTH)
		column.addWidget(title)
		details = CaptionLabel(box)
		_elide(
			details,
			f"{community_kind_caption(community)} · @{community.username or '—'}",
			_TITLE_WIDTH,
		)
		column.addWidget(details)
		row.addLayout(column, stretch=1)
		return box

	def _metrics(self, community: CommunityDto, queued: int) -> QWidget:
		"""Ряд метрик: три равные колонки «значение + подпись».

		Колонки фиксированы по числу и ширине — ряд не переносится
		и не меняет высоту карточки; новые метрики добавляются
		расширением этого ряда (и при нехватке места — высоты карточки
		одной константой).
		"""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(12)
		publisher = (
			community.default_account_label
			or (f"бот {community.bot_label}" if community.bot_label else None)
			or "—"
		)
		metrics = [
			(str(community.members_count), "участников"),
			(str(queued), "в очереди"),
			(publisher, "публикатор"),
		]
		column_width = (_CARD_WIDTH - 2 * _CARD_MARGIN - 12 * (len(metrics) - 1)) // len(metrics)
		for value, caption in metrics:
			row.addWidget(self._metric(box, value, caption, column_width), stretch=1)
		return box

	@staticmethod
	def _metric(parent: QWidget, value: str, caption: str, width: int) -> QWidget:
		"""Мини-показатель: значение сверху, подпись снизу (одной строкой)."""
		box = QWidget(parent)
		column = QVBoxLayout(box)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(0)
		value_label = StrongBodyLabel(box)
		_elide(value_label, value, width)
		column.addWidget(value_label)
		caption_label = CaptionLabel(box)
		_elide(caption_label, caption, width)
		column.addWidget(caption_label)
		return box

	def _bottom(self, community: CommunityDto) -> QWidget:
		"""Нижняя строка: статус; зарезервирована под быстрые действия.

		Есть у каждой карточки (пусть и пустая): прижимает содержимое
		к верху и держит место под будущие кнопки быстрых действий.
		"""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		status = CaptionLabel("" if community.enabled else "выключено — не публикуется", box)
		row.addWidget(status)
		row.addStretch()
		return box


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
			accounts, label=lambda acc: account_caption(acc.display, acc.phone)
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


class CommunitiesPage(ScrollArea):
	"""Дашборд сообществ: сводка, сетка карточек, фильтр, подключение.

	Сигналы для главного окна: ``communities_changed`` — свежий список
	(синхронизация подменю навигации), ``open_community`` — клик
	по карточке (переход на страницу сообщества).
	"""

	communities_changed = Signal(list)
	open_community = Signal(int)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("communities")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._communities: list[CommunityDto] = []
		self._queued: dict[int, int] = {}
		self._build()

	def _build(self) -> None:
		"""Собирает шапку, блок сводки и область сетки карточек."""
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
		self._stats_box = QWidget(self)
		# перенос вместо горизонтальной прокрутки: при узком окне (или
		# развёрнутой навигации) плитки уходят на вторую строку
		self._stats = FlowLayout(self._stats_box, needAni=False)
		self._stats.setContentsMargins(0, 0, 0, 0)
		self._stats.setHorizontalSpacing(12)
		self._stats.setVerticalSpacing(12)
		layout.addWidget(self._stats_box)
		self._cards_box = QWidget(self)
		self._cards = FlowLayout(self._cards_box, needAni=False)
		self._cards.setContentsMargins(0, 0, 0, 0)
		# сетке плиток нужен воздух: межстрочный интервал списков мал
		self._cards.setHorizontalSpacing(12)
		self._cards.setVerticalSpacing(12)
		layout.addWidget(self._cards_box)
		layout.addStretch()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет данные при каждом показе: страница возвратная
		(с неё уходят на страницы сообществ и приходят обратно)."""
		super().showEvent(event)
		self.reload()

	# --- данные -----------------------------------------------------------------

	def reload(self) -> None:
		"""Перечитывает сообщества и очередь отправки из движка."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._on_communities_loaded,
			self._show_error,
		)

	def _on_communities_loaded(self, communities: list[CommunityDto]) -> None:
		"""Список получен — вторым шагом состояние очереди отправки."""
		self._communities = communities
		run_in_engine(
			self._worker,
			self._worker.engine.publish_queue.state(),
			self,
			self._on_queue_loaded,
			self._show_error,
		)

	def _on_queue_loaded(self, items: list[QueueItemDto]) -> None:
		"""Очередь получена — карточкам нужны счётчики неотправленных.

		Ошибочные элементы тоже считаются: они не отправлены и ждут
		повтора — «завершёнными» их считает только жизненный цикл
		панели очереди, а не сводка дашборда.
		"""
		gone = (QueueItemStatus.DONE, QueueItemStatus.CANCELLED)
		queued: dict[int, int] = {}
		for item in items:
			if item.status not in gone:
				queued[item.community_id] = queued.get(item.community_id, 0) + 1
		self._queued = queued
		self._render()
		self.communities_changed.emit(list(self._communities))

	# --- отрисовка ---------------------------------------------------------------

	def _render(self) -> None:
		"""Перерисовывает сводку и сетку карточек по текущему фильтру."""
		self._render_stats()
		self._cards.takeAllWidgets()
		_label, predicate = _KIND_FILTERS[int(self._kind_filter.currentIndex())]
		shown = [community for community in self._communities if predicate(community)]
		if not shown:
			self._cards.addWidget(self._empty_state(filtered=bool(self._communities)))
			return
		for community in shown:
			card = CommunityCard(community, self._queued.get(community.id, 0), self._cards_box)
			card.clicked.connect(partial(self.open_community.emit, community.id))
			self._cards.addWidget(card)

	def _render_stats(self) -> None:
		"""Блок сводки: счётчики по всем сообществам (фильтр не влияет)."""
		self._stats.takeAllWidgets()
		communities = self._communities
		channels = sum(1 for c in communities if c.kind is CommunityKind.CHANNEL)
		without_publisher = sum(
			1 for c in communities if c.default_account_id is None and c.bot_id is None
		)
		tiles = [
			(str(len(communities)), "всего"),
			(str(channels), "каналов"),
			(str(len(communities) - channels), "групп"),
			(str(sum(1 for c in communities if c.enabled)), "активных"),
			(str(without_publisher), "без публикатора"),
			(str(sum(self._queued.values())), "в очереди отправки"),
		]
		for value, caption in tiles:
			self._stats.addWidget(self._stat_tile(value, caption))

	def _stat_tile(self, value: str, caption: str) -> QWidget:
		"""Плитка сводки: карточка с крупным значением и подписью.

		``SimpleCardWidget`` — без реакции на наведение: плитка
		информационная, кликать по ней нечего.
		"""
		# qfluentwidgets не типизирован: без аннотации mypy видит Any
		tile: QWidget = SimpleCardWidget(self._stats_box)
		tile.setFixedSize(_STAT_TILE_WIDTH, _STAT_TILE_HEIGHT)
		column = QVBoxLayout(tile)
		column.setContentsMargins(16, 10, 16, 10)
		column.setSpacing(0)
		column.addWidget(SubtitleLabel(value, tile))
		column.addWidget(CaptionLabel(caption, tile))
		return tile

	def _empty_state(self, filtered: bool = False) -> QWidget:
		"""Пустое состояние: ничего не подключено или фильтр всё скрыл."""
		box = QWidget(self._cards_box)
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
		InfoBar.success("Подключено", community.title, parent=self)
		self.reload()
