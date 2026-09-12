"""Рабочее окно обслуживания сообщества: чистка служебных записей.

Два шага, как требует [ADR-0026]: «Просмотреть» ничего не меняет
и показывает, сколько чего нашлось; «Удалить выбранное» спрашивает
подтверждение и чистит. Порядок не для удобства — удаление необратимо,
а ошибиться в наборе видов легко.

Ход работы виден панелью очереди (той же, что на «Публикации»
и «Видео»): прогресс, отмена, повтор после ошибки. Отчёт берётся
из завершённого задания и остаётся на экране, пока окно открыто.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
	BodyLabel,
	CaptionLabel,
	CheckBox,
	PushButton,
	SpinBox,
	StrongBodyLabel,
	SubtitleLabel,
)

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.maintenance import (
	DEFAULT_DELETE_LIMIT,
	DEFAULT_DEPTH,
	DEFAULT_KINDS,
	DELETE_LIMIT_RANGE,
	DEPTH_RANGE,
	MaintenanceItemDto,
	ServiceCleanReport,
	ServiceScanReport,
)
from pxcontrol.engine.telegram.types import ServiceMessageKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
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


def scan_summary(report: ServiceScanReport) -> str:
	"""Итог просмотра одной строкой (без разбивки по видам)."""
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


def clean_summary(report: ServiceCleanReport) -> str:
	"""Итог чистки одной строкой."""
	parts = [f"Удалено записей: {report.deleted}"]
	if report.skipped:
		parts.append(f"Telegram не дал удалить: {report.skipped}")
	if report.limited:
		parts.append("сработал предел за проход — повторите, чтобы продолжить")
	return ". ".join(parts) + "."


class MaintenanceDialog(WorkDialog):
	"""Окно обслуживания: просмотр служебных записей и их чистка."""

	def __init__(self, worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
		super().__init__(f"Обслуживание · {community.title}", parent)
		self._worker = worker
		self._community = community
		self._show_error = error_reporter(self)
		self._boxes: dict[ServiceMessageKind, CheckBox] = {}
		self._scan_job: int | None = None
		self._clean_job: int | None = None
		self._build()

	def _build(self) -> None:
		"""Каркас окна: параметры, результат, ход работы."""
		spacing = density.spacing()
		self.content.addWidget(SubtitleLabel("Служебные записи", self))
		self.content.addWidget(
			BodyLabel(
				"Это строки, которые пишет сам Telegram: «такой-то вступил», "
				"«сообщение закреплено», «название изменено». Сначала посмотрим, "
				"сколько их и каких, — удалять будете выбранные виды.",
				self,
			)
		)
		self.content.addLayout(self._depth_row())
		self._results_label = BodyLabel("Просмотр ещё не выполнялся.", self)
		self._results_label.setWordWrap(True)
		self.content.addWidget(self._results_label)
		area, self._results_box = list_area(self, spacing.list_spacing)
		self.content.addWidget(area, stretch=1)
		self.content.addLayout(self._clean_row())
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
		)
		scan = PushButton("Просмотреть", self)
		scan.clicked.connect(self._on_scan)
		self.buttons.addWidget(scan)

	def _depth_row(self) -> QHBoxLayout:
		"""Глубина просмотра: сколько последних сообщений пройти."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Просмотреть последних сообщений:", self))
		self._depth = SpinBox(self)
		self._depth.setRange(*DEPTH_RANGE)
		self._depth.setSingleStep(500)
		self._depth.setValue(DEFAULT_DEPTH)
		self._depth.setToolTip(
			"У Telegram нет поиска «только служебные» — историю приходится "
			"читать целиком, поэтому у прохода есть глубина"
		)
		row.addWidget(self._depth)
		row.addStretch()
		return row

	def _clean_row(self) -> QHBoxLayout:
		"""Потолок удаления и кнопка чистки."""
		row = QHBoxLayout()
		row.addWidget(BodyLabel("Удалять за проход не больше:", self))
		self._limit = SpinBox(self)
		self._limit.setRange(*DELETE_LIMIT_RANGE)
		self._limit.setSingleStep(100)
		self._limit.setValue(DEFAULT_DELETE_LIMIT)
		self._limit.setToolTip(
			"Сотни однотипных действий подряд с пользовательского аккаунта "
			"Telegram не приветствует — предел сдерживает поток"
		)
		row.addWidget(self._limit)
		row.addStretch()
		self._clean_button = PushButton("Удалить выбранное", self)
		self._clean_button.setEnabled(False)
		self._clean_button.clicked.connect(self._on_clean)
		row.addWidget(self._clean_button)
		return row

	# --- просмотр ---------------------------------------------------------------

	def _on_scan(self) -> None:
		"""Ставит задание просмотра (ничего не меняет)."""
		self._clean_button.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.maintenance.scan_service_messages(
				self._community.id, depth=self._depth.value()
			),
			self,
			self._on_scan_queued,
			self._show_error,
		)

	def _on_scan_queued(self, job_id: int) -> None:
		"""Задание просмотра поставлено — ждём его завершения панелью."""
		self._scan_job = job_id
		self._results_label.setText("Просмотр идёт…")
		clear_layout(self._results_box)

	def _on_job_finished(self, item: MaintenanceItemDto, done: bool) -> None:
		"""Панель сообщила об исходе задания — забираем отчёт.

		Панель снимает завершённые с показа, поэтому отчёт берётся
		здесь: иначе он исчез бы вместе с карточкой.
		"""
		if not done:
			return
		if item.id == self._scan_job and item.scan is not None:
			self._show_scan(item.scan)
		elif item.id == self._clean_job and item.clean is not None:
			self._results_label.setText(clean_summary(item.clean))
			# после чистки числа устарели: пусть человек посмотрит заново
			clear_layout(self._results_box)
			self._boxes.clear()
			self._clean_button.setEnabled(False)

	def _show_scan(self, report: ServiceScanReport) -> None:
		"""Показывает найденное: строка на вид, с галочкой у удаляемых."""
		self._results_label.setText(scan_summary(report))
		clear_layout(self._results_box)
		self._boxes.clear()
		for kind, count in sorted(report.found.items(), key=lambda pair: -pair[1]):
			self._results_box.addWidget(self._kind_row(kind, count))
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

	# --- чистка -----------------------------------------------------------------

	def _on_clean(self) -> None:
		"""Спрашивает подтверждение и ставит задание чистки."""
		chosen = [kind for kind, box in self._boxes.items() if box.isChecked()]
		if not chosen:
			self._show_error("Отметьте хотя бы один вид записей.")
			return
		names = "\n".join(f"— {kind_title(kind)};" for kind in chosen)
		if not confirm_delete(
			self,
			f"Удалить служебные записи в «{self._community.title}»?\n\n{names}\n\n"
			f"Удаление необратимо. За проход будет удалено не больше "
			f"{self._limit.value()} записей.",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.maintenance.clean_service_messages(
				self._community.id,
				chosen,
				depth=self._depth.value(),
				delete_limit=self._limit.value(),
			),
			self,
			self._on_clean_queued,
			self._show_error,
		)

	def _on_clean_queued(self, job_id: int) -> None:
		"""Задание чистки поставлено."""
		self._clean_job = job_id
		self._clean_button.setEnabled(False)
		self._results_label.setText("Чистка идёт…")

	@staticmethod
	def _job_subtitle(item: Any) -> str:
		"""Подпись карточки задания в панели хода работы."""
		if item.note:
			return str(item.note)
		return "просмотр истории" if item.scan is None and item.clean is None else "готово"


def open_maintenance(worker: EngineWorker, community: CommunityDto, parent: QWidget) -> None:
	"""Открывает окно обслуживания сообщества."""
	exec_dialog(MaintenanceDialog(worker, community, parent.window()))


__all__ = ["MaintenanceDialog", "clean_summary", "kind_title", "open_maintenance", "scan_summary"]
