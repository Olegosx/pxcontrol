"""Страница «Расписание»: отложенные записи каналов из Telegram.

Источник истины — сам канал (ADR-0010): список отложенных читается
из Telegram, править и удалять их можно из любого клиента Telegram.
Создание постов — на странице «Публикация». Фильтр по каналам —
презентационный: скрывает карточки, не меняя загруженный список.
"""

from __future__ import annotations

from functools import partial
from time import monotonic

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CardWidget,
	CheckBox,
	FluentIcon,
	PushButton,
	ScrollArea,
	StrongBodyLabel,
	SubtitleLabel,
	themeColor,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import ScheduledList, ScheduledPostDto
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import clear_layout, error_reporter, format_local, page_layout

#: Сколько список считается свежим: повторный показ вкладки в этот срок
#: не запускает новый обход Telegram. Минута — переключился и вернулся,
#: а отложенные за это время меняются редко (их создаёт сам человек).
_FRESH_FOR_S = 60.0


class SchedulePage(ScrollArea):
	"""Отложенные записи каналов (читаются из Telegram)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("schedule")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._items: list[ScheduledPostDto] = []
		# сообщества, чьи отложенные прочитать не удалось: пустой список
		# при непустом наборе означает «не спросили», а не «записей нет»
		self._unread: tuple[str, ...] = ()
		# снятые галки фильтра (id каналов): выбор переживает «Обновить».
		# Оговорка: канал, пропавший из списка и вернувшийся позже,
		# останется скрытым, пока галку не поставят заново, — набор
		# по текущему списку не чистится намеренно (временное отсутствие
		# отложенных не должно сбрасывать выбор пользователя)
		self._unchecked: set[int] = set()
		# идёт ли обход прямо сейчас и когда он закончился в прошлый раз:
		# показ вкладки не должен запускать второй обход поверх первого
		self._loading = False
		self._loaded_at: float | None = None
		self._build()
		# первичной загрузки здесь нет: её делает showEvent при первом
		# показе — Telegram не опрашивается, пока страницу не открыли

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — имя Qt
		"""Перечитывает список при показе страницы, но не чаще нужного.

		Истина — сам канал (ADR-0010): отложенные создаются на соседней
		«Публикации» и правятся из любого клиента Telegram, поэтому
		страница обновляется при открытии, как «Видео» и «Публикация».

		Но обход дорогой: он опрашивает **каждое** включённое сообщество,
		а группу — каждым её участником (ADR-0022). Безусловная
		перезагрузка означала, что праздное листание вкладок ставит
		обходы один поверх другого; флуд-лимит, пойманный на таком
		обходе, замораживает дорожку аккаунта целиком — и ждать его
		будет уже публикация (ADR-0024). Поэтому свежий список
		не перечитывается, а идущий обход не дублируется; кнопка
		«Обновить» перечитывает всегда — это явная воля человека.
		"""
		super().showEvent(event)
		if self._loading:
			return
		fresh = self._loaded_at is not None and monotonic() - self._loaded_at < _FRESH_FOR_S
		if not fresh:
			self._reload()

	def _build(self) -> None:
		"""Шапка с кнопками, фильтр по каналам и область списка."""
		layout = page_layout(self)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Отложенные записи", self))
		header.addStretch()
		refresh = PushButton(FluentIcon.SYNC, "Обновить", self)
		refresh.clicked.connect(self._reload)
		header.addWidget(refresh)
		layout.addLayout(header)
		hint = CaptionLabel(
			"Список читается из Telegram. Создание постов — на странице "
			"«Публикация»; править и удалять отложенные можно из любого "
			"клиента Telegram.",
			self,
		)
		layout.addWidget(hint)
		self._filter_box = QHBoxLayout()
		self._filter_box.setSpacing(density.spacing().row_spacing)
		layout.addLayout(self._filter_box)
		self._list = QVBoxLayout()
		self._list.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._list)
		layout.addStretch()

	# --- список отложенных (из Telegram) ---------------------------------------

	def _reload(self) -> None:
		"""Запускает обход Telegram (кнопка «Обновить» и первый показ)."""
		self._loading = True
		run_in_engine(
			self._worker,
			self._worker.engine.posts.list_scheduled(),
			self,
			self._show_scheduled,
			self._on_failed,
		)

	def _on_failed(self, message: str) -> None:
		"""Обход не удался: показываем причину и снимаем признак «идёт»."""
		self._loading = False
		self._show_error(message)

	def _show_scheduled(self, scheduled: ScheduledList) -> None:
		"""Принимает свежий список: перестраивает фильтр и карточки."""
		self._loading = False
		self._loaded_at = monotonic()
		self._items = scheduled.items
		self._unread = scheduled.unread
		self._rebuild_filter()
		self._render()

	# --- фильтр по каналам -------------------------------------------------------

	def _rebuild_filter(self) -> None:
		"""Строка чекбоксов: по одному на канал с отложенными записями.

		Состояние галок хранится по id канала и переживает обновление
		списка; канал, исчезнувший из списка, пропадает и из фильтра.
		"""
		clear_layout(self._filter_box)
		communities: dict[int, str] = {}
		for item in self._items:
			communities.setdefault(item.community_id, item.community_title)
		if not communities:
			return
		self._filter_box.addWidget(CaptionLabel("Показывать:", self))
		for community_id, title in communities.items():
			box = CheckBox(title, self)
			box.setChecked(community_id not in self._unchecked)
			box.toggled.connect(partial(self._on_filter_toggled, community_id))
			self._filter_box.addWidget(box)
		self._filter_box.addStretch()

	def _on_filter_toggled(self, community_id: int, checked: bool) -> None:
		"""Галка канала: показывает/скрывает его карточки (без перезагрузки)."""
		if checked:
			self._unchecked.discard(community_id)
		else:
			self._unchecked.add(community_id)
		self._render()

	# --- карточки ----------------------------------------------------------------

	def _render(self) -> None:
		"""Перерисовывает карточки с учётом фильтра."""
		clear_layout(self._list)
		if self._unread:
			# честность важнее краткости: часть сообществ опросить
			# не удалось, и список заведомо неполон (ADR-0010 —
			# истина живёт на сервере Telegram)
			names = ", ".join(f"«{title}»" for title in self._unread)
			warning = CaptionLabel(f"Не удалось прочитать отложенные: {names}.", self)
			warning.setWordWrap(True)
			self._list.addWidget(warning)
		if not self._items:
			self._list.addWidget(
				CaptionLabel(
					"Отложенных записей нет. Создайте пост на странице «Публикация»."
					if not self._unread
					else "У остальных сообществ отложенных записей нет.",
					self,
				)
			)
			return
		visible = [item for item in self._items if item.community_id not in self._unchecked]
		if not visible:
			self._list.addWidget(
				CaptionLabel(
					"Всё скрыто фильтром — включите хотя бы одно сообщество.",
					self,
				)
			)
			return
		for item in visible:
			self._list.addWidget(self._item_row(item))

	def _item_row(self, item: ScheduledPostDto) -> CardWidget:
		"""Карточка записи: момент публикации — первой строкой, акцентом.

		Время показывается местное (хранится UTC, как отдаёт Telegram).
		Цвет — акцентный цвет темы (``setTextColor`` перекрашивает и при
		смене темы, в отличие от жёсткого стиля).
		"""
		card = CardWidget(self)
		box = QVBoxLayout(card)
		box.setContentsMargins(*density.spacing().card_margins)
		box.setSpacing(2)
		when = StrongBodyLabel(format_local(item.scheduled_at), card)
		when.setTextColor(themeColor(), themeColor())
		box.addWidget(when)
		text = BodyLabel(item.text_preview, card)
		text.setWordWrap(True)
		box.addWidget(text)
		box.addWidget(CaptionLabel(item.community_title, card))
		return card
