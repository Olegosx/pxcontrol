"""Показ длинного списка постов: правило показа, страницы, органы управления.

Общий слой двух списков — очереди отправки (ADR-0016) и отложенных
записей на сервере Telegram (ADR-0010): у обоих элементы адресованы
сообществу и моменту публикации, и человек смотрит на них одинаково —
сортировка, фильтр по сообществу и по слоту времени, страницы по полсотни.
Правила показа — чистые функции (тестируются без Qt); органы управления
(:class:`ViewBar`, :class:`PagerRow`) — виджеты, которые строят окно
очереди и панель отложенных, чтобы списки не расходились ни видом,
ни поведением.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, PushButton

from pxcontrol.ui.pages.common import SLOT_NOW, DtoComboBox, bind, list_button, slot_label


class Placed(Protocol):
	"""Элемент списка, адресованный сообществу и моменту публикации."""

	@property
	def community_id(self) -> int: ...

	@property
	def community_title(self) -> str: ...

	@property
	def when(self) -> datetime | None: ...


_P = TypeVar("_P", bound=Placed)
_T = TypeVar("_T")

#: Сколько элементов показывать на одной странице. Полсотни карточек
#: перекрывают экран с запасом и строятся мгновенно, а список бывает
#: длинным: очередь (ADR-0016) — хвост сверх сотни отложек на канал,
#: отложенные — до сотни на каждое сообщество.
PAGE_SIZE = 50

#: Служебные первые пункты фильтров по сообществу и по слоту времени.
ALL_COMMUNITIES = "Все сообщества"
ALL_SLOTS = "Все слоты"

#: «Сейчас» сортируется раньше любых дат.
_NEAREST = datetime.min.replace(tzinfo=UTC)


def sort_nearest(items: Sequence[_P], order_key: Callable[[_P], Any]) -> list[_P]:
	"""Ближайшие сначала: по моменту публикации, «сейчас» — первыми.

	``order_key`` разводит элементы с одинаковым временем устойчиво
	(id элемента очереди, id записи в очереди отложенных).
	"""
	return sorted(items, key=lambda item: (item.when or _NEAREST, order_key(item)))


def sort_by_community(items: Sequence[_P], order_key: Callable[[_P], Any]) -> list[_P]:
	"""По сообществам (по алфавиту), внутри — ближайшие сначала.

	Id сообщества в ключе разводит тёзок: названия в Telegram
	не уникальны, и посты двух одноимённых каналов не должны
	перемешиваться.
	"""
	return sorted(
		items,
		key=lambda item: (
			item.community_title.casefold(),
			item.community_id,
			item.when or _NEAREST,
			order_key(item),
		),
	)


def filter_by_community(items: Sequence[_P], community_id: int | None) -> list[_P]:
	"""Только одно сообщество (None — все). Идентичность — по id."""
	if community_id is None:
		return list(items)
	return [item for item in items if item.community_id == community_id]


def filter_by_slot(items: Sequence[_P], slot: str | None) -> list[_P]:
	"""Только один слот времени «ЧЧ:ММ» или «сейчас» (None — все)."""
	if slot is None:
		return list(items)
	return [item for item in items if slot_label(item.when) == slot]


def list_slots(items: Sequence[Placed]) -> list[str]:
	"""Слоты, встречающиеся в списке: «сейчас» первым, дальше по времени."""
	labels = {slot_label(item.when) for item in items}
	timed = sorted(label for label in labels if label != SLOT_NOW)
	return ([SLOT_NOW] if SLOT_NOW in labels else []) + timed


def list_communities(items: Sequence[Placed]) -> list[tuple[int, str]]:
	"""Сообщества, живущие в списке: пары «id, название» по алфавиту."""
	return sorted(
		{(item.community_id, item.community_title) for item in items},
		key=lambda entry: (entry[1].casefold(), entry[0]),
	)


@dataclass(frozen=True)
class ListPage(Generic[_T]):
	"""Страница показа: срез элементов и его место в целом.

	Attributes:
		items: элементы страницы (после фильтра и сортировки).
		page: номер страницы с 1 (уже зажат в существующие границы).
		pages: сколько всего страниц (минимум 1 — даже у пустого списка).
		total: сколько элементов прошло фильтр.
		first: номер первого элемента страницы в общем счёте (с 1);
			0 — показывать нечего.
		last: номер последнего элемента страницы (0 — показывать нечего).
	"""

	items: list[_T]
	page: int
	pages: int
	total: int
	first: int
	last: int


def paginate(items: Sequence[_T], page: int, per_page: int = PAGE_SIZE) -> ListPage[_T]:
	"""Нарезает список на страницы и отдаёт запрошенную.

	Номер страницы зажимается в существующие границы, а не отвергается:
	список живой, и пока человек смотрит последнюю страницу, элементы
	уходят — страница исчезает под ним. Зажим возвращает его
	на последнюю существующую вместо пустого экрана.

	Args:
		items: элементы после фильтра и сортировки.
		page: желаемый номер страницы (с 1).
		per_page: сколько элементов на странице (меньше 1 не бывает).
	"""
	per_page = max(1, per_page)
	total = len(items)
	pages = max(1, -(-total // per_page))  # деление с округлением вверх
	page = min(max(1, page), pages)
	start = (page - 1) * per_page
	chunk = list(items[start : start + per_page])
	return ListPage(
		items=chunk,
		page=page,
		pages=pages,
		total=total,
		first=start + 1 if chunk else 0,
		last=start + len(chunk),
	)


@dataclass(frozen=True)
class ListWords:
	"""Слова итоговой строки под списком — у каждого списка свои.

	Attributes:
		empty: список пуст целиком («Очередь пуста.»).
		of_all: родительный падеж множественного числа для счёта
			(«элементов очереди», «отложенных записей»).
		within: где живут элементы, для скобки при фильтре
			(«в очереди 120» → «в очереди»).
	"""

	empty: str
	of_all: str
	within: str


def summary_text(view: ListPage[Any], total: int, words: ListWords) -> str:
	"""Итоговая строка под списком: сколько показано и из скольких.

	``total`` — сколько элементов в списке вообще, ``view.total`` —
	сколько из них прошло фильтр. Диапазон номеров появляется только
	при нескольких страницах: у единственной «показаны 1–7 из 7»
	звучит канцелярски и ничего не добавляет.
	"""
	if total == 0:
		return words.empty
	if view.total == 0:
		return f"Ни один из {total} {words.of_all} не подходит под фильтр."
	if view.pages == 1:
		return f"Показано {view.total} из {total} {words.of_all}."
	tail = words.of_all if view.total == total else f"подходящих ({words.within} {total})"
	return f"Показаны {view.first}–{view.last} из {view.total} {tail}."


class ViewBar(QObject):
	"""Строка управления показом: сортировка, фильтры по сообществу и слоту.

	Пункты фильтров строятся по элементам живого списка
	(:meth:`refresh`) и пересобираются только при смене набора —
	дёргать списки на каждый опрос незачем. Восстановление выбора
	и служебный пункт — забота ``DtoComboBox``: выбранное сообщество
	хранится по id, исчезнувшее из списка сбрасывается на «все».
	Дополнительные перечисления (фильтр по статусу у очереди) владелец
	добавляет :meth:`add_choice`. Любая смена выбора — сигнал
	:attr:`changed`; владелец применяет правило и перерисовывает список.
	"""

	changed = Signal()

	def __init__(
		self,
		parent: QWidget,
		sort_options: type[StrEnum],
		*,
		wanted_community: int | None = None,
	) -> None:
		"""Args:
		parent: виджет-владелец (родитель списков).
		sort_options: перечисление порядков показа (подписи — пункты).
		wanted_community: сообщество, которое выбрать при первом
			наполнении фильтра: пункты строятся по элементам списка,
			которых до первого опроса ещё нет. Сообщество, которого
			в списке нет, фильтром не становится.
		"""
		super().__init__(parent)
		self._parent = parent
		self.layout = QHBoxLayout()
		self.layout.addWidget(BodyLabel("Показ:", parent))
		self._choices = 0  # сколько списков-перечислений уже стоит после «Показ:»
		self._sort_options = list(sort_options)
		self.sort = self.add_choice(self._sort_options)
		self.community: DtoComboBox[tuple[int, str]] = DtoComboBox(
			parent, placeholder=ALL_COMMUNITIES
		)
		self.community.currentIndexChanged.connect(self._on_changed)
		self.layout.addWidget(self.community)
		self.slot: DtoComboBox[str] = DtoComboBox(parent, placeholder=ALL_SLOTS)
		self.slot.setToolTip("Слот — время публикации поста")
		self.slot.currentIndexChanged.connect(self._on_changed)
		self.layout.addWidget(self.slot)
		self.layout.addStretch()
		self._wanted_community = wanted_community
		self._known_communities: list[tuple[int, str]] = []
		self._known_slots: list[str] = []

	def add_choice(self, options: Sequence[StrEnum], current: StrEnum | None = None) -> ComboBox:
		"""Добавляет список выбора из перечисления (подписи — значения).

		Начальный пункт ставится до подключения сигнала: обработчик
		владельца обычно перерисовывает список, которого ещё нет.
		"""
		combo = ComboBox(self._parent)
		for option in options:
			combo.addItem(option.value)
		if current is not None:
			combo.setCurrentIndex(list(options).index(current))
		combo.currentIndexChanged.connect(self._on_changed)
		# перечисления стоят сразу за «Показ:», перед фильтрами сообщества
		# и слота, в порядке добавления
		self._choices += 1
		self.layout.insertWidget(self._choices, combo)
		return combo

	def sort_option(self) -> StrEnum:
		"""Выбранный порядок показа."""
		return self._sort_options[int(self.sort.currentIndex())]

	def community_id(self) -> int | None:
		"""Выбранное сообщество (None — все)."""
		selected = self.community.selected()
		return selected[0] if selected is not None else None

	def slot_value(self) -> str | None:
		"""Выбранный слот времени (None — все)."""
		return self.slot.selected()

	def refresh(self, items: Sequence[Placed]) -> None:
		"""Обновляет пункты фильтров по сообществам и слотам живого списка.

		Зовётся из правила показа при каждом опросе, без сигналов:
		выбор внутри опроса запустил бы второй опрос поверх первого.
		"""
		communities = list_communities(items)
		if communities != self._known_communities:
			self._known_communities = communities
			self.community.set_items(
				communities, label=lambda entry: entry[1], key=lambda entry: entry[0]
			)
			if self._wanted_community is not None:
				wanted = self._wanted_community
				self._wanted_community = None
				self.community.blockSignals(True)
				try:
					self.community.select(lambda entry: entry[0] == wanted)
				finally:
					self.community.blockSignals(False)
		slots = list_slots(items)
		if slots != self._known_slots:
			self._known_slots = slots
			self.slot.set_items(slots, label=lambda slot: slot, key=lambda slot: slot)

	def _on_changed(self, _index: int = 0) -> None:
		self.changed.emit()


class PagerRow:
	"""Нижняя строка списка: итог слева, перелистывание справа.

	Перелистывание прячется целиком, пока страница одна: кнопки,
	которые никуда не ведут, — шум. До первого наполнения строка
	пуста — иначе перелистывание мигнуло бы и спряталось.
	"""

	def __init__(
		self, parent: QWidget, on_step: Callable[[int], None], *, compact: bool = False
	) -> None:
		"""Args:
		parent: виджет-владелец.
		on_step: листание на ±1 страницу.
		compact: кнопки размером строки списка (28 / 13 — макет
			страницы сообщества), итог переносится по словам.
		"""
		self.layout = QHBoxLayout()
		self.summary = CaptionLabel("", parent)
		self.summary.setWordWrap(True)
		self.layout.addWidget(self.summary, stretch=1)

		def make(text: str) -> QPushButton:
			button: QPushButton = list_button(text, parent) if compact else PushButton(text, parent)
			return button

		self._prev = make("Назад")
		self._prev.clicked.connect(bind(on_step, -1))
		self._label = CaptionLabel("", parent)
		self._next = make("Вперёд")
		self._next.clicked.connect(bind(on_step, 1))
		for widget in (self._prev, self._label, self._next):
			self.layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignTop)
			widget.hide()

	def update(self, view: ListPage[Any], text: str) -> None:
		"""Итоговая строка и состояние перелистывания по снимку страницы."""
		self.summary.setText(text)
		multipage = view.pages > 1
		for widget in (self._prev, self._label, self._next):
			widget.setVisible(multipage)
		if multipage:
			self._label.setText(f"Страница {view.page} из {view.pages}")
			self._prev.setEnabled(view.page > 1)
			self._next.setEnabled(view.page < view.pages)


def step_page(page: int, delta: int, pages: int) -> int:
	"""Номер страницы после листания на ``delta``, зажатый в 1..``pages``."""
	return min(max(1, page + delta), pages)
