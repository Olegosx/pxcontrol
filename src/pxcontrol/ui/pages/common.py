"""Общие помощники страниц: привязка обработчиков, диалоги, плашки."""

from __future__ import annotations

import colorsys
import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Generic, TypeVar

from PySide6.QtCore import QDate, QEvent, QObject, QSize, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import (
	QColor,
	QDesktopServices,
	QFont,
	QImage,
	QKeyEvent,
	QMouseEvent,
	QResizeEvent,
)
from PySide6.QtWidgets import (
	QDialog,
	QFileDialog,
	QGraphicsOpacityEffect,
	QGridLayout,
	QHBoxLayout,
	QLabel,
	QLayout,
	QSizePolicy,
	QVBoxLayout,
	QWidget,
)
from qfluentwidgets import (
	AvatarWidget,
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	EditableComboBox,
	FluentIcon,
	FluentStyleSheet,
	HorizontalSeparator,
	IconWidget,
	InfoBadge,
	InfoBar,
	InfoLevel,
	LineEdit,
	MessageBox,
	MessageBoxBase,
	Pivot,
	PivotItem,
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
	getFont,
)

from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.publish_queue import QueueItemDto
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
from pxcontrol.ui.theme import ACCENT_COLOR

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
	# опрос — вид содержимого без файла (ADR-0033, C5): фильтра
	# у него нет, вместо списка файлов форма показывает его поля
	("Опрос", MediaKind.POLL, ""),
]


#: Подпись вложения, которого приложение не создаёт (опрос, геопозиция…):
#: такие приходят только из Telegram — в отложенных, поставленных из клиента.
READ_ONLY_KIND_LABEL = "Вложение"


def kind_label(kind: MediaKind) -> str:
	"""Подпись типа контента («Видео», «Файл»…) для сообщений и сегментов.

	Перечень ``CONTENT_KINDS`` знает только виды, которые человек
	выбирает в форме; вид, который приложение лишь читает, получает
	общую подпись — иначе карточка отложки с опросом падала бы поиском.
	"""
	if not kind.creatable:
		return READ_ONLY_KIND_LABEL
	return next(label for label, item_kind, _filter in CONTENT_KINDS if item_kind is kind)


