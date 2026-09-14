"""Панель очереди движка: опрос, статусы, прогресс, действия над заданиями.

Надстройка над общим списком карточек (:mod:`card_list`) для **любой
очереди движка на каркасе заданий** (ADR-0025): отправка постов
(ADR-0016; механика показа унаследована от ADR-0012), обработка видео
(ADR-0014), обслуживание сообщества (ADR-0026). Панель добавляет
к списку то, что есть только у очереди: таймер опроса, реакцию
на завершённые задания и снятие их с показа, признаки занятости,
кнопки «Отмена» / «Повторить» / «Убрать» и полосу прогресса.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, PushButton, TransparentToolButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.card_list import CardList
from pxcontrol.ui.pages.common import bind, error_reporter, list_button, noop, open_in_system

#: Период опроса состояния очередей движка (мс). Опрос вместо событий —
#: осознанный дизайн ADR-0016 (унаследован от ADR-0012): интерфейс читает снимок состояния.
QUEUE_POLL_MS = 500


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
	"""Панель очереди движка: опрос, карточки, прогресс, действия.

	Панель даёт таймер опроса, снятие завершённых с показа и точечное
	обновление карточек (общий список :class:`CardList` — меняется
	только то, что изменилось). Опрос живёт всегда, не только при
	видимой странице: завершения снимаются с показа, а кэш занятости
	нужен окну для подтверждения выхода.

	Карточка элемента может раскрываться формой правки прямо в списке
	(ADR-0016, п. 7): крючки ``editable`` и ``fill_body`` задаёт
	владелец панели. Очередь обработки видео их не передаёт — правки
	у неё нет, и её карточки не раскрываются.

	Контракт сервиса очереди (все три очереди движка ему следуют): корутины
	``state()``, ``cancel(id)``, ``retry(id)``, ``dismiss(id)``; элементы
	с полями ``id``, ``status`` (общий :class:`JobStatus`, ADR-0025 —
	панель спрашивает его признаками ``active()`` / ``finished()`` /
	``left_queue()``, а не сверяет имена), ``progress``, ``title``,
	``error``.
	Необязательные поля ``note`` (пометка состояния: авто-битрейт
	у обработки видео, флуд-пауза у отправки) и ``media_path`` (путь
	вложения: карточка даёт посмотреть файл) панель читает через
	``getattr`` — сервису без них ничего делать не нужно.
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
		dismiss_finished: ``False`` — панель-зритель (полный просмотр):
			завершёнными владеет панель страницы, зритель их только
			показывает. Две панели над одной очередью не должны
			наперегонки снимать элементы — иначе итоговые плашки
			страницы теряются.
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
		self._service = service
		self._on_finished = on_finished
		self._on_refreshed = on_refreshed
		self._on_drained = on_drained
		self._max_cards = max_cards
		self._transform = transform
		self._dismiss_finished = dismiss_finished
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
		self._handled: set[int] = set()  # завершённые, уже учтённые
		self._busy = False
		self._active = False
		self._timer = QTimer(page)
		self._timer.setInterval(QUEUE_POLL_MS)
		self._timer.timeout.connect(self.poll)
		self._timer.start()

	def set_polling(self, active: bool) -> None:
		"""Включает или приостанавливает опрос (панель на невидимой вкладке).

		Возобновление опрашивает сразу, не дожидаясь тика: человек
		открыл вкладку и ждёт свежих карточек.
		"""
		if active:
			if not self._timer.isActive():
				self._timer.start()
				self.poll()
		else:
			self._timer.stop()

	def busy(self) -> bool:
		"""Есть ли незавершённое в очереди (включая ждущих)."""
		return self._busy

	def active(self) -> bool:
		"""Идёт ли работа прямо сейчас (загрузка или обработка).

		В отличие от :meth:`busy`, ждущие элементы не в счёт: для
		персистентной очереди отправки (ADR-0016) вопрос при закрытии
		окна заслуживает только обрыв активной загрузки — ожидающие
		посты выход переживают.
		"""
		return self._active

	def refresh_leading(self) -> None:
		"""Перерисовывает начала шапок всех карточек (приехали аватары)."""
		self._list.refresh_leading()

	def poll(self) -> None:
		"""Запрашивает состояние очереди (по таймеру и после постановки)."""
		# ошибки опроса не показываем плашками: мост пишет их в лог,
		# а раз в полсекунды спамить пользователя нечем и незачем
		run_in_engine(self._worker, self._service().state(), self.page, self._show, noop)

	def dismiss(self, item_id: int, *, silent: bool = False) -> None:
		"""Убирает завершённый элемент из состояния очереди.

		``silent`` — снятие автоматическое (панель убирает завершённые
		сама): о таком человеку говорить нечего, след останется в логе.
		Нажатие «Убрать» — не автоматика: движок при нём двигает файл
		из папки очереди обратно в результаты и может отказать, и тогда
		молчание оставило бы человека в уверенности, что всё убрано.
		"""
		run_in_engine(
			self._worker,
			self._service().dismiss(item_id),
			self.page,
			lambda *_a: self.poll(),
			noop if silent else self._show_error,
		)

	def retry(self, item_id: int) -> None:
		"""Просит движок вернуть элемент с ошибкой в очередь на повтор."""
		run_in_engine(
			self._worker,
			self._service().retry(item_id),
			self.page,
			lambda *_a: self.poll(),  # карточка обновляется сразу, не по таймеру
			self._show_error,
		)

	def cancel(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди."""
		run_in_engine(
			self._worker,
			self._service().cancel(item_id),
			self.page,
			noop,
			self._show_error,
		)

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
		"""Обновляет панель; завершённые получают реакцию и снимаются с показа."""
		visible: list[Any] = []
		for item in items:
			if item.status.left_queue():
				self._finish(item, done=item.status is JobStatus.DONE)
			else:
				visible.append(item)
		# id, исчезнувшие из состояния движка (после dismiss), больше
		# не встретятся — набор «уже учтённых» не растёт бесконечно
		self._handled &= {item.id for item in items}
		busy = any(not item.status.finished() for item in visible)
		if self._busy and not busy and self._on_drained is not None:
			self._on_drained(visible)
		self._busy = busy
		self._active = any(item.status.active() for item in visible)
		if self._transform is not None:
			visible = self._transform(visible)
		self._list.sync(visible if self._max_cards is None else visible[: self._max_cards])
		if self._on_refreshed is not None:
			self._on_refreshed(visible)

	def _finish(self, item: Any, done: bool) -> None:
		"""Разовая реакция на завершённый элемент и снятие его с показа.

		Снятие асинхронное, до него элемент успевает попасть в опрос ещё
		раз-другой — набор «уже учтённых» защищает от повторной реакции.
		"""
		if item.id in self._handled:
			return
		self._handled.add(item.id)
		if self._on_finished is not None:
			self._on_finished(item, done)
		if self._dismiss_finished:
			self.dismiss(item.id, silent=True)

	def _actions(self, item: Any, parent: QWidget) -> list[QWidget]:
		"""Кнопки шапки под текущий статус элемента."""
		widgets: list[QWidget] = []
		media_path = getattr(item, "media_path", None)
		if media_path:
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
