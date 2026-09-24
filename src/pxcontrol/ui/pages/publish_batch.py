"""Редактор пакета отправки: черновики постов из готовой папки (ADR-0015).

Тело экрана «Пакет» раздела «Публикация» (ADR-0032). Строка на файл:
галочка, подпись (собрана по общему пресету подписи, правится),
переименование (по шаблону имени пресета, правится) и время публикации
(заполнено раскладкой по выбранной стратегии, правится). Подпись строки
собирается из общих значений окна сборки и значений, разобранных
из имени её файла по правилам пресета (:class:`BatchCaption`, ADR-0042);
до ADR-0042 здесь же жил блок «Правила разбора имени файла» — разбор
переехал в пресет.
:meth:`BatchEditor.drafts` отдаёт список черновиков ``PostDraft`` —
дальше работает обычная очередь отправки.

До 17.09.2026 редактор был рабочим окном поверх формы поста; экран
и окно отличаются только рамкой, поэтому переезд ничего в правилах
не менял — кроме того, что адресат пакета (сообщество, тема) и кнопки
под постом теперь живут на самом экране (:mod:`publish_batch_page`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CalendarPicker,
	CaptionLabel,
	CardWidget,
	CheckBox,
	ComboBox,
	LineEdit,
	PushButton,
	SpinBox,
	StrongBodyLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.captions import (
	CaptionPresetDto,
	build_caption,
	filename_source,
)
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.posts import MediaFile, PostDraft
from pxcontrol.engine.services.publish_route import file_needs_premium
from pxcontrol.engine.services.schedule_plan import (
	DAYS_STEP_RANGE,
	DEFAULT_HOURS_STEP,
	FALLBACK_START_TIME,
	HOURS_STEP_RANGE,
	PlanError,
	PlanKind,
	SchedulePlan,
	parse_hhmm,
	plan_times,
)
from pxcontrol.engine.services.video import VideoFile
from pxcontrol.engine.telegram.markup import PostMarkup
from pxcontrol.engine.telegram.rich_text import RichText, trimmed
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES,
	CAPTION_LENGTH_LIMIT,
	ExecutorRef,
	MediaKind,
)
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DEFAULT_SCHEDULE_OFFSET_S,
	CharCounter,
	ErrorLabel,
	SelectionRow,
	file_action_buttons,
	human_size,
	noop,
	show_error,
)
from pxcontrol.ui.pages.rich_edit import RichPostEdit

#: Формат времени публикации в строке черновика (местное время).
_WHEN_FORMAT = "%d.%m.%Y %H:%M"

#: Высота списка черновиков (прокрутка внутри, а не рост диалога).
#: Высота поля подписи в строке (несколько строк текста без прокрутки окна).
_CAPTION_HEIGHT = 64

#: Стратегии раскладки: подпись → вид плана и «раз в N дней?».
_STRATEGIES: list[tuple[str, PlanKind, bool]] = [
	("По временам сообщества", PlanKind.COMMUNITY_TIMES, False),
	("Каждый день в…", PlanKind.DAILY, False),
	("Раз в N дней в…", PlanKind.DAILY, True),
	("Каждые N часов от…", PlanKind.EVERY_HOURS, False),
	("Сейчас", PlanKind.NOW, False),
]


@dataclass(frozen=True)
class BatchCaption:
	"""Подпись пакета: пресет, отмеченные поля и общие значения.

	Окно сборки проходится один раз на пакет (ADR-0015) и даёт общие
	значения полей без правила разбора; поля с правилом у каждой строки
	берут значения из имени её файла (ADR-0042).
	"""

	preset: CaptionPresetDto
	enabled_ids: tuple[int, ...]
	common_values: dict[int, list[str]]

	def values_for(self, path: str) -> dict[int, list[str]]:
		"""Значения полей строки: общие плюс разобранные из имени файла."""
		return {**self.common_values, **self.preset.parsed(filename_source(path))}

	def caption_for(self, path: str) -> RichText:
		"""Подпись строки по пресету."""
		return build_caption(self.preset.lines(self.values_for(path), self.enabled_ids))


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
		editor: BatchEditor,
		video: VideoFile,
		caption: RichText,
		oversized: bool,
		caption_limit: int = CAPTION_LENGTH_LIMIT,
		*,
		premium_only: bool = False,
	) -> None:
		self.video = video
		self.card = CardWidget(editor)
		box = QVBoxLayout(self.card)
		# отступы — из механизма плотности: «Компактные отступы»
		# действуют и на карточки строк пакета
		box.setContentsMargins(*density.spacing().card_margins)
		box.setSpacing(density.spacing().card_body_spacing)
		head = QHBoxLayout()
		self.check = CheckBox("", self.card)
		self.check.setToolTip("Отправить пост в очередь («В очередь» берёт отмеченные)")
		# файл больше лимита канал не примет — галочка снята, но выбор
		# осознанно вернуть можно (например, для проверки ошибки)
		self.check.setChecked(not oversized)
		head.addWidget(self.check)
		label = f"{video.name} — {human_size(video.size_bytes)}"
		if oversized:
			label += " · ⚠ больше лимита Telegram"
		elif premium_only:
			label += " · только через Premium"
		title = StrongBodyLabel(label, self.card)
		title.setWordWrap(True)
		head.addWidget(title, stretch=1)
		head.addWidget(
			file_action_buttons(
				self.card,
				video.path,
				lambda: editor.remove_row(self),
				remove_tip="Убрать из пакета (файл на диске не трогается)",
			)
		)
		box.addLayout(head)
		# подпись с оформлением (ADR-0033): название жирное сущностью,
		# а не звёздочками — и правится тут же, как в форме поста
		self.rich_caption = RichPostEdit(self.card, height=_CAPTION_HEIGHT)
		self.caption = self.rich_caption.edit
		self.caption.setPlaceholderText("Подпись к видео (необязательно)…")
		self.rich_caption.set_rich(caption)
		box.addWidget(self.rich_caption)
		# подписи собраны общим пресетом: предел легко перерастает весь
		# пакет сразу, и увидеть это лучше здесь, чем при постановке
		self.counter = CharCounter(self.card, box, self.caption, caption_limit)
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

	def set_caption(self, caption: RichText) -> None:
		"""Показывает пересобранную подпись со всем её оформлением."""
		self.rich_caption.set_rich(caption)


class BatchEditor(QWidget):
	"""Черновики пакета отправки с раскладкой времени и правкой строк.

	Тело экрана «Пакет»: экран собирает редактор заново на каждый новый
	источник (папку или список файлов) и спрашивает у него
	:meth:`validate` и :meth:`drafts`. Адресат пакета (сообщество, тема
	форума) и кнопки под постом живут на экране, а не здесь: они общие
	на весь пакет и не зависят от строк.

	Правки состава и времени экран слушает сигналом :attr:`changed`:
	от них зависят и правила кнопок (отложенный пост и крупный файл
	уходят другим маршрутом, ADR-0031), и доступность постановки.
	"""

	#: Состав отмеченных строк или их время изменились.
	changed = Signal()

	def __init__(
		self,
		worker: EngineWorker,
		community: CommunityDto,
		root: str,
		files: list[VideoFile],
		parent: QWidget,
		caption: BatchCaption | None = None,
		community_times: list[str] | None = None,
		limit_bytes: int | None = None,
		caption_limit: int = CAPTION_LENGTH_LIMIT,
		schedule_allowed: bool = True,
		busy: list[datetime] | None = None,
	) -> None:
		"""``caption`` — пресет подписи с общими значениями (None — без
		подписей); переименование предлагается, если у пресета задан
		шаблон имени файла; ``limit_bytes`` — лимит
		файла выбранного канала (пометка и снятая галочка у больших);
		``caption_limit`` — предел длины подписи канала (счётчик под
		каждой подписью; у Premium-публикатора он выше базового);
		``schedule_allowed`` — доступна ли отложка (у бот-канала — нет);
		``busy`` — занятые моменты существующих отложек канала (местное
		наивное время) — раскладка их пропускает."""
		super().__init__(parent)
		self.content = QVBoxLayout(self)
		self.content.setContentsMargins(0, 0, 0, 0)
		self.content.setSpacing(density.spacing().row_spacing)
		self._worker = worker
		self._community = community
		self._community_times = list(community_times or [])
		self._schedule_allowed = schedule_allowed
		self._busy = list(busy or [])
		self._rows: list[_BatchRow] = []
		self._caption = caption
		folder = CaptionLabel(f"Папка: {root}", self)
		folder.setWordWrap(True)
		self.content.addWidget(folder)
		self._build_strategy_row()
		self._build_rows(files, limit_bytes, caption_limit)
		self._build_selection_row()
		self._error = ErrorLabel(self)
		self.content.addWidget(self._error)
		self._update_summary()
		self._request_renames()
		self._apply_initial_plan()

	def drafts(
		self,
		community_id: int,
		topic_id: int | None = None,
		markup: PostMarkup | None = None,
		markup_first: bool = False,
		identity: ExecutorRef | None = None,
	) -> list[PostDraft]:
		"""Черновики отмеченных строк (время — в UTC, как у формы поста).

		Адресат и кнопки — общие на весь пакет (ADR-0032, п. 4): пакет
		идёт в одно сообщество и одну тему, клавиатура у всех постов
		одна. Проверит её движок при постановке — атомарно на весь
		пакет (ADR-0015, ADR-0031).

		Args:
			community_id: сообщество пакета.
			topic_id: тема форума (None — общая лента).
			markup: клавиатура под каждым постом (None — кнопок нет).
			markup_first: режим «кнопки важнее» (ADR-0031, п. 4).

		Raises:
			ValueError: Время какой-то строки не разобралось (сначала
				зовите :meth:`validate` — экран это гарантирует).
		"""
		result: list[PostDraft] = []
		for row in self._checked():
			when_local = _parse_when(str(row.when.text()))
			caption = trimmed(row.rich_caption.rich())
			result.append(
				PostDraft(
					community_id,
					text=caption.text,
					entities=caption.entities,
					media=(
						MediaFile(
							row.video.path,
							MediaKind.VIDEO,
							str(row.rename.text()).strip() or None,
						),
					),
					when=when_local.astimezone(UTC) if when_local else None,
					topic_id=topic_id,
					markup=markup,
					markup_first=markup_first,
					identity=identity,
				)
			)
		return result

	def any_scheduled(self) -> bool:
		"""Есть ли среди отмеченных строк отложенные (для правил кнопок).

		Неразобранное время считается отложенным: пока человек печатает
		дату, обещать ему «кнопки будут сразу» нельзя — проверку времени
		делает :meth:`validate` при постановке.
		"""
		return any(str(row.when.text()).strip() for row in self._checked())

	def any_over(self, limit_bytes: int) -> bool:
		"""Есть ли среди отмеченных файл больше предела (для правил кнопок)."""
		return any(row.video.size_bytes > limit_bytes for row in self._checked())

	def checked_count(self) -> int:
		"""Сколько строк отмечено к отправке."""
		return len(self._checked())

	def validate(self) -> bool:
		"""Готов ли пакет к постановке (False — причина показана строкой)."""
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
					"у сообщества нет userbot-админа, только «сейчас»."
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
		self._days.setRange(*DAYS_STEP_RANGE)
		self._hours_label = BodyLabel("шаг, часов:", self)
		self._hours = SpinBox(self)
		self._hours.setRange(*HOURS_STEP_RANGE)
		self._hours.setValue(DEFAULT_HOURS_STEP)
		self._start_label = BodyLabel("старт:", self)
		self._start = LineEdit(self)
		self._start.setFixedWidth(220)
		self._start.setText(
			(datetime.now() + timedelta(seconds=DEFAULT_SCHEDULE_OFFSET_S)).strftime(_WHEN_FORMAT)
		)
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
		self.content.addLayout(row)
		if not self._schedule_allowed:
			# бот-канал не умеет отложку — только «сейчас»
			self._strategy.setCurrentIndex(len(_STRATEGIES) - 1)
			self._strategy.setEnabled(False)
			self._strategy.setToolTip("Отложенные требуют userbot-админа в сообществе")
		self._on_strategy_changed(int(self._strategy.currentIndex()))

	def _default_at(self) -> str:
		"""Время по умолчанию для «каждый день»: первое валидное у сообщества."""
		for item in self._community_times:
			try:
				hours, minutes = parse_hhmm(str(item))
			except ValueError:
				continue
			return f"{hours:02d}:{minutes:02d}"
		return FALLBACK_START_TIME

	def _build_rows(
		self,
		files: list[VideoFile],
		limit_bytes: int | None,
		caption_limit: int,
	) -> None:
		"""Строки черновиков списком карточек.

		Своей полосы прокрутки у списка нет: редактор живёт на экране,
		а экран прокручивается сам (ADR-0032). Вложенная прокрутка внутри
		прокрутки — две полосы на одно движение колеса и вечный спор
		о высоте; в рабочем окне она была нужна, на экране — нет.
		"""
		box = QVBoxLayout()
		box.setSpacing(density.spacing().list_spacing)
		for video in files:
			caption = (
				self._caption.caption_for(video.path) if self._caption is not None else RichText("")
			)
			oversized = limit_bytes is not None and video.size_bytes > limit_bytes
			# пометка «только через Premium» (ADR-0037) — у userbot-пути:
			# бот-путь с его 50 МБ такого файла не примет вовсе
			premium_only = (
				limit_bytes is not None
				and limit_bytes > BOT_MAX_FILE_BYTES
				and file_needs_premium(video.size_bytes)
			)
			row = _BatchRow(
				self, video, caption, oversized, caption_limit, premium_only=premium_only
			)
			row.check.stateChanged.connect(self._update_summary)
			# время строки меняет маршрут пакета (отложенный пост уходит
			# иначе) — экрану нужно знать о правке сразу
			row.when.textChanged.connect(self.changed)
			box.addWidget(row.card)
			self._rows.append(row)
		self.content.addLayout(box)

	def _build_selection_row(self) -> None:
		"""Кнопки выбора и итог по отмеченному."""
		self._selection = SelectionRow(self, self._set_all)
		self.content.addLayout(self._selection.layout)

	def _request_renames(self) -> None:
		"""Просит движок предложить имена файлов по шаблону имени пресета.

		Подсказка вспомогательная: ошибка одной строки не мешает
		остальным (и не показывается плашкой — просто поле пустое).
		Значения строки — общие плюс разобранные из имени её файла.
		"""
		caption = self._caption
		if caption is None or not caption.preset.filename_pattern:
			return
		for row in self._rows:
			run_in_engine(
				self._worker,
				self._worker.engine.captions.render_filename(
					caption.preset.id,
					self._community.id,
					caption.values_for(row.video.path),
					row.video.path,
				),
				self,
				partial(self._set_rename, row),
				noop,
			)

	def _set_rename(self, row: _BatchRow, filename: str) -> None:
		"""Подсказка имени — только в живую строку: пока движок отвечал,
		строку могли убрать из пакета (иначе setText по удалённому
		Qt-объекту — RuntimeError; ср. ``_is_stale`` на «Публикации»)."""
		if row in self._rows:
			row.rename.setText(filename)

	# --- раскладка времени -------------------------------------------------------

	def _on_strategy_changed(self, index: int) -> None:
		"""Показывает параметры, относящиеся к выбранной стратегии."""
		_label, kind, n_days = _STRATEGIES[index]
		daily = kind is PlanKind.DAILY
		hourly = kind is PlanKind.EVERY_HOURS
		# дата начала — у стратегий по дням (у «каждые N часов» есть
		# полный стартовый момент, у «сейчас» дата не нужна)
		dated = daily or kind is PlanKind.COMMUNITY_TIMES
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
			community_times=tuple(self._community_times),
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
				SchedulePlan(
					PlanKind.COMMUNITY_TIMES, community_times=tuple(self._community_times)
				),
				len(self._checked()),
				datetime.now(),
				busy=self._busy,
			)
		except PlanError:
			return  # времён у канала нет — все строки «сейчас»
		for row, moment in zip(self._checked(), moments, strict=True):
			row.when.setText("" if moment is None else moment.strftime(_WHEN_FORMAT))

	# --- выбор -----------------------------------------------------------------

	def remove_row(self, row: _BatchRow) -> None:
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

	def set_caption_limit(self, limit: int) -> None:
		"""Меняет предел длины подписи у всех строк (сменился маршрут).

		У поста, который отправляет бот, пределы всегда базовые —
		подписки у ботов не бывает (ADR-0031). Кнопки пакета меняют
		маршрут, а значит и предел, поэтому счётчики строк пересчитывают
		его по той же общей точке, что и форма поста.
		"""
		for row in self._rows:
			row.counter.set_limit(limit)

	def _update_summary(self, *_args: object) -> None:
		picked = self._checked()
		total = sum(row.video.size_bytes for row in picked)
		self._selection.set_summary(len(picked), len(self._rows), total)
		self.changed.emit()
