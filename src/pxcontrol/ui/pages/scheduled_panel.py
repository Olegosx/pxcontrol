"""Панель отложенных записей сервера Telegram — список как у очереди.

Отложенные записи живут на сервере Telegram (ADR-0010), у них нет
статусов и прогресса, а чтение списка — обход всех сообществ (группу —
каждым участником, ADR-0022). Поэтому панель устроена как очередь
по виду и действиям (тот же :class:`CardList`: логотип сообщества
и метка слота в шапке, раскрытие в форму правки), но не по обновлению:
опроса по таймеру нет, список перечитывается при показе, кнопкой
и **после каждого действия — только у затронутого сообщества**
(:meth:`ScheduledPanel.reload_community`), чтобы не ловить флуд-лимит
на полном обходе.

Действия карточки: «Сейчас» (запись уходит в ленту) и «Удалить»
(без публикации) — оба с подтверждением, оба необратимы; правка текста
и времени — в раскрытой карточке (:mod:`scheduled_edit`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from functools import partial
from time import monotonic
from typing import Any

from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import PushButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.posts import ScheduledList, ScheduledPostDto
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.card_list import CardList
from pxcontrol.ui.pages.common import (
	bind,
	confirm_delete,
	error_reporter,
	format_local,
	kind_label,
	list_button,
)
from pxcontrol.ui.pages.list_view import (
	ListWords,
	filter_by_community,
	filter_by_slot,
	sort_by_community,
	sort_nearest,
)
from pxcontrol.ui.pages.scheduled_edit import mount_scheduled_editor

#: Слова итоговой строки под списком отложенных.
SCHEDULED_WORDS = ListWords(
	empty="Отложенных записей нет.", of_all="отложенных записей", within="отложено"
)

#: Сколько список считается свежим: повторный показ в этот срок
#: не запускает новый обход Telegram. Минута — переключился и вернулся,
#: а отложенные за это время меняются редко (их создаёт сам человек).
FRESH_FOR_S = 60.0


class ScheduledSort(StrEnum):
	"""Порядок показа отложенных (подписи — пункты списка).

	«Порядка постановки» здесь нет: у записи на сервере нет момента
	постановки, только момент публикации.
	"""

	NEAREST = "Ближайшие сначала"
	COMMUNITY = "По сообществам"


def scheduled_key(item: ScheduledPostDto) -> tuple[int, int]:
	"""Ключ карточки: id записи уникален только внутри своего сообщества."""
	return (item.community_id, item.message_id)


def scheduled_signature(item: ScheduledPostDto) -> tuple[Any, ...]:
	"""Отпечаток записи: всё, что карточка показывает.

	Аккаунт-читатель не входит: он не виден на карточке, а в группе
	с несколькими читателями одна и та же запись могла бы приходить
	то от одного, то от другого.
	"""
	return (
		item.text_preview,
		item.scheduled_at,
		item.media_kind,
		item.community_title,
		item.topic_id,
	)


def scheduled_subtitle(item: ScheduledPostDto, *, with_community: bool = True) -> str:
	"""Подпись карточки отложенной записи: сообщество, момент, вид вложения.

	Момент — в местном времени (хранится в UTC, как отдаёт Telegram).
	``with_community=False`` — на странице сообщества, где название
	и так в шапке.
	"""
	kind = "текст" if item.media_kind is MediaKind.NONE else kind_label(item.media_kind).lower()
	subtitle = f"публикация: {format_local(item.scheduled_at)} · {kind}"
	if item.markup_promised:
		# кнопки у отложенной записи существовать не могут (их ставит бот
		# и только после выхода, ADR-0031) — человек должен знать, что
		# они обещаны, а не решить, что потерялись
		subtitle = f"{subtitle} · кнопки появятся после выхода"
	if with_community:
		subtitle = f"{item.community_title} · {subtitle}"
	return subtitle


def merge_community(
	items: Sequence[ScheduledPostDto], community_id: int, fresh: Sequence[ScheduledPostDto]
) -> list[ScheduledPostDto]:
	"""Заменяет записи одного сообщества свежими, остальные не трогает.

	После действия над записью перечитывается только её сообщество:
	полный обход дорог (ADR-0024), а истина о других сообществах
	за секунду не изменилась. Свежие записи встают на место старых —
	порядок показа всё равно задаёт правило показа.
	"""
	kept = [item for item in items if item.community_id != community_id]
	return kept + list(fresh)


def apply_scheduled_view(
	items: Sequence[ScheduledPostDto],
	sort: ScheduledSort,
	community_id: int | None,
	slot: str | None = None,
) -> list[ScheduledPostDto]:
	"""Правило показа отложенных: фильтры по сообществу и слоту, сортировка."""
	shown = filter_by_slot(filter_by_community(items, community_id), slot)
	if sort is ScheduledSort.COMMUNITY:
		return sort_by_community(shown, lambda item: item.message_id)
	return sort_nearest(shown, lambda item: item.message_id)


class ScheduledPanel:
	"""Панель отложенных записей: чтение с сервера, карточки, действия.

	Контракт с владельцем — как у панели очереди: компоновка для карточек,
	подпись, правило показа (``transform``), начало шапки (``leading``),
	крючки о ходе чтения (``on_loading`` / ``on_loaded``) и о показанном
	(``on_refreshed``). Сама панель не рисует ни строки состояния,
	ни предупреждения о непрочитанных сообществах — это место владельца.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		page: QWidget,
		box: QVBoxLayout,
		*,
		subtitle: Callable[[ScheduledPostDto], str] = scheduled_subtitle,
		community_id: int | None = None,
		transform: Callable[[list[ScheduledPostDto]], list[ScheduledPostDto]] | None = None,
		on_loading: Callable[[], None] | None = None,
		on_loaded: Callable[[ScheduledList], None] | None = None,
		on_refreshed: Callable[[list[ScheduledPostDto]], None] | None = None,
		leading: Callable[[ScheduledPostDto, QWidget], list[QWidget]] | None = None,
		compact: bool = False,
	) -> None:
		"""Args:
		worker: мост к движку.
		page: страница-владелец (родитель карточек и плашек).
		box: компоновка, в которую панель складывает карточки.
		subtitle: подпись карточки записи.
		community_id: только это сообщество (страница сообщества);
			None — все включённые (страница «Расписание»).
		transform: правило показа — сортировка/фильтр/страницы; зовётся
			при каждой перерисовке. Смена правила — :meth:`refresh_view`.
		on_loading: начался обход Telegram (владелец показывает «Читаю…»).
		on_loaded: обход кончился — полный список и непрочитанные сообщества.
		on_refreshed: список перерисован — что показано (после ``transform``).
		leading: виджеты в начале шапки (логотип сообщества, метка слота).
		compact: карточки по макету страницы сообщества.
		"""
		self._worker = worker
		self.page = page
		self._community_id = community_id
		self._transform = transform
		self._on_loading = on_loading
		self._on_loaded = on_loaded
		self._on_refreshed = on_refreshed
		self._compact = compact
		self._show_error = error_reporter(page)
		#: полный список (до правила показа) и сообщества, которые
		#: прочитать не удалось (ADR-0010: неполный список назван неполным)
		self.items: list[ScheduledPostDto] = []
		self.unread: tuple[str, ...] = ()
		self._loading = False
		self._loaded_at: float | None = None
		self._list = CardList(
			page,
			box,
			subtitle=subtitle,
			signature=scheduled_signature,
			key=scheduled_key,
			leading=leading,
			actions=self._actions,
			editable=lambda _item: True,
			fill_body=self._fill_editor,
			compact=compact,
			lost_edit_text="Запись покинула отложенные — незаконченная правка не сохранена.",
		)

	# --- чтение ------------------------------------------------------------------

	def loading(self) -> bool:
		"""Идёт ли обход Telegram прямо сейчас."""
		return self._loading

	def fresh(self) -> bool:
		"""Свежий ли список (моложе :data:`FRESH_FOR_S`)."""
		return self._loaded_at is not None and monotonic() - self._loaded_at < FRESH_FOR_S

	def reload(self) -> None:
		"""Перечитывает список целиком (первый показ, кнопка «Обновить»).

		Идущий обход не дублируется: праздное листание вкладок ставило
		бы обходы один поверх другого, а пойманный на таком обходе
		флуд-лимит замораживает дорожку аккаунта целиком (ADR-0024) —
		ждать его будет уже публикация.
		"""
		if self._loading:
			return
		self._loading = True
		if self._on_loading is not None:
			self._on_loading()
		run_in_engine(
			self._worker,
			self._worker.engine.posts.list_scheduled(self._community_id),
			self.page,
			self._show_all,
			self._on_failed,
		)

	def reload_community(self, community_id: int) -> None:
		"""Перечитывает одно сообщество после действия над его записью.

		Истина живёт на сервере (ADR-0010): после правки, «сейчас»
		или удаления список этого сообщества берётся заново, а не
		правится локально по предположению об исходе.
		"""
		run_in_engine(
			self._worker,
			self._worker.engine.posts.list_scheduled(community_id),
			self.page,
			lambda scheduled: self._show_community(community_id, scheduled),
			self._show_error,
		)

	def refresh_view(self) -> None:
		"""Перерисовывает список по текущему правилу показа (без сети)."""
		shown = self.items if self._transform is None else self._transform(list(self.items))
		self._list.sync(shown)
		if self._on_refreshed is not None:
			self._on_refreshed(shown)

	def refresh_leading(self) -> None:
		"""Перерисовывает начала шапок (приехали аватары сообществ)."""
		self._list.refresh_leading()

	def _show_all(self, scheduled: ScheduledList) -> None:
		self._loading = False
		self._loaded_at = monotonic()
		self.items = list(scheduled.items)
		self.unread = scheduled.unread
		if self._on_loaded is not None:
			self._on_loaded(scheduled)
		self.refresh_view()

	def _show_community(self, community_id: int, scheduled: ScheduledList) -> None:
		self.items = merge_community(self.items, community_id, scheduled.items)
		if scheduled.unread:
			# сообщество не прочиталось — честно сказать, а не оставить
			# старые записи как будто свежие
			self._show_error(f"Не удалось перечитать отложенные: {', '.join(scheduled.unread)}.")
		if self._on_loaded is not None:
			self._on_loaded(ScheduledList(items=list(self.items), unread=self.unread))
		self.refresh_view()

	def _on_failed(self, message: str) -> None:
		"""Обход не удался: причина человеку, признак «идёт» снимается."""
		self._loading = False
		self._show_error(message)
		if self._on_loaded is not None:
			self._on_loaded(ScheduledList(items=list(self.items), unread=self.unread))

	# --- действия -----------------------------------------------------------------

	def _actions(self, item: ScheduledPostDto, parent: QWidget) -> list[QWidget]:
		"""Кнопки шапки: «Сейчас» и «Удалить» — оба необратимы."""
		now = self._button("Сейчас", parent)
		now.setToolTip("Опубликовать запись прямо сейчас — она уйдёт в ленту сообщества")
		now.clicked.connect(bind(self._send_now, item))
		delete = self._button("Удалить", parent)
		delete.setToolTip("Убрать запись из отложенных, не публикуя")
		delete.clicked.connect(bind(self._delete, item))
		return [now, delete]

	def _button(self, text: str, parent: QWidget) -> QPushButton:
		button: QPushButton = (
			list_button(text, parent) if self._compact else PushButton(text, parent)
		)
		return button

	def _send_now(self, item: ScheduledPostDto) -> None:
		"""«Сейчас»: подтверждение, затем публикация и перечитывание сообщества."""
		if not confirm_delete(
			self.page,
			f"Опубликовать «{item.text_preview}» в «{item.community_title}» прямо сейчас? "
			"Запись уйдёт в ленту и из отложенных исчезнет.",
			"Опубликовать",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.posts.send_scheduled_now(item.ref),
			self.page,
			lambda *_a: self.reload_community(item.community_id),
			partial(self._on_action_failed, item.community_id),
		)

	def _delete(self, item: ScheduledPostDto) -> None:
		"""«Удалить»: подтверждение, затем удаление и перечитывание сообщества."""
		if not confirm_delete(
			self.page,
			f"Удалить отложенную запись «{item.text_preview}» "
			f"из «{item.community_title}»? Она не будет опубликована.",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.posts.delete_scheduled(item.ref),
			self.page,
			lambda *_a: self.reload_community(item.community_id),
			partial(self._on_action_failed, item.community_id),
		)

	def _on_action_failed(self, community_id: int, message: str) -> None:
		"""Отказ: причина человеку и перечитывание — истина могла измениться.

		Записи уже нет (опубликована или удалена из другого клиента) —
		ровно тот случай, когда карточка должна исчезнуть, а не остаться
		с кнопками, ведущими в никуда.
		"""
		self._show_error(message)
		self.reload_community(community_id)

	def _fill_editor(
		self, item: ScheduledPostDto, body: QVBoxLayout, collapse: Callable[[], None]
	) -> None:
		"""Наполняет раскрытую карточку формой правки текста и времени."""
		mount_scheduled_editor(
			self._worker,
			self.page,
			item,
			body,
			collapse,
			bind(self.reload_community, item.community_id),
		)
