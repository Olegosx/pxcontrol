"""Полный просмотр очереди отправки: сортировка и фильтры (ADR-0016).

Страница «Публикация» показывает только ближайшие карточки очереди;
кнопка «Вся очередь…» открывает этот диалог со всеми элементами.
Список живой (опрашивается тем же способом, что панель страницы, —
через :class:`QueuePanel`), действия у карточек те же: «Отмена»
у живых, «Повторить»/«Убрать» у ошибок. Правило показа (фильтр
по статусу и каналу + сортировка) и нарезка на страницы — чистые
функции :func:`apply_view` и :func:`paginate`, они тестируются без Qt.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, PushButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.publish_queue import (
	EDITABLE_STATUSES,
	QueueItemDto,
	QueueItemStatus,
)
from pxcontrol.ui import density
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	QueuePanel,
	WorkDialog,
	bind,
	format_local,
	list_area,
)
from pxcontrol.ui.pages.publish_queue_edit import mount_queue_item_editor

#: Служебный первый пункт фильтра по сообществу.
_ALL_COMMUNITIES = "Все сообщества"

#: Сколько элементов очереди показывать на одной странице. Полсотни
#: карточек перекрывают экран с запасом и строятся мгновенно, а очередь
#: (ADR-0016) — это хвост сверх сотни отложек на канал: показ всей разом
#: перестраивал бы сотни виджетов при каждой смене состава.
PAGE_SIZE = 50


class QueueSort(StrEnum):
	"""Порядок показа элементов очереди (подписи — пункты списка)."""

	NEAREST = "Ближайшие сначала"  # по дате публикации; «сейчас» — первыми
	ENQUEUED = "Порядок постановки"
	COMMUNITY = "По сообществам"  # по алфавиту, внутри — по дате


class QueueFilter(StrEnum):
	"""Фильтр показа по статусу (подписи — пункты списка)."""

	ALL = "Все статусы"
	SENDABLE = "К отправке"  # отправляется и ждёт своей очереди
	WAITING = "Ждут слота"  # ждут свободного слота отложек (ADR-0016)
	ERRORS = "Ошибки"


def queue_subtitle(item: QueueItemDto) -> str:
	"""Подпись карточки очереди: канал, момент публикации и статус.

	Общая для панели на «Публикации» и полного просмотра. Момент
	хранится в UTC (как отдаётся Telegram) и показывается в местном
	времени — как пользователь вводил его в форме.
	"""
	when_text = "сейчас" if item.when is None else format_local(item.when)
	if item.status is QueueItemStatus.SENDING:
		status = "отправляется"
	elif item.status is QueueItemStatus.ERROR:
		status = f"ошибка: {item.error}"
	elif item.status is QueueItemStatus.WAITING:
		# лимит Telegram — 100 отложек на канал (ADR-0016); хвост
		# публикует само приложение, поэтому оно должно быть запущено
		status = "ждёт слота отложек · уйдёт при запущенном приложении"
	else:
		status = "в очереди"
	subtitle = f"{item.community_title} · публикация: {when_text} · {status}"
	if item.note:
		subtitle += f" · {item.note}"
	return subtitle


def apply_view(
	items: list[QueueItemDto],
	sort: QueueSort,
	status: QueueFilter,
	community_id: int | None,
) -> list[QueueItemDto]:
	"""Правило показа: фильтр по статусу и каналу, затем сортировка.

	Args:
		items: видимые элементы очереди (без завершённых).
		sort: порядок показа.
		status: фильтр по статусу.
		community_id: id канала (None — все). Идентичность — по id:
			названия каналов Telegram не уникальны.
	"""
	if community_id is not None:
		items = [item for item in items if item.community_id == community_id]
	if status is QueueFilter.SENDABLE:
		wanted = (QueueItemStatus.PENDING, QueueItemStatus.SENDING)
		items = [item for item in items if item.status in wanted]
	elif status is QueueFilter.WAITING:
		items = [item for item in items if item.status is QueueItemStatus.WAITING]
	elif status is QueueFilter.ERRORS:
		items = [item for item in items if item.status is QueueItemStatus.ERROR]
	nearest = datetime.min.replace(tzinfo=UTC)  # «сейчас» — раньше любых дат
	if sort is QueueSort.NEAREST:
		return sorted(items, key=lambda item: (item.when or nearest, item.id))
	if sort is QueueSort.COMMUNITY:
		# id в ключе разводит каналы-тёзки, чтобы их посты не перемешивались
		return sorted(
			items,
			key=lambda item: (
				item.community_title.casefold(),
				item.community_id,
				item.when or nearest,
				item.id,
			),
		)
	return sorted(items, key=lambda item: item.id)


@dataclass(frozen=True)
class QueuePage:
	"""Страница показа очереди: срез элементов и его место в целом.

	Attributes:
		items: элементы страницы (после фильтра и сортировки).
		page: номер страницы с 1 (уже зажат в существующие границы).
		pages: сколько всего страниц (минимум 1 — даже у пустого списка).
		total: сколько элементов прошло фильтр.
		first: номер первого элемента страницы в общем счёте (с 1);
			0 — показывать нечего.
		last: номер последнего элемента страницы (0 — показывать нечего).
	"""

	items: list[QueueItemDto]
	page: int
	pages: int
	total: int
	first: int
	last: int


def paginate(items: list[QueueItemDto], page: int, per_page: int = PAGE_SIZE) -> QueuePage:
	"""Нарезает список на страницы и отдаёт запрошенную.

	Номер страницы зажимается в существующие границы, а не отвергается:
	очередь живая, и пока пользователь смотрит последнюю страницу, посты
	уходят — страница исчезает под ним. Зажим возвращает его на последнюю
	существующую вместо пустого экрана.

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
	chunk = items[start : start + per_page]
	return QueuePage(
		items=chunk,
		page=page,
		pages=pages,
		total=total,
		first=start + 1 if chunk else 0,
		last=start + len(chunk),
	)


