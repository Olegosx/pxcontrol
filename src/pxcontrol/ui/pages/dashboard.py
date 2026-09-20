"""Разделы дашбордов: заголовок и сетка карточек, обновляемые по отпечатку.

Дашборды сообществ и исполнителей раньше при каждом показе стирали
разделы и собирали карточки заново — около мегабайта виджетов
на карточку (замер 19.09.2026), на десяток сообществ это 10–15 МБ
оборота при каждом возвращении на страницу. Здесь тот же принцип,
что у списков карточек (:mod:`card_list`): план по ключу и отпечатку
(:func:`plan_cards`) решает, какие карточки создать, какие заменить,
какие убрать, — неизменившиеся живут. Карточка дашборда — неизменяемая
сборка из пяти блоков (шапка, метрики, плашка, кнопки, приглушение):
изменившаяся заменяется новой целиком, второй механизм обновления
по частям здесь не окупился бы — изменения на дашборде редкие.

:class:`SectionStack` держит разделы в заданном порядке: раздел
появляется с первым элементом и исчезает с последним, остальные
при этом не трогаются.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from typing import Any, Generic, Protocol, TypeVar

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon

from pxcontrol.ui.pages.card_list import plan_cards
from pxcontrol.ui.pages.common import FlowGrid, clear_layout, section_header

_T = TypeVar("_T")
_K = TypeVar("_K", bound=Hashable)


class Section(Protocol):
	"""Раздел дашборда: заголовок и тело — два виджета в стопке."""

	@property
	def header(self) -> QWidget: ...

	@property
	def body(self) -> QWidget: ...


class SectionHeader:
	"""Заголовок раздела с числом элементов; перестраивается только по числу.

	Штатный ``section_header`` — сборка без API для смены числа,
	поэтому заголовок живёт в контейнере и пересобирается, когда число
	изменилось: это один маленький виджет, а не карточки.
	"""

	def __init__(self, page: QWidget, title: str, icon: FluentIcon | None) -> None:
		self._page = page
		self._title = title
		self._icon = icon
		self._count: int | None = None
		self.widget = QWidget(page)
		self._box = QVBoxLayout(self.widget)
		self._box.setContentsMargins(0, 0, 0, 0)
		self.set_count(0)

	def set_count(self, count: int) -> None:
		"""Показывает число элементов раздела (только при смене)."""
		if count == self._count:
			return
		self._count = count
		clear_layout(self._box)
		self._box.addWidget(section_header(self._page, self._title, count, icon=self._icon))


class GridSection(Generic[_T]):
	"""Раздел с сеткой карточек: карточки живут, пока не сменился отпечаток."""

	def __init__(
		self,
		page: QWidget,
		title: str,
		icon: FluentIcon | None,
		*,
		min_width: int,
		spacing: int,
	) -> None:
		self._header = SectionHeader(page, title, icon)
		self._grid = FlowGrid([], page, min_width=min_width, spacing=spacing)
		self._cards: dict[Hashable, QWidget] = {}
		self._signatures: dict[Hashable, tuple[Any, ...]] = {}

	@property
	def header(self) -> QWidget:
		"""Заголовок раздела (виджет для стопки)."""
		return self._header.widget

	@property
	def body(self) -> QWidget:
		"""Сетка карточек (виджет для стопки)."""
		return self._grid

	@property
	def cards(self) -> dict[Hashable, QWidget]:
		"""Живые карточки по ключу — для точечных обновлений владельца."""
		return self._cards

	def sync(
		self,
		items: Sequence[_T],
		*,
		key: Callable[[_T], Hashable],
		signature: Callable[[_T], tuple[Any, ...]],
		make: Callable[[_T], QWidget],
	) -> None:
		"""Приводит сетку к списку элементов, трогая только изменившееся.

		Args:
			items: элементы раздела в порядке показа.
			key: ключ элемента (устойчив между снимками).
			signature: отпечаток — всё, что показывает карточка; сменился —
				карточка заменяется новой.
			make: сборка карточки элемента.
		"""
		plan = plan_cards(items, self._signatures, key=key, signature=signature)
		by_key = {key(item): item for item in items}
		for item_key in plan.removed:
			self._drop(item_key)
		for item_key in plan.added:
			self._cards[item_key] = make(by_key[item_key])
		for item_key in plan.changed:
			self._drop(item_key)
			self._cards[item_key] = make(by_key[item_key])
		self._signatures = {key(item): signature(item) for item in items}
		self._grid.set_cards([self._cards[item_key] for item_key in plan.order])
		self._header.set_count(len(items))

	def _drop(self, item_key: Hashable) -> None:
		card = self._cards.pop(item_key, None)
		if card is not None:
			card.setParent(None)
			card.deleteLater()


class SectionStack(Generic[_K]):
	"""Разделы дашборда в заданном порядке ключей.

	Раздел появляется, когда у него есть элементы, и снимается, когда
	их не осталось; остальные разделы при этом не пересобираются.
	"""

	def __init__(self, layout: QVBoxLayout, order: Sequence[_K]) -> None:
		"""``layout`` — стопка страницы под разделы; ``order`` — порядок ключей."""
		self._layout = layout
		self._order = list(order)
		self._sections: dict[_K, Section] = {}

	def get(self, key: _K) -> Section | None:
		"""Раздел по ключу (None — его сейчас нет)."""
		return self._sections.get(key)

	def ensure(self, key: _K, factory: Callable[[], Section]) -> Section:
		"""Раздел по ключу — существующий или созданный на своём месте."""
		section = self._sections.get(key)
		if section is not None:
			return section
		section = factory()
		self._sections[key] = section
		# место — после разделов, стоящих раньше в порядке и уже показанных
		ahead = sum(1 for other in self._order[: self._order.index(key)] if other in self._sections)
		self._layout.insertWidget(2 * ahead, section.header)
		self._layout.insertWidget(2 * ahead + 1, section.body)
		return section

	def drop(self, key: _K) -> None:
		"""Снимает раздел вместе с карточками (они — дети его тела)."""
		section = self._sections.pop(key, None)
		if section is None:
			return
		for widget in (section.header, section.body):
			self._layout.removeWidget(widget)
			widget.setParent(None)
			widget.deleteLater()

	def drop_all(self) -> None:
		"""Снимает все разделы (пустое состояние, смена вида)."""
		for key in list(self._sections):
			self.drop(key)

	def __contains__(self, key: object) -> bool:
		return key in self._sections
