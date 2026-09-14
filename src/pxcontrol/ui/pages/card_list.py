"""Список карточек с точечным обновлением и раскрытием в форму правки.

Общий слой показа для любого списка, чьи элементы живут своей жизнью
вне интерфейса и приходят снимками: очередь заданий движка (ADR-0025 —
через :class:`~pxcontrol.ui.pages.queue_panel.QueuePanel`) и отложенные
записи с сервера Telegram (ADR-0010). Список не знает, откуда снимок
и что в нём за элементы: всё предметное — подпись, отпечаток, кнопки,
начало шапки, прогресс, форма правки — крючки владельца.

Обновление точечное (:func:`plan_cards`): меняется только то, что
изменилось. Прежняя панель пересобирала весь список, стоило измениться
чему угодно в любом элементе, — это мигало на сотне карточек и делало
невозможной правку прямо в карточке: форма с набранным текстом умирала
от того, что у соседнего поста сменился статус.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import ProgressBar

from pxcontrol.ui.pages.common import CollapsibleCard, clear_layout, error_reporter

#: Ключ элемента списка (по умолчанию — ``item.id``).
KeyFn = Callable[[Any], Hashable]

#: Отпечаток элемента: по нему решается, обновлять ли карточку.
SignatureFn = Callable[[Any], tuple[Any, ...]]

#: Наполнение тела раскрытой карточки: элемент, компоновка тела,
#: «свернуть карточку» (форма зовёт после сохранения и по отмене).
FillBodyFn = Callable[[Any, QVBoxLayout, Callable[[], None]], None]

#: Виджеты для шапки карточки: элемент и родитель → список виджетов.
WidgetsFn = Callable[[Any, QWidget], list[QWidget]]

#: Прогресс элемента: доля 0.0..1.0 и подпись к ней; None — прогресса нет.
ProgressFn = Callable[[Any], tuple[float, str] | None]

#: Ширина полосы прогресса в шапке карточки (пиксели).
_BAR_WIDTH = 160


def default_key(item: Any) -> Hashable:
	"""Ключ по умолчанию — ``id`` элемента."""
	key: Hashable = item.id
	return key


@dataclass(frozen=True)
class CardPlan:
	"""Что сделать с карточками, чтобы список совпал со снимком.

	Attributes:
		removed: ключи карточек, которых в снимке больше нет.
		added: ключи новых карточек (в порядке показа).
		changed: ключи карточек, чьё содержимое изменилось.
		order: итоговый порядок ключей.
	"""

	removed: list[Hashable]
	added: list[Hashable]
	changed: list[Hashable]
	order: list[Hashable]


def plan_cards(
	shown: Sequence[Any],
	known: Mapping[Hashable, tuple[Any, ...]],
	*,
	key: KeyFn = default_key,
	signature: SignatureFn,
) -> CardPlan:
	"""План точечного обновления карточек по новому снимку.

	Args:
		shown: элементы снимка в порядке показа.
		known: отпечатки уже показанных карточек по ключам.
		key: ключ элемента (у очереди — id; у отложенной записи —
			пара «сообщество, id в его очереди отложенных»).
		signature: отпечаток элемента — всё, что карточка показывает
			и на что вешает действия.
	"""
	order = [key(item) for item in shown]
	target = set(order)
	return CardPlan(
		removed=[item_key for item_key in known if item_key not in target],
		added=[key(item) for item in shown if key(item) not in known],
		changed=[
			key(item)
			for item in shown
			if key(item) in known and signature(item) != known[key(item)]
		],
		order=order,
	)


class ListCard:
	"""Карточка элемента списка: шапка с действиями и тело для правки.

	Живёт столько же, сколько элемент в списке, и обновляется точечно:
	заголовок, сводка и кнопки меняются на месте, а тело (форма правки)
	при этом не пересоздаётся.
	"""

	def __init__(self, owner: CardList, item: Any) -> None:
		self._owner = owner
		self.key = owner.key(item)
		self._actions = QWidget(owner.page)
		self._actions_box = QHBoxLayout(self._actions)
		self._actions_box.setContentsMargins(0, 0, 0, 0)
		self._leading = QWidget(owner.page)
		self._leading_box = QHBoxLayout(self._leading)
		self._leading_box.setContentsMargins(0, 0, 0, 0)
		self._leading_box.setSpacing(6)
		self._item = item
		self._bar: ProgressBar | None = None
		self._filled = False  # тело уже наполнено формой правки
		self._compact = owner.compact
		self.widget = CollapsibleCard(
			item.title,
			owner.page,
			trailing=self._actions,
			leading=self._leading,
			keep_summary=True,
			stacked=self._compact,
		)
		self.widget.expanded_changed.connect(self._on_expanded)
		self.update(item)

	def update(self, item: Any) -> None:
		"""Приводит карточку к новому снимку элемента."""
		self._item = item
		self.refresh_leading()
		self.widget.set_title(item.title)
		self.widget.set_summary(self._owner.subtitle(item), alert=self._owner.alert_of(item))
		editable = self._owner.can_edit(item)
		self.widget.set_expandable(editable)
		if not editable:
			# элемент больше не правится: форма в теле уже не про него
			self._reset_body()
		self._fill_actions(item)
		self.set_progress(self._owner.progress_of(item))

	def refresh_leading(self) -> None:
		"""Перерисовывает начало шапки (логотип сообщества, метка слота).

		Отдельно от :meth:`update`: аватары приезжают из кэша статистики
		позже списка, и к этому моменту снимок элемента не менялся —
		обновлять карточку целиком было бы не с чего.
		"""
		clear_layout(self._leading_box)
		for widget in self._owner.leading_widgets(self._item, self._leading):
			self._leading_box.addWidget(widget)

	def set_progress(self, progress: tuple[float, str] | None) -> None:
		"""Двигает полосу прогресса (без пересборки карточки).

		В компактном режиме полоса — под названием (рисует карточка),
		иначе — в шапке; None прячет её.
		"""
		if self._compact:
			if progress is None:
				self.widget.set_progress(None)
			else:
				self.widget.set_progress(*progress)
			return
		if self._bar is not None and progress is not None:
			self._bar.setValue(int(progress[0] * 100))

	def editing(self) -> bool:
		"""Открыта ли в карточке форма правки."""
		return self._filled and self.widget.expanded()

	def collapse(self) -> None:
		"""Закрывает форму: сворачивает карточку и забывает её содержимое.

		Зовётся самой формой — после сохранения (данные устарели)
		и по «Отмене» (человек отказался от правки). Ручное сворачивание
		кликом по шапке тело не трогает: значения полей переживают его,
		как и у карточек параметров на «Видео».
		"""
		self.widget.set_expanded(False)
		self._reset_body()

	def _reset_body(self) -> None:
		"""Забывает форму: следующее раскрытие прочитает свежие данные."""
		if not self._filled:
			return
		clear_layout(self.widget.body)
		self._filled = False

	def _on_expanded(self, expanded: bool) -> None:
		"""Первое раскрытие наполняет тело формой правки (лениво).

		Форма тянет данные из движка — делать это для всех карточек
		списка заранее значило бы десятки лишних запросов на каждый опрос.
		"""
		if not expanded or self._filled:
			return
		self._filled = True
		self._owner.fill_body(self._item, self.widget.body, self.collapse)

	def _fill_actions(self, item: Any) -> None:
		"""Пересобирает правый край шапки: полоса прогресса и кнопки."""
		clear_layout(self._actions_box)
		self._bar = None
		progress = self._owner.progress_of(item)
		if progress is not None and not self._compact:
			# в компактном режиме полоса под названием — карточка рисует сама
			bar = ProgressBar(self._actions)
			bar.setRange(0, 100)
			bar.setValue(int(progress[0] * 100))
			bar.setFixedWidth(_BAR_WIDTH)
			self._actions_box.addWidget(bar)
			self._bar = bar
		for widget in self._owner.action_widgets(item, self._actions):
			self._actions_box.addWidget(widget)


class CardList:
	"""Список карточек: точечное обновление по снимку, раскрытие, шапки.

	Контракт элемента: ``title`` (заголовок карточки) и ключ
	(``key``, по умолчанию ``id``). Всё остальное владелец задаёт
	крючками — список не читает у элемента ничего сверх этого.
	"""

	def __init__(
		self,
		page: QWidget,
		box: QVBoxLayout,
		*,
		subtitle: Callable[[Any], str],
		signature: SignatureFn,
		key: KeyFn = default_key,
		leading: WidgetsFn | None = None,
		actions: WidgetsFn | None = None,
		progress: ProgressFn | None = None,
		alert: Callable[[Any], bool] | None = None,
		editable: Callable[[Any], bool] | None = None,
		fill_body: FillBodyFn | None = None,
		compact: bool = False,
		lost_edit_text: str = "Запись покинула список — незаконченная правка не сохранена.",
	) -> None:
		"""Args:
		page: виджет-владелец (родитель карточек и плашек ошибок).
		box: компоновка, в которую список складывает карточки.
		subtitle: подпись карточки для элемента.
		signature: отпечаток элемента — по нему решается обновление.
		key: ключ элемента (устойчив между снимками).
		leading: виджеты в начале шапки (логотип сообщества, метка слота).
		actions: кнопки правого края шапки под текущее состояние элемента.
		progress: доля и подпись прогресса элемента; None — прогресса нет.
		alert: подсветить сводку как тревожную (компактный режим:
			ошибка красит подпись и рамку).
		editable: можно ли раскрыть карточку элемента (правка). Без него
			карточки не раскрываются вовсе.
		fill_body: наполняет тело раскрытой карточки формой правки —
			получает элемент, компоновку тела и «свернуть карточку».
			Зовётся один раз, при первом раскрытии.
		compact: карточки по макету страницы сообщества — подпись под
			названием, кнопки-обводки 28, полоса прогресса под названием.
		lost_edit_text: что сказать человеку, когда элемент с открытой
			формой исчез из списка: набранное пропадает вместе с ней.
		"""
		#: страница-владелец: родитель карточек и плашек (читают карточки).
		self.page = page
		#: компактные карточки (читают карточки при сборке).
		self.compact = compact
		#: подпись карточки элемента (читают карточки).
		self.subtitle = subtitle
		#: ключ элемента (читают карточки).
		self.key = key
		self._box = box
		self._signature = signature
		self._leading = leading
		self._actions = actions
		self._progress = progress
		self._alert = alert
		self._editable = editable
		self._fill_body = fill_body
		self._lost_edit_text = lost_edit_text
		self._show_error = error_reporter(page)
		self._cards: dict[Hashable, ListCard] = {}
		self._signatures: dict[Hashable, tuple[Any, ...]] = {}

	# --- крючки владельца (читают карточки) ------------------------------------

	def can_edit(self, item: Any) -> bool:
		"""Раскрывается ли карточка этого элемента (правка на месте)."""
		return self._fill_body is not None and self._editable is not None and self._editable(item)

	def fill_body(self, item: Any, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет тело раскрытой карточки (крючок владельца)."""
		if self._fill_body is not None:
			self._fill_body(item, body, collapse)

	def leading_widgets(self, item: Any, parent: QWidget) -> list[QWidget]:
		"""Виджеты начала шапки карточки (крючок владельца)."""
		return [] if self._leading is None else self._leading(item, parent)

	def action_widgets(self, item: Any, parent: QWidget) -> list[QWidget]:
		"""Кнопки правого края шапки (крючок владельца)."""
		return [] if self._actions is None else self._actions(item, parent)

	def progress_of(self, item: Any) -> tuple[float, str] | None:
		"""Прогресс элемента (крючок владельца)."""
		return None if self._progress is None else self._progress(item)

	def alert_of(self, item: Any) -> bool:
		"""Тревожная ли сводка у элемента (крючок владельца)."""
		return self._alert is not None and self._alert(item)

	# --- обновление -------------------------------------------------------------

	def sync(self, shown: Sequence[Any]) -> None:
		"""Приводит список карточек к снимку, трогая только изменившееся."""
		plan = plan_cards(shown, self._signatures, key=self.key, signature=self._signature)
		by_key = {self.key(item): item for item in shown}
		for item_key in plan.removed:
			self._drop_card(item_key)
		for item_key in plan.added:
			card = ListCard(self, by_key[item_key])
			self._cards[item_key] = card
			self._box.addWidget(card.widget)
		for item_key in plan.changed:
			self._cards[item_key].update(by_key[item_key])
		self._signatures = {self.key(item): self._signature(item) for item in shown}
		for index, item_key in enumerate(plan.order):
			widget = self._cards[item_key].widget
			if self._box.indexOf(widget) != index:
				self._box.insertWidget(index, widget)
		for item in shown:  # прогресс — без пересборки карточек
			self._cards[self.key(item)].set_progress(self.progress_of(item))

	def refresh_leading(self) -> None:
		"""Перерисовывает начала шапок всех карточек.

		Зовётся, когда изменилось не состояние списка, а то, из чего
		рисуется шапка: приехали аватары сообществ из кэша статистики.
		"""
		for card in self._cards.values():
			card.refresh_leading()

	def _drop_card(self, item_key: Hashable) -> None:
		"""Убирает карточку элемента, покинувшего показ.

		Если в ней правили, молчать нельзя: набранное пропадает вместе
		с карточкой, и человек должен понимать, почему.
		"""
		card = self._cards.pop(item_key, None)
		self._signatures.pop(item_key, None)
		if card is None:
			return
		if card.editing():
			self._show_error(self._lost_edit_text)
		self._box.removeWidget(card.widget)
		card.widget.setParent(None)
		card.widget.deleteLater()