def summary_text(view: QueuePage, total: int) -> str:
	"""Итоговая строка под списком: сколько показано и из скольки.

	``total`` — сколько элементов в очереди вообще, ``view.total`` —
	сколько из них прошло фильтр. Диапазон номеров появляется только
	при нескольких страницах: у единственной «показаны 1–7 из 7»
	звучит канцелярски и ничего не добавляет.
	"""
	if total == 0:
		return "Очередь пуста."
	if view.total == 0:
		return f"Ни один из {total} элементов очереди не подходит под фильтр."
	if view.pages == 1:
		return f"Показано {view.total} из {total} элементов очереди."
	tail = "элементов очереди" if view.total == total else f"подходящих (в очереди {total})"
	return f"Показаны {view.first}–{view.last} из {view.total} {tail}."


class QueueViewDialog(WorkDialog):
	"""Вся очередь отправки: живой список с сортировкой и фильтрами."""

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__("Очередь отправки", parent, size=(880, 620))
		self._worker = worker
		self._sort = QueueSort.NEAREST
		self._status = QueueFilter.ALL
		self._community: int | None = None
		self._known_communities: list[tuple[int, str]] = []
		self._total = 0
		self._page = 1
		self._view = paginate([], 1)
		self._build_controls()
		area, box = list_area(self, spacing=density.spacing().list_spacing)
		self.content.addWidget(area, stretch=1)
		self._build_footer()
		self.add_close_button()
		self._panel = QueuePanel(
			worker,
			self,
			box,
			service=lambda: worker.engine.publish_queue,
			subtitle=queue_subtitle,
			transform=self._apply_view,
			on_refreshed=self._update_summary,
			# зритель: завершёнными владеет панель страницы «Публикация»,
			# иначе две панели наперегонки снимали бы элементы
			dismiss_finished=False,
			editable=lambda item: item.status in EDITABLE_STATUSES,
			fill_body=self._fill_editor,
		)

	def _fill_editor(self, item_id: int, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет раскрытую карточку формой правки (ADR-0016, п. 7)."""
		mount_queue_item_editor(self._worker, self, item_id, body, collapse, self._panel.poll)

	# --- сборка ----------------------------------------------------------------

	def _build_controls(self) -> None:
		"""Строка управления показом: сортировка и два фильтра."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Показ:", self))
		self._sort_combo = ComboBox(self)
		for option in QueueSort:
			self._sort_combo.addItem(option.value)
		self._sort_combo.currentIndexChanged.connect(self._on_view_changed)
		row.addWidget(self._sort_combo)
		self._status_combo = ComboBox(self)
		for status_option in QueueFilter:
			self._status_combo.addItem(status_option.value)
		self._status_combo.currentIndexChanged.connect(self._on_view_changed)
		row.addWidget(self._status_combo)
		self._community_combo: DtoComboBox[tuple[int, str]] = DtoComboBox(
			self, placeholder=_ALL_COMMUNITIES
		)
		self._community_combo.currentIndexChanged.connect(self._on_view_changed)
		row.addWidget(self._community_combo)
		row.addStretch()
		self.content.addLayout(row)

	def _build_footer(self) -> None:
		"""Нижняя строка: сводка слева, перелистывание справа."""
		row = QHBoxLayout()
		self._summary = CaptionLabel("", self)
		row.addWidget(self._summary)
		row.addStretch()
		self._prev_button = PushButton("Назад", self)
		self._prev_button.clicked.connect(bind(self._step, -1))
		row.addWidget(self._prev_button)
		self._page_label = CaptionLabel("", self)
		row.addWidget(self._page_label)
		self._next_button = PushButton("Вперёд", self)
		self._next_button.clicked.connect(bind(self._step, 1))
		row.addWidget(self._next_button)
		# до первого опроса (полсекунды) страниц ещё нет: показать
		# перелистывание сразу значило бы мигнуть им и спрятать
		for widget in (self._prev_button, self._page_label, self._next_button):
			widget.hide()
		self.content.addLayout(row)

	# --- правило показа --------------------------------------------------------

	def _apply_view(self, items: list[QueueItemDto]) -> list[QueueItemDto]:
		"""Крючок панели: правило показа и нарезка на страницы.

		Снимок страницы сохраняется целиком (:attr:`_view`): по нему
		рисуются сводка и перелистывание. Зажатый номер возвращается
		в :attr:`_page` — очередь живая, и страница, на которую смотрит
		пользователь, может исчезнуть под ним.
		"""
		self._total = len(items)
		self._refresh_communities(items)
		shown = apply_view(items, self._sort, self._status, self._community)
		self._view = paginate(shown, self._page)
		self._page = self._view.page
		return self._view.items

	def _step(self, delta: int) -> None:
		"""Листает страницу; показ обновляется сразу, не по таймеру."""
		self._page = min(max(1, self._page + delta), self._view.pages)
		self._panel.poll()

	def _refresh_communities(self, items: list[QueueItemDto]) -> None:
		"""Обновляет пункты фильтра канала по каналам, живущим в очереди.

		Пересборка — только при смене набора (каждые полсекунды дёргать
		комбобокс незачем). Восстановление выбора и служебный пункт —
		забота ``DtoComboBox``: выбранный канал сохраняется по id,
		исчезнувший из очереди — сбрасывается на «Все каналы».
		"""
		communities = sorted(
			{(item.community_id, item.community_title) for item in items},
			key=lambda entry: (entry[1].casefold(), entry[0]),
		)
		if communities == self._known_communities:
			return
		self._known_communities = communities
		self._community_combo.set_items(
			communities, label=lambda entry: entry[1], key=lambda entry: entry[0]
		)
		selected = self._community_combo.selected()
		self._community = selected[0] if selected is not None else None

	def _on_view_changed(self, _index: int = 0) -> None:
		"""Читает правило показа из списков; следующий опрос его применит."""
		self._sort = list(QueueSort)[int(self._sort_combo.currentIndex())]
		self._status = list(QueueFilter)[int(self._status_combo.currentIndex())]
		selected = self._community_combo.selected()
		self._community = selected[0] if selected is not None else None
		self._page = 1  # набор изменился — листаем с начала
		self._panel.poll()  # показ обновляется сразу, не по таймеру

	def _update_summary(self, _shown: list[QueueItemDto]) -> None:
		"""Итоговая строка и состояние перелистывания.

		Считается по снимку страницы (:attr:`_view`), а не по списку
		показанных: номера элементов в общем счёте знает только он.
		Перелистывание прячется целиком, пока страница одна: кнопки,
		которые никуда не ведут, — шум.
		"""
		view = self._view
		self._summary.setText(summary_text(view, self._total))
		multipage = view.pages > 1
		self._prev_button.setVisible(multipage)
		self._next_button.setVisible(multipage)
		self._page_label.setVisible(multipage)
		if multipage:
			self._page_label.setText(f"Страница {view.page} из {view.pages}")
			self._prev_button.setEnabled(view.page > 1)
			self._next_button.setEnabled(view.page < view.pages)
