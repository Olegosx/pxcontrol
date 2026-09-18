"""Панель очереди движка: показ карточек, прогресс, действия над заданиями.

Надстройка над общим списком карточек (:mod:`card_list`) для **любой
очереди движка на каркасе заданий** (ADR-0025): отправка постов
(ADR-0016; механика показа унаследована от ADR-0012), обработка видео
(ADR-0014), обслуживание сообщества (ADR-0026). Панель добавляет
к списку то, что есть только у очереди: статусы заданий, полосу
прогресса и кнопки «Отмена» / «Повторить» / «Убрать».

Опрос движка, снятие завершённых и признаки занятости живут не здесь,
а в :class:`~pxcontrol.ui.queue_watcher.QueueWatcher`: показ и владение
очередью — разные работы, и владелец не должен зависеть от того, открыт
ли экран (ADR-0032). Панель держит свой наблюдатель и обращается к нему
за снимком очереди и за действиями над заданием.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PushButton, TransparentToolButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.ui.pages.card_list import CardList
from pxcontrol.ui.pages.common import bind, error_reporter, list_button, open_in_system
from pxcontrol.ui.queue_watcher import QueueWatcher


def file_view_shown(status: JobStatus) -> bool:
	"""Показывать ли кнопку просмотра вложения на карточке очереди.

	У отправляющегося поста — нет. Переименование файла применяется
	подготовкой публикации (``prepare_publish``) **до** загрузки,
	и с этого мгновения путь, который несёт карточка, указывает
	на несуществующее имя: кнопка открыла бы «файл не найден». Пока
	пост ждёт, путь верен и кнопка полезна — это единственный способ
	увидеть, что именно уйдёт (файл уже уехал из «Готовых видео»).

	У элемента с ошибкой кнопка остаётся: до переименования доходит
	не всякая неудачная попытка, а если дошла — просмотр честно скажет,
	что файла по этому пути нет.
	"""
	return not status.active()


def queue_signature(item: Any) -> tuple[Any, ...]:
	"""Отпечаток элемента очереди — по нему решается обновление его карточки.

	Входит всё, что карточка показывает и на что вешает действия:
	статус, заголовок, текст ошибки, пометка состояния и путь вложения.
	Заголовок и путь тут не для красоты: правка элемента очереди
	(ADR-0016, п. 7) меняет их, не трогая статуса, — без них карточка
	осталась бы со старым именем, а кнопка просмотра вела бы
	на прежний файл. Момент публикации — по той же причине: он задаёт
	метку слота в шапке. Прогресс не входит: он обновляется точечно,
	без участия отпечатка.
	"""
	return (
		item.status,
		item.title,
		item.error,
		getattr(item, "note", None),
		getattr(item, "media_path", None),
		getattr(item, "when", None),
	)


class QueuePanel:
	"""Панель очереди движка: карточки, прогресс, действия.

	Панель даёт точечное обновление карточек (общий список
	:class:`CardList` — меняется только то, что изменилось) и кнопки
	действий; опрос движка и реакцию на завершённые задания ведёт
	её наблюдатель (:class:`~pxcontrol.ui.queue_watcher.QueueWatcher`).

	Карточка элемента может раскрываться формой правки прямо в списке
	(ADR-0016, п. 7): крючки ``editable`` и ``fill_body`` задаёт
	владелец панели. Очередь обработки видео их не передаёт — правки
	у неё нет, и её карточки не раскрываются.

	Контракт сервиса очереди — в докстринге наблюдателя; сверх него
	панель читает у элемента ``title``, ``error``, ``progress``
	и необязательные ``note`` (пометка состояния: авто-битрейт
	у обработки видео, флуд-пауза у отправки) и ``media_path`` (путь
	вложения: карточка даёт посмотреть файл) — через ``getattr``,
	сервису без них ничего делать не нужно.
	"""

	def __init__(
		self,
		worker: EngineWorker,
		page: QWidget,
		box: QVBoxLayout,
		*,
		service: Callable[[], Any],
		subtitle: Callable[[Any], str],
		on_finished: Callable[[Any, bool], None] | None = None,
		on_refreshed: Callable[[list[Any]], None] | None = None,
		on_drained: Callable[[list[Any]], None] | None = None,
		max_cards: int | None = None,
		transform: Callable[[list[Any]], list[Any]] | None = None,
		dismiss_finished: bool = True,
		editable: Callable[[Any], bool] | None = None,
		fill_body: Callable[[int, QVBoxLayout, Callable[[], None]], None] | None = None,
		leading: Callable[[Any, QWidget], list[QWidget]] | None = None,
		compact: bool = False,
	) -> None:
		"""Args:
		worker: мост к движку.
		page: страница-владелец (родитель карточек, таймера, плашек ошибок).
		box: компоновка, в которую панель складывает карточки.
		service: провайдер сервиса очереди (``lambda: worker.engine.…``).
		subtitle: подпись карточки для элемента.
		on_finished: разовая реакция на завершённый элемент
			(``True`` — готово, ``False`` — отменено) до снятия с показа.
		on_refreshed: вызывается после каждого обновления со списком
			показанных элементов (после ``transform``; сводка очереди,
			при ``max_cards`` — место сказать «и ещё N»).
		on_drained: вызывается с видимым остатком, когда занятость
			кончилась (итоговая плашка вместо плашки на каждый файл).
		max_cards: не больше стольких карточек на странице (None — все);
			длинный хвост ждущих (ADR-0016) не раздувает страницу.
		transform: правило показа — сортировка/фильтр видимого списка
			(полный просмотр очереди); занятость считается до него,
			по нефильтрованному списку. Смена правила отражается
			следующим опросом — после неё зовите :meth:`poll`.
		dismiss_finished: ``False`` — панель-зритель: завершёнными
			владеет кто-то другой (у очереди отправки — наблюдатель
			главного окна, ADR-0032), зритель их только показывает.
			Два владельца над одной очередью наперегонки снимали бы
			элементы — итоговые плашки при этом теряются.
		editable: можно ли раскрыть карточку элемента (правка). Без него
			карточки не раскрываются вовсе.
		fill_body: наполняет тело раскрытой карточки формой правки —
			получает id элемента, компоновку тела и «свернуть карточку».
			Зовётся один раз, при первом раскрытии.
		leading: виджеты в начале шапки карточки (логотип канала, метка
			слота времени) — получает элемент и родителя. Что именно
			показывать, решает владелец панели: очередь обработки видео
			крючок не передаёт, и её шапки начинаются с названия.
		compact: карточки по макету страницы сообщества — подпись под
			названием, кнопки-обводки 28, полоса прогресса под названием,
			ошибка красит подпись и рамку.
		"""
		self._worker = worker
		#: страница-владелец (нужна владельцам панели для плашек).
		self.page = page
		self._on_refreshed = on_refreshed
		self._max_cards = max_cards
		self._transform = transform
		self._compact = compact
		self._show_error = error_reporter(page)
		self._list = CardList(
			page,
			box,
			subtitle=subtitle,
			signature=queue_signature,
			leading=leading,
			actions=self._actions,
			progress=_progress_of,
			# в компактном режиме ошибка красит подпись и рамку
			alert=lambda item: compact and item.status is JobStatus.ERROR,
			editable=editable,
			fill_body=(
				None
				if fill_body is None
				else lambda item, body, collapse: fill_body(item.id, body, collapse)
			),
			compact=compact,
			lost_edit_text="Пост покинул очередь — незаконченная правка не сохранена.",
		)
		self._watcher = QueueWatcher(
			worker,
			page,
			service=service,
			on_state=self._show,
			on_finished=on_finished,
			on_drained=on_drained,
			dismiss_finished=dismiss_finished,
		)

	def set_polling(self, active: bool) -> None:
		"""Включает или приостанавливает опрос (панель на невидимом экране)."""
		self._watcher.set_polling(active)

	def busy(self) -> bool:
		"""Есть ли незавершённое в очереди (включая ждущих)."""
		return self._watcher.busy()

	def refresh_leading(self) -> None:
		"""Перерисовывает начала шапок всех карточек (приехали аватары)."""
		self._list.refresh_leading()

	def poll(self) -> None:
		"""Запрашивает состояние очереди (после постановки и смены показа)."""
		self._watcher.poll()

	def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из состояния очереди («Убрать»)."""
		self._watcher.dismiss(item_id)

	def retry(self, item_id: int) -> None:
		"""Просит движок вернуть элемент с ошибкой в очередь на повтор."""
		self._watcher.retry(item_id)

	def cancel(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди."""
		self._watcher.cancel(item_id)

	def play(self, path: str) -> None:
		"""Открывает вложение системным приложением.

		Путь проверяется: файл ждущего поста мог уехать или быть удалён
		мимо приложения, а безмолвный щелчок мимо цели выглядит поломкой.
		"""
		if not Path(path).is_file():
			self._show_error(f"Файл не найден: {path}")
			return
		open_in_system(path)

	# --- внутреннее ---------------------------------------------------------

	def _show(self, items: list[Any]) -> None:
		"""Рисует снимок наблюдателя: правило показа, карточки, сводка."""
		visible = items if self._transform is None else self._transform(items)
		self._list.sync(visible if self._max_cards is None else visible[: self._max_cards])
		if self._on_refreshed is not None:
			self._on_refreshed(visible)

	def _actions(self, item: Any, parent: QWidget) -> list[QWidget]:
		"""Кнопки шапки под текущий статус элемента."""
		widgets: list[QWidget] = []
		media_path = getattr(item, "media_path", None)
		if media_path and file_view_shown(item.status):
			# та же кнопка, что у карточек файлов на «Видео» и в пакете
			play = TransparentToolButton(FluentIcon.PLAY, parent)
			play.setToolTip("Посмотреть файл (системный плеер)")
			play.clicked.connect(bind(self.play, media_path))
			widgets.append(play)
		if item.status is JobStatus.ERROR:
			retry = self._button("Повторить", parent)
			retry.clicked.connect(bind(self.retry, item.id))
			widgets.append(retry)
			action = self._button("Убрать", parent)
			action.clicked.connect(bind(self.dismiss, item.id))
		else:
			action = self._button("Отмена", parent)
			action.clicked.connect(bind(self.cancel, item.id))
		widgets.append(action)
		return widgets

	def _button(self, text: str, parent: QWidget) -> QPushButton:
		"""Кнопка шапки: штатная; в компактном режиме — размером строки (28 / 13)."""
		button: QPushButton = (
			list_button(text, parent) if self._compact else PushButton(text, parent)
		)
		return button


def _progress_of(item: Any) -> tuple[float, str] | None:
	"""Прогресс задания: только у активных (WAITING/PENDING не растут)."""
	if not item.status.active():
		return None
	return item.progress, f"{int(item.progress * 100)}% · отправляется"
