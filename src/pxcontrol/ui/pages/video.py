"""Страница «Видео»: список файлов с параметрами и очередь подготовки.

Источник — две равнозначные кнопки: «Добавить файл…» (каждый выбор
добавляет карточку в список) и «Добавить папку…» (диалог сканирования
:mod:`video_batch`; отмеченные становятся карточками пакета). У каждого
файла — своя карточка со **снимком** параметров (``PresetFields``),
взятым из карточки-шаблона «[Параметры пресета]» (под строкой пресета)
в момент добавления; правятся параметры в одном общем редакторе
(:class:`_EntryEditor`), который встаёт в тело раскрытой карточки —
поэтому раскрыта одна карточка за раз. Шаблон правится всегда и служит
пресетам («загрузчик»: выбор пресета заполняет шаблон, сохранение —
по явным кнопкам). «Обработать все»
ставит в очередь движка (ADR-0014) весь список, «Обработать» — файлы,
отмеченные чекбоксами в шапках карточек (единственный файл в списке —
и без галочки); каждый — со своими параметрами; карточки очереди видны
на странице. Результат — файл в папке результатов; кнопка
«Опубликовать…» передаёт его странице «Публикация» (контракт — путь
к файлу). Выбор кадра заставки — отдельный диалог (:mod:`frame_picker`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	FluentIcon,
	PrimaryPushButton,
	PushButton,
	ScrollArea,
	SubtitleLabel,
	TransparentToolButton,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.errors import user_message
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.settings import COMMUNITY_DEFAULT_PRESET
from pxcontrol.engine.services.video import (
	BitrateAdvice,
	IntroSourceKind,
	PresetDto,
	PresetFields,
	ProcessedListing,
	SourceAdvice,
	VideoDirs,
	VideoFile,
	batch_subdir_name,
	build_intro_source,
	parse_intro_source,
	video_dialog_filter,
)
from pxcontrol.engine.services.video_queue import (
	ProcessingRequest,
	VideoItemDto,
)
from pxcontrol.engine.video.constants import is_upscale, scaled_size
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.card_list import CardList
from pxcontrol.ui.pages.common import (
	CollapsibleCard,
	DtoComboBox,
	FormDialog,
	bind,
	checked_or_single,
	community_combo_label,
	confirm_delete,
	error_reporter,
	exec_dialog,
	file_action_buttons,
	format_local,
	human_size,
	noop,
	open_in_system,
	page_layout,
	pick_dir,
	pick_file,
	show_info,
	show_success,
	show_warning,
)
from pxcontrol.ui.pages.frame_picker import FramePickerDialog
from pxcontrol.ui.pages.list_view import (
	ListPage,
	ListWords,
	PagerRow,
	paginate,
	step_page,
	summary_text,
)
from pxcontrol.ui.pages.queue_panel import QueuePanel
from pxcontrol.ui.pages.video_batch import BatchScanDialog
from pxcontrol.ui.pages.video_form import PresetForm, apply_bitrate_advice

#: Имя «пресета» в имени файла результата, когда пресет не выбран.
_MANUAL_NAME = "ручные"

#: Сколько ждать копирования выбранного кадра в папку очереди (сек).
_STASH_TIMEOUT_S = 10.0

#: Слова итоговой строки под списком готовых видео.
PROCESSED_WORDS = ListWords(
	empty="Готовых видео пока нет — обработайте исходник кнопкой выше.",
	of_all="готовых видео",
	within="в папке",
)


def processed_title(item: VideoFile) -> str:
	"""Заголовок карточки готового видео: имя файла без подпапки.

	Заголовок карточки однострочный и обрезается по ширине, поэтому
	подпапка пакета («пакет/файл.mp4») уходит в подпись — там ей место
	рядом с размером и датой.
	"""
	return Path(item.name).name


def processed_subtitle(item: VideoFile) -> str:
	"""Подпись карточки: подпапка (если файл в ней), размер, дата изменения."""
	parts = [human_size(item.size_bytes), format_local(item.modified_at)]
	subdir = Path(item.name).parent.as_posix()
	if subdir != ".":
		parts.insert(0, f"подпапка «{subdir}»")
	return " · ".join(parts)


def processed_signature(item: VideoFile) -> tuple[Any, ...]:
	"""Отпечаток готового видео — всё, что показывает карточка."""
	return (item.name, item.size_bytes, item.modified_at)


class _AbortRun(Exception):  # noqa: N818 — служебный сигнал, не ошибка
	"""Служебный сигнал прохода выбора кадров: отменить постановку целиком."""


class _FileEntry:
	"""Карточка файла в списке подготовки: шапка и снимок параметров.

	Шапка — имя и размер файла, пометки (пакет, авто-битрейт, апскейл),
	чекбокс «обрабатывать» (по умолчанию выключен: на обработку уходят
	только отмеченные) и кнопки «посмотреть» / «убрать из списка». Тело
	пустое: параметры файла живут снимком :attr:`fields`, а правятся
	в общем редакторе страницы (:class:`_EntryEditor`), который встаёт
	в тело на время раскрытия карточки. Своя форма у каждого файла
	стоила бы около 9 МБ памяти на карточку (замер 19.09.2026: 130
	виджетов на форму) — пакет из двух сотен файлов съедал бы гигабайты.
	"""

	def __init__(
		self,
		parent: QWidget,
		path: str,
		size_bytes: int,
		batch: str,
		fields: PresetFields,
		on_remove: Callable[[_FileEntry], None],
	) -> None:
		"""``fields`` — снимок шаблона на момент добавления; его ``name`` —
		имя пресета (или «ручные»), оно уходит в имя файла результата.
		``on_remove`` — что делать по кнопке «убрать из списка»."""
		self.path = path
		self.batch = batch  # подпапка пакета («» — одиночное добавление)
		self.fields = fields  # параметры обработки этого файла
		self.bitrate_suggested = False  # битрейт в снимке подставлен рекомендацией
		self.advice_note = ""  # пометка авто-битрейта (после совета движка)
		# размеры кадра исходника: у пакета приезжают со сканированием,
		# у одиночного файла — с подсказкой движка; None — ещё не знаем
		self.source_frame: tuple[int, int] | None = None
		self.scale_note = ""  # пометка об апскейле (зависит и от ступени)
		trailing = file_action_buttons(
			parent,
			path,
			bind(on_remove, self),
			remove_tip="Убрать из списка (файл на диске не трогается)",
		)
		# чекбокс выбора — слева, перед названием (клик не сворачивает карточку)
		self.check = CheckBox("", parent)
		self.check.setToolTip("Отправить файл на обработку («Обработать» берёт отмеченные)")
		title = f"{Path(path).name} — {human_size(size_bytes)}"
		self.card = CollapsibleCard(title, parent, trailing=trailing, leading=self.check)
		self.refresh_summary()

	def refresh_summary(self) -> None:
		"""Сводка шапки: пакет и пометки (видны у свёрнутой карточки)."""
		parts = []
		if self.batch:
			parts.append(f"пакет «{self.batch}»")
		if self.scale_note:
			parts.append(self.scale_note)
		if self.advice_note:
			parts.append(self.advice_note)
		self.card.set_summary(" · ".join(parts))


class _EntryEditor:
	"""Единственная форма параметров на все карточки файлов страницы.

	Раскрытая карточка получает форму в тело, заполненную своим снимком;
	сворачивание, раскрытие другой карточки, удаление карточки и чтение
	параметров при постановке возвращают состояние формы в снимок. Форма
	одна — поэтому раскрыта одна карточка за раз: раскрытие следующей
	сворачивает предыдущую. Все обращения страницы к параметрам файла
	идут через редактор: он один знает, где сейчас правда — в форме
	(карточка раскрыта) или в снимке.
	"""

	def __init__(self, page: QWidget) -> None:
		self._page = page
		self.form = PresetForm(page)
		self.form.hide()
		self._entry: _FileEntry | None = None

	@property
	def editing(self) -> _FileEntry | None:
		"""Карточка, в которой сейчас стоит форма (None — форма спрятана)."""
		return self._entry

	def attach(self, entry: _FileEntry) -> None:
		"""Ставит форму в тело карточки, заполнив её снимком файла.

		Прежняя карточка (если была) получает снимок обратно и сворачивается.
		"""
		if self._entry is entry:
			return
		previous = self._entry
		self.detach()
		if previous is not None:
			previous.card.set_expanded(False)
		# карточка назначается до заполнения: сигналы формы (смена ступени)
		# приходят на страницу уже во время fill и должны найти адресата
		self._entry = entry
		self.form.fill(entry.fields)
		# fill пишет в поле битрейта и снимает признак автоподстановки —
		# у файла он свой и переживает раскрытие
		self.form.set_bitrate_suggested(entry.bitrate_suggested)
		self.form.set_scale_note(entry.scale_note)
		entry.card.body.addWidget(self.form)
		self.form.show()

	def detach(self) -> None:
		"""Возвращает состояние формы в снимок карточки и прячет форму."""
		entry = self._entry
		if entry is None:
			return
		self._entry = None
		entry.fields = self.form.fields(entry.fields.name)
		entry.bitrate_suggested = self.form.bitrate_suggested()
		entry.card.body.removeWidget(self.form)
		# форма — страницы, не карточки: карточку удалят, форма останется
		self.form.setParent(self._page)
		self.form.hide()

	def release(self, entry: _FileEntry) -> None:
		"""Карточка уходит из списка: форма возвращается странице."""
		if self._entry is entry:
			self.detach()

	def fields_of(self, entry: _FileEntry) -> PresetFields:
		"""Параметры файла: живые из формы у раскрытой карточки, иначе снимок."""
		if self._entry is entry:
			return self.form.fields(entry.fields.name)
		return entry.fields

	def suggest_bitrate(self, entry: _FileEntry, mbps: float) -> bool:
		"""Подставляет рекомендованный битрейт файлу — в форму или в снимок.

		Правило одно (:func:`apply_bitrate_advice`): свободное поле
		заполняется, занятое рукой или пресетом — нет.

		Returns:
			True, если значение подставлено.
		"""
		if self._entry is entry:
			return self.form.suggest_bitrate(mbps)
		kbps = apply_bitrate_advice(entry.fields.video_bitrate_kbps, entry.bitrate_suggested, mbps)
		if kbps is None:
			return False
		entry.fields = replace(entry.fields, video_bitrate_kbps=kbps)
		entry.bitrate_suggested = True
		return True

	def set_scale_note(self, entry: _FileEntry, note: str) -> None:
		"""Предупреждение об апскейле — в форму, если карточка раскрыта."""
		if self._entry is entry:
			self.form.set_scale_note(note)


class VideoPage(ScrollArea):
	"""Панель параметров обработки и подготовка видеофайла."""

	#: Просьба опубликовать готовый файл: путь и id канала со страницы
	#: (0 — канал не выбран). Ловит главное окно → «Публикация».
	publish_requested = Signal(str, int)

	#: Просьба опубликовать несколько готовых файлов пакетом (ADR-0015):
	#: список путей и id канала. Ловит главное окно → пакет на «Публикации».
	publish_files_requested = Signal(list, int)

	#: Просьба опубликовать папку готовых видео пакетом: путь к папке
	#: и id канала. Ловит главное окно → пакет на «Публикации».
	publish_folder_requested = Signal(str, int)

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("video")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._session_done = 0  # готовых с последней итоговой плашки
		self._entries: list[_FileEntry] = []  # карточки файлов к обработке
		self._processed_items: list[VideoFile] = []  # вся подпапка результатов
		self._processed_checked: set[str] = set()  # пути отмеченных к публикации
		self._processed_page = 1
		self._processed_view: ListPage[VideoFile] = paginate([], 1)
		self._processed_dir = ""  # папка текущего списка готовых видео
		# общий редактор параметров карточек файлов (см. _EntryEditor)
		self._editor = _EntryEditor(self)
		self._editor.form.resolution_changed.connect(self._on_editor_resolution)
		self._build()
		self._reload_presets()

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Обновляет каналы и готовые видео при каждом открытии страницы.

		Список перечитывается с диска: файлы могли уехать в опубликованные
		(после отправки поста) или измениться мимо приложения.
		"""
		super().showEvent(event)
		self._reload_communities()
		self._reload_processed()

	# --- сборка страницы ---------------------------------------------------------

	def _build(self) -> None:
		layout = page_layout(self)
		layout.addWidget(SubtitleLabel("Подготовка видео", self))
		self._build_source_row(layout)
		self._build_community_row(layout)
		self._build_preset_row(layout)
		self._build_template_card(layout)
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Файлы и параметры обработки", self))
		self._empty_hint = CaptionLabel("Файлы не выбраны", self)
		layout.addWidget(self._empty_hint)
		self._files_box = QVBoxLayout()
		self._files_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._files_box)
		self._build_process_row(layout)
		self._build_queue_block(layout)
		self._build_processed_block(layout)
		layout.addStretch()

	def _build_template_card(self, layout: QVBoxLayout) -> None:
		"""Карточка-шаблон «[Параметры пресета]» — сразу под строкой пресета.

		Параметры без файла: правятся и сохраняются в пресеты всегда,
		а добавляемые файлы получают их снимок в свои карточки.
		"""
		self._template_card = CollapsibleCard("[Параметры пресета]", self)
		self._template_card.set_summary("шаблон: эти параметры получат добавляемые файлы")
		self._form = PresetForm(self)
		self._template_card.body.addWidget(self._form)
		layout.addWidget(self._template_card)

	def _update_empty_hint(self) -> None:
		"""Заглушка «Файлы не выбраны» видна только при пустом списке."""
		self._empty_hint.setVisible(not self._entries)

	def _build_queue_block(self, layout: QVBoxLayout) -> None:
		"""Панель очереди обработки: итоговая строка и карточки элементов."""
		self._queue_summary = CaptionLabel("", self)
		self._queue_summary.hide()
		layout.addWidget(self._queue_summary)
		queue_box = QVBoxLayout()
		queue_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(queue_box)
		self._queue = QueuePanel(
			self._worker,
			self,
			queue_box,
			service=lambda: self._worker.engine.video_queue,
			subtitle=self._queue_subtitle,
			on_finished=self._on_queue_finished,
			on_refreshed=self._update_queue_summary,
			on_drained=self._notify_drained,
		)

	def _build_processed_block(self, layout: QVBoxLayout) -> None:
		"""Раздел готовых видео: папка результатов текущей подпапки.

		Список ведёт общий ``CardList``: снимок с диска приходит после
		каждого готового файла и при каждом показе страницы, и карточки
		меняются точечно по отпечатку, а не пересобираются все (190 строк
		по 1,3 МБ на каждое завершённое задание — замер 19.09.2026).
		Показ страницами, как у очереди и отложенных записей.
		"""
		layout.addSpacing(8)
		layout.addWidget(SubtitleLabel("Готовые видео", self))
		self._processed_hint = CaptionLabel("", self)
		self._processed_hint.setWordWrap(True)
		layout.addWidget(self._processed_hint)
		self._result_box = QVBoxLayout()
		self._result_box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._result_box)
		self._processed_list = CardList(
			self,
			self._result_box,
			title=processed_title,
			subtitle=processed_subtitle,
			signature=processed_signature,
			key=lambda item: item.path,
			actions=self._processed_actions,
			compact=True,
		)
		# итог и перелистывание — общие с очередью и отложенными
		self._processed_pager = PagerRow(self, self._step_processed)
		layout.addLayout(self._processed_pager.layout)
		self._build_processed_actions(layout)
		# список идёт за подпапкой: она задаёт папку, куда уйдёт результат
		self._form.subdir_changed.connect(self._on_subdir_changed)

	def _build_processed_actions(self, layout: QVBoxLayout) -> None:
		"""Кнопки массовой публикации под списком готовых видео."""
		row = QHBoxLayout()
		self._publish_all_button = PushButton(FluentIcon.SEND, "Опубликовать все", self)
		self._publish_all_button.setToolTip("Все видео списка — пакетом на «Публикацию»")
		self._publish_all_button.clicked.connect(self._publish_all_processed)
		row.addWidget(self._publish_all_button)
		self._publish_checked_button = PushButton("Опубликовать отмеченные", self)
		self._publish_checked_button.setToolTip(
			"Видео, отмеченные чекбоксами, — пакетом на «Публикацию»"
		)
		self._publish_checked_button.clicked.connect(self._publish_checked_processed)
		row.addWidget(self._publish_checked_button)
		publish_folder = PushButton("Опубликовать папку…", self)
		publish_folder.setToolTip(
			"Выбрать подпапку в обработанных и отправить её пакетом на «Публикацию»"
		)
		publish_folder.clicked.connect(self._publish_processed_folder)
		row.addWidget(publish_folder)
		row.addStretch()
		layout.addLayout(row)

	def _build_source_row(self, layout: QVBoxLayout) -> None:
		"""Источник: две равнозначные кнопки — файл или папка.

		Каждый выбранный файл добавляется карточкой в список (не заменяет
		прежний); папка добавляет пачку через диалог сканирования.
		"""
		src_row = QHBoxLayout()
		add_file = PushButton(FluentIcon.VIDEO, "Добавить файл…", self)
		add_file.setToolTip("Выбрать видеофайл — он добавится карточкой в список ниже")
		add_file.clicked.connect(self._add_file)
		src_row.addWidget(add_file)
		add_folder = PushButton(FluentIcon.FOLDER, "Добавить папку…", self)
		add_folder.setToolTip(
			"Рекурсивно найти видео в папке и добавить выбранные в список "
			"(результаты пакета — в его подпапке)"
		)
		add_folder.clicked.connect(self._add_folder)
		src_row.addWidget(add_folder)
		src_row.addStretch()
		layout.addLayout(src_row)

	def _build_community_row(self, layout: QVBoxLayout) -> None:
		"""Канал: выбор подставляет его пресет по умолчанию (настройка канала)."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Сообщество:", self))
		self._community_combo: DtoComboBox[CommunityDto] = DtoComboBox(
			self, placeholder="(не выбран)"
		)
		self._community_combo.setToolTip(
			"Выбор канала загружает его пресет по умолчанию "
			"(задаётся на странице сообщества → «Настройки…»)"
		)
		self._community_combo.currentIndexChanged.connect(self._on_community_selected)
		row.addWidget(self._community_combo, stretch=1)
		layout.addLayout(row)

	def _build_preset_row(self, layout: QVBoxLayout) -> None:
		"""Пресет: выбор-загрузка и кнопки сохранения/удаления."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Пресет:", self))
		self._preset_combo: DtoComboBox[PresetDto] = DtoComboBox(
			self, placeholder="(свои настройки)"
		)
		self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
		row.addWidget(self._preset_combo, stretch=1)
		self._save_button = PushButton(FluentIcon.SAVE, "Сохранить", self)
		self._save_button.clicked.connect(self._on_save_preset)
		row.addWidget(self._save_button)
		save_as = PushButton("Сохранить как…", self)
		save_as.clicked.connect(self._on_save_preset_as)
		row.addWidget(save_as)
		self._delete_button = PushButton(FluentIcon.DELETE, "Удалить", self)
		self._delete_button.clicked.connect(self._on_delete_preset)
		row.addWidget(self._delete_button)
		layout.addLayout(row)

	def _build_process_row(self, layout: QVBoxLayout) -> None:
		run_row = QHBoxLayout()
		process_all = PushButton(FluentIcon.PLAY, "Обработать все", self)
		process_all.setToolTip("Поставить в очередь весь список — галочки не важны")
		process_all.clicked.connect(self._on_process_all)
		run_row.addWidget(process_all)
		self._process_button = PrimaryPushButton(FluentIcon.PLAY, "Обработать", self)
		self._process_button.clicked.connect(self._on_process)
		run_row.addWidget(self._process_button)
		run_row.addStretch()
		layout.addLayout(run_row)

	# --- пресеты -------------------------------------------------------------------

	def _reload_presets(self, select_name: str | None = None) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_presets(),
			self,
			partial(self._show_presets, select_name),
			self._show_error,
		)

	def _show_presets(self, select_name: str | None, presets: list[PresetDto]) -> None:
		"""Наполняет список пресетов (выбор сохраняется по id пресета)."""
		self._preset_combo.set_items(
			presets,
			label=lambda preset: preset.name,
			key=lambda preset: preset.id,
		)
		if select_name is not None:
			self._preset_combo.select(lambda preset: preset.name == select_name)
		self._update_preset_buttons()

	# --- канал и его пресет по умолчанию -------------------------------------------

	def _reload_communities(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._show_communities,
			self._show_error,
		)

	def _show_communities(self, communities: list[CommunityDto]) -> None:
		"""Наполняет список каналов (выбор сохраняется по id канала)."""
		self._community_combo.set_items(
			communities,
			label=community_combo_label,
			key=lambda community: community.id,
		)

	def _on_community_selected(self, _index: int) -> None:
		"""Выбор канала — загрузка его пресета по умолчанию в панель."""
		community = self._community_combo.selected()
		if community is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get_for(COMMUNITY_DEFAULT_PRESET, community.id),
			self,
			partial(self._apply_community_preset, community),
			self._show_error,
		)

	def _is_stale_community(self, community_id: int) -> bool:
		"""Пришёл ли ответ движка для уже переключённого канала.

		Пока движок занят, ответы задерживаются (та же гонка, что
		``_is_stale`` на «Публикации»): без проверки пресет канала A
		лёг бы в шаблон уже выбранного канала B.
		"""
		return not self._community_combo.is_current_id(community_id)

	def _apply_community_preset(self, community: CommunityDto, preset_id: int | None) -> None:
		"""Подставляет пресет сообщества; нет пресета — форма не трогается.

		Выбор в списке вызывает ``_on_preset_selected`` — панель заполнится.
		Ссылка на удалённый пресет равнозначна «не задан».
		"""
		if self._is_stale_community(community.id):
			return
		if preset_id is None or not self._preset_combo.select(
			lambda preset: preset.id == preset_id
		):
			show_info(
				self,
				"Пресет не задан",
				f"У «{community.title}» нет пресета по умолчанию — "
				"задайте его на странице сообщества → «Настройки…».",
			)

	def _update_preset_buttons(self) -> None:
		"""«Сохранить»/«Удалить» доступны только при выбранном пресете."""
		has_preset = self._preset_combo.selected() is not None
		self._save_button.setEnabled(has_preset)
		self._delete_button.setEnabled(has_preset)

	def _on_preset_selected(self, _index: int) -> None:
		"""Выбор пресета — загрузка его значений в панель."""
		self._update_preset_buttons()
		preset = self._preset_combo.selected()
		if preset is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.get_preset_fields(preset.id),
			self,
			partial(self._apply_preset_fields, preset.id),
			self._show_error,
		)

	def _apply_preset_fields(self, preset_id: int, fields: PresetFields) -> None:
		"""Заполняет панель, если пресет всё ещё выбран (защита от гонки)."""
		if self._preset_combo.is_current_id(preset_id):
			self._form.fill(fields)

	def _on_save_preset(self) -> None:
		"""Перезаписывает выбранный пресет текущим состоянием панели."""
		preset = self._preset_combo.selected()
		if preset is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.save_preset(self._form.fields(preset.name), preset.id),
			self,
			self._on_preset_saved,
			self._show_error,
		)

	def _on_save_preset_as(self) -> None:
		"""Сохраняет состояние панели новым пресетом (спрашивает имя)."""
		dialog = FormDialog(
			"Сохранить пресет",
			[("name", "Имя пресета…")],
			self.window(),
			accept_text="Сохранить",
		)
		if not exec_dialog(dialog):
			return
		name = dialog.value("name")
		if not name:
			self._show_error("У пресета должно быть имя.")
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.save_preset(self._form.fields(name)),
			self,
			self._on_preset_saved,
			self._show_error,
		)

	def _on_preset_saved(self, preset: PresetDto) -> None:
		show_success(self, "Пресет сохранён", preset.name)
		self._reload_presets(select_name=preset.name)

	def _on_delete_preset(self) -> None:
		preset = self._preset_combo.selected()
		if preset is None:
			return
		if not confirm_delete(self, f"Удалить пресет «{preset.name}»?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.delete_preset(preset.id),
			self,
			lambda *_a: self._reload_presets(),
			self._show_error,
		)

	# --- список файлов и постановка в очередь ---------------------------------------

	def _template_fields(self) -> PresetFields:
		"""Параметры шаблона; имя — от выбранного пресета или «ручные»."""
		preset = self._preset_combo.selected()
		return self._form.fields(preset.name if preset else _MANUAL_NAME)

	def _add_file(self) -> None:
		"""«Добавить файл…»: диалог — в папке исходников подпапки шаблона."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker,
			self._worker.engine.video.dirs_for(subdir),
			self,
			self._pick_file_source,
			self._show_error,
		)

	def _pick_file_source(self, dirs: VideoDirs) -> None:
		"""Выбор одиночного файла — он добавляется карточкой в список."""
		path = pick_file(
			self,
			"Исходное видео",
			f"{video_dialog_filter()};;Все файлы (*)",
			start_dir=dirs.source,
		)
		if path:
			self._add_entry(path, batch="")

	def _add_folder(self) -> None:
		"""«Добавить папку…»: сканирование и выбор — как пакет ADR-0014."""
		subdir = str(self._form.fields("").subdir)
		run_in_engine(
			self._worker,
			self._worker.engine.video.dirs_for(subdir),
			self,
			self._pick_folder_source,
			self._show_error,
		)

	def _pick_folder_source(self, dirs: VideoDirs) -> None:
		"""Выбор папки; отмеченные в диалоге файлы добавляются карточками."""
		root = pick_dir(self, "Папка с исходниками", start_dir=dirs.source)
		if not root:
			return
		dialog = BatchScanDialog(self._worker, root, self.window())
		if not exec_dialog(dialog):
			return
		files = dialog.selected()
		if not files:
			return
		# правило имени подпапки пакета держит движок (ADR-0014, п. 4)
		batch = batch_subdir_name(root)
		added = 0
		for video in files:
			if self._add_entry(
				video.path, batch=batch, size_bytes=video.size_bytes, frame=video.frame
			):
				added += 1
		if added:
			show_success(self, "Файлы добавлены", f"В списке новых: {added}")

	def _add_entry(
		self,
		path: str,
		batch: str,
		size_bytes: int | None = None,
		frame: tuple[int, int] | None = None,
	) -> bool:
		"""Добавляет файл карточкой; параметры — снимок шаблона.

		Args:
			path: путь к исходнику.
			batch: подпапка пакета («» — одиночное добавление).
			size_bytes: размер файла, если он уже известен.
			frame: размеры кадра, если исходник уже прощупан (пакет:
				сканирование папки прощупывает каждый файл). Известные
				размеры избавляют от второй пробы ffprobe на файл.

		Returns:
			True — карточка добавлена; False — файл уже в списке
			или не читается.
		"""
		if any(entry.path == path for entry in self._entries):
			show_info(self, "Уже в списке", Path(path).name)
			return False
		if size_bytes is None:
			try:
				size_bytes = Path(path).stat().st_size
			except OSError:
				self._show_error(f"Файл не читается: {path}")
				return False
		fields = self._template_fields()
		entry = _FileEntry(self, path, size_bytes, batch, fields, self._remove_entry)
		# раскрытие карточки ставит в неё общий редактор, сворачивание — убирает
		entry.card.expanded_changed.connect(partial(self._on_entry_expanded, entry))
		self._entries.append(entry)
		self._files_box.addWidget(entry.card)
		self._update_empty_hint()
		# подсказки вспомогательные: сбой не мешает добавлению файла
		if frame is not None:
			# размеры уже есть — спрашиваем движок только о битрейте
			# (для файла в лимите это лишь чтение размера файла)
			entry.source_frame = frame
			self._refresh_scale_note(entry)
			run_in_engine(
				self._worker,
				self._worker.engine.video.bitrate_advice(path, fields.trim_start, fields.trim_end),
				self,
				partial(self._on_entry_bitrate, entry),
				noop,
			)
		else:
			# одиночный файл: размеры кадра и совет по битрейту — одной пробой
			run_in_engine(
				self._worker,
				self._worker.engine.video.source_advice(path, fields.trim_start, fields.trim_end),
				self,
				partial(self._on_entry_source, entry),
				noop,
			)
		return True

	def _on_entry_source(self, entry: _FileEntry, advice: SourceAdvice | None) -> None:
		"""Сведения об исходнике пришли: размеры кадра и совет по битрейту."""
		if advice is None or entry not in self._entries:
			return  # файл не прочитан или карточку уже убрали
		entry.source_frame = (advice.width, advice.height)
		self._refresh_scale_note(entry)
		self._on_entry_bitrate(entry, advice.bitrate)

	def _on_entry_bitrate(self, entry: _FileEntry, advice: BitrateAdvice | None) -> None:
		"""Совет битрейта пришёл — подставляем в параметры карточки файла."""
		if advice is None or entry not in self._entries:
			return  # файл в лимите или карточку уже убрали
		if self._editor.suggest_bitrate(entry, advice.mbps):
			entry.advice_note = (
				f"больше лимита {advice.limit_gb} ГБ — качество {advice.mbps:g} Мбит/с"
			)
			entry.refresh_summary()

	def _refresh_scale_note(self, entry: _FileEntry) -> None:
		"""Пересчитывает предупреждение об апскейле для карточки файла.

		Зовётся и при появлении размеров исходника, и при смене ступени
		разрешения. Пока размеры неизвестны (файл не прощупан), молчим:
		врать про увеличение хуже, чем не сказать ничего.
		"""
		if entry not in self._entries:
			return
		note = ""
		target = self._editor.fields_of(entry).target_resolution
		if entry.source_frame is not None and is_upscale(*entry.source_frame, target):
			width, height = entry.source_frame
			out_width, out_height = scaled_size(width, height, target)
			note = f"апскейл: {width}×{height} → {out_width}×{out_height}"
		entry.scale_note = note
		self._editor.set_scale_note(entry, note)
		entry.refresh_summary()

	def _on_entry_expanded(self, entry: _FileEntry, expanded: bool) -> None:
		"""Раскрытие карточки ставит в неё общий редактор, сворачивание — убирает."""
		if expanded:
			self._editor.attach(entry)
		elif self._editor.editing is entry:
			self._editor.detach()

	def _on_editor_resolution(self) -> None:
		"""Смена ступени в редакторе — пересчёт пометки у раскрытой карточки."""
		entry = self._editor.editing
		if entry is not None:
			self._refresh_scale_note(entry)

	def _remove_entry(self, entry: _FileEntry) -> None:
		"""Убирает карточку файла из списка (сам файл не трогается)."""
		self._remove_entries([entry])

	def _remove_entries(self, entries: list[_FileEntry]) -> None:
		"""Убирает перечисленные карточки (после постановки или корзинкой)."""
		for entry in entries:
			if entry in self._entries:
				self._entries.remove(entry)
				self._editor.release(entry)  # форма — страницы, а не карточки
				entry.card.deleteLater()
		self._update_empty_hint()

	def _on_process_all(self) -> None:
		"""Ставит в очередь весь список — независимо от галочек."""
		if not self._entries:
			self._show_error("Добавьте файл или папку — список пуст.")
			return
		self._process_entries(list(self._entries))

	def _on_process(self) -> None:
		"""Ставит отмеченные файлы в очередь — каждый со своими параметрами."""
		if not self._entries:
			self._show_error("Добавьте файл или папку — список пуст.")
			return
		checked = [entry for entry in self._entries if entry.check.isChecked()]
		selected = checked_or_single(self._entries, checked)
		if selected is None:
			self._show_error("Отметьте чекбоксами файлы, которые обрабатывать.")
			return
		self._process_entries(selected)

	def _process_entries(self, entries: list[_FileEntry]) -> None:
		"""Собирает запросы перечисленных карточек и ставит их в очередь."""
		collected = self._collect_requests(entries)
		if collected is None:
			return
		requests, submitted = collected
		if not requests:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video_queue.enqueue_many(requests),
			self,
			partial(self._on_enqueued, submitted),
			self._show_error,
		)

	def _collect_requests(
		self, entries: list[_FileEntry]
	) -> tuple[list[ProcessingRequest], list[_FileEntry]] | None:
		"""Заявки по отмеченным карточкам; «кадры на выбор» — проход по файлам.

		Интерактивный источник кадра несовместим с фоновой очередью,
		поэтому кадры выбираются заранее: диалог по разу на файл, выбранный
		кадр копируется в папку очереди (следующая партия кандидатов стёрла
		бы его). Отмена выбора: для единственного файла — отмена запуска,
		иначе — предложение исключить файл (остальные не теряются).

		Returns:
			Пара (заявки, их карточки) — карточки убираются после успешной
			постановки; None — запуск отменён целиком.
		"""
		single = len(entries) == 1
		requests: list[ProcessingRequest] = []
		submitted: list[_FileEntry] = []
		for entry in entries:
			fields = self._editor.fields_of(entry)
			kind, _value = parse_intro_source(fields.intro_source)
			if not (kind is IntroSourceKind.RANDOM_CHOICE and (fields.intro or fields.cover)):
				requests.append(ProcessingRequest(entry.path, fields, batch_subdir=entry.batch))
				submitted.append(entry)
				continue
			try:
				intro = self._pick_frame_for(entry, fields, single)
			except _AbortRun:
				return None
			if intro is None:
				continue  # файл исключён из постановки — карточка остаётся
			requests.append(
				ProcessingRequest(entry.path, fields, intro_source=intro, batch_subdir=entry.batch)
			)
			submitted.append(entry)
		return requests, submitted

	def _pick_frame_for(self, entry: _FileEntry, fields: PresetFields, single: bool) -> str | None:
		"""Выбор кадра заставки для одного файла (до постановки).

		Returns:
			Строка «image:путь» с копией кадра; None — файл исключён.

		Raises:
			_AbortRun: Пользователь отменил постановку целиком.
		"""
		while True:
			dialog = FramePickerDialog(
				self._worker,
				entry.path,
				self.window(),
				trim_start=fields.trim_start,
				trim_end=fields.trim_end,
				target_resolution=fields.target_resolution,
				file_label=None if single else Path(entry.path).name,
			)
			accepted = exec_dialog(dialog)
			chosen = dialog.chosen_path()
			if accepted and chosen is not None:
				stashed = self._stash_frame(chosen)
				if stashed is None:
					raise _AbortRun  # ошибка уже показана
				return build_intro_source(IntroSourceKind.IMAGE, stashed)
			if single:
				raise _AbortRun
			if confirm_delete(
				self,
				f"Кадр для «{Path(entry.path).name}» не выбран. Исключить файл из постановки?",
				accept_text="Исключить",
			):
				return None
			# «Отмена» в подтверждении — вернуться к выбору кадра

	def _stash_frame(self, chosen: str) -> str | None:
		"""Копирует выбранный кадр в папку очереди (синхронно, с таймаутом).

		Синхронный вызов оправдан: копирование PNG — мгновенное, а проход
		выбора кадров — последовательность модальных диалогов, где колбэки
		моста только запутали бы поток управления.
		"""
		try:
			future = self._worker.submit(self._worker.engine.video_queue.stash_frame(chosen))
			stashed: str = future.result(timeout=_STASH_TIMEOUT_S)
			return stashed
		except Exception as exc:  # noqa: BLE001 — показываем и прерываем постановку
			self._show_error(user_message(exc))
			return None

	def _on_enqueued(self, submitted: list[_FileEntry], _ids: list[int]) -> None:
		"""Заявки приняты — поставленные карточки уходят из списка.

		Неотмеченные (и исключённые при выборе кадров) остаются —
		их можно доправить и поставить следующим заходом.
		"""
		self._remove_entries(submitted)
		if len(submitted) > 1:
			show_success(self, "Пакет в очереди", f"Файлов: {len(submitted)}")
		self._queue.poll()  # карточки видны сразу, не по таймеру

	# --- панель очереди обработки -------------------------------------------------

	def queue_busy(self) -> bool:
		"""Есть ли необработанное в очереди (для подтверждения выхода)."""
		return self._queue.busy()

	def _on_queue_finished(self, item: VideoItemDto, done: bool) -> None:
		"""Учитывает завершённый элемент (плашки на каждый файл нет:
		готовый файл — строка «Готовых видео», а не элемент очереди)."""
		del item  # реакции важен только исход
		if done:
			self._session_done += 1
			self._reload_processed()  # готовый файл появляется в списке

	def _notify_drained(self, visible: list[VideoItemDto]) -> None:
		"""Одна итоговая плашка, когда очередь доработала (вместо плашки
		на каждый файл — пакет их наплодил бы десятками).

		Родитель — окно: опрос живёт всегда, и завершение может прийти
		при скрытой странице — плашка на ней погасла бы незамеченной.
		"""
		errors = sum(1 for item in visible if item.status is JobStatus.ERROR)
		if self._session_done:
			text = f"Готово файлов: {self._session_done}"
			if errors:
				text += f" · ошибок: {errors} (см. карточки)"
			show_success(self.window(), "Обработка завершена", text)
		elif errors:
			show_warning(
				self.window(),
				"Обработка завершена",
				f"Ошибок: {errors} — «Повторить» или «Убрать» на карточках.",
			)
		self._session_done = 0

	def _update_queue_summary(self, items: list[VideoItemDto]) -> None:
		"""Итоговая строка над карточками: сколько осталось и ошибки."""
		if not items:
			self._queue_summary.hide()
			return
		left = sum(1 for item in items if not item.status.finished())
		errors = sum(1 for item in items if item.status is JobStatus.ERROR)
		parts = []
		if left:
			parts.append(f"осталось {left}")
		if self._session_done:
			parts.append(f"готово {self._session_done}")
		if errors:
			parts.append(f"ошибок {errors}")
		self._queue_summary.setText("Очередь обработки: " + ", ".join(parts))
		self._queue_summary.show()

	@staticmethod
	def _queue_subtitle(item: VideoItemDto) -> str:
		"""Подпись карточки: пакет, статус и пометки выполнения."""
		status_text = {
			JobStatus.PENDING: "в очереди",
			JobStatus.RUNNING: "кодируется",
			JobStatus.DONE: "готово",
			JobStatus.ERROR: f"ошибка: {item.error}",
			JobStatus.CANCELLED: "отменено",
		}[item.status]
		parts = []
		if item.batch:
			parts.append(f"пакет «{item.batch}»")
		parts.append(status_text)
		if item.note:
			parts.append(item.note)
		return " · ".join(parts)

	# --- готовые видео -----------------------------------------------------------

	def _reload_processed(self, *_args: object) -> None:
		"""Перечитывает список готовых видео текущей подпапки."""
		run_in_engine(
			self._worker,
			self._worker.engine.video.list_processed(self._form.fields("").subdir),
			self,
			self._show_processed,
			self._show_error,
		)

	def _on_subdir_changed(self, _subdir: str) -> None:
		"""Сменилась подпапка шаблона — другой список, листаем с начала."""
		self._processed_page = 1
		self._reload_processed()

	def _show_processed(self, listing: ProcessedListing) -> None:
		"""Снимок папки получен: список и отметки — карточки меняются точечно."""
		self._processed_hint.setText(f"Папка: {listing.directory}")
		self._processed_dir = listing.directory  # старт диалога «Опубликовать папку…»
		self._processed_items = list(listing.items)
		# отметки исчезнувших файлов (уехали в опубликованные, удалены) снимаются
		self._processed_checked &= {item.path for item in self._processed_items}
		has_items = bool(self._processed_items)
		self._publish_all_button.setEnabled(has_items)
		self._publish_checked_button.setEnabled(has_items)
		self._render_processed()

	def _render_processed(self) -> None:
		"""Страница списка: карточки по отпечатку, итог и перелистывание."""
		items = self._processed_items
		self._processed_view = paginate(items, self._processed_page)
		# зажатый номер возвращается: файлы под человеком могли исчезнуть
		self._processed_page = self._processed_view.page
		self._processed_list.sync(self._processed_view.items)
		self._processed_pager.update(
			self._processed_view, summary_text(self._processed_view, len(items), PROCESSED_WORDS)
		)

	def _step_processed(self, delta: int) -> None:
		"""Листает страницу готовых видео."""
		self._processed_page = step_page(self._processed_page, delta, self._processed_view.pages)
		self._render_processed()

	def _processed_actions(self, item: VideoFile, parent: QWidget) -> list[QWidget]:
		"""Правый край карточки: действия над файлом, отметка, удаление.

		Отметка живёт у страницы множеством путей, а не в чекбоксе:
		карточку могут обновить (сменился размер файла) или увести
		на другую страницу — отмеченное не должно пропасть.
		"""
		open_btn = PushButton(FluentIcon.PLAY, "Открыть", parent)
		open_btn.clicked.connect(bind(open_in_system, item.path))
		folder_btn = PushButton(FluentIcon.FOLDER, "Показать в папке", parent)
		folder_btn.clicked.connect(bind(open_in_system, str(Path(item.path).parent)))
		publish_btn = PrimaryPushButton(FluentIcon.SEND, "Опубликовать…", parent)
		publish_btn.clicked.connect(bind(self._request_publish, item.path))
		check = CheckBox("", parent)
		check.setToolTip("Отметить для «Опубликовать отмеченные»")
		check.setChecked(item.path in self._processed_checked)
		check.toggled.connect(partial(self._set_processed_checked, item.path))
		delete = TransparentToolButton(FluentIcon.DELETE, parent)
		delete.setToolTip("Удалить файл с диска (с подтверждением)")
		delete.clicked.connect(bind(self._on_delete_processed, item))
		return [open_btn, folder_btn, publish_btn, check, delete]

	def _set_processed_checked(self, path: str, checked: bool) -> None:
		"""Чекбокс карточки переключён — отметка в множестве страницы."""
		if checked:
			self._processed_checked.add(path)
		else:
			self._processed_checked.discard(path)

	# --- массовая публикация готовых видео (ADR-0015) -------------------------------

	def _current_community(self) -> CommunityDto | None:
		"""Выбранный канал или подсказка (имя — как на «Публикации»)."""
		community = self._community_combo.selected()
		if community is None:
			self._show_error(
				"Выберите сообщество (список над пресетом) — пакет публикуется в него."
			)
		return community

	def _publish_all_processed(self) -> None:
		"""Все видео подпапки (все страницы) — пакетом на «Публикацию»."""
		self._emit_publish_files(list(self._processed_items))

	def _publish_checked_processed(self) -> None:
		"""Отмеченные видео (на любой странице) — пакетом на «Публикацию»."""
		items = list(self._processed_items)
		checked = [item for item in items if item.path in self._processed_checked]
		picked = checked_or_single(items, checked)
		if picked is None:
			self._show_error("Отметьте чекбоксами готовые видео для публикации.")
			return
		self._emit_publish_files(picked)

	def _emit_publish_files(self, items: list[VideoFile]) -> None:
		"""Передаёт файлы пакетом на «Публикацию» (через главное окно)."""
		if not items:
			self._show_error("Готовых видео нет — публиковать нечего.")
			return
		community = self._current_community()
		if community is None:
			return
		if len(items) == 1:
			# один файл — не пакет: обычная форма публикации,
			# как у кнопки «Опубликовать…» на карточке
			self.publish_requested.emit(items[0].path, community.id)
			return
		self.publish_files_requested.emit([item.path for item in items], community.id)

	def _publish_processed_folder(self) -> None:
		"""Выбор подпапки в обработанных — вся она пакетом на «Публикацию»."""
		community = self._current_community()
		if community is None:
			return
		root = pick_dir(self, "Папка готовых видео", start_dir=self._processed_dir)
		if root:
			self.publish_folder_requested.emit(root, community.id)

	def _on_delete_processed(self, item: VideoFile) -> None:
		"""Удаляет готовое видео с диска (вместе с кадром-превью)."""
		if not confirm_delete(
			self,
			f"Удалить файл «{item.name}» с диска? Вместе с ним удалится "
			"кадр-превью. Отменить удаление будет нельзя.",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.video.delete_processed(item.path),
			self,
			lambda *_a: self._reload_processed(),
			self._show_error,
		)

	def _request_publish(self, path: str) -> None:
		"""Передаёт файл на «Публикацию» вместе с выбранным каналом."""
		community = self._community_combo.selected()
		self.publish_requested.emit(path, community.id if community else 0)