def kind_file_filter(kind: MediaKind) -> str:
	"""Фильтр диалога выбора файла для типа контента.

	У видов без файла (текст, опрос) он пуст: выбирать нечего.
	"""
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

	def set_text(self, text: str) -> None:
		"""Меняет полный текст: наблюдатель у надписи всегда один."""
		self._text = text
		self._apply()

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

	Повторный вызов обновляет текст прежнему наблюдателю, а не заводит
	второго: живые карточки обновляются на месте (активность аккаунта —
	раз в пять секунд), а Qt вызывает фильтры от позже установленного
	к раньше установленному — и при изменении ширины последнее слово
	осталось бы за самым старым, то есть надпись вернула бы давно
	устаревший текст.
	"""
	watcher = label.findChild(_Elider)
	if watcher is not None:
		watcher.set_text(text)
		return
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


@dataclass(frozen=True)
class QueueCounts:
	"""Сводка очереди одного сообщества для карточки дашборда и шапки страницы.

	Attributes:
		planned: неотправленное без ошибок — включая ждущих слота.
		waiting: из них ждут слота отложек (ADR-0016) — второе число.
		errors: элементы с ошибкой: они ждут повтора и требуют внимания,
			поэтому в «к отправке» не входят.
	"""

	planned: int = 0
	waiting: int = 0
	errors: int = 0


def queue_counts(items: list[QueueItemDto]) -> dict[int, QueueCounts]:
	"""Считает сводку очереди по сообществам (чистая функция).

	Правило показа — предметное (ADR-0016), поэтому живёт отдельно
	от вёрстки и закрыто тестом: в вёрстке его проверить нечем.
	"""
	counts: dict[int, QueueCounts] = {}
	for item in items:
		if item.status.left_queue():
			continue
		current = counts.get(item.community_id, QueueCounts())
		if item.status is JobStatus.ERROR:
			current = replace(current, errors=current.errors + 1)
		else:
			current = replace(
				current,
				planned=current.planned + 1,
				waiting=current.waiting + (item.status is JobStatus.WAITING),
			)
		counts[item.community_id] = current
	return counts


_NUMBER_RE = re.compile(r"\d[\d\u202f]*")


def bold_numbers(text: str) -> str:
	"""Размечает числа в тексте полужирным (rich text для ``BodyLabel``).

	Макет карточки: «**5** к отправке · **2** ждут». Числа с узким
	неразрывным пробелом (``format_count``) считаются одним числом.
	Живёт рядом с остальным форматированием чисел: карточки сообществ
	и карточки исполнителей пользуются им одинаково, и раздел
	«Пользователи и боты» тянул ради него весь модуль чужого дашборда.
	"""
	return _NUMBER_RE.sub(lambda m: f"<b>{m.group(0)}</b>", html.escape(text))


def plural(count: int, one: str, few: str, many: str) -> str:
	"""Форма слова по числу (правила русского языка).

	«1 подписчик», «2 подписчика», «5 подписчиков», «11 подписчиков»,
	«21 подписчик». Формы передаются явно — склонять слово автоматически
	приложение не берётся.
	"""
	tail = abs(count) % 100
	if 11 <= tail <= 19:
		return many
	tail %= 10
	if tail == 1:
		return one
	if 2 <= tail <= 4:
		return few
	return many


def format_count(value: int) -> str:
	"""Число с разделителем тысяч: «18 420».

	Разделитель — узкий неразрывный пробел: число не рвётся на перенос
	и не расходится, как с обычным пробелом.
	"""
	return f"{value:,}".replace(",", "\u202f")


# --- оформление: только штатные элементы библиотеки ------------------------------
#
# Правило проекта (ADR-0023, п. 5): интерфейс собирается из штатных
# элементов QFluentWidgets, без собственных стилей и рисования. Цвет
# текста надписей задаётся их же API ``setTextColor`` — пары ниже
# для него (светлая тема, тёмная), это не листы стилей.

#: Цвета текста для ``setTextColor`` — одна пара на роль (спека страницы
#: сообщества, раздел 1): акцент, ошибка, предупреждение, приглушённый.
ACCENT_TEXT = (ACCENT_COLOR, ACCENT_COLOR)
ERROR_TEXT = ("#c42b1c", "#ff99a4")
WARNING_TEXT = ("#9d5d00", "#fff100")
DIM_TEXT = ("#5f5f5f", "#9c9c9c")


def font_px(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
	"""Шрифт библиотеки нужного кегля (уважает масштаб из настроек)."""
	font: QFont = getFont(size, weight)
	return font


def tinted(label: Any, pair: tuple[str, str]) -> Any:
	"""Красит библиотечную надпись её же API ``setTextColor`` (обе темы)."""
	label.setTextColor(QColor(pair[0]), QColor(pair[1]))
	return label


#: Кнопка в строке списка по макету: ниже штатной (28 вместо 33), кегль 13.
LIST_BUTTON_HEIGHT = 28
LIST_BUTTON_FONT_PX = 13


def list_button(text: str, parent: QWidget, *, height: int = LIST_BUTTON_HEIGHT) -> PushButton:
	"""Штатная ``PushButton`` размером строки списка (макет: 28 / 13 px)."""
	button = PushButton(text, parent)
	button.setFixedHeight(height)
	button.setFont(font_px(LIST_BUTTON_FONT_PX))
	return button


def flow_columns(width: int, min_width: int, spacing: int) -> int:
	"""Сколько карточек шириной не меньше ``min_width`` помещается в ``width``.

	Между колонками ``spacing``; меньше одной колонки не бывает.
	"""
	return max(1, (width + spacing) // (min_width + spacing))


class FlowGrid(QWidget):
	"""Потоковая сетка карточек: число колонок — по своей ширине, колонки тянутся.

	Общая сборка (дашборд, «Обзор» сообщества): карточки перекладываются
	при смене числа колонок — на широком окне в ряд встаёт больше,
	на узком меньше; на каждое движение окна перекладывать незачем.
	Высота строго по содержимому: лишнее место страницы забирает её
	растяжка, а не сетка. Сетка усыновляет карточки.
	"""

	def __init__(
		self, cards: Sequence[QWidget], parent: QWidget, *, min_width: int, spacing: int
	) -> None:
		super().__init__(parent)
		self._cards = list(cards)
		self._min_width = min_width
		self._spacing = spacing
		self._columns = 0
		self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
		self._grid = QGridLayout(self)
		self._grid.setContentsMargins(0, 0, 0, 0)
		self._grid.setHorizontalSpacing(spacing)
		self._grid.setVerticalSpacing(spacing)
		self._place(1)

	def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — API Qt
		super().resizeEvent(event)
		columns = flow_columns(self.width(), self._min_width, self._spacing)
		if columns != self._columns:
			self._place(columns)
			# высота сетки — по новой раскладке: без этого родитель оставил бы
			# высоту от прежнего числа колонок, и строки растянулись бы под неё
			self.updateGeometry()

	def set_cards(self, cards: Sequence[QWidget]) -> None:
		"""Меняет состав карточек и перекладывает их по текущим колонкам.

		Карточки, которых в новом составе нет, из сетки снимаются (удаляет
		их владелец), новые усыновляются; неизменившиеся не пересоздаются —
		на этом стоит обновление дашбордов по отпечатку (``dashboard``).
		Число колонок не пересчитывается: ширина сетки не менялась.
		"""
		self._cards = list(cards)
		self._place(max(1, self._columns))
		self.updateGeometry()

	def _place(self, columns: int) -> None:
		"""Раскладывает карточки по колонкам, колонки — на всю ширину."""
		previous = self._columns
		self._columns = columns
		while self._grid.count():
			self._grid.takeAt(0)
		for index, card in enumerate(self._cards):
			self._grid.addWidget(card, index // columns, index % columns)
		for column in range(max(columns, previous)):
			self._grid.setColumnStretch(column, 1 if column < columns else 0)


def dim_widget(widget: QWidget, opacity: float) -> None:
	"""Приглушает виджет штатным эффектом Qt (выключенное сообщество в макете)."""
	effect = QGraphicsOpacityEffect(widget)
	effect.setOpacity(opacity)
	widget.setGraphicsEffect(effect)


def section_header(
	parent: QWidget,
	title: str,
	count: int | None = None,
	*,
	icon: FluentIcon | None = None,
	trailing: Sequence[QWidget] | None = None,
) -> QWidget:
	"""Заголовок раздела: значок, подпись капителью, число, разделитель.

	Штатные элементы: ``IconWidget``, ``CaptionLabel``,
	``HorizontalSeparator``; ``trailing`` — виджеты справа (кнопки
	заголовка списка).
	"""
	box = QWidget(parent)
	layout = QHBoxLayout(box)
	layout.setContentsMargins(0, 0, 0, 0)
	layout.setSpacing(12)
	if icon is not None:
		icon_widget = IconWidget(box)
		icon_widget.setIcon(icon)
		icon_widget.setFixedSize(14, 14)
		layout.addWidget(icon_widget)
	layout.addWidget(CaptionLabel(title.upper(), box))
	if count is not None:
		layout.addWidget(tinted(CaptionLabel(str(count), box), DIM_TEXT))
	layout.addWidget(HorizontalSeparator(box), stretch=1)
	for widget in trailing or []:
		layout.addWidget(widget)
	return box


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


def open_link(url: str) -> None:
	"""Открывает ссылку системным браузером (пост в Telegram, справка)."""
	QDesktopServices.openUrl(QUrl(url))


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
		stacked: bool = False,
	) -> None:
		"""``trailing`` — виджет с кнопками в правом краю шапки (например,
		просмотр и удаление у карточки файла); ``leading`` — виджет перед
		названием (например, чекбокс выбора). Клики по обоим остаются
		их виджетам и карточку не сворачивают (Qt не передаёт их шапке).
		``keep_summary`` — не прятать сводку у развёрнутой карточки: у
		элемента очереди она говорит состояние («ждёт слота», «ошибка:…»),
		и оно нужно как раз тогда, когда карточку раскрыли для правки.
		``stacked`` — сводка под названием, а не в строку с ним (макет
		страницы сообщества); в этом режиме доступен :meth:`set_progress`
		(полоса под названием)."""
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
		head_row.setContentsMargins(*((14, 10, 14, 10) if stacked else (12, 8, 16, 8)))
		head_row.setSpacing(12 if stacked else 8)
		if stacked:
			self._chevron.setIconSize(QSize(13, 13))
		head_row.addWidget(self._chevron)
		if leading is not None:
			leading.setParent(header)
			head_row.addWidget(leading)
		self._title = StrongBodyLabel(title, header)
		self._keep_summary = keep_summary
		self._expandable = True
		self._summary_text = ""
		self._summary = CaptionLabel("", header)
		self._summary.setTextColor(*DIM_TEXT)
		# сводка занимает остаток шапки и обрезается, а не распирает форму
		self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		self._progress_row: QWidget | None = None
		self._bar: ProgressBar | None = None
		self._progress_text: CaptionLabel | None = None
		if stacked:
			# колонка: название сверху, сводка (или полоса прогресса) под ним;
			# название — обычным начертанием (макет: 14 px, без жирного)
			self._title.setFont(font_px(14))
			self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
			column = QVBoxLayout()
			column.setSpacing(2)
			column.addWidget(self._title)
			column.addWidget(self._summary)
			self._progress_row = QWidget(header)
			progress = QHBoxLayout(self._progress_row)
			progress.setContentsMargins(0, 2, 0, 0)
			progress.setSpacing(10)
			self._bar = ProgressBar(self._progress_row)
			self._bar.setRange(0, 100)
			self._bar.setMaximumWidth(220)
			progress.addWidget(self._bar, stretch=1)
			self._progress_text = CaptionLabel("", self._progress_row)
			self._progress_text.setTextColor(*DIM_TEXT)
			progress.addWidget(self._progress_text)
			column.addWidget(self._progress_row)
			self._progress_row.hide()
			head_row.addLayout(column, stretch=1)
		else:
			head_row.addWidget(self._title)
			head_row.addWidget(self._summary, stretch=1)
			head_row.addStretch()
		if trailing is not None:
			trailing.setParent(header)
			head_row.addWidget(trailing)
		outer.addWidget(header)
		# разделитель между шапкой и телом (макет); виден вместе с телом
		self._divider: HorizontalSeparator | None = None
		if stacked:
			self._divider = HorizontalSeparator(self)
			self._divider.setContentsMargins(14, 0, 14, 0)
			outer.addWidget(self._divider)
			self._divider.hide()
		self._body = QWidget(self)
		#: Компоновка тела — раздел добавляет сюда своё содержимое.
		self.body = QVBoxLayout(self._body)
		if stacked:
			self.body.setContentsMargins(14, 0, 14, 14)
			self.body.setSpacing(10)
		else:
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
		if self._divider is not None:
			self._divider.setVisible(expanded)
		icon = FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED
		self._chevron.setIcon(icon)
		self._refresh_summary()
		if changed:
			self.expanded_changed.emit(expanded)

	def set_summary(self, text: str, *, alert: bool = False) -> None:
		"""Сводка значений для шапки.

		По умолчанию видна только у свёрнутой карточки: у развёрнутой
		она дублировала бы поля прямо под шапкой. Исключение —
		``keep_summary`` (см. конструктор). ``alert`` — сводка говорит
		об ошибке: цвет ошибки (обе темы).
		"""
		self._summary_text = text
		if alert:
			self._summary.setTextColor(*ERROR_TEXT)
		else:
			self._summary.setTextColor(*DIM_TEXT)
		self._refresh_summary()

	def set_progress(self, fraction: float | None, text: str = "") -> None:
		"""Полоса под названием (только ``stacked``): доля 0..1 и подпись.

		None — работа не идёт: полоса прячется, сводка возвращается.
		"""
		if self._progress_row is None or self._bar is None or self._progress_text is None:
			return
		if fraction is None:
			self._progress_row.hide()
			self._refresh_summary()
			return
		self._bar.setValue(int(fraction * 100))
		self._progress_text.setText(text)
		self._progress_row.show()
		self._summary.hide()

	def _refresh_summary(self) -> None:
		self._summary.setText(self._summary_text)
		visible = bool(self._summary_text) and (self._keep_summary or not self._body.isVisible())
		if self._progress_row is not None and self._progress_row.isVisible():
			visible = False
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


#: Цвета подложки логотипа-заглушки (когда аватара нет): по кругу,
#: чтобы соседние сообщества различались с одного взгляда.
_LOGO_COLORS = ("#e17076", "#eda86c", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae")


#: Сколько картинок аватаров держать прочитанными (сообществ и исполнителей
#: в приложении десятки; ключ — путь и время изменения файла).
_AVATAR_CACHE_SIZE = 256


@lru_cache(maxsize=_AVATAR_CACHE_SIZE)
def _avatar_image(path: str, mtime_ns: int) -> QImage:
	"""Читает картинку аватара с диска — один раз на путь и версию файла."""
	del mtime_ns  # часть ключа: сменился файл — прочитать заново
	return QImage(path)


def avatar_image(path: str) -> QImage | None:
	"""Картинка аватара из кэша по пути; None — файла нет или он не картинка.

	Кэш статистики перезаписывает файл при смене аватара сообщества —
	ключ кэша включает время изменения, поэтому новая картинка
	подхватывается сама, а неизменная не перечитывается: прежде каждая
	карточка очереди читала файл с диска при каждой пересборке шапки.
	"""
	try:
		stamp = Path(path).stat().st_mtime_ns
	except OSError:
		return None  # файл исчез из кэша — карточка покажет букву
	image = _avatar_image(path, stamp)
	return None if image.isNull() else image


def entity_avatar(
	parent: QWidget, seed: int, title: str, avatar_path: str | None, size: int
) -> AvatarWidget:
	"""Аватар сущности: картинка из кэша, без неё — буква на подложке.

	Общий для сообществ (плитки дашборда, карточки очереди, шапка
	страницы) и для исполнителей (карточки раздела «Пользователи
	и боты»): правило одно — картинка, если есть, иначе первая буква
	названия на цвете, выбранном по ``seed``. Цвет по идентификатору,
	а не случайный: у одной и той же записи он всегда один.
	Виджет — штатный ``AvatarWidget`` библиотеки: картинку он кадрирует
	по кругу сам, без неё рисует первую букву текста на подложке.
	Картинка отдаётся готовым ``QImage`` из кэша (:func:`avatar_image`),
	чтобы файл не читался при каждой сборке шапки.
	"""
	logo = AvatarWidget(parent)
	logo.setRadius(size // 2)
	logo.setText(title[:1].upper() or "?")
	color = QColor(_LOGO_COLORS[seed % len(_LOGO_COLORS)])
	logo.setBackgroundColor(color, color)
	image = avatar_image(avatar_path) if avatar_path else None
	if image is not None:
		logo.setImage(image)
		logo.setRadius(size // 2)
	return logo


#: Метка поста, у которого времени публикации нет (уйдёт сразу).
SLOT_NOW = "сейчас"

#: Шаг раскладки часов по цветовому кругу. Семь взаимно просто с 24,
#: поэтому час → тон — соответствие без совпадений, но **соседние часы
#: оказываются на разных сторонах круга** (09:00 синий, 10:00 малиновый,
#: 11:00 жёлтый). Это и нужно: в очереди посты отсортированы по времени,
#: то есть рядом всегда стоят соседние часы — их и важнее всего различать.
#: Плавная радуга «по ходу суток» давала бы между соседями 15°, а такие
#: тона в списке сливаются.
_SLOT_HUE_STRIDE = 7

#: Насколько тон уезжает внутри часа (градусы на полный час): 12:00
#: и 12:30 — родственные, но различимые. Разницу внутри часа делят
#: между собой сдвиг тона и глубина: одной глубины не хватало —
#: к 40-й минуте метка выцветала почти в белый.
_SLOT_MINUTE_DRIFT = 18.0

#: Насыщенность метки: на светлом фоне глубже, на тёмном мягче.
#: К концу часа она растёт: полутон делается светлее, а светлый цвет
#: без добавки насыщенности выцветает в белый и теряет свой тон.
_SLOT_SATURATION_LIGHT = 0.80
_SLOT_SATURATION_DARK = 0.62
_SLOT_SATURATION_GAIN = 0.16

#: Требуемый контраст метки к фону карточки — им задаётся светлота.
#: Задавать светлоту числом нельзя: при одной и той же светлоте жёлтый
#: и синий читаются совершенно по-разному (замер 18.09.2026: худший
#: случай давал 2.3 при норме AA 4.5). Нижняя граница — для ровного часа,
#: верхняя — для 59-й минуты: так минуты внутри часа и дают полутон,
#: не рискуя читаемостью.
_SLOT_CONTRAST_MIN = 4.8
_SLOT_CONTRAST_MAX = 6.4

#: Фон карточки: белый в светлой теме (худший случай для тёмного текста)
#: и #2b2b2b в тёмной — снято с настоящей карточки QFluentWidgets.
_SLOT_BG_LIGHT = (1.0, 1.0, 1.0)
_SLOT_BG_DARK = (0x2B / 255, 0x2B / 255, 0x2B / 255)


def _relative_luminance(color: tuple[float, float, float]) -> float:
	"""Относительная яркость цвета по WCAG 2.1 (каналы 0..1)."""

	def channel(value: float) -> float:
		return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

	red, green, blue = (channel(part) for part in color)
	return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(color: tuple[float, float, float], background: tuple[float, float, float]) -> float:
	"""Контраст цвета к фону по WCAG (1..21; 4.5 — норма для текста)."""
	first, second = _relative_luminance(color), _relative_luminance(background)
	return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _tone(hue: float, saturation: float, target: float, *, light: bool) -> str:
	"""Цвет нужного тона, чья светлота даёт требуемый контраст к фону.

	Контраст монотонен по светлоте (на белом фоне падает, на тёмном
	растёт), поэтому светлота ищется делением отрезка пополам —
	два десятка шагов дают точность, которой глазу с запасом хватает.

	Returns:
		Цвет в виде ``#rrggbb``.
	"""
	background = _SLOT_BG_LIGHT if light else _SLOT_BG_DARK
	low, high = (0.05, 0.60) if light else (0.40, 0.95)
	for _ in range(24):
		middle = (low + high) / 2
		reached = _contrast(colorsys.hls_to_rgb(hue / 360, middle, saturation), background)
		too_pale = reached < target
		if light:
			low, high = (low, middle) if too_pale else (middle, high)
		else:
			low, high = (middle, high) if too_pale else (low, middle)
	red, green, blue = colorsys.hls_to_rgb(hue / 360, (low + high) / 2, saturation)
	return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _slot_minutes(label: str) -> tuple[int, int] | None:
	"""Разбирает метку «ЧЧ:ММ» (None — это не время)."""
	hours, _, minutes = label.partition(":")
	if not hours.isdigit() or not minutes.isdigit():
		return None
	hour, minute = int(hours), int(minutes)
	if not (0 <= hour < 24 and 0 <= minute < 60):
		return None
	return hour, minute


def slot_label(when: datetime | None) -> str:
	"""Слот поста: «ЧЧ:ММ» местного времени публикации или «сейчас».

	Момент хранится в UTC (так его отдаёт Telegram и хранит очередь),
	а показывается в местном — как пользователь его вводил.
	"""
	if when is None:
		return SLOT_NOW
	return when.astimezone().strftime("%H:%M")


@lru_cache(maxsize=256)
def slot_color(label: str) -> tuple[str, str]:
	"""Цвет метки слота: пара «светлая тема, тёмная тема».

	Цвет **выводится из самого времени**: час задаёт тон (24 часа —
	24 тона по кругу, шаг :data:`_SLOT_HUE_STRIDE` разводит соседние
	часы по разным сторонам круга), минуты — полутон того же тона.
	Поэтому один и тот же слот всегда выглядит одинаково — в разных
	каналах, в разных списках и после перезапуска, — а разные слоты
	никогда не совпадают.

	Прежде цвет брался хешем подписи по палитре из восьми цветов: на
	24 часа совпадения были неизбежны, и какие именно слоты сольются,
	решала контрольная сумма, а не смысл.

	Светлота не задана числом, а подобрана под контраст к фону карточки
	(:func:`_tone`) — иначе жёлтые слоты оказывались бы заметно бледнее
	синих. У поста «сейчас» слота нет: он получает приглушённый цвет
	подписи.

	Результат кэшируется: слотов немного, а метку красят при каждой
	перерисовке списка карточек.
	"""
	parsed = _slot_minutes(label)
	if parsed is None:  # «сейчас» и всё, что не время
		return DIM_TEXT
	hour, minute = parsed
	part = minute / 60
	hue = ((hour * _SLOT_HUE_STRIDE) % 24) * (360 / 24) + part * _SLOT_MINUTE_DRIFT
	target = _SLOT_CONTRAST_MIN + (_SLOT_CONTRAST_MAX - _SLOT_CONTRAST_MIN) * part
	gain = _SLOT_SATURATION_GAIN * part
	return (
		_tone(hue % 360, min(1.0, _SLOT_SATURATION_LIGHT + gain), target, light=True),
		_tone(hue % 360, min(1.0, _SLOT_SATURATION_DARK + gain), target, light=False),
	)


# --- вкладки страниц ---------------------------------------------------------------

#: Полоса под активной вкладкой и кегль подписи — по макету страницы
#: сообщества (раздел 2).
_TAB_INDICATOR_LENGTH = 26
_TAB_FONT_PX = 14
_TAB_COUNT_PX = 12
_TAB_COUNT_GAP = 7
_TAB_BADGE_HEIGHT = 18
#: Поля пункта вкладок (лево, верх, право, низ): по макету 14 по бокам.
_TAB_ITEM_MARGINS = (14, 0, 14, 0)


class TabItem(PivotItem):
	"""Пункт вкладок: подпись и число рядом (макет, раздел 2).

	Штатный ``PivotItem`` — кнопка; подпись и счётчик лежат в её
	компоновке, поэтому число стоит вплотную к подписи (зазор 7)
	при любой ширине пункта. У активной вкладки подпись полужирная,
	счётчик — пилюля ``InfoBadge`` (акцент темы); у остальных число —
	приглушённая ``CaptionLabel`` без подложки. Свой текст у кнопки
	пустой: его рисовала бы кнопка, а не компоновка.
	"""

	def __init__(self, text: str, parent: QWidget) -> None:
		super().__init__(parent)
		self._text = text
		self._count: QWidget | None = None
		self._active = False
		self._box = QHBoxLayout(self)
		self._box.setContentsMargins(*_TAB_ITEM_MARGINS)
		self._box.setSpacing(_TAB_COUNT_GAP)
		self.setMinimumWidth(0)  # ширина — по подписи, а не по умолчанию кнопки
		self._label = BodyLabel(text, self)
		self._label.setFont(font_px(_TAB_FONT_PX))
		# подпись и число — по центру пункта: полоса под активной вкладкой
		# рисуется по центру пункта, и при любой его ширине она должна
		# стоять под подписью, а не правее
		self._box.addStretch()
		self._box.addWidget(self._label)
		self._box.addStretch()

	def setSelected(self, isSelected: bool) -> None:  # noqa: N802, N803 — API библиотеки
		super().setSelected(isSelected)
		weight = QFont.Weight.DemiBold if isSelected else QFont.Weight.Normal
		self._label.setFont(font_px(_TAB_FONT_PX, weight))
		self.updateGeometry()

	def set_count(self, count: int | None, active: bool) -> None:
		"""Показывает число (None или 0 — без него); активной — пилюлей."""
		if self._count is not None:
			self._box.removeWidget(self._count)
			self._count.hide()  # deleteLater сработает позже, а след виден сразу
			self._count.deleteLater()
			self._count = None
		if count:
			if active:
				badge = InfoBadge(str(count), self, InfoLevel.ATTENTION)
				badge.setFont(font_px(_TAB_COUNT_PX))
				badge.setContentsMargins(4, 0, 4, 0)
				badge.setFixedHeight(_TAB_BADGE_HEIGHT)
				self._count = badge
			else:
				self._count = tinted(CaptionLabel(str(count), self), DIM_TEXT)
				self._count.setFont(font_px(_TAB_COUNT_PX))
			self._count.adjustSize()
			self._box.insertWidget(self._box.count() - 1, self._count)  # перед хвостовой растяжкой
			self._count.show()
		self.updateGeometry()

	def sizeHint(self) -> QSize:  # noqa: N802 — API Qt
		hint: QSize = super().sizeHint()
		return QSize(self._box.sizeHint().width(), hint.height())

	def minimumSizeHint(self) -> QSize:  # noqa: N802 — API Qt
		return self.sizeHint()


def tab_strip(parent: QWidget, layout: QVBoxLayout) -> Pivot:
	"""Полоса вкладок страницы: ``Pivot`` слева и разделитель под ней.

	Заведена для страницы сообщества. Прежде её делил раздел «Расписание»,
	но тот растворился в стадиях поста (ADR-0032), и второй страницы
	с вкладками сейчас нет — полоса осталась здесь, чтобы следующая
	собиралась так же, а не заново.
	``Pivot``, а не ``SegmentedWidget``: по макету вкладки — подписи
	с полосой под активной, без рамки-подложки. Ширина полосы — строго
	по пунктам: штатная политика «может расти» отдавала ей лишнее место
	ряда, и пункты растягивались. Пункты — :class:`TabItem`, добавляет
	их вызывающий (``pivot.addWidget(key, TabItem(title, pivot))``).
	"""
	pivot = Pivot(parent)
	pivot.setIndicatorLength(_TAB_INDICATOR_LENGTH)
	pivot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
	row = QHBoxLayout()
	row.setContentsMargins(0, 0, 0, 0)
	row.addWidget(pivot)
	row.addStretch()
	box = QVBoxLayout()
	box.setContentsMargins(0, 0, 0, 0)
	box.setSpacing(0)
	box.addLayout(row)
	box.addWidget(HorizontalSeparator(parent))
	layout.addLayout(box)
	return pivot


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
		self.label.setTextColor(*(ERROR_TEXT if length > self._limit else DIM_TEXT))


class _EscapableLineEdit(LineEdit):
	"""Поле ввода, сообщающее об Esc отдельным сигналом."""

	cancelled = Signal()

	def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — API Qt
		if event.key() == Qt.Key.Key_Escape:
			self.cancelled.emit()
			return
		super().keyPressEvent(event)


class TitleEditor(QWidget):
	"""Правка заголовка на месте: подпись сменяется полем ввода и обратно.

	Владелец отдаёт подпись (любой ``QLabel``), виджет кладёт под ней
	скрытое поле. :meth:`begin` показывает поле с начальным текстом;
	Enter или уход фокуса — сигнал ``submitted`` с обрезанным текстом,
	Esc — отмена без сигнала. Что значит пустой текст (снять пометку
	или отказ), решает владелец.
	"""

	submitted = Signal(str)

	#: Наименьшая ширина поля правки при ``fit_text``: короткое имя
	#: («+79001») не должно давать поле в три буквы; длинное имя
	#: прокручивается внутри поля, как в любом поле ввода.
	_EDIT_MIN_WIDTH = 220

	def __init__(self, label: QLabel, parent: QWidget, *, fit_text: bool = False) -> None:
		"""``fit_text`` — не шире собственного текста подписи (плюс запас):
		так соседи в строке (плашка, карандаш) встают сразу за заголовком,
		а не у правого края. Поле правки той же ширины (но не уже
		``_EDIT_MIN_WIDTH``): правка на месте не должна прыгать на всю строку."""
		super().__init__(parent)
		self._label = label
		self._active = False
		self._fit_text = fit_text
		self._text = ""
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		label.setParent(self)
		layout.addWidget(label)
		self._edit = _EscapableLineEdit(self)
		self._edit.setClearButtonEnabled(True)
		self._edit.hide()
		self._edit.editingFinished.connect(self._finish)
		self._edit.cancelled.connect(self.cancel)
		layout.addWidget(self._edit)
		# «занимай, что дадут»: как у самой подписи — иначе поле требовало
		# бы свою ширину и распирало строку
		self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

	def set_text(self, text: str) -> None:
		"""Текст подписи (с сокращением) и, при ``fit_text``, предел ширины по нему."""
		self._text = text
		elide_text(self._label, text)
		self._fit()

	def _fit(self, editing: bool = False) -> None:
		if not self._fit_text:
			return
		width = self._label.fontMetrics().horizontalAdvance(self._text) + 8
		self.setMaximumWidth(max(width, self._EDIT_MIN_WIDTH) if editing else width)

	def begin(self, initial: str, placeholder: str) -> None:
		"""Показывает поле ввода вместо подписи — той же ширины, что заголовок."""
		self._active = True
		self._fit(editing=True)
		self._edit.setPlaceholderText(placeholder)
		self._edit.setText(initial)
		self._label.hide()
		self._edit.show()
		self._edit.setFocus()
		self._edit.selectAll()

	def cancel(self) -> None:
		"""Прячет поле без сохранения (Esc)."""
		if not self._active:
			return
		# флаг снимается до потери фокуса: editingFinished после hide()
		# не должен считаться сохранением
		self._active = False
		self._edit.hide()
		self._label.show()
		self._fit()

	def _finish(self) -> None:
		"""Enter или уход фокуса: один сигнал на одну правку."""
		if not self._active:
			return
		self._active = False
		text = str(self._edit.text()).strip()
		self._edit.hide()
		self._label.show()
		self._fit()
		self.submitted.emit(text)


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
		self.setTextColor(*ERROR_TEXT)
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
		self.setTextColor(*WARNING_TEXT)
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

	def __init__(
		self,
		dialog: QWidget,
		layout: QVBoxLayout,
		*,
		compact: bool = False,
		trailing: Sequence[QWidget] = (),
		on_now_changed: Callable[[bool], None] | None = None,
	) -> None:
		"""``on_now_changed`` — крючок переключения «сейчас ↔ отложенно»:
		от времени зависят кнопки под постом (ADR-0031), и форма должна
		узнать о смене сразу, а не при отправке.
		``compact`` — ряд по макету карточки очереди: переключатель без
		подписей On/Off, дата «дд.мм.гггг» шириной 126, время 92, дата
		и время сразу за переключателем; ``trailing`` — виджеты в правом
		краю ряда (кнопки формы)."""
		row = QHBoxLayout()
		row.setSpacing(10 if compact else row.spacing())
		row.addWidget(BodyLabel("Опубликовать сейчас", dialog))
		self._on_now_changed = on_now_changed
		self._now_switch = SwitchButton(dialog)
		self._now_switch.setChecked(False)
		self._now_switch.checkedChanged.connect(self._on_now_toggled)
		row.addWidget(self._now_switch)
		if not compact:
			row.addStretch()
		self._date = CalendarPicker(dialog)
		self._date.setDate(QDate.currentDate())
		self._time = EditableComboBox(dialog)
		self._time.setPlaceholderText("ЧЧ:ММ")
		self._time.setText(QTime.currentTime().addSecs(DEFAULT_SCHEDULE_OFFSET_S).toString("HH:mm"))
		self._time.setMaximumWidth(120)
		if compact:
			self._now_switch.setOnText("")
			self._now_switch.setOffText("")
			self._date.setDateFormat("dd.MM.yyyy")
			self._date.setFixedWidth(126)
			self._time.setFixedWidth(92)
		row.addWidget(self._date)
		row.addWidget(self._time)
		if trailing:
			row.addStretch()
			for widget in trailing:
				row.addWidget(widget)
		layout.addLayout(row)

	def _on_now_toggled(self, now: bool) -> None:
		self._date.setVisible(not now)
		self._time.setVisible(not now)
		if self._on_now_changed is not None:
			self._on_now_changed(now)

	def is_now(self) -> bool:
		"""Выбрана ли публикация «сейчас» (не разбирая время).

		Отдельно от :meth:`when`: та бросает ошибку на негодном времени,
		а спросить «отложенный ли это пост» нужно и при недописанном
		«ЧЧ:ММ» — например, чтобы решить, доступны ли кнопки.
		"""
		return bool(self._now_switch.isChecked())

	def set_schedule_allowed(self, allowed: bool, hint: str = "") -> None:
		"""Разрешает/запрещает отложенную публикацию (иначе — только «сейчас»)."""
		if not allowed:
			self._now_switch.setChecked(True)
		self._now_switch.setEnabled(allowed)
		self._now_switch.setToolTip("" if allowed else hint)

	def set_now_allowed(self, allowed: bool, hint: str = "") -> None:
		"""Разрешает/запрещает «сейчас» (иначе — только отложенно).

		Обратное :meth:`set_schedule_allowed`: у правки отложенной записи
		«сейчас» — отдельное действие карточки, а не вариант времени.
		"""
		if not allowed:
			self._now_switch.setChecked(False)
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
