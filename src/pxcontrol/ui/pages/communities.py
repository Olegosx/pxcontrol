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
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
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
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.engine.telegram.types import CommunityKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	ErrorLabel,
	account_caption,
	bot_caption,
	community_kind_caption,
	elide_text,
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
_CARD_WIDTH = 360
_CARD_HEIGHT = 150

#: Внутренние отступы карточки (пиксели).
_CARD_MARGIN = 16

#: Цвет значения метрики «ошибки», когда они есть (светлая/тёмная тема).
_ERROR_LIGHT = QColor(196, 43, 28)
_ERROR_DARK = QColor(255, 153, 164)

#: Предел ширины значения метрики (пиксели): подписи короткие, а вот
#: значение (число подписчиков) может разрастись — упирается в предел
#: и сокращается многоточием под свою настоящую ширину.
_METRIC_VALUE_WIDTH = 90

#: Размер плитки сводки (пиксели): одинаковый у всех — ряд ровный,
#: ширины хватает самой длинной подписи («в очереди отправки»).
_STAT_TILE_HEIGHT = 72
_STAT_TILE_WIDTH = 160


def _logo_placeholder(parent: QWidget, community: CommunityDto) -> QLabel:
	"""Логотип-заглушка: первая буква названия на цветной подложке."""
	label = QLabel(community.title[:1].upper() or "?", parent)
	label.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
	label.setAlignment(Qt.AlignmentFlag.AlignCenter)
	color = _LOGO_COLORS[community.id % len(_LOGO_COLORS)]
	label.setStyleSheet(
		f"background: {color}; color: white; border-radius: {_LOGO_SIZE // 2}px;"
		"font-size: 18px; font-weight: 600;"
	)
	return label


def _round_pixmap(path: str, size: int) -> QPixmap | None:
	"""Круглая миниатюра из файла (None — файл не читается)."""
	source = QPixmap(path)
	if source.isNull():
		return None
	scaled = source.scaled(
		size,
		size,
		Qt.AspectRatioMode.KeepAspectRatioByExpanding,
		Qt.TransformationMode.SmoothTransformation,
	)
	rounded = QPixmap(size, size)
	rounded.fill(Qt.GlobalColor.transparent)
	painter = QPainter(rounded)
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	clip = QPainterPath()
	clip.addEllipse(0, 0, size, size)
	painter.setClipPath(clip)
	painter.drawPixmap(0, 0, scaled)
	painter.end()
	return rounded


def _logo_widget(parent: QWidget, community: CommunityDto, avatar_path: str | None) -> QLabel:
	"""Логотип сообщества: аватар из кэша, без него — буква-заглушка."""
	if avatar_path:
		pixmap = _round_pixmap(avatar_path, _LOGO_SIZE)
		if pixmap is not None:
			label = QLabel(parent)
			label.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
			label.setPixmap(pixmap)
			return label
	return _logo_placeholder(parent, community)


