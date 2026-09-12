"""Рабочее окно обслуживания сообщества: уборка в ленте и в участниках.

Два раздела-сегмента (ADR-0026): **служебные записи** («такой-то
вступил», «сообщение закреплено») и **удалённые аккаунты** — мёртвые
души в списке участников. У каждого свой набор параметров и свой отчёт,
поэтому они разведены, а не свалены на один экран.

Оба устроены одинаково и по одному правилу: сначала «Просмотреть» —
он ничего не меняет и отвечает числами, — и только потом удаление
с подтверждением. Порядок не для удобства: удаление необратимо.

Ход работы виден панелью очереди (той же, что на «Публикации»
и «Видео»): прогресс, отмена, повтор после ошибки.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	PushButton,
	SegmentedWidget,
	SpinBox,
	StrongBodyLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.jobs import JobStatus
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.maintenance import (
	DEFAULT_DELETE_LIMIT,
	DEFAULT_DEPTH,
	DEFAULT_KICK_LIMIT,
	DEFAULT_KINDS,
	DELETE_LIMIT_RANGE,
	DEPTH_RANGE,
	KICK_LIMIT_RANGE,
	MaintenanceItemDto,
	MembersReport,
	ServiceReport,
)
from pxcontrol.engine.telegram.types import ServiceMessageKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	ErrorLabel,
	QueuePanel,
	WorkDialog,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
	format_local,
	list_area,
)

#: Человеческие названия видов служебных записей (ADR-0026).
_KIND_TITLES = {
	ServiceMessageKind.MEMBERS: "Вступления и уходы",
	ServiceMessageKind.PINS: "Закрепления сообщений",
	ServiceMessageKind.APPEARANCE: "Оформление: название, аватар, тема, обои",
	ServiceMessageKind.CALLS: "Видеочаты и звонки",
	ServiceMessageKind.OTHER: "Прочее служебное: подарки, бусты, платежи",
	ServiceMessageKind.PROTECTED: "Темы форума и история сообщества",
}

#: Почему защищённые записи не предлагаются к удалению.
_PROTECTED_HINT = (
	"не удаляются: тема форума — это её первое сообщение, "
	"а создание и переезд сообщества — его история"
)


def kind_title(kind: ServiceMessageKind) -> str:
	"""Человеческое название вида служебных записей."""
	return _KIND_TITLES.get(kind, str(kind))


def service_summary(report: ServiceReport) -> str:
	"""Итог прохода по служебным записям одной строкой."""
	if report.deleted or report.skipped:
		parts = [f"Удалено записей: {report.deleted}"]
		if report.skipped:
			parts.append(f"Telegram не дал удалить: {report.skipped}")
		if report.limited:
			parts.append("сработал предел за проход — повторите, чтобы продолжить")
		return ". ".join(parts) + "."
	if not report.found:
		return f"Служебных записей не найдено — просмотрено {report.scanned}."
	tail = (
		"история просмотрена целиком"
		if report.exhausted
		else f"просмотрено {report.scanned} последних сообщений"
	)
	if report.oldest_date is not None:
		tail += f", до {format_local(report.oldest_date)}"
	return f"Найдено служебных записей: {sum(report.found.values())} — {tail}."


def members_summary(report: MembersReport) -> str:
	"""Итог прохода по участникам одной строкой."""
	if report.removed or report.skipped:
		parts = [f"Исключено удалённых аккаунтов: {report.removed}"]
		if report.skipped:
			parts.append(f"Telegram не дал исключить: {report.skipped}")
		if report.limited:
			parts.append("сработал предел за проход — повторите, чтобы продолжить")
		if report.service_left:
			parts.append(
				f"записей «удалил участника» осталось в ленте: {report.service_left} "
				"(нет права удалять сообщения)"
			)
		return ". ".join(parts) + "."
	seen = f"просмотрено участников: {report.scanned}"
	if report.total:
		seen += f" из {report.total}"
	if report.capped:
		seen += " — дальше Telegram список не отдаёт"
	if not report.found:
		return f"Удалённых аккаунтов не найдено ({seen})."
	return f"Найдено удалённых аккаунтов: {report.found} ({seen})."


class MaintenanceDialog(WorkDialog):
	"""Окно обслуживания: служебные записи и удалённые аккаунты."""

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(f"Обслуживание · {community.title}", parent)
		self._worker = worker
		self._community = community
		self._show_error = error_reporter(self)
		self._boxes: dict[ServiceMessageKind, CheckBox] = {}
		# id задания → метка раздела, который ждёт его исхода. Чужие
		# задания (окно открывали для другого сообщества) в словарь
		# не попадают и на этот экран не влияют
		self._jobs: dict[int, BodyLabel] = {}
		self._build()

	# --- каркас -------------------------------------------------------------------

	def _build(self) -> None:
		"""Каркас окна: сегменты разделов, их страницы и панель хода."""
		spacing = density.spacing()
		self._segments = SegmentedWidget(self)
		self._pages = QStackedWidget(self)
		self.content.addWidget(self._segments)
		self.content.addWidget(self._pages, stretch=1)
		self._add_page("service", "Служебные записи", self._service_page())
		self._add_page("members", "Удалённые аккаунты", self._members_page())
		self._segments.currentItemChanged.connect(self._on_segment)
		self._segments.setCurrentItem("service")
		self.content.addWidget(CaptionLabel("Ход работы", self))
		queue_box = QVBoxLayout()
		queue_box.setSpacing(spacing.list_spacing)
		self.content.addLayout(queue_box)
		self._panel = QueuePanel(
			self._worker,
			self,
			queue_box,
			service=lambda: self._worker.engine.maintenance,
			subtitle=self._job_subtitle,
			on_finished=self._on_job_finished,
			# задание с ошибкой очередь не покидает — до `on_finished`
			# оно не доходит, и без этой сверки метка раздела навсегда
			# осталась бы «Просмотр идёт…»
			on_refreshed=self._on_jobs_refreshed,
		)

	def _add_page(self, key: str, title: str, page: QWidget) -> None:
		"""Добавляет раздел: сегмент сверху и страницу в стопке."""
		self._pages.addWidget(page)
		self._segments.addItem(routeKey=key, text=title, onClick=lambda: self._show(key))

	def _show(self, key: str) -> None:
		"""Показывает страницу раздела."""
		self._pages.setCurrentIndex(0 if key == "service" else 1)

	def _on_segment(self, key: str) -> None:
		"""Смена сегмента — смена страницы."""
		self._show(key)

	# --- раздел «служебные записи» -------------------------------------------------

	def _service_page(self) -> QWidget:
		"""Страница чистки служебных записей."""
		page = QWidget(self)
		box = QVBoxLayout(page)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(
			"Это строки, которые пишет сам Telegram: «такой-то вступил», "
			"«сообщение закреплено», «название изменено». Сначала посмотрим, "
			"сколько их и каких, — удалять будете выбранные виды.",
			page,
		)
		hint.setWordWrap(True)
		box.addWidget(hint)
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Просмотреть последних сообщений:", page))
		self._depth = SpinBox(page)
		self._depth.setRange(*DEPTH_RANGE)
		self._depth.setSingleStep(500)
		self._depth.setValue(DEFAULT_DEPTH)
		self._depth.setToolTip(
			"У Telegram нет поиска «только служебные» — историю приходится "
			"читать целиком, поэтому у прохода есть глубина"
		)
		row.addWidget(self._depth)
		row.addStretch()
		scan = PushButton("Просмотреть", page)
		scan.clicked.connect(self._on_scan_service)
		row.addWidget(scan)
		box.addLayout(row)
		self._service_label = BodyLabel("Просмотр ещё не выполнялся.", page)
		self._service_label.setWordWrap(True)
		box.addWidget(self._service_label)
		area, self._kinds_box = list_area(page, density.spacing().list_spacing)
		box.addWidget(area, stretch=1)
		# проверка заполнения — красной подписью рядом с формой, а не
		# всплывающей плашкой: плашка уезжает и не привязана к месту,
		# где человеку нужно что-то поправить (единый приём проекта)
		self._service_error = ErrorLabel(page)
		box.addWidget(self._service_error)
		box.addLayout(self._clean_row(page))
		return page

	def _clean_row(self, page: QWidget) -> QHBoxLayout:
		"""Предел удаления и кнопка чистки записей."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Удалять за проход не больше:", page))
		self._delete_limit = SpinBox(page)
		self._delete_limit.setRange(*DELETE_LIMIT_RANGE)
		self._delete_limit.setSingleStep(100)
		self._delete_limit.setValue(DEFAULT_DELETE_LIMIT)
		self._delete_limit.setToolTip(
			"Сотни однотипных действий подряд с пользовательского аккаунта "
			"Telegram не приветствует — предел сдерживает поток"
		)
		row.addWidget(self._delete_limit)
		row.addStretch()
		self._clean_button = PushButton("Удалить выбранное", page)
		self._clean_button.setEnabled(False)
		self._clean_button.clicked.connect(self._on_clean_service)
		row.addWidget(self._clean_button)
		return row

	def _on_scan_service(self) -> None:
		"""Ставит задание просмотра записей (ничего не меняет)."""
		self._clean_button.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.maintenance.scan_service_messages(
				self._community.id, depth=self._depth.value()
			),
			self,
			lambda job_id: self._track(job_id, "service", self._service_label, "Просмотр идёт…"),
			self._show_error,
		)

	def _on_clean_service(self) -> None:
		"""Спрашивает подтверждение и ставит задание чистки записей."""
		chosen = [kind for kind, box in self._boxes.items() if box.isChecked()]
		if not chosen:
			self._service_error.fail("Отметьте хотя бы один вид записей.")
			return
		self._service_error.succeed()
		names = "\n".join(f"— {kind_title(kind)};" for kind in chosen)
		if not confirm_delete(
			self,
			f"Удалить служебные записи в «{self._community.title}»?\n\n{names}\n\n"
			f"Удаление необратимо. За проход будет удалено не больше "
			f"{self._delete_limit.value()} записей.",
		):
			return
		self._clean_button.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.maintenance.clean_service_messages(
				self._community.id,
				chosen,
				depth=self._depth.value(),
				delete_limit=self._delete_limit.value(),
			),
			self,
			lambda job_id: self._track(job_id, "service", self._service_label, "Чистка идёт…"),
			self._show_error,
		)

	def _show_service(self, report: ServiceReport) -> None:
		"""Показывает найденные записи: строка на вид, галочка у удаляемых."""
		self._service_label.setText(service_summary(report))
		clear_layout(self._kinds_box)
		self._boxes.clear()
		if report.deleted or report.skipped:
			# после чистки числа устарели — честнее попросить посмотреть заново
			self._clean_button.setEnabled(False)
			return
		for kind, count in sorted(report.found.items(), key=lambda pair: -pair[1]):
			self._kinds_box.addWidget(self._kind_row(kind, count))
		self._clean_button.setEnabled(report.removable > 0)

	def _kind_row(self, kind: ServiceMessageKind, count: int) -> QWidget:
		"""Строка вида: галочка (если удаляем), название и число."""
		box = QWidget(self)
		row = QHBoxLayout(box)
		row.setContentsMargins(0, 0, 0, 0)
		if kind.removable():
			check = CheckBox(kind_title(kind), box)
			check.setChecked(kind in DEFAULT_KINDS)
			self._boxes[kind] = check
			row.addWidget(check, stretch=1)
		else:
			label = BodyLabel(f"{kind_title(kind)} — {_PROTECTED_HINT}", box)
			label.setWordWrap(True)
			row.addWidget(label, stretch=1)
		row.addWidget(StrongBodyLabel(str(count), box))
		return box

	# --- раздел «удалённые аккаунты» -----------------------------------------------

	def _members_page(self) -> QWidget:
		"""Страница чистки удалённых аккаунтов."""
		page = QWidget(self)
		box = QVBoxLayout(page)
		box.setContentsMargins(0, 0, 0, 0)
		box.setSpacing(density.spacing().row_spacing)
		hint = BodyLabel(
			"Удалённый аккаунт — учётка, которую владелец удалил; в списке "
			"участников она остаётся мёртвой душой. Исключение необратимо "
			"и уменьшает число участников, поэтому за проход убирается "
			"не больше выбранного количества.",
			page,
		)
		hint.setWordWrap(True)
		box.addWidget(hint)
		row = QHBoxLayout()
		scan = PushButton("Найти удалённые аккаунты", page)
		scan.clicked.connect(self._on_scan_members)
		row.addWidget(scan)
		row.addStretch()
		box.addLayout(row)
		self._members_label = BodyLabel("Поиск ещё не выполнялся.", page)
		self._members_label.setWordWrap(True)
		box.addWidget(self._members_label)
		box.addStretch()
		kick = QHBoxLayout()
		kick.addWidget(BodyLabel("Исключать за проход не больше:", page))
		self._kick_limit = SpinBox(page)
		self._kick_limit.setRange(*KICK_LIMIT_RANGE)
		self._kick_limit.setSingleStep(5)
		self._kick_limit.setValue(DEFAULT_KICK_LIMIT)
		self._kick_limit.setToolTip(
			"Число участников видно всем: резкое падение бьёт по охватам, "
			"поэтому чистить лучше порциями"
		)
		kick.addWidget(self._kick_limit)
		kick.addStretch()
		self._kick_button = PushButton("Исключить удалённые", page)
		self._kick_button.setEnabled(False)
		self._kick_button.clicked.connect(self._on_clean_members)
		kick.addWidget(self._kick_button)
		box.addLayout(kick)
		return page

	def _on_scan_members(self) -> None:
		"""Ставит задание поиска удалённых аккаунтов."""
		self._kick_button.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.maintenance.scan_deleted_accounts(self._community.id),
			self,
			lambda job_id: self._track(job_id, "members", self._members_label, "Поиск идёт…"),
			self._show_error,
		)

	def _on_clean_members(self) -> None:
		"""Спрашивает подтверждение и ставит задание исключения."""
		limit = self._kick_limit.value()
		if not confirm_delete(
			self,
			f"Исключить удалённые аккаунты из «{self._community.title}»?\n\n"
			f"За проход будет исключено не больше {limit}. Исключение "
			"необратимо и уменьшит число участников.",
			accept_text="Исключить",
		):
			return
		self._kick_button.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.maintenance.clean_deleted_accounts(self._community.id, limit=limit),
			self,
			lambda job_id: self._track(job_id, "members", self._members_label, "Чистка идёт…"),
			self._show_error,
		)

	def _show_members(self, report: MembersReport) -> None:
		"""Показывает итог по участникам."""
		self._members_label.setText(members_summary(report))
		# после исключения числа устарели: пусть поищет заново
		self._kick_button.setEnabled(report.removed == 0 and report.found > 0)

	# --- общее --------------------------------------------------------------------

	def _track(self, job_id: int, section: str, label: BodyLabel, text: str) -> None:
		"""Запоминает, какой раздел ждёт исхода этого задания."""
		self._jobs[job_id] = label
		label.setText(text)

	def _on_job_finished(self, item: MaintenanceItemDto, done: bool) -> None:
		"""Панель сообщила об исходе задания — забираем отчёт.

		Панель снимает завершённые с показа, поэтому отчёт берётся
		здесь: иначе он исчез бы вместе с карточкой. Сюда доходят
		только исходы, покидающие очередь (готово и отменено);
		ошибку ловит :meth:`_on_jobs_refreshed`.
		"""
		label = self._jobs.pop(item.id, None)
		if label is None:
			return  # задание другого окна
		if not done:
			label.setText("Отменено — числа не обновлялись.")
			return
		if item.service is not None:
			self._show_service(item.service)
		elif item.members is not None:
			self._show_members(item.members)

	def _on_jobs_refreshed(self, items: list[Any]) -> None:
		"""Замечает задание, остановившееся на ошибке.

		Элемент с ошибкой остаётся в очереди (его повторяют или
		убирают руками), поэтому реакции на завершение панель для него
		не зовёт. Без этой сверки раздел показывал бы «идёт…» до
		закрытия окна, а причина отказа была бы видна только в журнале.
		"""
		for item in items:
			if item.status is not JobStatus.ERROR:
				continue
			label = self._jobs.pop(item.id, None)
			if label is not None:
				label.setText(f"Не удалось: {item.error}")

	@staticmethod
	def _job_subtitle(item: Any) -> str:
		"""Подпись карточки задания в панели хода работы.

		Показывает исход, а не только ход работы: у задания с ошибкой
		причина обязана быть на карточке — рядом с кнопкой «Повторить».
		"""
		if item.status is JobStatus.ERROR:
			return f"ошибка: {item.error}"
		if item.status is JobStatus.CANCELLED:
			return "отменено"
		if item.status is JobStatus.DONE:
			return "готово"
		if item.note:
			return str(item.note)
		return "идёт обращение к Telegram"


def open_maintenance(worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
	"""Открывает окно обслуживания сообщества."""
	exec_dialog(MaintenanceDialog(worker, community, parent.window()))
