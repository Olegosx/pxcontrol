"""Очередь обработки видео: последовательно, с прогрессом и отменой.

Единый конвейер подготовки видео (ADR-0014): и одиночная, и пакетная
обработка ставят элементы в одну очередь — два ffmpeg не дерутся
за процессор, а логика выполнения (авто-битрейт, отмена, повтор)
не двоится. Очередь живёт в памяти цикла событий движка, как очередь
отправки постов (ADR-0012): персистентности нет сознательно —
готовые файлы уже на диске (результат пишется атомарно), остальное
после перезапуска ставится заново.

Все методы выполняются в цикле движка (вызовы — через мост интерфейса),
поэтому состояние не требует блокировок. Отмена работающей обработки —
кооперативная: колбэк прогресса бросает :class:`ProcessingCancelled`,
и ``run_streaming`` убивает процесс ffmpeg (контракт закреплён тестом
модуля видео). Фазы без прогресса (ffprobe, кадр заставки, вшивание
обложки) не прерываются — отмена подхватится на первой строке прогресса.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from pxcontrol.engine.errors import user_message
from pxcontrol.engine.services.video import (
	IntroSourceKind,
	PresetFields,
	VideoError,
	VideoService,
	parse_intro_source,
)

logger = logging.getLogger(__name__)

#: Сколько ждать завершения текущей обработки при остановке движка (сек):
#: ffmpeg гаснет на первой строке прогресса, страховка — на фазы без него.
_SHUTDOWN_TIMEOUT = 30.0


class ProcessingCancelled(Exception):  # noqa: N818 — служебный сигнал, не ошибка
	"""Служебный сигнал отмены обработки.

	Бросается колбэком прогресса и сознательно не наследует ни
	``EngineError`` (это не ошибка для пользователя), ни ``RuntimeError``/
	``ValueError`` (иначе :meth:`VideoService.prepare` завернул бы его
	в ``VideoError`` и очередь не отличила бы отмену от сбоя).
	"""


class VideoItemStatus(StrEnum):
	"""Статус элемента очереди обработки."""

	PENDING = "pending"  # ждёт своей очереди
	PROCESSING = "processing"  # кодируется ffmpeg
	DONE = "done"  # результат готов
	ERROR = "error"  # обработка не удалась (текст — в error)
	CANCELLED = "cancelled"  # отменён пользователем

	def finished(self) -> bool:
		"""Завершён ли элемент (в любом исходе)."""
		return self in (self.DONE, self.ERROR, self.CANCELLED)


@dataclass(frozen=True)
class ProcessingRequest:
	"""Заявка на обработку одного файла (постановка в очередь).

	Attributes:
		source_path: путь к исходнику.
		intro_source: персональный источник кадра заставки («image:путь»
			после выбора кадра пользователем); None — как в параметрах.
	"""

	source_path: str
	intro_source: str | None = None


@dataclass(frozen=True)
class VideoItemDto:
	"""Элемент очереди обработки для интерфейса.

	Attributes:
		id: идентификатор элемента (для отмены, повтора и снятия с показа).
		title: имя исходного файла.
		batch: подпапка пакета («» — одиночная обработка).
		status: текущий статус.
		progress: доля кодирования 0.0..1.0 (для обрабатывающегося).
		error: текст ошибки (для статуса ERROR).
		note: пометка выполнения (например, автоснижение битрейта).
		output_path: путь к результату (для статуса DONE).
	"""

	id: int
	title: str
	batch: str
	status: VideoItemStatus
	progress: float
	error: str | None
	note: str | None
	output_path: str | None


class _Item:
	"""Внутреннее состояние элемента очереди (изменяемое)."""

	def __init__(
		self,
		item_id: int,
		request: ProcessingRequest,
		fields: PresetFields,
		batch_subdir: str,
	) -> None:
		self.id = item_id
		self.request = request
		self.fields = fields
		self.batch_subdir = batch_subdir
		self.status = VideoItemStatus.PENDING
		self.progress = 0.0
		self.error: str | None = None
		self.note: str | None = None
		self.output_path: str | None = None
		self.cancel_requested = False

	def dto(self) -> VideoItemDto:
		"""Снимок элемента для интерфейса."""
		return VideoItemDto(
			id=self.id,
			title=Path(self.request.source_path).name,
			batch=self.batch_subdir,
			status=self.status,
			progress=self.progress,
			error=self.error,
			note=self.note,
			output_path=self.output_path,
		)


class ProcessingQueue:
	"""Последовательная обработка видео с прогрессом, отменой и повтором.

	Пока один файл кодируется, новые свободно встают в хвост; ошибка
	или отмена одного элемента не трогает остальные (кодировщик x264
	сам загружает все ядра — параллельные ffmpeg ничего не ускорили бы).
	Элемент с ошибкой можно вернуть в очередь (:meth:`retry`).
	"""

	def __init__(self, video: VideoService) -> None:
		self._video = video
		self._items: list[_Item] = []
		self._next_id = 1
		self._worker: asyncio.Task[None] | None = None
		self._closing = False  # остановка движка: гасит активный ffmpeg
		self._frames_dir: str | None = None  # выбранные кадры заставки пакета
		self._next_frame = 1

	async def enqueue(
		self,
		request: ProcessingRequest,
		fields: PresetFields,
		batch_subdir: str = "",
	) -> int:
		"""Ставит один файл в очередь; проверки — сразу.

		Returns:
			Идентификатор элемента очереди.

		Raises:
			VideoError: Файл или ffmpeg не найдены.
		"""
		return (await self.enqueue_many([request], fields, batch_subdir))[0]

	async def enqueue_many(
		self,
		requests: list[ProcessingRequest],
		fields: PresetFields,
		batch_subdir: str = "",
	) -> list[int]:
		"""Ставит пакет файлов в очередь; проверки — до постановки.

		Постановка атомарна: сначала проверяются все файлы, потом
		добавляются все элементы — битый путь в середине списка
		не оставляет пакет поставленным наполовину.

		Returns:
			Идентификаторы элементов в порядке заявок.

		Raises:
			VideoError: Список пуст, файл или ffmpeg не найдены.
		"""
		if not requests:
			raise VideoError("Список файлов пуст — обрабатывать нечего.")
		for request in requests:
			self._video.ensure_ready(request.source_path)
		ids: list[int] = []
		for request in requests:
			item = _Item(self._next_id, request, fields, batch_subdir)
			self._next_id += 1
			self._items.append(item)
			ids.append(item.id)
		self._ensure_worker()
		logger.info(
			"Обработка: %s файл(ов) в очереди (пакет «%s», параметры «%s»).",
			len(requests),
			batch_subdir or "—",
			fields.name,
		)
		return ids

	async def stash_frame(self, path: str) -> str:
		"""Сохраняет выбранный кадр заставки в папку очереди; возвращает копию.

		Партией кадров-кандидатов владеет сервис видео, и следующий вызов
		``extract_random_frames`` (следующий файл пакета) её удаляет —
		выбранный кадр без копии исчез бы. Копия живёт до снятия элемента
		с показа или остановки движка.

		Raises:
			VideoError: Кадр скопировать не удалось.
		"""
		if self._frames_dir is None:
			self._frames_dir = tempfile.mkdtemp(prefix="pxcontrol-queue-frames-")
		target = Path(self._frames_dir) / f"frame_{self._next_frame:04d}.png"
		self._next_frame += 1
		try:
			await asyncio.to_thread(shutil.copyfile, path, str(target))
		except OSError as exc:
			raise VideoError(f"Не удалось сохранить выбранный кадр: {exc.strerror or exc}") from exc
		return str(target)

	async def cancel(self, item_id: int) -> None:
		"""Отменяет элемент: ожидающий убирается, кодирующийся обрывается.

		Активный ffmpeg гаснет кооперативно — на ближайшей строке
		прогресса (см. докстринг модуля).
		"""
		for item in self._items:
			if item.id != item_id:
				continue
			if item.status is VideoItemStatus.PENDING:
				item.status = VideoItemStatus.CANCELLED
				self._drop_stashed_frame(item)
				logger.info("Элемент обработки id=%s отменён (ждал).", item_id)
			elif item.status is VideoItemStatus.PROCESSING:
				item.cancel_requested = True
			return

	async def retry(self, item_id: int) -> None:
		"""Возвращает элемент с ошибкой в очередь на новую попытку.

		Файл перепроверяется, как при постановке (мог исчезнуть).
		Элементы в других статусах не трогаются.

		Raises:
			VideoError: Файл больше не годен — элемент остаётся в ошибке.
		"""
		for item in self._items:
			if item.id != item_id or item.status is not VideoItemStatus.ERROR:
				continue
			self._video.ensure_ready(item.request.source_path)
			item.status = VideoItemStatus.PENDING
			item.progress = 0.0
			item.error = None
			item.note = None
			# флаг мог взвестись, если отмена совпала с ошибкой прошлой
			# попытки: не сбросить — новая попытка тут же отменилась бы
			item.cancel_requested = False
			self._ensure_worker()
			logger.info("Элемент обработки id=%s возвращён в очередь на повтор.", item_id)
			return

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из списка (живые не трогаются)."""
		for item in self._items:
			if item.id == item_id and item.status.finished():
				self._drop_stashed_frame(item)
		self._items = [
			item for item in self._items if not (item.id == item_id and item.status.finished())
		]

	async def state(self) -> list[VideoItemDto]:
		"""Снимок очереди для интерфейса (в порядке постановки)."""
		return [item.dto() for item in self._items]

	async def has_unfinished(self) -> bool:
		"""Есть ли элементы, которые ещё ждут или кодируются."""
		return any(not item.status.finished() for item in self._items)

	async def shutdown(self) -> None:
		"""Останавливает очередь при остановке движка.

		Ожидающие элементы помечаются отменёнными, активному ffmpeg
		взводится флаг отмены (погаснет на первой строке прогресса);
		воркер дожидается с таймаутом — страховка на фазы без прогресса.
		"""
		self._closing = True
		for item in self._items:
			if item.status is VideoItemStatus.PENDING:
				item.status = VideoItemStatus.CANCELLED
		if self._worker is not None:
			with suppress(TimeoutError, asyncio.CancelledError):
				await asyncio.wait_for(self._worker, timeout=_SHUTDOWN_TIMEOUT)
			self._worker = None
		if self._frames_dir is not None:
			shutil.rmtree(self._frames_dir, ignore_errors=True)
			self._frames_dir = None

	# --- внутреннее ---------------------------------------------------------

	def _ensure_worker(self) -> None:
		"""Запускает фоновую задачу обработки, если она не крутится."""
		if self._worker is None or self._worker.done():
			self._worker = asyncio.create_task(self._run())

	async def _run(self) -> None:
		"""Обрабатывает элементы по одному, пока очередь не опустеет."""
		while (item := self._next_pending()) is not None:
			await self._process(item)

	def _next_pending(self) -> _Item | None:
		"""Первый ожидающий элемент (или None — очередь пуста)."""
		for item in self._items:
			if item.status is VideoItemStatus.PENDING:
				return item
		return None

	async def _process(self, item: _Item) -> None:
		"""Обрабатывает один элемент; исход пишется в его статус."""

		def _on_progress(fraction: float) -> None:
			# вызывается из рабочего потока ffmpeg: исключение убивает
			# процесс (контракт run_streaming) — так работает отмена
			if item.cancel_requested or self._closing:
				raise ProcessingCancelled
			item.progress = fraction

		item.status = VideoItemStatus.PROCESSING
		try:
			fields = await self._fit_bitrate(item)
			item.output_path = await self._video.prepare(
				item.request.source_path,
				fields,
				intro_source=item.request.intro_source,
				on_progress=_on_progress,
				extra_subdir=item.batch_subdir,
			)
		except ProcessingCancelled:
			item.status = VideoItemStatus.CANCELLED
			self._drop_stashed_frame(item)
			logger.info("Обработка id=%s отменена.", item.id)
		except asyncio.CancelledError:
			# отменили сам воркер (остановка цикла): очередь не продолжается
			raise
		except Exception as exc:  # noqa: BLE001 — исход элемента, не очереди
			item.status = VideoItemStatus.ERROR
			# карточка очереди показывает этот текст как есть — сворачиваем
			# недоменные исключения, как мост интерфейса (контракт errors.py)
			item.error = user_message(exc)
			logger.exception("Обработка id=%s не удалась.", item.id)
		else:
			item.status = VideoItemStatus.DONE
			item.progress = 1.0
			self._drop_stashed_frame(item)

	async def _fit_bitrate(self, item: _Item) -> PresetFields:
		"""Вписывает исходник больше лимита Telegram в лимит (ADR-0014).

		Пакет спросить пользователя не может, поэтому рекомендация
		битрейта («лимит минус 1 %») подставляется автоматически —
		но только если параметры не задают битрейт ещё ниже (вручную
		заниженное качество не повышаем). Пометка — в карточке элемента.

		Raises:
			VideoError: Видео не влезает в лимит даже минимальным битрейтом.
		"""
		fields = item.fields
		advice = await self._video.bitrate_advice(
			item.request.source_path, fields.trim_start, fields.trim_end
		)
		if advice is None:
			return fields
		if fields.video_bitrate_kbps is not None and fields.video_bitrate_kbps <= advice.kbps:
			return fields
		item.note = (
			f"битрейт снижен до {advice.mbps:g} Мбит/с — итог впишется "
			f"в лимит Telegram {advice.limit_gb} ГБ"
		)
		logger.info("Обработка id=%s: %s.", item.id, item.note)
		return replace(fields, video_bitrate_kbps=advice.kbps)

	def _drop_stashed_frame(self, item: _Item) -> None:
		"""Удаляет копию выбранного кадра, если ею владеет очередь.

		Кадры вне папки очереди (свой PNG из пресета, «image:» руками)
		не трогаются. Элемент с ошибкой кадр сохраняет — он нужен повтору;
		копия удаляется при снятии элемента с показа.
		"""
		if self._frames_dir is None or item.request.intro_source is None:
			return
		kind, value = parse_intro_source(item.request.intro_source)
		if kind is not IntroSourceKind.IMAGE or not value:
			return
		path = Path(value)
		if Path(self._frames_dir) in path.parents:
			path.unlink(missing_ok=True)
