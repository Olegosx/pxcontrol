"""Диалог пакетной отправки: черновики постов из готовой папки (ADR-0015).

Строка на файл: галочка, подпись (собрана по общему шаблону, правится),
переименование (по шаблону имени, правится) и время публикации (заполнено
раскладкой по выбранной стратегии, правится). «В очередь» отдаёт список
черновиков ``PostDraft`` — дальше работает обычная очередь отправки.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import partial

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	FluentIcon,
	LineEdit,
	MessageBoxBase,
	PushButton,
	SpinBox,
	StrongBodyLabel,
	SubtitleLabel,
	TextEdit,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import CaptionLine, build_caption, title_from_filename
from pxcontrol.engine.services.channels import ChannelDto
from pxcontrol.engine.services.posts import PostDraft
from pxcontrol.engine.services.publish_plan import (
	PlanError,
	PlanKind,
	SchedulePlan,
	plan_times,
)
from pxcontrol.engine.services.video import ReadyVideo
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ErrorLabel,
	SelectionRow,
	fixed_list_area,
	human_size,
	noop,
	open_in_system,
	parse_hhmm,
	show_error,
)

#: Формат времени публикации в строке черновика (местное время).
_WHEN_FORMAT = "%d.%m.%Y %H:%M"

#: Высота списка черновиков (прокрутка внутри, а не рост диалога).
_LIST_HEIGHT = 420

#: Высота поля подписи в строке (несколько строк текста без прокрутки окна).
_CAPTION_HEIGHT = 64

#: Стратегии раскладки: подпись → вид плана и «раз в N дней?».
_STRATEGIES: list[tuple[str, PlanKind, bool]] = [
	("По временам канала", PlanKind.CHANNEL_TIMES, False),
	("Каждый день в…", PlanKind.DAILY, False),
	("Раз в N дней в…", PlanKind.DAILY, True),
	("Каждые N часов от…", PlanKind.EVERY_HOURS, False),
	("Сейчас", PlanKind.NOW, False),
]


def _parse_when(text: str) -> datetime | None:
	"""Разбирает время строки черновика; пусто — None («сейчас»).

	Raises:
		ValueError: Текст не в формате ДД.ММ.ГГГГ ЧЧ:ММ.
	"""
	cleaned = text.strip()
	if not cleaned:
		return None
	try:
		return datetime.strptime(cleaned, _WHEN_FORMAT)
	except ValueError:
		raise ValueError(
			f"время «{cleaned}» — в формате ДД.ММ.ГГГГ ЧЧ:ММ (пусто — «сейчас»)."
		) from None


class _BatchRow:
	"""Строка черновика: файл и его правимые параметры поста.

	Шапка: чекбокс выбора перед названием, справа — кнопки «посмотреть»
	(системный плеер) и «убрать из пакета».
	"""

	def __init__(
		self,
		dialog: PublishBatchDialog,
		video: ReadyVideo,
		caption: str,
		oversized: bool,
	) -> None:
		self.video = video
		self.card = CardWidget(dialog)
		box = QVBoxLayout(self.card)
		box.setContentsMargins(12, 8, 12, 8)
		box.setSpacing(6)
		head = QHBoxLayout()
		self.check = CheckBox("", self.card)
		self.check.setToolTip("Отправить пост в очередь («В очередь» берёт отмеченные)")
		# файл больше лимита канал не примет — галочка снята, но выбор
		# осознанно вернуть можно (например, для проверки ошибки)
		self.check.setChecked(not oversized)
		head.addWidget(self.check)
		label = f"{video.name} — {human_size(video.size_bytes)}"
		if oversized:
			label += " · ⚠ больше лимита канала"
		title = StrongBodyLabel(label, self.card)
		title.setWordWrap(True)
		head.addWidget(title, stretch=1)
		play = TransparentToolButton(FluentIcon.PLAY, self.card)
		play.setToolTip("Посмотреть файл (системный плеер)")
		play.clicked.connect(lambda: open_in_system(video.path))
		head.addWidget(play)
		remove = TransparentToolButton(FluentIcon.DELETE, self.card)
		remove.setToolTip("Убрать из пакета (файл на диске не трогается)")
		remove.clicked.connect(lambda: dialog._remove_row(self))  # noqa: SLF001 — класс диалога
		head.addWidget(remove)
		box.addLayout(head)
		self.caption = TextEdit(self.card)
		self.caption.setPlaceholderText("Подпись к видео (необязательно)…")
		self.caption.setPlainText(caption)
		self.caption.setFixedHeight(_CAPTION_HEIGHT)
		box.addWidget(self.caption)
		bottom = QHBoxLayout()
		self.rename = LineEdit(self.card)
		self.rename.setPlaceholderText("Переименовать при отправке (пусто — как есть)…")
		bottom.addWidget(self.rename, stretch=1)
		bottom.addWidget(BodyLabel("Время:", self.card))
		self.when = LineEdit(self.card)
		self.when.setPlaceholderText("ДД.ММ.ГГГГ ЧЧ:ММ (пусто — сейчас)")
		self.when.setFixedWidth(220)
		bottom.addWidget(self.when)
		box.addLayout(bottom)


class PublishBatchDialog(MessageBoxBase):
	"""Черновики пакета отправки с раскладкой времени и правкой строк."""

	def __init__(
		self,
		worker: EngineWorker,
		channel: ChannelDto,
		root: str,
		files: list[ReadyVideo],
		parent: QWidget,
		caption_lines: list[CaptionLine] | None = None,
		filename_template_id: int | None = None,
		used_values: dict[int, list[str]] | None = None,
		channel_times: list[str] | None = None,
		limit_bytes: int | None = None,
		schedule_allowed: bool = True,
		busy: list[datetime] | None = None,
	) -> None:
		"""``caption_lines`` — строки общего шаблона подписи (None — без
		подписей); ``filename_template_id`` — шаблон имени файла для
		переименования (None — не предлагать); ``limit_bytes`` — лимит
		файла выбранного канала (пометка и снятая галочка у больших);
		``schedule_allowed`` — доступна ли отложка (у бот-канала — нет);
		``busy`` — занятые моменты существующих отложек канала (местное
		наивное время) — раскладка их пропускает."""
		super().__init__(parent)
		self._worker = worker
		self._channel = channel
		self._channel_times = list(channel_times or [])
		self._schedule_allowed = schedule_allowed
		self._busy = list(busy or [])
		self._rows: list[_BatchRow] = []
		self.viewLayout.addWidget(SubtitleLabel(f"Пакет в «{channel.title}»", self))
		folder = CaptionLabel(f"Папка: {root}", self)
		folder.setWordWrap(True)
		self.viewLayout.addWidget(folder)
		self._build_strategy_row()
		self._build_rows(files, caption_lines, limit_bytes)
		self._build_selection_row()
		self._error = ErrorLabel(self)
		self.viewLayout.addWidget(self._error)
		self.yesButton.setText("В очередь")
		self.cancelButton.setText("Отмена")
		self.widget.setMinimumWidth(900)
		self._update_summary()
		self._request_renames(filename_template_id, used_values or {})
		self._apply_initial_plan()

	def drafts(self, channel_id: int) -> list[PostDraft]:
		"""Черновики отмеченных строк (время — в UTC, как у формы).

		Raises:
			ValueError: Время какой-то строки не разобралось (сначала
				зовите ``validate`` — крючок диалога это гарантирует).
		"""
		result: list[PostDraft] = []
		for row in self._checked():
			when_local = _parse_when(str(row.when.text()))
			result.append(
				PostDraft(
					channel_id,
					text=str(row.caption.toPlainText()).strip(),
					media_path=row.video.path,
					media_kind=MediaKind.VIDEO,
					when=when_local.astimezone(UTC) if when_local else None,
					rename_to=str(row.rename.text()).strip() or None,
				)
			)
		return result

	def validate(self) -> bool:
		"""Крючок MessageBoxBase: False не даёт диалогу закрыться."""
		checked = self._checked()
		if not checked:
			return self._error.fail("Отметьте хотя бы один файл.")
		for row in checked:
			try:
				when = _parse_when(str(row.when.text()))
			except ValueError as exc:
				return self._error.fail(f"{row.video.name}: {exc}")
			if when is not None and not self._schedule_allowed:
				return self._error.fail(
					f"{row.video.name}: отложенная публикация недоступна — "
					"у канала нет userbot-админа, только «сейчас»."
				)
		return self._error.succeed()

	# --- сборка ----------------------------------------------------------------

	def _build_strategy_row(self) -> None:
		"""Стратегия раскладки времени и её параметры."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Раскладка:", self))
		self._strategy = ComboBox(self)
		for label, _kind, _n_days in _STRATEGIES:
			self._strategy.addItem(label)
		self._strategy.currentIndexChanged.connect(self._on_strategy_changed)
		row.addWidget(self._strategy)
		self._date_label = BodyLabel("с даты:", self)
		self._date = CalendarPicker(self)
		self._date.setDate(QDate.currentDate())
		self._at_label = BodyLabel("в", self)
		self._at = LineEdit(self)
		self._at.setPlaceholderText("ЧЧ:ММ")
		self._at.setFixedWidth(90)
		self._at.setText(self._default_at())
		self._days_label = BodyLabel("шаг, дней:", self)
		self._days = SpinBox(self)
		self._days.setRange(2, 30)
		self._hours_label = BodyLabel("шаг, часов:", self)
		self._hours = SpinBox(self)
		self._hours.setRange(1, 48)
		self._hours.setValue(3)
		self._start_label = BodyLabel("старт:", self)
		self._start = LineEdit(self)
		self._start.setFixedWidth(220)
		self._start.setText((datetime.now() + timedelta(hours=1)).strftime(_WHEN_FORMAT))
		for widget in (
			self._date_label,
			self._date,
			self._days_label,
			self._days,
			self._at_label,
			self._at,
			self._hours_label,
			self._hours,
			self._start_label,
			self._start,
		):
			row.addWidget(widget)
		apply_button = PushButton("Разложить", self)
		apply_button.setToolTip("Заполнить время отмеченных строк по стратегии (правится дальше)")
		apply_button.clicked.connect(self._apply_plan)
		row.addWidget(apply_button)
		row.addStretch()
		self.viewLayout.addLayout(row)
		if not self._schedule_allowed:
			# бот-канал не умеет отложку — только «сейчас»
			self._strategy.setCurrentIndex(len(_STRATEGIES) - 1)
			self._strategy.setEnabled(False)
			self._strategy.setToolTip("Отложенные требуют userbot-админа в канале")
		self._on_strategy_changed(int(self._strategy.currentIndex()))

	def _default_at(self) -> str:
		"""Время по умолчанию для «каждый день»: первое валидное у канала."""
		for item in self._channel_times:
			try:
				hours, minutes = parse_hhmm(str(item))
			except ValueError:
				continue
			return f"{hours:02d}:{minutes:02d}"
		return "12:00"

	def _build_rows(
		self,
		files: list[ReadyVideo],
		caption_lines: list[CaptionLine] | None,
		limit_bytes: int | None,
	) -> None:
		"""Строки черновиков в прокручиваемом списке."""
		area, box = fixed_list_area(self, _LIST_HEIGHT, spacing=8)
		for video in files:
			caption = (
				build_caption(title_from_filename(video.path), caption_lines)
				if caption_lines is not None
				else ""
			)
			oversized = limit_bytes is not None and video.size_bytes > limit_bytes
			row = _BatchRow(self, video, caption, oversized)
			row.check.stateChanged.connect(self._update_summary)
			box.addWidget(row.card)
			self._rows.append(row)
		box.addStretch()
		self.viewLayout.addWidget(area)

	def _build_selection_row(self) -> None:
		"""Кнопки выбора и итог по отмеченному."""
		self._selection = SelectionRow(self, self._set_all)
		self.viewLayout.addLayout(self._selection.layout)

	def _request_renames(self, template_id: int | None, used_values: dict[int, list[str]]) -> None:
		"""Просит движок предложить имена файлов по шаблону имени.

		Подсказка вспомогательная: ошибка одной строки не мешает
		остальным (и не показывается плашкой — просто поле пустое).
		"""
		if template_id is None:
			return
		for row in self._rows:
			run_in_engine(
				self._worker,
				self._worker.engine.captions.render_filename(
					template_id,
					self._channel.id,
					title_from_filename(row.video.path),
					used_values,
					row.video.path,
				),
				self,
				partial(self._set_rename, row),
				noop,
			)

	@staticmethod
	def _set_rename(row: _BatchRow, filename: str) -> None:
		row.rename.setText(filename)

	# --- раскладка времени -------------------------------------------------------

	def _on_strategy_changed(self, index: int) -> None:
		"""Показывает параметры, относящиеся к выбранной стратегии."""
		_label, kind, n_days = _STRATEGIES[index]
		daily = kind is PlanKind.DAILY
		hourly = kind is PlanKind.EVERY_HOURS
		# дата начала — у стратегий по дням (у «каждые N часов» есть
		# полный стартовый момент, у «сейчас» дата не нужна)
		dated = daily or kind is PlanKind.CHANNEL_TIMES
		self._date_label.setVisible(dated)
		self._date.setVisible(dated)
		self._at_label.setVisible(daily)
		self._at.setVisible(daily)
		self._days_label.setVisible(daily and n_days)
		self._days.setVisible(daily and n_days)
		self._hours_label.setVisible(hourly)
		self._hours.setVisible(hourly)
		self._start_label.setVisible(hourly)
		self._start.setVisible(hourly)

	def _start_date(self) -> date:
		"""Дата начала раскладки из календаря (для стратегий по дням)."""
		picked = self._date.getDate()
		return date(picked.year(), picked.month(), picked.day())

	def _plan(self) -> SchedulePlan:
		"""Собирает параметры раскладки из строки стратегии.

		Raises:
			ValueError: Время «в» или «старт» не разобрались.
		"""
		_label, kind, n_days = _STRATEGIES[int(self._strategy.currentIndex())]
		if kind is PlanKind.DAILY:
			return SchedulePlan(
				kind,
				at=parse_hhmm(str(self._at.text())),
				every_days=int(self._days.value()) if n_days else 1,
				start_date=self._start_date(),
			)
		if kind is PlanKind.EVERY_HOURS:
			start = _parse_when(str(self._start.text()))
			if start is None:
				raise ValueError("Укажите стартовый момент раскладки.")
			return SchedulePlan(kind, every_hours=int(self._hours.value()), start=start)
		return SchedulePlan(
			kind,
			channel_times=tuple(self._channel_times),
			start_date=self._start_date(),
		)

	def _apply_plan(self) -> None:
		"""Заполняет время отмеченных строк по стратегии (правится дальше)."""
		checked = self._checked()
		if not checked:
			show_error(self, "Отметьте хотя бы один файл.")
			return
		try:
			moments = plan_times(self._plan(), len(checked), datetime.now(), busy=self._busy)
		except (PlanError, ValueError) as exc:
			show_error(self, str(exc))
			return
		for row, moment in zip(checked, moments, strict=True):
			row.when.setText("" if moment is None else moment.strftime(_WHEN_FORMAT))

	def _apply_initial_plan(self) -> None:
		"""Первичное заполнение времени при открытии диалога.

		По временам канала, если отложка доступна и времена заданы;
		иначе строки остаются пустыми («сейчас»). Ошибки молча
		пропускаются — это только начальное значение.
		"""
		if not self._schedule_allowed:
			return
		try:
			moments = plan_times(
				SchedulePlan(PlanKind.CHANNEL_TIMES, channel_times=tuple(self._channel_times)),
				len(self._checked()),
				datetime.now(),
				busy=self._busy,
			)
		except PlanError:
			return  # времён у канала нет — все строки «сейчас»
		for row, moment in zip(self._checked(), moments, strict=True):
			row.when.setText("" if moment is None else moment.strftime(_WHEN_FORMAT))

	# --- выбор -----------------------------------------------------------------

	def _remove_row(self, row: _BatchRow) -> None:
		"""Убирает строку из пакета (сам файл на диске не трогается)."""
		if row in self._rows:
			self._rows.remove(row)
			row.card.deleteLater()
			self._update_summary()

	def _checked(self) -> list[_BatchRow]:
		"""Отмеченные строки (в порядке списка)."""
		return [row for row in self._rows if row.check.isChecked()]

	def _set_all(self, checked: bool) -> None:
		for row in self._rows:
			row.check.setChecked(checked)

	def _update_summary(self, *_args: object) -> None:
		picked = self._checked()
		total = sum(row.video.size_bytes for row in picked)
		self._selection.set_summary(len(picked), len(self._rows), total)