class CommunityCard(CardWidget):
	"""Плитка сообщества: шапка с аватаром, ряд метрик, нижняя строка.

	Метрики задаются данными (:meth:`_metric`) — новые показатели
	добавляются в ряд без перестройки каркаса. Нижняя строка
	зарезервирована под индикаторы и быстрые действия.
	"""

	def __init__(
		self,
		community: CommunityDto,
		queue_counts: tuple[int, int, int],
		stats: CommunityStatsDto | None,
		parent: QWidget,
	) -> None:
		"""``queue_counts`` — (запланировано, из них ждут слота, ошибки);
		``stats`` — снимок кэша статистики (None — ещё не собирался)."""
		super().__init__(parent)
		# высота фиксирована наравне с шириной: сетка из одинаковых плиток
		self.setFixedSize(_CARD_WIDTH, _CARD_HEIGHT)
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(_CARD_MARGIN, 12, _CARD_MARGIN, 12)
		layout.setSpacing(10)
		layout.addWidget(self._header(community, stats))
		layout.addWidget(self._metrics(community, queue_counts, stats))
		layout.addStretch()
		layout.addWidget(self._bottom(community))

	def _header(self, community: CommunityDto, stats: CommunityStatsDto | None) -> QWidget:
		"""Шапка: аватар (или буква-заглушка), название, вид и @имя.

		Название и детали — по одной строке с многоточием: высота
		шапки одинакова у всех карточек.
		"""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(12)
		avatar_path = stats.avatar_path if stats is not None else None
		row.addWidget(_logo_widget(box, community, avatar_path))
		column = QVBoxLayout()
		column.setSpacing(2)
		title = StrongBodyLabel(box)
		# «занимай, что дадут»: иначе длинное название требовало бы свою
		# ширину и распирало карточку, а сокращать было бы нечего
		title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(title, community.title)
		column.addWidget(title)
		details = CaptionLabel(box)
		details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		elide_text(details, f"{community_kind_caption(community)} · @{community.username or '—'}")
		column.addWidget(details)
		row.addLayout(column, stretch=1)
		return box

	def _metrics(
		self,
		community: CommunityDto,
		queue_counts: tuple[int, int, int],
		stats: CommunityStatsDto | None,
	) -> QWidget:
		"""Ряд метрик: подписчики · запланировано (ждут слота) · отложено · ошибки.

		Числа короткие — колонки размещаются по естественной ширине
		подписей, ряд не переносится и высоту карточки не меняет.
		«—» — данные из Telegram ещё не приезжали (кэш пуст).
		"""
		planned, waiting, errors = queue_counts
		participants = stats.participants if stats is not None else None
		scheduled = stats.scheduled_count if stats is not None else None
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(16)
		metrics = [
			(
				str(participants) if participants is not None else "—",
				"участники" if community.kind is CommunityKind.GROUP else "подписчики",
				"Из Telegram, обновляется фоном",
				False,
			),
			(
				f"{planned} ({waiting})" if waiting else str(planned),
				"запланировано",
				"Очередь приложения: всего к отправке"
				+ (f", из них {waiting} ждут слота отложек" if waiting else ""),
				False,
			),
			(
				str(scheduled) if scheduled is not None else "—",
				"отложено",
				"Отложенные записи на сервере Telegram",
				False,
			),
			(
				str(errors),
				"ошибки",
				"Элементы очереди с ошибкой — ждут повтора",
				errors > 0,
			),
		]
		for value, caption, tooltip, alert in metrics:
			row.addWidget(self._metric(box, value, caption, tooltip, alert))
		row.addStretch()
		return box

	@staticmethod
	def _metric(parent: QWidget, value: str, caption: str, tooltip: str, alert: bool) -> QWidget:
		"""Мини-показатель: значение сверху, подпись снизу.

		``alert`` подсвечивает значение цветом ошибки (обе темы).
		"""
		box = QWidget(parent)
		box.setToolTip(tooltip)
		column = QVBoxLayout(box)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(0)
		value_label = StrongBodyLabel(box)
		value_label.setMaximumWidth(_METRIC_VALUE_WIDTH)
		elide_text(value_label, value)
		if alert:
			value_label.setTextColor(_ERROR_LIGHT, _ERROR_DARK)
		column.addWidget(value_label)
		column.addWidget(CaptionLabel(caption, box))
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
		# счётчики очереди сообщества: (запланировано, ждут слота, ошибки)
		self._queue_counts: dict[int, tuple[int, int, int]] = {}
		self._stats_cache: dict[int, CommunityStatsDto] = {}
		self._stats_refreshing = False
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
		"""Очередь получена — считаем по сообществам план, слоты и ошибки.

		«Запланировано» — всё неотправленное без ошибок (в том числе
		ждущие слота отложек — они выделяются вторым числом); ошибки —
		отдельно: они ждут повтора и требуют внимания.
		"""
		counts: dict[int, tuple[int, int, int]] = {}
		for item in items:
			if item.status in (QueueItemStatus.DONE, QueueItemStatus.CANCELLED):
				continue
			planned, waiting, errors = counts.get(item.community_id, (0, 0, 0))
			if item.status is QueueItemStatus.ERROR:
				errors += 1
			else:
				planned += 1
				if item.status is QueueItemStatus.WAITING:
					waiting += 1
			counts[item.community_id] = (planned, waiting, errors)
		self._queue_counts = counts
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._on_stats_loaded,
			self._show_error,
		)

	def _on_stats_loaded(self, stats: list[CommunityStatsDto]) -> None:
		"""Кэш статистики получен — рисуем и запускаем фоновое обновление."""
		self._stats_cache = {item.community_id: item for item in stats}
		self._render()
		self.communities_changed.emit(list(self._communities))
		self._refresh_stats_in_background()

	def _refresh_stats_in_background(self) -> None:
		"""Фоновое обновление кэша статистики (не чаще одного за раз).

		Ошибки не показываются: обновление вспомогательное, каждый сбой
		уже залогирован движком — всплывашка при каждом открытии
		страницы без сети только раздражала бы.
		"""
		if self._stats_refreshing:
			return
		self._stats_refreshing = True
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.refresh_stale(),
			self,
			self._on_stats_refreshed,
			lambda _message: setattr(self, "_stats_refreshing", False),
		)

	def _on_stats_refreshed(self, changed: bool) -> None:
		"""Кэш обновился — перечитываем снимок (без нового обновления)."""
		self._stats_refreshing = False
		if not changed:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.community_stats.snapshot(),
			self,
			self._on_fresh_stats,
			self._show_error,
		)

	def _on_fresh_stats(self, stats: list[CommunityStatsDto]) -> None:
		"""Свежий снимок после фонового обновления — только перерисовка."""
		self._stats_cache = {item.community_id: item for item in stats}
		self._render()

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
			card = CommunityCard(
				community,
				self._queue_counts.get(community.id, (0, 0, 0)),
				self._stats_cache.get(community.id),
				self._cards_box,
			)
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
			(
				str(sum(planned + errors for planned, _w, errors in self._queue_counts.values())),
				"в очереди отправки",
			),
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
