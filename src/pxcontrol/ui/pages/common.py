"""Общие помощники страниц: привязка обработчиков, диалоги, плашки."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Generic, TypeVar

from PySide6.QtCore import QDate, QObject, QTime, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
	QDialog,
	QFileDialog,
	QHBoxLayout,
	QLayout,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	ComboBox,
	EditableComboBox,
	FluentIcon,
	InfoBar,
	LineEdit,
	MessageBox,
	MessageBoxBase,
	ProgressBar,
	PushButton,
	ScrollArea,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine

_T = TypeVar("_T")

#: Длительность всплывающих плашек с ошибками/предупреждениями (мс).
TOAST_DURATION_MS = 6000

#: Пауза после последнего нажатия клавиши до реакции на ввод (мс):
#: достаточно, чтобы не дёргать движок/диск на каждый символ, и незаметно
#: для пользователя, закончившего печатать.
INPUT_DEBOUNCE_MS = 400


def debounced(parent: QObject, interval_ms: int, action: Callable[[], None]) -> Callable[..., None]:
	"""Обёртка «выполнить после паузы»: перезапускает одноразовый таймер.

	Подключается к сигналам вроде ``textChanged``: действие выполняется
	один раз, через ``interval_ms`` после последнего срабатывания, —
	набор слова не превращается в серию обращений к движку и диску.
	Аргументы сигнала игнорируются: действие само читает текущее состояние.
	"""
	timer = QTimer(parent)
	timer.setSingleShot(True)
	timer.setInterval(interval_ms)
	timer.timeout.connect(action)

	def restart(*_args: object) -> None:
		timer.start()

	return restart


def noop(*_args: object) -> None:
	"""Пустой колбэк для операций, результат которых не нужен интерфейсу."""


def format_local(moment: datetime) -> str:
	"""Дата-время для показа: хранится UTC — показывается местное.

	Единая точка правила проекта; наивные значения (mtime файла)
	трактуются как местные и показываются как есть.
	"""
	return moment.astimezone().strftime("%d.%m.%Y %H:%M")


def bot_caption(label: str, username: str | None) -> str:
	"""Единая метка бота в списках и диалогах: «Имя (@username)»."""
	return f"{label} (@{username or '—'})"


def bind(action: Callable[[_T], None], item: _T) -> Callable[[], None]:
	"""Ранняя привязка элемента к обработчику (замена lambda в цикле).

	Обычная lambda в цикле захватывает переменную, а не значение, и все
	обработчики получили бы последний элемент списка.
	"""

	def handler() -> None:
		action(item)

	return handler


def page_layout(page: ScrollArea, spacing: int | None = None) -> QVBoxLayout:
	"""Каркас прокручиваемой страницы: контейнер с едиными отступами.

	Одна сборка вместо одинаковых семи строк на каждой странице:
	контейнер, поля страницы и интервал блоков из :mod:`density`
	(None — обычный интервал блоков), растяжение по ширине
	и прозрачный фон (после ``setWidget`` — иначе фон контейнера
	не перекрашивается).

	Returns:
		Компоновка контейнера — страница добавляет в неё содержимое.
	"""
	container = QWidget(page)
	layout = QVBoxLayout(container)
	layout.setContentsMargins(*density.spacing().page_margins)
	layout.setSpacing(spacing if spacing is not None else density.spacing().block_spacing)
	page.setWidget(container)
	page.setWidgetResizable(True)
	page.enableTransparentBackground()
	return layout


def clear_layout(layout: QLayout) -> None:
	"""Опустошает компоновку: виджеты, вложенные компоновки, распорки.

	Виджеты удаляются; вложенные компоновки чистятся рекурсивно;
	распорки просто изымаются (владение переходит Python-обёртке,
	сборщик мусора её освобождает).
	"""
	while layout.count():
		item = layout.takeAt(0)
		if item is None:
			break
		widget = item.widget()
		if widget is not None:
			widget.deleteLater()
			continue
		child = item.layout()
		if child is not None:
			clear_layout(child)


def human_size(size_bytes: int) -> str:
	"""Размер файла для человека: «412 МБ», «1,8 ГБ», «6,4 КБ».

	Единицы десятичные (КБ = 1000 байт) — как их считает Telegram
	и файловые менеджеры; дробная часть — через запятую, по-русски.
	"""
	units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
	value = float(size_bytes)
	unit = 0
	while value >= 1000 and unit < len(units) - 1:
		value /= 1000
		unit += 1
	if unit == 0:
		return f"{int(value)} {units[unit]}"
	text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
	return f"{text.replace('.', ',')} {units[unit]}"


def format_duration(seconds: float) -> str:
	"""Длительность для списков и подписей: «12:34» или «1:23:45»."""
	total = int(seconds)
	hours, rest = divmod(total, 3600)
	minutes, secs = divmod(rest, 60)
	if hours:
		return f"{hours}:{minutes:02d}:{secs:02d}"
	return f"{minutes}:{secs:02d}"


def open_in_system(path: str) -> None:
	"""Открывает файл или папку системным приложением (плеер, проводник)."""
	QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def exec_dialog(dialog: QDialog) -> bool:
	"""Показывает модальный диалог и удаляет его после закрытия.

	Диалоги QFluentWidgets не удаляют себя после ``exec()`` (нет
	``WA_DeleteOnClose``) и накапливались бы детьми окна до выхода —
	вместе с содержимым (например, плитками кадров с картинками).
	Все страницы показывают диалоги через эту обёртку.

	Returns:
		True — диалог принят (кнопка подтверждения).
	"""
	accepted = bool(dialog.exec())
	dialog.deleteLater()
	return accepted


def confirm_delete(parent: QWidget, text: str, accept_text: str = "Удалить") -> bool:
	"""Спрашивает подтверждение необратимого действия."""
	box = MessageBox("Подтверждение", text, parent.window())
	box.yesButton.setText(accept_text)
	box.cancelButton.setText("Отмена")
	return exec_dialog(box)


def show_error(parent: QWidget, message: str) -> None:
	"""Показывает ошибку всплывающей плашкой."""
	InfoBar.error("Ошибка", message, parent=parent, duration=TOAST_DURATION_MS)


def show_warning(parent: QWidget, title: str, message: str) -> None:
	"""Показывает предупреждение всплывающей плашкой.

	Единая точка правила «предупреждения видны столько же, сколько
	ошибки»: у ``InfoBar.warning`` умолчание — 1 секунда, за которую
	пользователь плашку не успевает заметить.
	"""
	InfoBar.warning(title, message, parent=parent, duration=TOAST_DURATION_MS)


def error_reporter(parent: QWidget) -> Callable[[str], None]:
	"""Колбэк показа ошибок, привязанный к странице/диалогу.

	Один помощник вместо одинаковых методов ``_show_error`` на каждой
	странице; результат передаётся в ``run_in_engine`` как ``on_error``.
	"""
	return partial(show_error, parent)


def pick_file(parent: QWidget, caption: str, file_filter: str, start_dir: str = "") -> str | None:
	"""Открывает диалог выбора файла; None — пользователь отменил.

	``start_dir`` — стартовая папка диалога (пусто — на усмотрение Qt).
	"""
	path, _ = QFileDialog.getOpenFileName(parent, caption, start_dir, file_filter)
	return path or None


def pick_dir(parent: QWidget, caption: str, start_dir: str = "") -> str | None:
	"""Открывает диалог выбора папки; None — пользователь отменил."""
	path = QFileDialog.getExistingDirectory(parent, caption, start_dir)
	return path or None


def row_card(
	parent: QWidget,
	title: str,
	subtitle: str,
	trailing: QWidget | None = None,
	on_delete: Callable[[], None] | None = None,
) -> CardWidget:
	"""Карточка-строка списка: название, подпись, хвостовые элементы.

	Единый вид строк на всех страницах (аккаунты, каналы, расписание).
	"""
	card = CardWidget(parent)
	layout = QHBoxLayout(card)
	layout.setContentsMargins(*density.spacing().card_margins)
	column = QVBoxLayout()
	column.setSpacing(2)
	# перенос строк: длинный текст (имя файла и т.п.) не должен
	# распирать карточку и уводить элементы за пределы окна
	title_label = StrongBodyLabel(title, card)
	title_label.setWordWrap(True)
	subtitle_label = CaptionLabel(subtitle, card)
	subtitle_label.setWordWrap(True)
	column.addWidget(title_label)
	column.addWidget(subtitle_label)
	layout.addLayout(column, stretch=1)
	if trailing is not None:
		layout.addWidget(trailing)
	if on_delete is not None:
		delete_button = TransparentToolButton(FluentIcon.DELETE, card)
		delete_button.clicked.connect(on_delete)
		layout.addWidget(delete_button)
	return card


class DtoComboBox(ComboBox, Generic[_T]):
	"""Комбобокс списка DTO, хранящий элементы рядом с виджетом.

	Заменяет ручную арифметику «индекс минус служебный пункт» и парные
	списки DTO на страницах — из этой ручной синхронизации вырастали
	ошибки, когда выбор восстанавливался по позиции в изменившемся
	списке и молча указывал на другой элемент.
	"""

	def __init__(self, parent: QWidget, placeholder: str | None = None) -> None:
		"""``placeholder`` — служебный первый пункт («(не выбран)»);
		None — список начинается сразу с элементов."""
		super().__init__(parent)
		self._placeholder = placeholder
		self._dtos: list[_T] = []

	def set_items(
		self,
		items: list[_T],
		label: Callable[[_T], str],
		key: Callable[[_T], object] | None = None,
	) -> None:
		"""Пересобирает список без промежуточных сигналов.

		Сигналы блокируются на время пересборки (первый ``addItem``
		Qt-комбобокса излучает ``currentIndexChanged``) — обработчик
		выбора страница вызывает сама один раз после пересборки.

		Args:
			items: новые элементы списка.
			label: текст пункта для элемента.
			key: идентичность элемента (обычно ``lambda x: x.id``) — по ней
				восстанавливается прежний выбор; None или элемент исчез —
				выбор встаёт на первый пункт.
		"""
		previous = self.selected()
		self.blockSignals(True)
		try:
			self.clear()
			self._dtos = list(items)
			if self._placeholder is not None:
				self.addItem(self._placeholder)
			for item in self._dtos:
				self.addItem(label(item))
			index = 0 if self.count() else -1
			if key is not None and previous is not None:
				wanted = key(previous)
				for position, item in enumerate(self._dtos):
					if key(item) == wanted:
						index = position + self._offset()
						break
			self.setCurrentIndex(index)
		finally:
			self.blockSignals(False)

	def selected(self) -> _T | None:
		"""Выбранный элемент; None — служебный пункт или пустой список."""
		index = int(self.currentIndex()) - self._offset()
		# локальная переменная с типом: базовый класс не типизирован,
		# и чтение атрибута через self даёт Any
		dtos: list[_T] = self._dtos
		if 0 <= index < len(dtos):
			return dtos[index]
		return None

	def select(self, predicate: Callable[[_T], bool]) -> bool:
		"""Выбирает первый подходящий элемент; False — такого нет.

		Контракт: успешный выбор излучает ``currentIndexChanged`` (сигналы
		не блокируются) — обработчик смены сработает сам, звать его следом
		вручную не нужно (страницы полагаются на это поведение).
		"""
		for position, item in enumerate(self._dtos):
			if predicate(item):
				self.setCurrentIndex(position + self._offset())
				return True
		return False

	def _offset(self) -> int:
		"""Сдвиг индексов элементов из-за служебного пункта."""
		return 1 if self._placeholder is not None else 0


#: Период опроса состояния очередей движка (мс). Опрос вместо событий —
#: осознанный дизайн ADR-0012: интерфейс читает снимок состояния.
QUEUE_POLL_MS = 500


class QueuePanel:
	"""Панель очереди движка: опрос, карточки, прогресс, действия.

	Общий каркас панелей «Публикации» (очередь отправки, ADR-0012)
	и «Видео» (очередь обработки, ADR-0014): таймер опроса, снятие
	завершённых с показа, пересборка карточек только при смене состава
	и точечное обновление прогресса без пересборки. Опрос живёт всегда,
	не только при видимой странице: завершения снимаются с показа,
	а кэш занятости нужен окну для подтверждения выхода.

	Контракт сервиса очереди (оба сервиса движка ему следуют): корутины
	``state()``, ``cancel(id)``, ``retry(id)``, ``dismiss(id)``; элементы
	с полями ``id``, ``status`` (перечисление со значениями
	PENDING/DONE/ERROR/CANCELLED + активное и методом ``finished()``),
	``progress``, ``title``, ``error``.
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
	) -> None:
		"""Args:
		worker: мост к движку.
		page: страница-владелец (родитель карточек, таймера, плашек ошибок).
		box: компоновка, в которую панель складывает карточки.
		service: провайдер сервиса очереди (``lambda: worker.engine.…``).
		subtitle: подпись карточки для элемента.
		on_finished: разовая реакция на завершённый элемент
			(``True`` — готово, ``False`` — отменено) до снятия с показа.
		on_refreshed: вызывается после каждого обновления с ПОЛНЫМ списком
			видимых элементов (сводка очереди; при ``max_cards`` — место
			сказать «и ещё N»).
		on_drained: вызывается с видимым остатком, когда занятость
			кончилась (итоговая плашка вместо плашки на каждый файл).
		max_cards: не больше стольких карточек на странице (None — все);
			длинный хвост ждущих (ADR-0016) не раздувает страницу.
		"""
		self._worker = worker
		self._page = page
		self._box = box
		self._service = service
		self._subtitle = subtitle
		self._on_finished = on_finished
		self._on_refreshed = on_refreshed
		self._on_drained = on_drained
		self._max_cards = max_cards
		self._show_error = error_reporter(page)
		self._signature: tuple[tuple[Any, ...], ...] = ()
		self._bars: dict[int, ProgressBar] = {}
		self._handled: set[int] = set()  # завершённые, уже учтённые
		self._busy = False
		timer = QTimer(page)
		timer.setInterval(QUEUE_POLL_MS)
		timer.timeout.connect(self.poll)
		timer.start()

	def busy(self) -> bool:
		"""Есть ли незавершённое в очереди (для подтверждения выхода)."""
		return self._busy

	def poll(self) -> None:
		"""Запрашивает состояние очереди (по таймеру и после постановки)."""
		# ошибки опроса не показываем плашками: мост пишет их в лог,
		# а раз в полсекунды спамить пользователя нечем и незачем
		run_in_engine(self._worker, self._service().state(), self._page, self._show, noop)

	def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из состояния очереди."""
		run_in_engine(
			self._worker,
			self._service().dismiss(item_id),
			self._page,
			lambda *_a: self.poll(),
			noop,
		)

	# --- внутреннее ---------------------------------------------------------

	def _show(self, items: list[Any]) -> None:
		"""Обновляет панель; завершённые получают реакцию и снимаются с показа."""
		visible: list[Any] = []
		for item in items:
			if item.status.name in ("DONE", "CANCELLED"):
				self._finish(item, done=item.status.name == "DONE")
			else:
				visible.append(item)
		# id, исчезнувшие из состояния движка (после dismiss), больше
		# не встретятся — набор «уже учтённых» не растёт бесконечно
		self._handled &= {item.id for item in items}
		busy = any(not item.status.finished() for item in visible)
		if self._busy and not busy and self._on_drained is not None:
			self._on_drained(visible)
		self._busy = busy
		signature = tuple((i.id, i.status, i.error, getattr(i, "note", None)) for i in visible)
		if signature != self._signature:
			self._signature = signature
			self._rebuild(visible)
		for item in visible:  # прогресс — без пересборки карточек
			bar = self._bars.get(item.id)
			if bar is not None:
				bar.setValue(int(item.progress * 100))
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
		self.dismiss(item.id)

	def _rebuild(self, items: list[Any]) -> None:
		"""Перестраивает карточки (только при смене состава/статусов)."""
		clear_layout(self._box)
		self._bars = {}
		shown = items if self._max_cards is None else items[: self._max_cards]
		for item in shown:
			self._box.addWidget(self._row(item))

	def _row(self, item: Any) -> CardWidget:
		"""Карточка элемента: прогресс у активного, «Отмена» у живого,
		«Повторить» и «Убрать» у ошибки."""
		trailing = QWidget(self._page)
		row = QHBoxLayout(trailing)
		row.setContentsMargins(0, 0, 0, 0)
		# полоса прогресса — только у активных (WAITING/PENDING не растут)
		if item.status.name in ("SENDING", "PROCESSING"):
			bar = ProgressBar(trailing)
			bar.setRange(0, 100)
			bar.setValue(int(item.progress * 100))
			bar.setFixedWidth(160)
			row.addWidget(bar)
			self._bars[item.id] = bar
		if item.status.name == "ERROR":
			retry = PushButton("Повторить", trailing)
			retry.clicked.connect(bind(self._retry, item.id))
			row.addWidget(retry)
			action = PushButton("Убрать", trailing)
			action.clicked.connect(bind(self.dismiss, item.id))
		else:
			action = PushButton("Отмена", trailing)
			action.clicked.connect(bind(self._cancel, item.id))
		row.addWidget(action)
		return row_card(self._page, item.title, self._subtitle(item), trailing=trailing)

	def _retry(self, item_id: int) -> None:
		"""Просит движок вернуть элемент с ошибкой в очередь на повтор."""
		run_in_engine(
			self._worker,
			self._service().retry(item_id),
			self._page,
			lambda *_a: self.poll(),  # карточка обновляется сразу, не по таймеру
			self._show_error,
		)

	def _cancel(self, item_id: int) -> None:
		"""Просит движок отменить элемент очереди."""
		run_in_engine(
			self._worker,
			self._service().cancel(item_id),
			self._page,
			noop,
			self._show_error,
		)


class ErrorLabel(CaptionLabel):
	"""Красная подпись ошибки валидации диалога (единые цвета обеих тем).

	Скрыта, пока ошибки нет; :meth:`fail` показывает текст и возвращает
	False — крючок ``validate`` диалога завершается одной строкой.
	Единая точка цветов вместо повторённых магических значений.
	"""

	def __init__(self, parent: QWidget) -> None:
		# ВАЖНО: базовому классу нельзя передавать текст. Конструктор
		# QFluentWidgets-подписей — диспетчер по типам, и вариант
		# «текст + родитель» внутри делает self.__init__(parent):
		# у подкласса это снова этот метод — бесконечная рекурсия
		# (RecursionError, ловилось вживую на диалоге пакета).
		super().__init__(parent)
		self.setTextColor("#c42b1c", "#ff99a4")
		self.hide()

	def fail(self, message: str) -> bool:
		"""Показывает ошибку; False не даёт диалогу закрыться."""
		self.setText(message)
		self.show()
		return False

	def succeed(self) -> bool:
		"""Прячет ошибку; True позволяет диалогу закрыться."""
		self.hide()
		return True


class SelectionRow:
	"""Строка выбора пакетного диалога: «Выбрать все»/«Снять все» и итог.

	Общий блок диалогов пакетов (сканирование папки и отправка):
	``layout`` добавляется в компоновку диалога, итог обновляется
	через :meth:`set_summary`.
	"""

	def __init__(self, parent: QWidget, set_all: Callable[[bool], None]) -> None:
		self.layout = QHBoxLayout()
		self._select_all = PushButton("Выбрать все", parent)
		self._select_all.clicked.connect(lambda: set_all(True))
		self.layout.addWidget(self._select_all)
		self._clear_all = PushButton("Снять все", parent)
		self._clear_all.clicked.connect(lambda: set_all(False))
		self.layout.addWidget(self._clear_all)
		self._summary = CaptionLabel("", parent)
		self.layout.addWidget(self._summary, stretch=1)

	def set_enabled(self, enabled: bool) -> None:
		"""Доступность кнопок (пока список пуст — выключены)."""
		self._select_all.setEnabled(enabled)
		self._clear_all.setEnabled(enabled)

	def set_summary(self, picked: int, total: int, picked_bytes: int) -> None:
		"""Итог по отмеченному: «Отмечено N из M · размер»."""
		self._summary.setText(f"Отмечено {picked} из {total} · {human_size(picked_bytes)}")


def fixed_list_area(parent: QWidget, height: int, spacing: int) -> tuple[ScrollArea, QVBoxLayout]:
	"""Прокручиваемый список фиксированной высоты для диалога.

	Прокрутка живёт внутри области — длинный список не раздувает диалог.

	Returns:
		Область (её добавляет в диалог вызывающий) и компоновка
		контейнера — в неё складываются строки; распорку в конец,
		если нужна, добавляет вызывающий.
	"""
	area = ScrollArea(parent)
	container = QWidget(area)
	box = QVBoxLayout(container)
	box.setContentsMargins(0, 0, 0, 0)
	box.setSpacing(spacing)
	area.setWidget(container)
	area.setWidgetResizable(True)
	area.enableTransparentBackground()
	area.setFixedHeight(height)
	return area, box


def parse_hhmm(text: str) -> tuple[int, int]:
	"""Разбирает время «ЧЧ:ММ» (часы 0–23, минуты 0–59).

	Returns:
		Пара (часы, минуты).

	Raises:
		ValueError: Формат не «ЧЧ:ММ» или значения вне диапазона.
	"""
	error = ValueError("Время — в формате ЧЧ:ММ, например 18:30.")
	parts = text.strip().split(":")
	if len(parts) != 2 or not all(part.isdigit() for part in parts):
		raise error
	hours, minutes = int(parts[0]), int(parts[1])
	if hours > 23 or minutes > 59:
		raise error
	return hours, minutes


class WhenRow:
	"""Строка «Опубликовать сейчас» + дата и время отложенной записи.

	По умолчанию «сейчас» выключен: посты обычно отложенные. Время —
	редактируемый список (`EditableComboBox`): пункты — стандартные
	времена канала (:func:`set_times`), текст правится вручную («ЧЧ:ММ»).
	"""

	def __init__(self, dialog: QWidget, layout: QVBoxLayout) -> None:
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Опубликовать сейчас", dialog))
		self._now_switch = SwitchButton(dialog)
		self._now_switch.setChecked(False)
		self._now_switch.checkedChanged.connect(self._on_now_toggled)
		row.addWidget(self._now_switch)
		row.addStretch()
		self._date = CalendarPicker(dialog)
		self._date.setDate(QDate.currentDate())
		self._time = EditableComboBox(dialog)
		self._time.setPlaceholderText("ЧЧ:ММ")
		self._time.setText(QTime.currentTime().addSecs(3600).toString("HH:mm"))
		self._time.setMaximumWidth(120)
		row.addWidget(self._date)
		row.addWidget(self._time)
		layout.addLayout(row)

	def _on_now_toggled(self, now: bool) -> None:
		self._date.setVisible(not now)
		self._time.setVisible(not now)

	def set_schedule_allowed(self, allowed: bool, hint: str = "") -> None:
		"""Разрешает/запрещает отложенную публикацию (иначе — только «сейчас»)."""
		if not allowed:
			self._now_switch.setChecked(True)
		self._now_switch.setEnabled(allowed)
		self._now_switch.setToolTip("" if allowed else hint)

	def set_times(self, times: list[str]) -> None:
		"""Наполняет список стандартными временами канала (первое — выбрано).

		Битые элементы пропускаются; пустой список — текущее время + 1 ч.
		Если выбранное время сегодня уже прошло — дата переставляется
		на завтра (пользователь видит это в календаре).
		"""
		valid: list[str] = []
		for item in times:
			try:
				parse_hhmm(str(item))
			except ValueError:
				continue
			valid.append(str(item).strip())
		self._time.clear()
		self._time.addItems(valid)
		if valid:
			self._time.setCurrentIndex(0)
			self._time.setText(valid[0])
		else:
			self._time.setCurrentIndex(-1)
			self._time.setText(QTime.currentTime().addSecs(3600).toString("HH:mm"))
		self._adjust_date()

	def _adjust_date(self) -> None:
		"""Сегодняшнее прошедшее время переносит дату на завтра."""
		try:
			hours, minutes = parse_hhmm(str(self._time.text()))
		except ValueError:
			return
		today = QDate.currentDate()
		if self._date.getDate() > today:
			return  # дата уже выбрана вперёд — не трогаем
		passed = QTime(hours, minutes) <= QTime.currentTime()
		self._date.setDate(today.addDays(1) if passed else today)

	def when(self) -> datetime | None:
		"""None — «сейчас», иначе выбранный момент (в UTC).

		Raises:
			ValueError: Время не в формате «ЧЧ:ММ».
		"""
		if self._now_switch.isChecked():
			return None
		hours, minutes = parse_hhmm(str(self._time.text()))
		date = self._date.getDate()
		local = datetime(date.year(), date.month(), date.day(), hours, minutes)
		return local.astimezone(UTC)


class FormDialog(MessageBoxBase):
	"""Диалог с набором текстовых полей.

	``validator`` — правило пригодности введённого: получает словарь
	«ключ поля → текст», возвращает текст ошибки или None («всё годно»).
	При ошибке диалог показывает её и НЕ закрывается — введённое
	не пропадает (крючок ``validate`` библиотеки, как в диалоге
	настроек канала).
	"""

	def __init__(
		self,
		title: str,
		fields: list[tuple[str, str]],
		parent: QWidget,
		accept_text: str = "Добавить",
		password_fields: tuple[str, ...] = (),
		validator: Callable[[dict[str, str]], str | None] | None = None,
	) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel(title, self))
		self._edits: dict[str, LineEdit] = {}
		self._validator = validator
		self._error = ErrorLabel(self)
		for key, placeholder in fields:
			edit = LineEdit(self)
			edit.setPlaceholderText(placeholder)
			edit.setClearButtonEnabled(True)
			if key in password_fields:
				edit.setEchoMode(LineEdit.EchoMode.Password)
			self.viewLayout.addWidget(edit)
			self._edits[key] = edit
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText(accept_text)
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(420)

	def value(self, key: str) -> str:
		"""Возвращает введённый текст поля без крайних пробелов."""
		return str(self._edits[key].text()).strip()

	def validate(self) -> bool:
		"""Крючок MessageBoxBase: False не даёт диалогу закрыться."""
		if self._validator is None:
			return True
		message = self._validator({key: self.value(key) for key in self._edits})
		if message is None:
			return self._error.succeed()
		return self._error.fail(message)


def require_filled(
	*keys: str, message: str = "Заполните все поля."
) -> Callable[[dict[str, str]], str | None]:
	"""Готовый валидатор FormDialog: перечисленные поля непусты."""

	def check(values: dict[str, str]) -> str | None:
		return None if all(values.get(key) for key in keys) else message

	return check
