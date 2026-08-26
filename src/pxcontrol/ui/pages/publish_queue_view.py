"""Полный просмотр очереди отправки: сортировка и фильтры (ADR-0016).

Страница «Публикация» показывает только ближайшие карточки очереди;
кнопка «Вся очередь…» открывает этот диалог со всеми элементами.
Список живой (опрашивается тем же способом, что панель страницы, —
через :class:`QueuePanel`), действия у карточек те же: «Отмена»
у живых, «Повторить»/«Убрать» у ошибок. Правило показа (фильтр
по статусу и каналу + сортировка) — чистая функция :func:`apply_view`,
она тестируется без Qt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, MessageBoxBase, SubtitleLabel

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.publish_queue import QueueItemDto, QueueItemStatus
from pxcontrol.ui.pages.common import DtoComboBox, QueuePanel, fixed_list_area, format_local

#: Высота списка элементов (прокрутка внутри, а не рост диалога).
_LIST_HEIGHT = 480

#: Служебный первый пункт фильтра по каналу.
_ALL_CHANNELS = "Все каналы"


class QueueSort(StrEnum):
	"""Порядок показа элементов очереди (подписи — пункты списка)."""

	NEAREST = "Ближайшие сначала"  # по дате публикации; «сейчас» — первыми
	ENQUEUED = "Порядок постановки"
	CHANNEL = "По каналам"  # каналы по алфавиту, внутри — по дате


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
	return f"{item.channel_title} · публикация: {when_text} · {status}"


def apply_view(
	items: list[QueueItemDto],
	sort: QueueSort,
	status: QueueFilter,
	channel_id: int | None,
) -> list[QueueItemDto]:
	"""Правило показа: фильтр по статусу и каналу, затем сортировка.

	Args:
		items: видимые элементы очереди (без завершённых).
		sort: порядок показа.
		status: фильтр по статусу.
		channel_id: id канала (None — все). Идентичность — по id:
			названия каналов Telegram не уникальны.
	"""
	if channel_id is not None:
		items = [item for item in items if item.channel_id == channel_id]
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
	if sort is QueueSort.CHANNEL:
		# id в ключе разводит каналы-тёзки, чтобы их посты не перемешивались
		return sorted(
			items,
			key=lambda item: (
				item.channel_title.casefold(),
				item.channel_id,
				item.when or nearest,
				item.id,
			),
		)
	return sorted(items, key=lambda item: item.id)


class QueueViewDialog(MessageBoxBase):
	"""Вся очередь отправки: живой список с сортировкой и фильтрами."""

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__(parent)
		self._sort = QueueSort.NEAREST
		self._status = QueueFilter.ALL
		self._channel: int | None = None
		self._known_channels: list[tuple[int, str]] = []
		self._total = 0
		self.viewLayout.addWidget(SubtitleLabel("Очередь отправки", self))
		self._build_controls()
		area, box = fixed_list_area(self, _LIST_HEIGHT, spacing=8)
		self.viewLayout.addWidget(area)
		self._summary = CaptionLabel("", self)
		self.viewLayout.addWidget(self._summary)
		self.yesButton.setText("Закрыть")
		self.cancelButton.hide()
		self.widget.setMinimumWidth(880)
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
		)

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
		self._channel_combo: DtoComboBox[tuple[int, str]] = DtoComboBox(
			self, placeholder=_ALL_CHANNELS
		)
		self._channel_combo.currentIndexChanged.connect(self._on_view_changed)
		row.addWidget(self._channel_combo)
		row.addStretch()
		self.viewLayout.addLayout(row)

	# --- правило показа --------------------------------------------------------

	def _apply_view(self, items: list[QueueItemDto]) -> list[QueueItemDto]:
		"""Крючок панели: запоминает общее число и применяет правило показа."""
		self._total = len(items)
		self._refresh_channels(items)
		return apply_view(items, self._sort, self._status, self._channel)

	def _refresh_channels(self, items: list[QueueItemDto]) -> None:
		"""Обновляет пункты фильтра канала по каналам, живущим в очереди.

		Пересборка — только при смене набора (каждые полсекунды дёргать
		комбобокс незачем). Восстановление выбора и служебный пункт —
		забота ``DtoComboBox``: выбранный канал сохраняется по id,
		исчезнувший из очереди — сбрасывается на «Все каналы».
		"""
		channels = sorted(
			{(item.channel_id, item.channel_title) for item in items},
			key=lambda entry: (entry[1].casefold(), entry[0]),
		)
		if channels == self._known_channels:
			return
		self._known_channels = channels
		self._channel_combo.set_items(
			channels, label=lambda entry: entry[1], key=lambda entry: entry[0]
		)
		selected = self._channel_combo.selected()
		self._channel = selected[0] if selected is not None else None

	def _on_view_changed(self, _index: int = 0) -> None:
		"""Читает правило показа из списков; следующий опрос его применит."""
		self._sort = list(QueueSort)[int(self._sort_combo.currentIndex())]
		self._status = list(QueueFilter)[int(self._status_combo.currentIndex())]
		selected = self._channel_combo.selected()
		self._channel = selected[0] if selected is not None else None
		self._panel.poll()  # показ обновляется сразу, не по таймеру

	def _update_summary(self, shown: list[QueueItemDto]) -> None:
		"""Итоговая строка: сколько показано из общего числа в очереди."""
		if self._total == 0:
			self._summary.setText("Очередь пуста.")
			return
		self._summary.setText(f"Показано {len(shown)} из {self._total} элементов очереди.")
