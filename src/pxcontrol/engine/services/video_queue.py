"""Очередь обработки видео: последовательно, с прогрессом и отменой.

Единый конвейер подготовки видео (ADR-0014): и одиночная, и пакетная
обработка ставят элементы в одну очередь — два ffmpeg не дерутся
за процессор, а логика выполнения (авто-битрейт, отмена, повтор)
не двоится. Жизненный цикл элементов держит общий каркас заданий
(:mod:`pxcontrol.engine.jobs`, ADR-0025); здесь остаётся предметное:
проверки при постановке, авто-битрейт под лимит Telegram, копии
выбранных кадров заставки и уборка за ними.

Персистентности нет сознательно (ADR-0014): готовые файлы уже на диске
(результат пишется атомарно), остальное после перезапуска ставится
заново — поэтому при остановке движка ожидающие элементы честно
помечаются отменёнными.

Все методы выполняются в цикле движка (вызовы — через мост интерфейса),
поэтому состояние не требует блокировок. Единственное исключение —
колбэк прогресса ffmpeg: он приходит из рабочего потока, но лишь
присваивает ``job.progress`` и читает ``cancel_requested`` — атомарные
операции над простыми полями, безопасные под GIL. Отмена работающей
обработки кооперативная: колбэк прогресса бросает :class:`JobCancelled`,
и ``run_streaming`` убивает процесс ffmpeg (контракт закреплён тестом
модуля видео). Отмена задачи вместо этого не годится — посторонний
процесс от неё не остановится, поэтому каркас по запросу отмены лишь
взводит флаг, а гасит работу сам исполнитель. Фазы без прогресса (ffprobe, кадр заставки, вшивание
обложки) не прерываются — отмена подхватится на первой строке прогресса.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from pxcontrol.engine.jobs import Job, JobCancelled, JobQueue, JobStatus
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


@dataclass(frozen=True)
class ProcessingRequest:
	"""Заявка на обработку одного файла (постановка в очередь).

	Заявка самодостаточна: несёт свои параметры обработки — у каждого
	файла списка на странице «Видео» они свои (дополнение к ADR-0014).

	Attributes:
		source_path: путь к исходнику.
		fields: параметры обработки этого файла.
		intro_source: персональный источник кадра заставки («image:путь»
			после выбора кадра пользователем); None — как в параметрах.
		batch_subdir: подпапка пакета в результатах («» — без неё).
	"""

	source_path: str
	fields: PresetFields
	intro_source: str | None = None
	batch_subdir: str = ""


@dataclass(frozen=True)
class VideoItemDto:
	"""Элемент очереди обработки для интерфейса.

	Attributes:
		id: идентификатор элемента (для отмены, повтора и снятия с показа).
		title: имя исходного файла.
		batch: подпапка пакета («» — одиночная обработка).
		status: текущий статус (общий для очередей движка, ADR-0025).
		progress: доля кодирования 0.0..1.0 (для обрабатывающегося).
		error: текст ошибки (для статуса ERROR).
		note: пометка выполнения (например, автоснижение битрейта).
		output_path: путь к результату (для статуса DONE).
	"""

	id: int
	title: str
	batch: str
	status: JobStatus
	progress: float
	error: str | None
	note: str | None
	output_path: str | None


class _VideoJob(Job):
	"""Задание обработки: заявка и путь готового файла."""

	def __init__(self, job_id: int, request: ProcessingRequest) -> None:
		super().__init__(job_id)
		self.request = request
		self.output_path: str | None = None

	def dto(self) -> VideoItemDto:
		"""Снимок задания для интерфейса."""
		return VideoItemDto(
			id=self.id,
			title=Path(self.request.source_path).name,
			batch=self.request.batch_subdir,
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
		self._jobs: JobQueue[_VideoJob] = JobQueue(
			self._process,
			name="Обработка",
			# очередь не переживает перезапуск (ADR-0014) — честно сказать
			# об этом ожидающим элементам при остановке движка
			cancel_pending_on_shutdown=True,
			shutdown_timeout_s=_SHUTDOWN_TIMEOUT,
		)
		self._frames_dir: str | None = None  # выбранные кадры заставки пакета
		self._next_frame = 1

	async def enqueue(self, request: ProcessingRequest) -> int:
		"""Ставит один файл в очередь; проверки — сразу.

		Returns:
			Идентификатор элемента очереди.

		Raises:
			VideoError: Файл или ffmpeg не найдены.
		"""
		return (await self.enqueue_many([request]))[0]

	async def enqueue_many(self, requests: list[ProcessingRequest]) -> list[int]:
		"""Ставит пакет файлов в очередь; проверки — до постановки.

		Постановка атомарна: сначала проверяются все файлы, потом
		добавляются все элементы — битый путь в середине списка
		не оставляет пакет поставленным наполовину. Параметры обработки
		у каждой заявки свои (:class:`ProcessingRequest`).

		Returns:
			Идентификаторы элементов в порядке заявок.

		Raises:
			VideoError: Список пуст, файл или ffmpeg не найдены.
		"""
		if self._jobs.stopping:
			# постановка в окно shutdown: элемент всё равно не начался бы,
			# а после перезапуска очередь пуста (ADR-0014)
			raise VideoError("Движок останавливается — постановка отклонена.")
		if not requests:
			raise VideoError("Список файлов пуст — обрабатывать нечего.")
		await self._video.ensure_ready([request.source_path for request in requests])
		ids: list[int] = []
		for request in requests:
			job = _VideoJob(self._jobs.new_id(), request)
			self._jobs.add(job)
			ids.append(job.id)
			logger.info(
				"Обработка: «%s» в очереди (id=%s, параметры «%s», пакет «%s»).",
				Path(request.source_path).name,
				job.id,
				request.fields.name,
				request.batch_subdir or "—",
			)
		self._jobs.ensure_worker()
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
			# mkdtemp — диск: вне цикла событий движка
			self._frames_dir = await asyncio.to_thread(
				tempfile.mkdtemp, prefix="pxcontrol-queue-frames-"
			)
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
		job = self._jobs.get(item_id)
		if job is None:
			return
		if job.status is JobStatus.PENDING:
			job.status = JobStatus.CANCELLED
			await self._drop_stashed_frame(job)
			logger.info("Элемент обработки id=%s отменён (ждал).", item_id)
		elif job.status is JobStatus.RUNNING:
			self._jobs.request_cancel(job)

	async def retry(self, item_id: int) -> None:
		"""Возвращает элемент с ошибкой в очередь на новую попытку.

		Файл перепроверяется, как при постановке (мог исчезнуть).
		Элементы в других статусах не трогаются.

		Raises:
			VideoError: Файл больше не годен — элемент остаётся в ошибке.
		"""
		if self._jobs.stopping:
			return  # движок останавливается — повтор не начнётся
		job = self._jobs.get(item_id)
		if job is None or job.status is not JobStatus.ERROR:
			return
		await self._video.ensure_ready([job.request.source_path])
		self._jobs.reset_for_retry(job)
		logger.info("Элемент обработки id=%s возвращён в очередь на повтор.", item_id)

	async def dismiss(self, item_id: int) -> None:
		"""Убирает завершённый элемент из списка (живые не трогаются)."""
		job = self._jobs.get(item_id)
		if job is None or not job.status.finished():
			return
		await self._drop_stashed_frame(job)
		self._jobs.remove(job)

	async def state(self) -> list[VideoItemDto]:
		"""Снимок очереди для интерфейса (в порядке постановки)."""
		return [job.dto() for job in self._jobs.all()]

	async def shutdown(self) -> None:
		"""Останавливает очередь при остановке движка.

		Ожидающие элементы помечаются отменёнными, активному ffmpeg
		взводится флаг отмены (колбэк прогресса вызывается не реже раза
		в секунду даже при молчании ffmpeg — см. ``run_streaming``);
		каркас дожидается воркера с таймаутом — страховка на шаги
		без колбэка. Копии выбранных кадров удаляются последними:
		активное задание могло читать свою до последней секунды.
		"""
		await self._jobs.shutdown()
		if self._frames_dir is not None:
			await asyncio.to_thread(shutil.rmtree, self._frames_dir, ignore_errors=True)
			self._frames_dir = None

	# --- выполнение ---------------------------------------------------------

	async def _process(self, job: _VideoJob) -> None:
		"""Готовит один файл; исход записывает каркас заданий.

		Raises:
			JobCancelled: Отмену запросил человек или остановка движка.
			VideoError: Обработка не удалась (текст — на карточку).
		"""

		def _on_progress(fraction: float) -> None:
			# вызывается из рабочего потока ffmpeg: исключение убивает
			# процесс (контракт run_streaming) — так работает отмена
			if job.cancel_requested or self._jobs.stopping:
				raise JobCancelled
			job.progress = fraction

		try:
			fields = await self._fit_bitrate(job)
			job.output_path = await self._video.prepare(
				job.request.source_path,
				fields,
				intro_source=job.request.intro_source,
				on_progress=_on_progress,
				extra_subdir=job.request.batch_subdir,
			)
		except JobCancelled:
			# кадр отменённого элемента больше не нужен: повтора не будет
			await self._drop_stashed_frame(job)
			raise
		# успех: копия выбранного кадра сделала своё дело (у элемента
		# с ошибкой она остаётся — её ждёт повтор)
		await self._drop_stashed_frame(job)

	async def _fit_bitrate(self, job: _VideoJob) -> PresetFields:
		"""Вписывает исходник больше лимита Telegram в лимит (ADR-0014).

		Пакет спросить пользователя не может, поэтому рекомендация
		битрейта («лимит минус 1 %») подставляется автоматически —
		но только если параметры не задают битрейт ещё ниже (вручную
		заниженное качество не повышаем). Пометка — в карточке элемента.

		Raises:
			VideoError: Видео не влезает в лимит даже минимальным битрейтом.
		"""
		fields = job.request.fields
		advice = await self._video.bitrate_advice(
			job.request.source_path, fields.trim_start, fields.trim_end
		)
		if advice is None:
			return fields
		if fields.video_bitrate_kbps is not None and fields.video_bitrate_kbps <= advice.kbps:
			return fields
		job.note = (
			f"битрейт снижен до {advice.mbps:g} Мбит/с — итог впишется "
			f"в лимит Telegram {advice.limit_gb} ГБ"
		)
		logger.info("Обработка id=%s: %s.", job.id, job.note)
		return replace(fields, video_bitrate_kbps=advice.kbps)

	async def _drop_stashed_frame(self, job: _VideoJob) -> None:
		"""Удаляет копию выбранного кадра, если ею владеет очередь.

		Кадры вне папки очереди (свой PNG из пресета, «image:» руками)
		не трогаются. Элемент с ошибкой кадр сохраняет — он нужен повтору;
		копия удаляется при снятии элемента с показа.
		"""
		if self._frames_dir is None or job.request.intro_source is None:
			return
		kind, value = parse_intro_source(job.request.intro_source)
		if kind is not IntroSourceKind.IMAGE or not value:
			return
		path = Path(value)
		if Path(self._frames_dir) in path.parents:
			# unlink — диск: вне цикла событий движка
			await asyncio.to_thread(path.unlink, missing_ok=True)
