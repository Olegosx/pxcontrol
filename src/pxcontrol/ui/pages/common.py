"""Общие помощники страниц: привязка обработчиков, диалоги, плашки."""

from __future__ import annotations

import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Generic, TypeVar

from PySide6.QtCore import QDate, QEvent, QObject, QSize, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import (
	QDesktopServices,
	QMouseEvent,
	QPainter,
	QPainterPath,
	QPixmap,
)
from PySide6.QtWidgets import (
	QDialog,
	QFileDialog,
	QHBoxLayout,
	QLabel,
	QLayout,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	EditableComboBox,
	FluentIcon,
	FluentStyleSheet,
	InfoBar,
	LineEdit,
	MessageBox,
	MessageBoxBase,
	PrimaryPushButton,
	ProgressBar,
	PushButton,
	ScrollArea,
	SegmentedWidget,
	StrongBodyLabel,
	SubtitleLabel,
	SwitchButton,
	TextEdit,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.schedule_plan import parse_hhmm
from pxcontrol.engine.services.video import video_dialog_filter
from pxcontrol.engine.telegram.types import (
	GENERAL_TOPIC_ID,
	TEXT_LENGTH_LIMIT,
	CommunityKind,
	ForumTopicInfo,
	MediaKind,
	UserbotRole,
	telegram_text_length,
)
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


#: Умолчание времени публикации: «через час» — предзаполнение поля
#: времени (WhenRow) и старта раскладки пакета.
DEFAULT_SCHEDULE_OFFSET_S = 3600


#: Типы контента поста: подпись сегмента → тип вложения → фильтр диалога
#: выбора файла. Общий для формы «Публикации» и окна правки элемента
#: очереди: списки типов не должны разъезжаться между ними.
CONTENT_KINDS: list[tuple[str, MediaKind, str]] = [
	("Текст", MediaKind.NONE, ""),
	("Фото", MediaKind.PHOTO, "Изображения (*.png *.jpg *.jpeg *.webp)"),
	("Видео", MediaKind.VIDEO, video_dialog_filter()),
	("Аудио", MediaKind.AUDIO, "Аудио (*.mp3 *.m4a *.flac *.ogg *.wav)"),
	("Файл", MediaKind.DOCUMENT, "Все файлы (*)"),
]


def kind_label(kind: MediaKind) -> str:
	"""Подпись типа контента («Видео», «Файл»…) для сообщений и сегментов."""
	return next(label for label, item_kind, _filter in CONTENT_KINDS if item_kind is kind)


def kind_file_filter(kind: MediaKind) -> str:
	"""Фильтр диалога выбора файла для типа контента."""
	return next(
		file_filter for _label, item_kind, file_filter in CONTENT_KINDS if item_kind is kind
	)


def visible_topics(
	topics: list[ForumTopicInfo], role: UserbotRole | None
) -> tuple[list[ForumTopicInfo], int]:
	"""Темы, доступные для выбора, и число скрытых закрытых (ADR-0022).

	General не показывается отдельным пунктом — он и есть «Общая лента».
	В закрытую тему пишет только админ: участнику такие темы недоступны
	и скрываются, их число возвращается для честной подписи.

	Returns:
		Пара «темы для выбора, сколько закрытых скрыто».
	"""
	shown = [topic for topic in topics if topic.id != GENERAL_TOPIC_ID]
	if role is UserbotRole.ADMIN:
		return shown, 0
	closed = sum(1 for topic in shown if topic.closed)
	return [topic for topic in shown if not topic.closed], closed


def topic_label(topic: ForumTopicInfo) -> str:
	"""Подпись темы в списке: закрытая помечается (её видит только админ)."""
	return f"{topic.title} (закрыта)" if topic.closed else topic.title


def checked_or_single(items: list[_T], checked: list[_T]) -> list[_T] | None:
	"""Отмеченные элементы, а если не отмечено ничего — единственный.

	Правило списков с галочками на странице «Видео»: когда элемент
	один, галочка избыточна — человек и так указал, о чём речь.
	Ничего не отмечено при нескольких элементах — выбор не сделан.

	Returns:
		Список для работы или None, если выбор не сделан (вызывающий
		скажет об этом своими словами — списки разные).
	"""
	if checked:
		return checked
	return list(items) if len(items) == 1 else None


def closed_topics_hint(closed: int) -> str:
	"""Подпись «сколько закрытых тем скрыто» (одна на обе формы поста)."""
	return f"Закрытых тем скрыто: {closed} — в них пишет только админ."


def caption_placeholder(is_text: bool) -> str:
	"""Подсказка в поле текста: у поста с вложением это подпись к файлу."""
	return "Текст поста…" if is_text else "Подпись к файлу (необязательно)…"


@dataclass
class TopicRow:
	"""Ряд выбора темы форума: коробка ряда, список тем и подпись."""

	box: QWidget
	combo: DtoComboBox[ForumTopicInfo]
	hint: CaptionLabel


def topic_row(parent: QWidget, layout: QVBoxLayout, *, tooltip: str = "") -> TopicRow:
	"""Собирает ряд выбора темы форума (ADR-0021).

	Общий для обеих форм поста — создания на «Публикации» и правки
	в карточке очереди: расходиться их рядам незачем, а тексты
	у них уже начинали расходиться. Наполнение списка и правила
	видимости — забота вызывающего: на странице темы читаются
	из Telegram асинхронно, в карточке приходят уже готовыми.
	"""
	box = QWidget(parent)
	row = QHBoxLayout(box)
	row.setContentsMargins(0, 0, 0, 0)
	row.addWidget(BodyLabel("Тема форума:", box))
	combo: DtoComboBox[ForumTopicInfo] = DtoComboBox(box, placeholder="Общая лента")
	if tooltip:
		combo.setToolTip(tooltip)
	row.addWidget(combo, stretch=1)
	hint = CaptionLabel("", box)
	row.addWidget(hint)
	layout.addWidget(box)
	return TopicRow(box, combo, hint)


@dataclass
class RenameRow:
	"""Ряд «переименовать при отправке»: галочка и поле имени."""

	box: QWidget
	check: CheckBox
	edit: LineEdit


def rename_row(
	parent: QWidget, layout: QVBoxLayout, *, checked: bool = True, name: str = ""
) -> RenameRow:
	"""Собирает ряд переименования файла при отправке."""
	box = QWidget(parent)
	row = QHBoxLayout(box)
	row.setContentsMargins(0, 0, 0, 0)
	check = CheckBox("Переименовать при отправке:", box)
	check.setChecked(checked)
	row.addWidget(check)
	edit = LineEdit(box)
	edit.setText(name)
	edit.setPlaceholderText("Новое имя файла с расширением…")
	row.addWidget(edit, stretch=1)
	layout.addWidget(box)
	return RenameRow(box, check, edit)


def kind_segments(
	parent: QWidget,
	layout: QVBoxLayout,
	on_changed: Callable[[str], None],
	*,
	current: str | None = None,
) -> SegmentedWidget:
	"""Собирает переключатель типа контента (текст, фото, видео, …)."""
	segments = SegmentedWidget(parent)
	for label, kind, _file_filter in CONTENT_KINDS:
		segments.addItem(routeKey=kind.value, text=label)
	if current is not None:
		segments.setCurrentItem(current)
	segments.currentItemChanged.connect(on_changed)
	layout.addWidget(segments)
	return segments


def bot_caption(label: str, username: str | None) -> str:
	"""Единая метка бота в списках и диалогах: «Имя (@username)»."""
	return f"{label} (@{username or '—'})"


def role_caption(role: UserbotRole) -> str:
	"""Человеческая метка роли userbot-аккаунта (ADR-0022)."""
	return "админ" if role is UserbotRole.ADMIN else "участник"


def community_kind_caption(community: CommunityDto) -> str:
	"""Человеческая метка вида сообщества (карточки, подсказки)."""
	if community.kind is CommunityKind.GROUP:
		return "Группа-форум" if community.forum else "Группа"
	return "Канал"


def community_combo_label(community: CommunityDto) -> str:
	"""Подпись сообщества в выпадающих списках: группам — пометка вида."""
	if community.kind is CommunityKind.GROUP:
		return f"{community.title} — группа"
	return community.title


def account_caption(display: str, phone: str | None) -> str:
	"""Единая метка userbot-аккаунта в списках и диалогах: «Имя (телефон)».

	``display`` — отображаемое имя из движка (``TgAccountDto.display``:
	пометка → имя из Telegram → @имя → телефон). Если имя и есть телефон
	(аккаунт без пометки до первого входа) — телефон не дублируется.
	"""
	if phone and phone != display:
		return f"{display} ({phone})"
	return display


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


class _Elider(QObject):
	"""Держит полный текст надписи и подгоняет его под её живую ширину.

	Живёт при надписи (она же родитель) и переподгоняет текст на каждом
	изменении её размера: ширину даёт Qt, а не наш расчёт «ширина
	карточки минус поля минус значок».
	"""

	def __init__(self, label: QLabel, text: str) -> None:
		super().__init__(label)
		self._label = label
		self._text = text
		label.installEventFilter(self)
		self._apply()

	def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 — API Qt
		"""Ширина надписи изменилась — пересчитать сокращение."""
		if event.type() == QEvent.Type.Resize:
			self._apply()
		return False

	def _apply(self) -> None:
		"""Ставит полный текст или сокращённый — по настоящей ширине."""
		metrics = self._label.fontMetrics()
		width = self._label.contentsRect().width()
		# сокращаем, только когда текст действительно шире: elidedText
		# сокращает уже при равенстве, и короткое значение превращалось
		# бы в одно многоточие
		if metrics.horizontalAdvance(self._text) > width:
			self._label.setText(metrics.elidedText(self._text, Qt.TextElideMode.ElideRight, width))
			self._label.setToolTip(self._text)
		else:
			self._label.setText(self._text)
			self._label.setToolTip("")


def elide_text(label: QLabel, text: str) -> None:
	"""Показывает текст в надписи, сокращая его под её настоящую ширину.

	У ``QLabel`` своего сокращения нет, поэтому его делает наблюдатель
	при надписи: он берёт ширину у самой надписи (``contentsRect``)
	и повторяет подгонку при каждом изменении размера. Полный текст
	остаётся во всплывающей подсказке.

	Ширину надписи задаёт вызывающий — обычной вёрсткой: либо предел
	(``setMaximumWidth``), либо политика размера «занимай, что дадут»
	(``QSizePolicy.Policy.Ignored``) у надписи, которой отдана колонка.
	Считать доступную ширину арифметикой по чужим отступам не нужно.
	"""
	_Elider(label, text)


def clear_layout(layout: QLayout) -> None:
	"""Опустошает компоновку: виджеты, вложенные компоновки, распорки.

	Виджеты удаляются; вложенные компоновки чистятся рекурсивно;
	распорки просто изымаются (владение переходит Python-обёртке,
	сборщик мусора её освобождает).

	Связь с родителем рвётся сразу (``setParent(None)``), а не только
	``deleteLater``: до отложенного удаления виджет остаётся дочерним
	и сохраняет свою геометрию, то есть продолжает рисоваться на старом
	месте — поверх того, что встало на его место в компоновке. Ловилось
	на метке слота в шапке карточки очереди: после правки времени поверх
	новой метки кадром висела прежняя.
	"""
	while layout.count():
		item = layout.takeAt(0)
		if item is None:
			break
		widget = item.widget()
		if widget is not None:
			widget.setParent(None)
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


def file_action_buttons(
	parent: QWidget, path: str, on_remove: Callable[[], None], *, remove_tip: str
) -> QWidget:
	"""Пара кнопок карточки файла: «посмотреть» и «убрать».

	Общая шапка карточек файлов («Видео», пакет «Публикации»):
	просмотр — системным плеером, «убрать» — только из списка (файл
	на диске не трогается — это обещает подсказка ``remove_tip``).
	"""
	trailing = QWidget(parent)
	buttons = QHBoxLayout(trailing)
	buttons.setContentsMargins(0, 0, 0, 0)
	buttons.setSpacing(4)
	play = TransparentToolButton(FluentIcon.PLAY, trailing)
	play.setToolTip("Посмотреть файл (системный плеер)")
	play.clicked.connect(bind(open_in_system, path))
	buttons.addWidget(play)
	remove = TransparentToolButton(FluentIcon.DELETE, trailing)
	remove.setToolTip(remove_tip)
	remove.clicked.connect(on_remove)
	buttons.addWidget(remove)
	return trailing


#: Приглушённый цвет сводки в шапке карточки: светлая/тёмная тема.
#: Подпись, а не ошибка — единая точка цветов ошибок (``ErrorLabel``
#: в ``common``) тут не подходит.
_SUMMARY_COLORS = ("#5f5f5f", "#9c9c9c")

#: Цвета «это ошибка» для светлой и тёмной темы: подпись валидации
#: (``ErrorLabel``) и счётчик символов при превышении предела.
ERROR_COLORS = ("#c42b1c", "#ff99a4")


class _CardHeader(QWidget):
	"""Шапка сворачиваемой карточки: ловит клик по всей своей площади."""

	clicked = Signal()

	def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — API Qt
		"""Левый клик в любом месте шапки — сигнал о сворачивании."""
		if event.button() == Qt.MouseButton.LeftButton:
			self.clicked.emit()
		super().mouseReleaseEvent(event)


class CollapsibleCard(CardWidget):
	"""Карточка-раздел, сворачиваемая кликом по шапке.

	Разделы параметров свёрнуты по умолчанию: форма занимает несколько
	строк вместо целого экрана (параметры обычно приходят из пресета
	и правятся редко). Свёрнутость — только про показ: виджеты скрытого
	тела сохраняют значения, и :meth:`PresetForm.fields` читает их
	как обычно.

	Сигнал :attr:`expanded_changed` сообщает о раскрытии: карточка
	элемента очереди наполняет тело формой правки лениво, при первом
	раскрытии, а не для всего списка заранее.
	"""

	expanded_changed = Signal(bool)

	def __init__(
		self,
		title: str,
		parent: QWidget,
		trailing: QWidget | None = None,
		leading: QWidget | None = None,
		keep_summary: bool = False,
	) -> None:
		"""``trailing`` — виджет с кнопками в правом краю шапки (например,
		просмотр и удаление у карточки файла); ``leading`` — виджет перед
		названием (например, чекбокс выбора). Клики по обоим остаются
		их виджетам и карточку не сворачивают (Qt не передаёт их шапке).
		``keep_summary`` — не прятать сводку у развёрнутой карточки: у
		элемента очереди она говорит состояние («ждёт слота», «ошибка:…»),
		и оно нужно как раз тогда, когда карточку раскрыли для правки."""
		super().__init__(parent)
		outer = QVBoxLayout(self)
		outer.setContentsMargins(0, 0, 0, 0)
		outer.setSpacing(0)
		self._chevron = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
		self._chevron.setFixedSize(24, 24)
		self._chevron.setIconSize(QSize(12, 12))
		self._chevron.clicked.connect(self.toggle)
		header = _CardHeader(self)
		header.setCursor(Qt.CursorShape.PointingHandCursor)
		header.clicked.connect(self.toggle)
		head_row = QHBoxLayout(header)
		head_row.setContentsMargins(12, 8, 16, 8)
		head_row.setSpacing(8)
		head_row.addWidget(self._chevron)
		if leading is not None:
			leading.setParent(header)
			head_row.addWidget(leading)
		self._title = StrongBodyLabel(title, header)
		head_row.addWidget(self._title)
		self._keep_summary = keep_summary
		self._expandable = True
		self._summary_text = ""
		self._summary = CaptionLabel("", header)
		self._summary.setTextColor(*_SUMMARY_COLORS)
		# сводка занимает остаток шапки и обрезается, а не распирает форму
		self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		head_row.addWidget(self._summary, stretch=1)
		head_row.addStretch()
		if trailing is not None:
			trailing.setParent(header)
			head_row.addWidget(trailing)
		outer.addWidget(header)
		self._body = QWidget(self)
		#: Компоновка тела — раздел добавляет сюда своё содержимое.
		self.body = QVBoxLayout(self._body)
		self.body.setContentsMargins(*density.spacing().card_body_margins)
		self.body.setSpacing(density.spacing().card_body_spacing)
		outer.addWidget(self._body)
		self._body.hide()

	def toggle(self) -> None:
		"""Разворачивает свёрнутое и наоборот (клик по шапке или стрелке)."""
		if not self._expandable:
			return
		self.set_expanded(not self._body.isVisible())

	def set_title(self, title: str) -> None:
		"""Меняет заголовок шапки (карточка живёт дольше своего названия)."""
		self._title.setText(title)

	def set_expandable(self, expandable: bool) -> None:
		"""Разрешает или запрещает раскрытие карточки.

		Запрет — для элементов, у которых внутри ничего нет: пост
		в очереди отправки, который уже грузится в Telegram, править
		нельзя, и стрелка, ведущая в пустоту, только обманывает.
		Запрет сворачивает уже раскрытое.
		"""
		self._expandable = expandable
		self._chevron.setVisible(expandable)
		if not expandable and self.expanded():
			self.set_expanded(False)

	def expanded(self) -> bool:
		"""Раскрыта ли карточка сейчас."""
		return bool(self._body.isVisible())

	def set_expanded(self, expanded: bool) -> None:
		"""Показывает или прячет тело; стрелка отражает состояние."""
		changed = self._body.isVisible() != expanded
		self._body.setVisible(expanded)
		icon = FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED
		self._chevron.setIcon(icon)
		self._refresh_summary()
		if changed:
			self.expanded_changed.emit(expanded)

	def set_summary(self, text: str) -> None:
		"""Сводка значений для шапки.

		По умолчанию видна только у свёрнутой карточки: у развёрнутой
		она дублировала бы поля прямо под шапкой. Исключение —
		``keep_summary`` (см. конструктор).
		"""
		self._summary_text = text
		self._refresh_summary()

	def _refresh_summary(self) -> None:
		self._summary.setText(self._summary_text)
		visible = bool(self._summary_text) and (self._keep_summary or not self._body.isVisible())
		self._summary.setVisible(visible)


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


def show_success(parent: QWidget, title: str, message: str = "") -> None:
	"""Показывает успешный исход всплывающей плашкой.

	Та же единая точка, что у ошибок и предупреждений: длительность
	задаётся здесь, а не умолчанием библиотеки (одна секунда, за которую
	плашку не успевают прочитать). Прежде успех и справка звались
	напрямую классом библиотеки — половина плашек приложения жила
	с чужим умолчанием.
	"""
	InfoBar.success(title, message, parent=parent, duration=TOAST_DURATION_MS)


def show_info(parent: QWidget, title: str, message: str = "") -> None:
	"""Показывает справочное сообщение всплывающей плашкой."""
	InfoBar.info(title, message, parent=parent, duration=TOAST_DURATION_MS)


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

	Единый вид строк там, где список однородный: аккаунты, боты, ключи
	ИИ, файлы на «Видео». Сообщества показываются плитками
	(``CommunityCard``), а отложенные записи — своей карточкой:
	у них другой состав, и загонять их сюда параметрами было бы хуже.
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

	def is_current_id(self, item_id: object) -> bool:
		"""Выбран ли сейчас элемент с данным ``id``.

		Общая проверка «ответ движка не устарел»: пока движок занят,
		ответы задерживаются, и без неё данные элемента A легли бы
		в виджеты уже выбранного элемента B. Контракт: элементы списка
		несут поле ``id`` (все DTO движка ему следуют).
		"""
		current = self.selected()
		return current is not None and getattr(current, "id", None) == item_id

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
#: осознанный дизайн ADR-0016 (унаследован от ADR-0012): интерфейс читает снимок состояния.
QUEUE_POLL_MS = 500


#: Цвета подложки логотипа-заглушки (когда аватара нет): по кругу,
#: чтобы соседние сообщества различались с одного взгляда.
_LOGO_COLORS = ("#e17076", "#eda86c", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae")


def round_pixmap(path: str, size: int) -> QPixmap | None:
	"""Круглая миниатюра из файла (None — файл не читается)."""
	source = QPixmap(path)
	if source.isNull():
		return None
	scaled = source.scaled(
		size,
		size,
		Qt.AspectRatioMode.KeepAspectRatioByExpanding,
		Qt.TransformationMode.SmoothTransformation,
	)
	rounded = QPixmap(size, size)
	rounded.fill(Qt.GlobalColor.transparent)
	painter = QPainter(rounded)
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	clip = QPainterPath()
	clip.addEllipse(0, 0, size, size)
	painter.setClipPath(clip)
	painter.drawPixmap(0, 0, scaled)
	painter.end()
	return rounded


def community_logo(
	parent: QWidget, community_id: int, title: str, avatar_path: str | None, size: int
) -> QLabel:
	"""Логотип сообщества: аватар из кэша, без него — буква на подложке.

	Общий для плиток дашборда и карточек очереди отправки: аватар
	лежит файлом в кэше (``community_stats``), и читают его одинаково.
	Цвет подложки заглушки берётся по id — у одного сообщества он
	не меняется от показа к показу.
	"""
	if avatar_path:
		pixmap = round_pixmap(avatar_path, size)
		if pixmap is not None:
			label = QLabel(parent)
			label.setFixedSize(size, size)
			label.setPixmap(pixmap)
			return label
	label = QLabel(title[:1].upper() or "?", parent)
	label.setFixedSize(size, size)
	label.setAlignment(Qt.AlignmentFlag.AlignCenter)
	color = _LOGO_COLORS[community_id % len(_LOGO_COLORS)]
	label.setStyleSheet(
		f"background: {color}; color: white; border-radius: {size // 2}px;"
		f"font-size: {max(10, size // 2)}px; font-weight: 600;"
	)
	return label


#: Метка поста, у которого времени публикации нет (уйдёт сразу).
SLOT_NOW = "сейчас"

#: Палитра слотов времени: пары «светлая тема, тёмная тема». Цвет нужен,
#: чтобы посты одного времени публикации узнавались в списке одним
#: взглядом, поэтому тона взяты разнотонные, а не оттенки одного.
_SLOT_COLORS = (
	("#0f6cbd", "#62abf5"),  # синий
	("#0f7b6c", "#5fd3bc"),  # бирюзовый
	("#8a5a00", "#f0b429"),  # янтарный
	("#8b2f8b", "#e08ce0"),  # пурпурный
	("#0b6a0b", "#6ccb6c"),  # зелёный
	("#a4262c", "#ff8a8a"),  # красный
	("#5b5fc7", "#a6a9f5"),  # индиго
	("#b3541e", "#ff9a62"),  # оранжевый
)


def slot_label(when: datetime | None) -> str:
	"""Слот поста: «ЧЧ:ММ» местного времени публикации или «сейчас».

	Момент хранится в UTC (так его отдаёт Telegram и хранит очередь),
	а показывается в местном — как пользователь его вводил.
	"""
	if when is None:
		return SLOT_NOW
	return when.astimezone().strftime("%H:%M")


def slot_color(label: str) -> tuple[str, str]:
	"""Цвет метки слота: пара «светлая тема, тёмная тема».

	Цвет выводится из самой метки, поэтому один и тот же слот всегда
	выглядит одинаково — и в разных каналах, и после перезапуска.
	Берётся контрольная сумма, а не встроенный ``hash``: тот
	рандомизируется между запусками, и цвета прыгали бы от запуска
	к запуску. У поста «сейчас» слота нет — он получает приглушённый
	цвет подписи.
	"""
	if label == SLOT_NOW:
		return _SUMMARY_COLORS
	return _SLOT_COLORS[zlib.crc32(label.encode("utf-8")) % len(_SLOT_COLORS)]


def card_signature(item: Any) -> tuple[Any, ...]:
	"""Отпечаток одного элемента — по нему решается обновление его карточки.

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


@dataclass(frozen=True)
class CardPlan:
	"""Что сделать с карточками, чтобы список совпал со снимком очереди.

	Attributes:
		removed: карточки, которых в снимке больше нет.
		added: новые карточки (в порядке показа).
		changed: карточки, чьё содержимое изменилось.
		order: итоговый порядок карточек.
	"""

	removed: list[int]
	added: list[int]
	changed: list[int]
	order: list[int]


def plan_cards(shown: Sequence[Any], known: Mapping[int, tuple[Any, ...]]) -> CardPlan:
	"""План точечного обновления карточек по новому снимку очереди.

	Прежняя панель пересобирала весь список, стоило измениться чему
	угодно в любом элементе. Это и мигало на сотне карточек, и делало
	невозможной правку прямо в карточке: форма с набранным текстом
	умирала от того, что у соседнего поста сменился статус.

	Args:
		shown: элементы снимка в порядке показа.
		known: отпечатки уже показанных карточек (``card_signature``).
	"""
	order = [item.id for item in shown]
	target = set(order)
	return CardPlan(
		removed=[item_id for item_id in known if item_id not in target],
		added=[item.id for item in shown if item.id not in known],
		changed=[
			item.id for item in shown if item.id in known and card_signature(item) != known[item.id]
		],
		order=order,
	)


class _QueueCard:
	"""Карточка элемента очереди: шапка с действиями и тело для правки.

	Часть реализации :class:`QueuePanel` — живёт столько же, сколько
	элемент в очереди, и обновляется точечно: заголовок, состояние
	и кнопки меняются на месте, а тело (форма правки) при этом
	не пересоздаётся — иначе набранный текст умирал бы от того,
	что у соседнего поста сменился статус.
	"""

	def __init__(self, panel: QueuePanel, item: Any) -> None:
		self._panel = panel
		self.item_id = int(item.id)
		self._actions = QWidget(panel.page)
		self._actions_box = QHBoxLayout(self._actions)
		self._actions_box.setContentsMargins(0, 0, 0, 0)
		self._leading = QWidget(panel.page)
		self._leading_box = QHBoxLayout(self._leading)
		self._leading_box.setContentsMargins(0, 0, 0, 0)
		self._leading_box.setSpacing(6)
		self._item = item
		self._bar: ProgressBar | None = None
		self._filled = False  # тело уже наполнено формой правки
		self.widget = CollapsibleCard(
			item.title,
			panel.page,
			trailing=self._actions,
			leading=self._leading,
			keep_summary=True,
		)
		self.widget.expanded_changed.connect(self._on_expanded)
		self.update(item)

	def update(self, item: Any) -> None:
		"""Приводит карточку к новому снимку элемента."""
		self._item = item
		self.refresh_leading()
		self.widget.set_title(item.title)
		self.widget.set_summary(self._panel.subtitle(item))
		editable = self._panel.can_edit(item)
		self.widget.set_expandable(editable)
		if not editable:
			# пост ушёл в отправку: форма в теле уже не про него
			self._reset_body()
		self._fill_actions(item)

	def refresh_leading(self) -> None:
		"""Перерисовывает начало шапки (логотип канала, метка слота).

		Отдельно от :meth:`update`: аватары приезжают из кэша статистики
		позже списка, и к этому моменту снимок элемента не менялся —
		обновлять карточку целиком было бы не с чего.
		"""
		clear_layout(self._leading_box)
		for widget in self._panel.leading_widgets(self._item, self._leading):
			self._leading_box.addWidget(widget)

	def set_progress(self, fraction: float) -> None:
		"""Двигает полосу загрузки (без пересборки карточки)."""
		if self._bar is not None:
			self._bar.setValue(int(fraction * 100))

	def editing(self) -> bool:
		"""Открыта ли в карточке форма правки."""
		return self._filled and self.widget.expanded()

	def collapse(self) -> None:
		"""Закрывает форму: сворачивает карточку и забывает её содержимое.

		Зовётся самой формой — после сохранения (данные устарели)
		и по «Отмене» (человек отказался от правки). Ручное сворачивание
		кликом по шапке тело не трогает: значения полей переживают его,
		как и у карточек параметров на «Видео».
		"""
		self.widget.set_expanded(False)
		self._reset_body()

	def _reset_body(self) -> None:
		"""Забывает форму: следующее раскрытие прочитает свежие данные."""
		if not self._filled:
			return
		clear_layout(self.widget.body)
		self._filled = False

	def _on_expanded(self, expanded: bool) -> None:
		"""Первое раскрытие наполняет тело формой правки (лениво).

		Форма тянет данные из движка (черновик, сообщество, пределы,
		темы) — делать это для всех карточек списка заранее значило бы
		десятки лишних запросов на каждый опрос.
		"""
		if not expanded or self._filled:
			return
		self._filled = True
		self._panel.fill_body(self.item_id, self.widget.body, self.collapse)

	def _fill_actions(self, item: Any) -> None:
		"""Пересобирает кнопки шапки под текущий статус элемента."""
		clear_layout(self._actions_box)
		self._bar = None
		panel = self._panel
		media_path = getattr(item, "media_path", None)
		if media_path:
			# та же кнопка, что у карточек файлов на «Видео» и в пакете
			play = TransparentToolButton(FluentIcon.PLAY, self._actions)
			play.setToolTip("Посмотреть файл (системный плеер)")
			play.clicked.connect(bind(panel.play, media_path))
			self._actions_box.addWidget(play)
		# полоса прогресса — только у активных (WAITING/PENDING не растут)
		if item.status.active():
			bar = ProgressBar(self._actions)
			bar.setRange(0, 100)
			bar.setValue(int(item.progress * 100))
			bar.setFixedWidth(160)
			self._actions_box.addWidget(bar)
			self._bar = bar
		if item.status is JobStatus.ERROR:
			retry = PushButton("Повторить", self._actions)
			retry.clicked.connect(bind(panel.retry, item.id))
			self._actions_box.addWidget(retry)
			action = PushButton("Убрать", self._actions)
			action.clicked.connect(bind(panel.dismiss, item.id))
		else:
			action = PushButton("Отмена", self._actions)
			action.clicked.connect(bind(panel.cancel, item.id))
		self._actions_box.addWidget(action)


class QueuePanel:
	"""Панель очереди движка: опрос, карточки, прогресс, действия.

	Общий каркас панелей для **любой очереди движка на каркасе заданий**
	(ADR-0025): отправка постов (ADR-0016; механика показа унаследована
	от ADR-0012), обработка видео (ADR-0014), обслуживание сообщества
	(ADR-0026). Панель даёт таймер опроса, снятие завершённых с показа
	и точечное обновление карточек — меняется только то, что изменилось.
	Опрос живёт всегда, не только при видимой странице: завершения
	снимаются с показа, а кэш занятости нужен окну для подтверждения
	выхода.

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
		dismiss_finished: ``False`` — панель-зритель (диалог полного
			просмотра): завершёнными владеет панель страницы, зритель
			их только показывает. Две панели над одной очередью
			не должны наперегонки снимать элементы — иначе итоговые
			плашки страницы теряются.
		editable: можно ли раскрыть карточку элемента (правка). Без него
			карточки не раскрываются вовсе.
		fill_body: наполняет тело раскрытой карточки формой правки —
			получает id элемента, компоновку тела и «свернуть карточку».
			Зовётся один раз, при первом раскрытии.
		leading: виджеты в начале шапки карточки (логотип канала, метка
			слота времени) — получает элемент и родителя. Что именно
			показывать, решает владелец панели: очередь обработки видео
			крючок не передаёт, и её шапки начинаются с названия.
		"""
		self._worker = worker
		#: страница-владелец: родитель карточек и плашек (читают карточки).
		self.page = page
		self._box = box
		self._service = service
		#: подпись карточки элемента (читают карточки).
		self.subtitle = subtitle
		self._on_finished = on_finished
		self._on_refreshed = on_refreshed
		self._on_drained = on_drained
		self._max_cards = max_cards
		self._transform = transform
		self._dismiss_finished = dismiss_finished
		self._editable = editable
		self._fill_body = fill_body
		self._leading = leading
		self._show_error = error_reporter(page)
		self._cards: dict[int, _QueueCard] = {}
		self._signatures: dict[int, tuple[Any, ...]] = {}
		self._handled: set[int] = set()  # завершённые, уже учтённые
		self._busy = False
		self._active = False
		timer = QTimer(page)
		timer.setInterval(QUEUE_POLL_MS)
		timer.timeout.connect(self.poll)
		timer.start()

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

	def can_edit(self, item: Any) -> bool:
		"""Раскрывается ли карточка этого элемента (правка на месте)."""
		return self._fill_body is not None and self._editable is not None and self._editable(item)

	def fill_body(self, item_id: int, body: QVBoxLayout, collapse: Callable[[], None]) -> None:
		"""Наполняет тело раскрытой карточки (крючок владельца панели)."""
		if self._fill_body is not None:
			self._fill_body(item_id, body, collapse)

	def leading_widgets(self, item: Any, parent: QWidget) -> list[QWidget]:
		"""Виджеты начала шапки карточки (крючок владельца панели)."""
		return [] if self._leading is None else self._leading(item, parent)

	def refresh_leading(self) -> None:
		"""Перерисовывает начала шапок всех карточек.

		Зовётся, когда изменилось не состояние очереди, а то, из чего
		рисуется шапка: приехали аватары сообществ из кэша статистики.
		"""
		for card in self._cards.values():
			card.refresh_leading()

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
		self._sync_cards(visible if self._max_cards is None else visible[: self._max_cards])
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

	def _sync_cards(self, shown: list[Any]) -> None:
		"""Приводит список карточек к снимку, трогая только изменившееся."""
		plan = plan_cards(shown, self._signatures)
		by_id = {item.id: item for item in shown}
		for item_id in plan.removed:
			self._drop_card(item_id)
		for item_id in plan.added:
			card = _QueueCard(self, by_id[item_id])
			self._cards[item_id] = card
			self._box.addWidget(card.widget)
		for item_id in plan.changed:
			self._cards[item_id].update(by_id[item_id])
		self._signatures = {item.id: card_signature(item) for item in shown}
		for index, item_id in enumerate(plan.order):
			widget = self._cards[item_id].widget
			if self._box.indexOf(widget) != index:
				self._box.insertWidget(index, widget)
		for item in shown:  # прогресс — без пересборки карточек
			self._cards[item.id].set_progress(item.progress)

	def _drop_card(self, item_id: int) -> None:
		"""Убирает карточку элемента, покинувшего показ.

		Если в ней правили пост, молчать нельзя: набранное пропадает
		вместе с карточкой, и человек должен понимать, почему.
		"""
		card = self._cards.pop(item_id, None)
		self._signatures.pop(item_id, None)
		if card is None:
			return
		if card.editing():
			self._show_error("Пост покинул очередь — незаконченная правка не сохранена.")
		self._box.removeWidget(card.widget)
		card.widget.setParent(None)
		card.widget.deleteLater()


def counter_text(length: int, limit: int) -> str:
	"""Подпись счётчика символов под полем текста.

	Превышение называется числом: «сократите» без цифры оставляет
	пользователя считать самому.
	"""
	if length <= limit:
		return f"{length} / {limit}"
	return f"{length} / {limit} — на {length - limit} больше предела Telegram"


class CharCounter:
	"""Счётчик символов под полем текста поста.

	Длина считается так же, как её считает Telegram
	(:func:`telegram_text_length`): счётчик и проверка движка не должны
	расходиться на эмодзи. Предел меняется на ходу — от типа контента
	(подпись к файлу вчетверо короче поста без вложения) и от канала
	(у Premium-публикатора пределы выше).
	"""

	def __init__(
		self,
		parent: QWidget,
		layout: QVBoxLayout,
		edit: TextEdit,
		limit: int = TEXT_LENGTH_LIMIT,
	) -> None:
		"""Args:
		parent: владелец подписи.
		layout: компоновка, в которую встаёт счётчик (обычно под полем).
		edit: поле текста, за которым он следит.
		limit: стартовый предел (уточняется :meth:`set_limit`).
		"""
		self._edit = edit
		self._limit = limit
		self.label = CaptionLabel("", parent)
		self.label.setAlignment(Qt.AlignmentFlag.AlignRight)
		layout.addWidget(self.label)
		edit.textChanged.connect(self.refresh)
		self.refresh()

	def set_limit(self, limit: int) -> None:
		"""Меняет действующий предел и перерисовывает счётчик."""
		self._limit = limit
		self.refresh()

	def refresh(self) -> None:
		"""Пересчитывает длину и красит подпись по факту превышения."""
		length = telegram_text_length(self._edit.toPlainText())
		self.label.setText(counter_text(length, self._limit))
		self.label.setTextColor(*(ERROR_COLORS if length > self._limit else _SUMMARY_COLORS))


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
		self.setTextColor(*ERROR_COLORS)
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


class WarningLabel(CaptionLabel):
	"""Янтарная подпись-предупреждение (единые цвета обеих тем).

	Не ошибка: сообщает о последствии выбора, ничего не запрещая
	(например, об увеличении кадра при выбранном разрешении). Пустой
	текст прячет подпись, непустой — показывает.
	"""

	def __init__(self, parent: QWidget) -> None:
		# текст базовому классу не передаём — причина в комментарии
		# конструктора ErrorLabel (рекурсия в диспетчере QFluentWidgets)
		super().__init__(parent)
		self.setTextColor("#9d5d00", "#fff100")
		self.hide()

	def set_note(self, message: str) -> None:
		"""Показывает предупреждение; пустая строка прячет подпись."""
		self.setText(message)
		self.setVisible(bool(message))


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


def list_area(parent: QWidget, spacing: int) -> tuple[ScrollArea, QVBoxLayout]:
	"""Прокручиваемый список для рабочего окна.

	Высота не задаётся: область добавляется в окно с растяжением
	и занимает свободное место, а полосу прокрутки показывает сама,
	когда содержимое не влезло. Считать высоту руками (прежний
	``fixed_list_area``) было нужно только диалогу-вопросу, который
	растёт под содержимое и своего размера не имеет.

	Returns:
		Область (её добавляет в окно вызывающий — обычно
		``content.addWidget(area, stretch=1)``) и компоновка
		контейнера: в неё складываются строки.
	"""
	area = ScrollArea(parent)
	container = QWidget(area)
	box = QVBoxLayout(container)
	box.setContentsMargins(0, 0, 0, 0)
	box.setSpacing(spacing)
	area.setWidget(container)
	area.setWidgetResizable(True)
	area.enableTransparentBackground()
	return area, box


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
		self._time.setText(QTime.currentTime().addSecs(DEFAULT_SCHEDULE_OFFSET_S).toString("HH:mm"))
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
			self._time.setText(
				QTime.currentTime().addSecs(DEFAULT_SCHEDULE_OFFSET_S).toString("HH:mm")
			)
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

	def set_when(self, moment: datetime | None) -> None:
		"""Показывает заданный момент: None — «сейчас», иначе дата и время.

		Момент приходит в UTC (так его хранит очередь отправки)
		и показывается в местном времени — симметрично :meth:`when`.
		Дата ставится как есть, без переноса на завтра: правится
		существующий пост, и подменять его время самовольно нельзя —
		прошедшее увидит проверка при сохранении.
		"""
		self._now_switch.setChecked(moment is None)
		if moment is None:
			return
		local = moment.astimezone()
		self._date.setDate(QDate(local.year, local.month, local.day))
		self._time.setText(local.strftime("%H:%M"))

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


#: Размер рабочего окна по умолчанию (ширина, высота в пикселях).
#: Не ограничение, а разумная точка старта: окно меняется мышью.
WORK_DIALOG_SIZE = (640, 560)


class WorkDialog(QDialog):
	"""Рабочее окно: обычное окно приложения с рамками от системы.

	Второй вид окон интерфейса — рядом с диалогом-вопросом
	(``MessageBoxBase``), а не вместо него. Диалог-вопрос прибит маской
	к главному окну, не двигается и растёт под содержимое: это верно
	для короткого вопроса («удалить?», «введите код»), но не для экрана,
	на котором работают.

	Рамки и заголовок рисует система, а не мы. Безрамочный вариант
	(как у главного окна) обходился слишком дорого: в нём приложение
	само считает, у какого края курсор, а под Wayland оно не знает
	своего места на экране — для дочернего окна расчёт разъезжался,
	и полосу захвата пришлось бы чинить своими руками. Системные рамки
	снимают этот класс задач целиком: перетаскивание, растягивание
	за любой край и угол, привязку к краям экрана и кнопки окна даёт
	оконный менеджер. Плата — заголовок выглядит системным, а не
	в стиле остального приложения.

	Собственный размер окна — не украшение, а условие штатной механики
	Qt: пока высота бралась «по содержимому» (как у диалога-вопроса),
	область прокрутки внутри росла вместе с содержимым и полосе
	неоткуда было взяться. Здесь окно имеет свой размер, компоновка
	раздаёт его детям, и представление (таблица, список) или
	``ScrollArea`` сами показывают полосу, когда содержимое не влезло, —
	высоты нигде не считаются.

	Содержимое кладётся в :attr:`content`, кнопки — в :attr:`buttons`
	(нижняя строка, прижата вправо). Показывается той же обёрткой
	:func:`exec_dialog`, что и диалоги-вопросы; клавиша Esc закрывает
	окно штатно.
	"""

	def __init__(
		self,
		title: str,
		parent: QWidget,
		size: tuple[int, int] = WORK_DIALOG_SIZE,
	) -> None:
		"""Args:
		title: заголовок окна (показывается в его шапке).
		parent: окно-родитель (обычно ``self.window()`` страницы).
		size: стартовый размер (ширина, высота); дальше — мышью.
		"""
		super().__init__(parent)
		self.setWindowTitle(title)
		self.resize(*size)
		# уголок растягивания в правом нижнем углу: там, где система
		# рисует тонкие рамки, он даёт заведомую точку захвата
		self.setSizeGripEnabled(True)
		# фон окна под текущую тему (тот же лист стилей, что у диалогов
		# библиотеки) — при смене темы применяется заново, без нашего участия
		FluentStyleSheet.DIALOG.apply(self)
		spacing = density.spacing()
		root = QVBoxLayout(self)
		root.setContentsMargins(*spacing.page_margins)
		root.setSpacing(spacing.block_spacing)
		self.content = QVBoxLayout()
		self.content.setSpacing(spacing.row_spacing)
		root.addLayout(self.content, stretch=1)
		self.buttons = QHBoxLayout()
		self.buttons.addStretch()
		root.addLayout(self.buttons)

	def add_close_button(self, text: str = "Закрыть") -> PushButton:
		"""Добавляет кнопку закрытия окна в нижнюю строку.

		Для окон, где правки применяются сразу и «отменить всё» нечем:
		кнопка дублирует крестик заголовка — привычный выход для тех,
		кто ищет его внизу.
		"""
		button = PushButton(text, self)
		button.clicked.connect(self.accept)
		self.buttons.addWidget(button)
		return button

	def add_accept_buttons(
		self, accept_text: str, cancel_text: str = "Отмена"
	) -> PrimaryPushButton:
		"""Добавляет пару «принять / отмена» в нижнюю строку.

		Returns:
			Кнопку принятия — окна включают и выключают её по мере
			готовности (например, пока не выбран ни один файл).
		"""
		# кнопка отмены окну потом не нужна — её состоянием никто
		# не управляет, в отличие от кнопки принятия
		cancel = PushButton(cancel_text, self)
		cancel.clicked.connect(self.reject)
		self.buttons.addWidget(cancel)
		self.accept_button = PrimaryPushButton(accept_text, self)
		self.accept_button.clicked.connect(self.accept)
		self.buttons.addWidget(self.accept_button)
		return self.accept_button

	def validate(self) -> bool:
		"""Крючок перед закрытием по «принять»: False оставляет окно.

		Тот же контракт, что у диалога-вопроса библиотеки: окно
		показывает причину отказа и не закрывается, а введённое
		не пропадает.
		"""
		return True

	def accept(self) -> None:
		"""Закрывает окно принятием, если :meth:`validate` разрешает."""
		if self.validate():
			super().accept()


class FormDialog(MessageBoxBase):
	"""Диалог с набором текстовых полей.

	``validator`` — правило пригодности введённого: получает словарь
	«ключ поля → текст», возвращает текст ошибки или None («всё годно»).
	При ошибке диалог показывает её и НЕ закрывается — введённое
	не пропадает (крючок ``validate`` библиотеки, как в диалоге
	настроек канала). ``initial`` — начальные значения полей
	(для диалогов правки существующего: например, пометки аккаунта).
	"""

	def __init__(
		self,
		title: str,
		fields: list[tuple[str, str]],
		parent: QWidget,
		accept_text: str = "Добавить",
		password_fields: tuple[str, ...] = (),
		validator: Callable[[dict[str, str]], str | None] | None = None,
		initial: dict[str, str] | None = None,
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
			if initial and key in initial:
				edit.setText(initial[key])
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
