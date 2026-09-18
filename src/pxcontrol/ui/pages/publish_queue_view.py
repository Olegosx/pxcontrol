"""Вид списка очереди отправки: сортировка и фильтры (ADR-0016).

Тело экрана «Очередь» раздела «Публикация» (ADR-0032) —
:class:`QueueView`. Форма нового поста показывает только ближайшие
карточки очереди, всё целиком живёт здесь; сюда же ведут кнопки
«Вся очередь…» с формы и со страницы сообщества и действия дашборда.
Список живой (опрашивается тем же способом, что панель формы, — через
:class:`QueuePanel`), действия у карточек те же: «Отмена» у живых,
«Повторить»/«Убрать» у ошибок. Правило показа — чистая
:func:`apply_view` поверх общих правил списков (:mod:`list_view`:
сортировка, фильтры по сообществу и слоту, страницы); от общего
у очереди — фильтр по статусу и порядок постановки.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import StrongBodyLabel

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.community_stats import CommunityStatsDto
from pxcontrol.engine.services.publish_queue import (
	EDITABLE_STATUSES,
	QueueItemDto,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	entity_avatar,
	font_px,
	format_local,
	noop,
	slot_color,
	slot_label,
)
from pxcontrol.ui.pages.list_view import (
	ListPage,
	ListWords,
	PagerRow,
	ViewBar,
	filter_by_community,
	filter_by_slot,
	paginate,
	sort_by_community,
	sort_nearest,
	step_page,
	summary_text,
)
from pxcontrol.ui.pages.publish_queue_edit import mount_queue_item_editor
from pxcontrol.ui.pages.queue_panel import QueuePanel

#: Слова итоговой строки под очередью.
QUEUE_WORDS = ListWords(empty="Очередь пуста.", of_all="элементов очереди", within="в очереди")


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


def queue_subtitle(item: QueueItemDto, *, with_community: bool = True) -> str:
	"""Подпись карточки очереди: канал, момент публикации и статус.

	Общая для панели формы поста, экрана «Очередь» и вкладки
	«Очередь» страницы сообщества — там название сообщества и так
	в шапке, и ``with_community=False`` его убирает. Момент хранится
	в UTC (как отдаётся Telegram) и показывается в местном времени —
	как пользователь вводил его в форме.
	"""
	when_text = "сейчас" if item.when is None else format_local(item.when)
	if item.status is JobStatus.RUNNING:
		status = "отправляется"
	elif item.status is JobStatus.ERROR:
		status = f"ошибка: {item.error}"
	elif item.status is JobStatus.WAITING:
		# лимит Telegram — 100 отложек на канал (ADR-0016). Приписки
		# «уйдёт при запущенном приложении» здесь нет намеренно: она
		# повторялась в каждой строке списка, ничего не добавляя
		# к следующей; само ограничение — в ADR-0016, п. 8
		status = "ждёт слота отложек"
	else:
		status = "в очереди"
	subtitle = f"публикация: {when_text} · {status}"
	if with_community:
		subtitle = f"{item.community_title} · {subtitle}"
	if item.note:
		subtitle += f" · {item.note}"
	return subtitle


#: Размер логотипа сообщества в шапке карточки (пиксели).
LOGO_SIZE = 24


def slot_chip(when: object, parent: QWidget, *, compact: bool = False) -> StrongBodyLabel:
	"""Метка слота времени в шапке карточки: «[ЧЧ:ММ]» цветом слота.

	Цвет выводится из самого времени, поэтому посты одного слота
	узнаются пачкой — в очереди из сотен постов «когда» и «куда» —
	два первых вопроса. ``compact`` — кегль 13 / 600 по макету
	страницы сообщества.
	"""
	from datetime import datetime

	label = slot_label(when if isinstance(when, datetime) else None)
	chip = StrongBodyLabel(f"[{label}]", parent)
	if compact:
		chip.setFont(font_px(13, QFont.Weight.DemiBold))
	chip.setTextColor(*slot_color(label))
	chip.setToolTip("Время публикации (слот)")
	return chip


def post_leading(
	community_id: int, community_title: str, when: object, parent: QWidget, avatar_path: str | None
) -> list[QWidget]:
	"""Начало шапки карточки поста: логотип сообщества и метка слота.

	Общее для очереди и отложенных записей: логотип отвечает на «в какой
	канал», метка — на «когда».
	"""
	return [
		entity_avatar(parent, community_id, community_title, avatar_path, LOGO_SIZE),
		slot_chip(when, parent),
	]


def queue_leading(item: QueueItemDto, parent: QWidget, avatar_path: str | None) -> list[QWidget]:
	"""Начало шапки карточки очереди: логотип сообщества и метка слота."""
	return post_leading(item.community_id, item.community_title, item.when, parent, avatar_path)


def apply_view(
	items: list[QueueItemDto],
	sort: QueueSort,
	status: QueueFilter,
	community_id: int | None,
	slot: str | None = None,
) -> list[QueueItemDto]:
	"""Правило показа: фильтры по статусу, каналу и слоту, затем сортировка.

	Args:
		items: видимые элементы очереди (без завершённых).
		sort: порядок показа.
		status: фильтр по статусу.
		community_id: id канала (None — все). Идентичность — по id:
			названия каналов Telegram не уникальны.
		slot: слот времени публикации, «ЧЧ:ММ» или «сейчас»
			(None — все слоты).
	"""
	items = filter_by_slot(filter_by_community(items, community_id), slot)
	if status is QueueFilter.SENDABLE:
		wanted = (JobStatus.PENDING, JobStatus.RUNNING)
		items = [item for item in items if item.status in wanted]
	elif status is QueueFilter.WAITING:
		items = [item for item in items if item.status is JobStatus.WAITING]
	elif status is QueueFilter.ERRORS:
		items = [item for item in items if item.status is JobStatus.ERROR]
	if sort is QueueSort.NEAREST:
		return sort_nearest(items, lambda item: item.id)
	if sort is QueueSort.COMMUNITY:
		return sort_by_community(items, lambda item: item.id)
	return sorted(items, key=lambda item: item.id)


class QueueView(QWidget):
	"""Вся очередь отправки: живой список с сортировкой и фильтрами.

	Опрос очереди идёт, только пока экран виден (:meth:`set_polling`);
	правило показа ставится извне (:meth:`show_filter`) — с дашборда
	сообществ плашкой ошибок (фильтр «ошибки») и кнопкой «Очередь»
	карточки (фильтр по сообществу). Сообщество, которого в очереди нет,
	фильтром не становится — показывается вся очередь.
	"""

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		# аватары сообществ из кэша статистики: карточки рисуют их
		# в шапке, читать их на каждый опрос незачем
		self._avatars: dict[int, str | None] = {}
		self._total = 0
		self._page = 1
		self._view: ListPage[QueueItemDto] = paginate([], 1)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().row_spacing)
		self._bar = ViewBar(self, QueueSort)
		self._status_combo = self._bar.add_choice(list(QueueFilter), QueueFilter.ALL)
		self._bar.changed.connect(self._on_view_changed)
		layout.addLayout(self._bar.layout)
		box = QVBoxLayout()
		box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(box)
		self._pager = PagerRow(self, self._step)
		layout.addLayout(self._pager.layout)
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
			leading=lambda item, parent: queue_leading(
				item, parent, self._avatars.get(item.community_id)
			),
		)
		self._panel.set_polling(False)  # включит экран, когда станет виден
		run_in_engine(
			worker,
			worker.engine.community_stats.snapshot(),
			self,
			self._apply_avatars,
			# аватар — украшение шапки: без него карточка рисует букву
			noop,
		)

	def set_polling(self, active: bool) -> None:
		"""Опрос очереди — только пока экран виден."""
		self._panel.set_polling(active)

	def show_filter(self, community_id: int | None, status: QueueFilter | None = None) -> None:
		"""Ставит правило показа извне: сообщество и/или статус.

		``community_id`` None — все сообщества; ``status`` None — не менять.
		"""
		if status is not None:
			self._status_combo.setCurrentIndex(list(QueueFilter).index(status))
		self._bar.want_community(community_id)
		self._page = 1
		self._panel.poll()

	def _apply_avatars(self, stats: list[CommunityStatsDto]) -> None:
		"""Раскладывает аватары сообществ и перерисовывает шапки карточек."""
		self._avatars = {item.community_id: item.avatar_path for item in stats}
		self._panel.refresh_leading()

	def _fill_editor(self, item_id: int, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет раскрытую карточку формой правки (ADR-0016, п. 7)."""
		mount_queue_item_editor(self._worker, self, item_id, body, collapse, self._panel.poll)

	# --- правило показа --------------------------------------------------------

	def _apply_view(self, items: list[QueueItemDto]) -> list[QueueItemDto]:
		"""Крючок панели: правило показа и нарезка на страницы.

		Снимок страницы сохраняется целиком (:attr:`_view`): по нему
		рисуются сводка и перелистывание. Зажатый номер возвращается
		в :attr:`_page` — очередь живая, и страница, на которую смотрит
		пользователь, может исчезнуть под ним.
		"""
		self._total = len(items)
		self._bar.refresh(items)
		status = list(QueueFilter)[int(self._status_combo.currentIndex())]
		sort = QueueSort(self._bar.sort_option())
		shown = apply_view(items, sort, status, self._bar.community_id(), self._bar.slot_value())
		self._view = paginate(shown, self._page)
		self._page = self._view.page
		return self._view.items

	def _step(self, delta: int) -> None:
		"""Листает страницу; показ обновляется сразу, не по таймеру."""
		self._page = step_page(self._page, delta, self._view.pages)
		self._panel.poll()

	def _on_view_changed(self) -> None:
		"""Правило показа сменилось: листаем с начала, следующий опрос его применит."""
		self._page = 1  # набор изменился — листаем с начала
		self._panel.poll()  # показ обновляется сразу, не по таймеру

	def _update_summary(self, _shown: list[QueueItemDto]) -> None:
		"""Итоговая строка и состояние перелистывания.

		Считается по снимку страницы (:attr:`_view`), а не по списку
		показанных: номера элементов в общем счёте знает только он.
		"""
		self._pager.update(self._view, summary_text(self._view, self._total, QUEUE_WORDS))
