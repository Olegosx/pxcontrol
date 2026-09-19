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

Точечность доведена до частей карточки (аудит 19.09.2026): заголовок
и сводка обновляются на месте всегда, а начало шапки (логотип, метка
слота) и правый край (кнопки, светофор, полоса) пересобираются только
когда меняется **их собственный отпечаток** — владелец задаёт его
крючками ``leading_signature`` и ``actions_signature``. Смена пометки
или текста ошибки у поста не трогает аватар, а приезд аватаров
из кэша статистики пересобирает начало шапки только у тех карточек,
где картинка появилась или сменилась.
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


def default_title(item: Any) -> str:
	"""Заголовок по умолчанию — ``title`` элемента."""
	return str(item.title)


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
	заголовок и сводка меняются на месте, начало шапки и кнопки
	пересобираются только по своим отпечаткам, полоса прогресса двигается
	только при новом значении, а тело (форма правки) не пересоздаётся.
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
		# отпечатки частей шапки: часть пересобирается при расхождении
		self._leading_signature: tuple[Any, ...] | None = None
		self._actions_signature: tuple[Any, ...] | None = None
		# последний переданный прогресс: повтор того же значения — не работа
		self._progress: tuple[float, str] | None = None
		self._progress_known = False
		self.widget = CollapsibleCard(
			owner.title(item),
			owner.page,
			trailing=self._actions,
			leading=self._leading,
			keep_summary=True,
			stacked=self._compact,
		)
		self.widget.expanded_changed.connect(self._on_expanded)
		self.update(item)

	def update(self, item: Any) -> None:
		"""Приводит карточку к новому снимку элемента.

		Заголовок и сводка — на месте; начало шапки и правый край —
		только если сменился их отпечаток (крючки владельца).
		"""
		self._item = item
		self.widget.set_title(self._owner.title(item))
		self.widget.set_summary(self._owner.subtitle(item), alert=self._owner.alert_of(item))
		editable = self._owner.can_edit(item)
		self.widget.set_expandable(editable)
		if not editable:
			# элемент больше не правится: форма в теле уже не про него
			self._reset_body()
		leading_signature = self._owner.leading_signature(item)
		if leading_signature != self._leading_signature:
			self._leading_signature = leading_signature
			self._fill_leading(item)
		actions_signature = self._owner.actions_signature(item)
		if actions_signature != self._actions_signature:
			self._actions_signature = actions_signature
			self._fill_actions(item)
		self.set_progress(self._owner.progress_of(item))

	def _fill_leading(self, item: Any) -> None:
		"""Пересобирает начало шапки (логотип сообщества, метка слота)."""
		clear_layout(self._leading_box)
		for widget in self._owner.leading_widgets(item, self._leading):
			self._leading_box.addWidget(widget)

	def set_progress(self, progress: tuple[float, str] | None) -> None:
		"""Двигает полосу прогресса (без пересборки карточки).

		В компактном режиме полоса — под названием (рисует карточка),
		иначе — в шапке; None прячет её. То же значение подряд ничего
		не делает: у ждущих элементов прогресса нет, и прятать полосу
		на каждый снимок незачем.
		"""
		if self._progress_known and progress == self._progress:
			return
		self._progress = progress
		self._progress_known = True
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
		"""Пересобирает правый край шапки: полоса прогресса и кнопки.

		Зовётся только при смене отпечатка правого края (у очереди —
		статус: набор кнопок и светофор от него и зависят).
		"""
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

	Контракт элемента — только ключ (``key``, по умолчанию ``id``)
	и заголовок (``title``, по умолчанию поле ``title`` элемента).
	Всё остальное владелец задаёт крючками — список не читает
	у элемента ничего сверх этого.
	"""

	def __init__(
		self,
		page: QWidget,
		box: QVBoxLayout,
		*,
		subtitle: Callable[[Any], str],
		signature: SignatureFn,
		key: KeyFn = default_key,
		title: Callable[[Any], str] = default_title,
		leading: WidgetsFn | None = None,
		leading_signature: SignatureFn | None = None,
		actions: WidgetsFn | None = None,
		actions_signature: SignatureFn | None = None,
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
		signature: отпечаток элемента — по нему решается обновление
			карточки (заголовок, сводка, раскрываемость).
		key: ключ элемента (устойчив между снимками).
		title: заголовок карточки элемента (у файла на «Видео» — имя
			файла; у элементов очереди и записей — их ``title``).
		leading: виджеты в начале шапки (логотип сообщества, метка слота).
		leading_signature: отпечаток начала шапки — всё, от чего зависят
			его виджеты (время слота, путь аватара из кэша страницы);
			меняется он — начало шапки пересобирается. Без него начало
			пересобирается при любом изменении ``signature``.
		actions: кнопки правого края шапки под текущее состояние элемента.
		actions_signature: отпечаток правого края (у очереди — статус
			и наличие вложения); без него правый край пересобирается
			при любом изменении ``signature``.
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
		#: заголовок карточки элемента (читают карточки).
		self.title = title
		self._box = box
		self._signature = signature
		self._leading = leading
		self._leading_signature = leading_signature
		self._actions = actions
		self._actions_signature = actions_signature
		self._progress = progress
		self._alert = alert
		self._editable = editable
		self._fill_body = fill_body
		self._lost_edit_text = lost_edit_text
		self._show_error = error_reporter(page)
		self._cards: dict[Hashable, ListCard] = {}
		self._signatures: dict[Hashable, tuple[Any, ...]] = {}
		#: ключи карточек в порядке компоновки — зеркало ``box``, чтобы
		#: выравнивать порядок без линейного ``indexOf`` на каждую карточку
		self._order: list[Hashable] = []

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

	def leading_signature(self, item: Any) -> tuple[Any, ...]:
		"""Отпечаток начала шапки (крючок владельца; иначе — отпечаток элемента)."""
		if self._leading_signature is None:
			return self._signature(item)
		return self._leading_signature(item)

	def action_widgets(self, item: Any, parent: QWidget) -> list[QWidget]:
		"""Кнопки правого края шапки (крючок владельца)."""
		return [] if self._actions is None else self._actions(item, parent)

	def actions_signature(self, item: Any) -> tuple[Any, ...]:
		"""Отпечаток правого края (крючок владельца; иначе — отпечаток элемента)."""
		if self._actions_signature is None:
			return self._signature(item)
		return self._actions_signature(item)

	def _full_signature(self, item: Any) -> tuple[Any, ...]:
		"""Отпечаток карточки целиком: элемент и обе части шапки.

		Начало шапки может зависеть не от элемента, а от кэша страницы
		(аватары приезжают позже списка) — без частей в общем отпечатке
		:func:`plan_cards` не увидел бы, что карточку пора обновить.
		"""
		return (self._signature(item), self.leading_signature(item), self.actions_signature(item))

	def progress_of(self, item: Any) -> tuple[float, str] | None:
		"""Прогресс элемента (крючок владельца)."""
		return None if self._progress is None else self._progress(item)

	def alert_of(self, item: Any) -> bool:
		"""Тревожная ли сводка у элемента (крючок владельца)."""
		return self._alert is not None and self._alert(item)

	# --- обновление -------------------------------------------------------------

	def sync(self, shown: Sequence[Any]) -> None:
		"""Приводит список карточек к снимку, трогая только изменившееся."""
		plan = plan_cards(shown, self._signatures, key=self.key, signature=self._full_signature)
		by_key = {self.key(item): item for item in shown}
		for item_key in plan.removed:
			self._drop_card(item_key)
		for item_key in plan.added:
			card = ListCard(self, by_key[item_key])
			self._cards[item_key] = card
			self._box.addWidget(card.widget)
			self._order.append(item_key)
		for item_key in plan.changed:
			self._cards[item_key].update(by_key[item_key])
		self._signatures = {self.key(item): self._full_signature(item) for item in shown}
		self._reorder(plan.order)
		for item in shown:  # прогресс — без пересборки карточек
			self._cards[self.key(item)].set_progress(self.progress_of(item))

	def _reorder(self, order: list[Hashable]) -> None:
		"""Выравнивает порядок карточек в компоновке по целевому.

		Сравнивается зеркало компоновки, а не сама компоновка: при
		совпадении (обычный случай) это один проход без обращений к Qt,
		переставляется только то, что стоит не на месте.
		"""
		if self._order == order:
			return
		for index, item_key in enumerate(order):
			if self._order[index] == item_key:
				continue
			self._box.insertWidget(index, self._cards[item_key].widget)
			self._order.remove(item_key)
			self._order.insert(index, item_key)

	def _drop_card(self, item_key: Hashable) -> None:
		"""Убирает карточку элемента, покинувшего показ.

		Если в ней правили, молчать нельзя: набранное пропадает вместе
		с карточкой, и человек должен понимать, почему.
		"""
		card = self._cards.pop(item_key, None)
		self._signatures.pop(item_key, None)
		if card is None:
			return
		self._order.remove(item_key)
		if card.editing():
			self._show_error(self._lost_edit_text)
		self._box.removeWidget(card.widget)
		card.widget.setParent(None)
		card.widget.deleteLater()
