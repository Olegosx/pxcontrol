"""Панель очереди движка: показ карточек, прогресс, действия над заданиями.

Надстройка над общим списком карточек (:mod:`card_list`) для **любой
очереди движка на каркасе заданий** (ADR-0025): отправка постов
(ADR-0016; механика показа унаследована от ADR-0012), обработка видео
(ADR-0014), обслуживание сообщества (ADR-0026). Панель добавляет
к списку то, что есть только у очереди: статусы заданий, полосу
прогресса и кнопки «Отмена» / «Повторить» / «Убрать».

Подписка на движок, снятие завершённых и признаки занятости живут
не здесь, а в :class:`~pxcontrol.ui.queue_watcher.QueueWatcher`: показ
и владение очередью — разные работы, и владелец не должен зависеть
от того, открыт ли экран (ADR-0032). Наблюдатель один на очередь
(ADR-0034), панель — его зритель: получает снимок из кэша, пока
её экран виден (:meth:`QueuePanel.set_active`), и обращается к нему
за действиями над заданием.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import (
	DotInfoBadge,
	FluentIcon,
	InfoLevel,
	PushButton,
	TransparentToolButton,
)

from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.telegram.types import TELEGRAM_MAX_SCHEDULED
from pxcontrol.ui.pages.card_list import CardList
from pxcontrol.ui.pages.common import (
	WARNING_TEXT,
	bind,
	error_reporter,
	list_button,
	open_in_system,
)
from pxcontrol.ui.queue_watcher import QueueView, QueueWatcher


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


class SendLight(StrEnum):
	"""Светофор карточки очереди: можно ли прямо сейчас отправить пост."""

	GREEN = "green"  # отправка возможна — пост уйдёт в свой черёд
	YELLOW = "yellow"  # свободного слота отложек у канала нет, пост ждёт
	RED = "red"  # отправка не удалась


#: Подсказка к кружку: цвет без объяснения — ребус.
SEND_LIGHT_HINTS = {
	SendLight.GREEN: "Отправка возможна — пост уйдёт в свой черёд",
	SendLight.YELLOW: (
		f"Нет свободного слота отложенных: у канала их {TELEGRAM_MAX_SCHEDULED}. "
		"Пост ждёт, пока слот освободится"
	),
	SendLight.RED: "Отправить не удалось — причина в подписи карточки",
}


def send_light(status: JobStatus) -> SendLight | None:
	"""Цвет светофора по состоянию элемента (None — кружка нет).

	Светофор отвечает на один вопрос: **есть ли сейчас возможность
	отправить пост в Telegram**. Поэтому зелёный — и у поста «сейчас»
	(слот ему не нужен вовсе), и у отложенного, чей слот уже получен;
	жёлтый — только у ждущего слота (ADR-0016); красный — у ошибки.

	У завершённого задания кружка нет: отправлять уже нечего, а зелёный
	читался бы как «можно отправить».
	"""
	if status is JobStatus.ERROR:
		return SendLight.RED
	if status is JobStatus.WAITING:
		return SendLight.YELLOW
	if status.finished():
		return None
	return SendLight.GREEN


#: Поперечник кружка, пиксели. Библиотечные четыре — это точка-пометка
#: на углу значка, а здесь кружок несёт смысл сам по себе и стоит
#: в ряду кнопок: незаметный индикатор бесполезен ровно так же, как
#: строка мелким шрифтом, которую он заменил.
_LIGHT_DOT_PX = 12


def light_dot(light: SendLight, parent: QWidget) -> DotInfoBadge:
	"""Кружок светофора — штатный ``DotInfoBadge`` (ADR-0023, п. 5).

	Зелёный и красный берутся пресетами уровня, жёлтый — парой цветов
	проекта: у библиотечного ``WARNING`` кружок в тёмной теме почти
	белый и жёлтым не читается. Размер задаётся своим API виджета
	(он рисует круг по своему прямоугольнику), лист стилей библиотеки
	при этом не трогается.
	"""
	if light is SendLight.YELLOW:
		dot = DotInfoBadge.custom(QColor(WARNING_TEXT[0]), QColor(WARNING_TEXT[1]), parent)
	else:
		level = InfoLevel.ERROR if light is SendLight.RED else InfoLevel.SUCCESS
		dot = DotInfoBadge(parent, level)
	dot.setFixedSize(_LIGHT_DOT_PX, _LIGHT_DOT_PX)
	dot.setToolTip(SEND_LIGHT_HINTS[light])
	return dot


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


def actions_signature(item: Any) -> tuple[Any, ...]:
	"""Отпечаток правого края карточки очереди: от чего зависят кнопки.

	Набор кнопок и светофор — от статуса; кнопка просмотра — от наличия
	вложения (и того же статуса). Смена пометки, текста ошибки или
	времени кнопок не меняет — и правый край не пересобирается.
	"""
	return (item.status, getattr(item, "media_path", None) is not None)


class QueuePanel:
	"""Панель очереди движка: карточки, прогресс, действия.

	Панель даёт точечное обновление карточек (общий список
	:class:`CardList` — меняется только то, что изменилось) и кнопки
	действий; снимки очереди и снятие завершённых ведёт наблюдатель
	очереди (:class:`~pxcontrol.ui.queue_watcher.QueueWatcher`), панель
	присоединяется к нему зрителем.

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
		page: QWidget,
		box: QVBoxLayout,
		*,
		watcher: QueueWatcher,
		subtitle: Callable[[Any], str],
		on_finished: Callable[[Any, bool], None] | None = None,
		on_refreshed: Callable[[list[Any]], None] | None = None,
		max_cards: int | None = None,
		transform: Callable[[list[Any]], list[Any]] | None = None,
		editable: Callable[[Any], bool] | None = None,
		fill_body: Callable[[int, QVBoxLayout, Callable[[], None]], None] | None = None,
		leading: Callable[[Any, QWidget], list[QWidget]] | None = None,
		leading_signature: Callable[[Any], tuple[Any, ...]] | None = None,
		compact: bool = False,
		active: bool = True,
	) -> None:
		"""Args:
		page: страница-владелец (родитель карточек и плашек ошибок).
		box: компоновка, в которую панель складывает карточки.
		watcher: наблюдатель этой очереди (один на очередь, при главном
			окне — ``QueueWatchers``); панель присоединяется к нему зрителем.
		subtitle: подпись карточки для элемента.
		on_finished: разовая реакция панели на завершённый элемент
			(``True`` — готово, ``False`` — отменено) до снятия с показа;
			доходит, пока панель активна.
		on_refreshed: вызывается после каждого обновления со списком
			показанных элементов (после ``transform``; сводка очереди,
			при ``max_cards`` — место сказать «и ещё N»).
		max_cards: не больше стольких карточек на странице (None — все);
			длинный хвост ждущих (ADR-0016) не раздувает страницу.
		transform: правило показа — сортировка/фильтр видимого списка
			(полный просмотр очереди); занятость считается до него,
			по нефильтрованному списку. После смены правила зовите
			:meth:`refresh` — снимок в кэше, в движок ходить незачем.
		editable: можно ли раскрыть карточку элемента (правка). Без него
			карточки не раскрываются вовсе.
		fill_body: наполняет тело раскрытой карточки формой правки —
			получает id элемента, компоновку тела и «свернуть карточку».
			Зовётся один раз, при первом раскрытии.
		leading: виджеты в начале шапки карточки (логотип канала, метка
			слота времени) — получает элемент и родителя. Что именно
			показывать, решает владелец панели: очередь обработки видео
			крючок не передаёт, и её шапки начинаются с названия.
		leading_signature: отпечаток начала шапки — всё, от чего зависят
			его виджеты (время слота, путь аватара из кэша страницы):
			начало пересобирается только при его смене.
		compact: карточки по макету страницы сообщества — подпись под
			названием, кнопки-обводки 28, полоса прогресса под названием,
			ошибка красит подпись и рамку.
		active: присоединиться к наблюдателю сразу (окно обслуживания,
			вкладка); ``False`` — владелец присоединит при показе
			(:meth:`set_active`).
		"""
		#: страница-владелец (нужна владельцам панели для плашек).
		self.page = page
		self._watcher = watcher
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
			leading_signature=leading_signature,
			actions=self._actions,
			actions_signature=actions_signature,
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
		self._view = QueueView(on_state=self._show, on_finished=on_finished)
		if active:
			self.set_active(True)

	def set_active(self, active: bool) -> None:
		"""Присоединяет панель к наблюдателю или отсоединяет (экран скрыт).

		Присоединение сразу рисует последний снимок из кэша — человек
		открыл экран и ждёт свежих карточек; движок при этом не трогается.
		"""
		if active:
			self._watcher.attach(self.page, self._view)
		else:
			self._watcher.detach(self._view)

	def refresh(self) -> None:
		"""Перерисовывает карточки из кэша наблюдателя (листание, фильтр)."""
		self._show(self._watcher.items)

	def busy(self) -> bool:
		"""Есть ли незавершённое в очереди (включая ждущих)."""
		return self._watcher.busy()

	def poll(self) -> None:
		"""Запрашивает свежий снимок очереди (после постановки: карточка сразу)."""
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
		light = send_light(item.status)
		if light is not None:
			widgets.append(light_dot(light, parent))
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
