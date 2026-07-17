"""Оркестрация обработки: сборка и запуск ffmpeg (порт из makeVideo).

Поведение — по SPEC.md референса makeVideo: FullHD-вписывание, вотермарк
с окном показа (отсчёт от исходного видео), заставка hold+xfade для
статичного превью, опциональная обложка attached_pic.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pxcontrol.engine.video.constants import (
	AUDIO_BITRATE,
	AUDIO_CODEC,
	ENCODE_PRESET,
	FALLBACK_CRF,
	TARGET_PIX_FMT,
	VIDEO_CODEC,
	fitted_size,
)
from pxcontrol.engine.video.ffmpeg import ProgressCallback, run_streaming, run_tool
from pxcontrol.engine.video.filtergraph import WatermarkOptions, build_filter_complex
from pxcontrol.engine.video.frames import extract_still, prepare_still
from pxcontrol.engine.video.probe import VideoInfo, probe_video, trimmed_info

logger = logging.getLogger(__name__)

# Запас длительности входа-заставки сверх hold+xfade: ffmpeg обрезает
# зацикленную картинку по -t, и без запаса последний кадр перехода
# мог не попасть в поток.
_STILL_INPUT_MARGIN = 0.1

__all__ = ["ProcessingOptions", "ProgressCallback", "process"]


@dataclass(frozen=True)
class ProcessingOptions:
	"""Параметры обработки одного видео.

	Все поля пресета обязательны: их значения по умолчанию живут в одном
	месте — ``PresetFields`` (сервис видео), а здесь — только контракт
	исполнения. ``video_bitrate_kbps``: целевой битрейт видео в кбит/с;
	None — «как в оригинале» (битрейт исходника, а если он неизвестен —
	CRF 20).
	"""

	input: str
	output: str
	# обрезка с краёв (сек; 0 — не резать): остальные параметры — окно
	# вотермарка, кадр заставки, длительности — считаются от обрезанной версии
	trim_start: float
	trim_end: float
	# затухание на краях итога (сек; 0 — без эффекта): появление из чёрного /
	# уход в чёрное, видео и звук вместе; к обрезке не привязано
	fade_in: float
	fade_out: float
	video_bitrate_kbps: int | None
	watermark: str | None
	wm_corner: str
	wm_margin: int
	wm_opacity: float
	wm_scale: float
	# окно показа вотермарка: отступ от начала и отступ ДО КОНЦА ролика (сек)
	wm_start_offset: float | None
	wm_end_offset: float | None
	# плавность появления/исчезания на краях окна (сек; 0 — резко)
	wm_fade: float
	intro: bool
	intro_source: str
	intro_hold: float
	xfade: float
	cover: bool
	no_audio: bool
	# комментарий в метаданные контейнера (тег comment); None — не писать
	meta_comment: str | None
	ffmpeg_bin: str = "ffmpeg"
	ffprobe_bin: str = "ffprobe"


def _fps_arg(fps: float) -> str:
	"""Форматирует кадровую частоту строкой для аргументов ffmpeg."""
	return f"{fps:.5f}"


def _watermark_options(opts: ProcessingOptions, info: VideoInfo) -> WatermarkOptions:
	"""Собирает параметры вотермарка; отступы от краёв → абсолютное окно.

	Начало окна = отступ от начала; конец = длительность − отступ до конца.
	Нулевой отступ означает «без ограничения» (как и незаданный). Граф
	фильтров работает с абсолютными моментами, как и раньше.

	Raises:
		ValueError: Окно показа пустое (отступы не помещаются в ролик).
	"""
	start = opts.wm_start_offset if opts.wm_start_offset else None
	end = info.duration - opts.wm_end_offset if opts.wm_end_offset else None
	if opts.watermark and (start is not None or end is not None):
		window = (end if end is not None else info.duration) - (start or 0.0)
		if window <= 0:
			raise ValueError(
				"Окно показа вотермарка пустое: отступы "
				"не помещаются в длительность ролика."
			)
		fade_span = opts.wm_fade * (
			(start is not None) + (end is not None)
		)
		if fade_span > window:
			raise ValueError(
				"Плавность переходов не помещается в окно показа вотермарка."
			)
	return WatermarkOptions(
		corner=opts.wm_corner,
		margin=opts.wm_margin,
		opacity=opts.wm_opacity,
		scale=opts.wm_scale,
		start=start,
		end=end,
		fade=opts.wm_fade,
	)


def _build_inputs(
	opts: ProcessingOptions, info: VideoInfo, still_path: str | None
) -> tuple[list[str], int | None, int | None]:
	"""Готовит список входов ffmpeg и индексы вотермарка и заставки.

	Обрезка — входными аргументами: ``-ss`` пропускает начало, ``-t``
	ограничивает длительность (``info`` — уже обрезанная версия).
	Временные метки при этом обнуляются на точке среза, поэтому весь
	граф фильтров живёт во времени обрезанной версии.

	Returns:
		Кортеж (аргументы входов, индекс вотермарка, индекс заставки).
		Индекс равен None, если соответствующий вход не нужен.
	"""
	inputs: list[str] = []
	if opts.trim_start:
		inputs += ["-ss", f"{opts.trim_start:.3f}"]
	if opts.trim_start or opts.trim_end:
		inputs += ["-t", f"{info.duration:.3f}"]
	inputs += ["-i", opts.input]
	index = 1
	wm_index: int | None = None
	still_index: int | None = None
	if opts.watermark:
		inputs += ["-i", opts.watermark]
		wm_index, index = index, index + 1
	if opts.intro:
		duration = opts.intro_hold + opts.xfade + _STILL_INPUT_MARGIN
		inputs += [
			"-loop", "1", "-framerate", _fps_arg(info.fps),
			"-t", f"{duration:.3f}", "-i", str(still_path),
		]
		still_index, index = index, index + 1
	return inputs, wm_index, still_index


def _video_quality_args(opts: ProcessingOptions, info: VideoInfo) -> list[str]:
	"""Аргументы качества видео: заданный битрейт, битрейт исходника или CRF.

	Приоритет: явный битрейт пресета → битрейт исходника («как в оригинале»,
	режим по умолчанию) → CRF, если ffprobe битрейт не сообщил.
	"""
	kbps = opts.video_bitrate_kbps or info.bitrate_kbps
	if kbps:
		return ["-b:v", f"{kbps}k"]
	return ["-crf", FALLBACK_CRF]


def _assemble_command(
	ffmpeg_bin: str, inputs: list[str], filter_complex: str, video_label: str,
	audio_label: str | None, quality: list[str], meta_comment: str | None,
	output: str,
) -> list[str]:
	"""Собирает полную команду ffmpeg для основной обработки."""
	cmd = [ffmpeg_bin, "-y", *inputs, "-filter_complex", filter_complex, "-map", video_label]
	if audio_label:
		cmd += ["-map", audio_label]
	cmd += ["-c:v", VIDEO_CODEC, "-preset", ENCODE_PRESET, *quality, "-pix_fmt", TARGET_PIX_FMT]
	if audio_label:
		cmd += ["-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE]
	if meta_comment:
		cmd += ["-metadata", f"comment={meta_comment}"]
	# -progress pipe:1 — ffmpeg пишет ход кодирования в stdout (для прогресс-бара)
	cmd += ["-progress", "pipe:1", "-nostats", "-movflags", "+faststart", output]
	return cmd


def _run_main(
	opts: ProcessingOptions,
	info: VideoInfo,
	still_path: str | None,
	output: str,
	on_progress: ProgressCallback | None,
) -> None:
	"""Запускает основную обработку: FullHD, вотермарк, заставка, кодирование."""
	inputs, wm_index, still_index = _build_inputs(opts, info, still_path)
	has_audio = info.has_audio and not opts.no_audio
	width, height = fitted_size(info.width, info.height)
	# длительность итога = исходник + удержание кадра заставки (см. SPEC)
	total = info.duration + (opts.intro_hold if opts.intro else 0.0)
	if opts.fade_in + opts.fade_out > total:
		raise ValueError(
			"Затухания в начале и в конце не помещаются "
			"в длительность ролика."
		)
	graph = build_filter_complex(
		fps=_fps_arg(info.fps), width=width, height=height, duration=total,
		hold=opts.intro_hold, xfade=opts.xfade, still_index=still_index,
		wm=_watermark_options(opts, info) if opts.watermark else None,
		wm_index=wm_index, has_audio=has_audio,
		fade_in=opts.fade_in, fade_out=opts.fade_out,
	)
	cmd = _assemble_command(
		opts.ffmpeg_bin, inputs, graph.filter_complex, graph.video_label,
		graph.audio_label, _video_quality_args(opts, info),
		opts.meta_comment, output,
	)
	run_streaming(cmd, "обработка видео", total, on_progress)


def _attach_cover(
	ffmpeg_bin: str, video_path: str, cover_path: str, output: str
) -> None:
	"""Вшивает картинку как обложку mp4 (attached_pic) без перекодирования."""
	cmd = [
		ffmpeg_bin, "-y", "-i", video_path, "-i", cover_path,
		"-map", "0", "-map", "1", "-c", "copy", "-c:v:1", "png",
		"-disposition:v:1", "attached_pic",
		# +faststart — moov в начало файла (главный проход его уже ставил,
		# но ремукс с обложкой без флага увёл бы индекс в хвост)
		"-movflags", "+faststart", output,
	]
	run_tool(cmd, "вшивание обложки")


def _save_preview(
	opts: ProcessingOptions, info: VideoInfo, still_path: str | None
) -> None:
	"""Сохраняет кадр-превью рядом с результатом (тот же стем, .png).

	Кадр заставки/обложки, если готовился (он и задуман «лицом» ролика),
	иначе — первый кадр результата. Превью — вспомогательный артефакт:
	его отказ не роняет успешную обработку, только предупреждение в лог.
	Слой публикации режет из него миниатюру видео.
	"""
	preview = str(Path(opts.output).with_suffix(".png"))
	try:
		if still_path is not None:
			shutil.copyfile(still_path, preview)
		else:
			width, height = fitted_size(info.width, info.height)
			extract_still(opts.output, 0.0, preview, width, height, opts.ffmpeg_bin)
	except (OSError, RuntimeError):
		logger.warning("Не удалось сохранить превью %s.", preview, exc_info=True)
	else:
		logger.info("Превью сохранено: %s", preview)


def process(
	opts: ProcessingOptions, on_progress: ProgressCallback | None = None
) -> None:
	"""Обрабатывает одно видео по заданным параметрам (блокирующе).

	Вызывающая сторона отвечает за вынос в поток/executor — модуль
	сознательно синхронный, как и его тесты. Рядом с результатом
	сохраняется кадр-превью (см. :func:`_save_preview`).

	Args:
		opts: параметры обработки.
		on_progress: необязательный колбэк хода кодирования (0.0..1.0).

	Raises:
		RuntimeError: Если ffprobe/ffmpeg завершились с ошибкой.
		ValueError: Файл вотермарка/картинки заставки не найден, режим
			источника кадра не распознан, обрезка съедает всё видео,
			окно вотермарка или затухания не помещаются.
	"""
	# пути из параметров проверяются до запуска ffmpeg: несуществующий
	# файл дал бы вместо точной причины многострочный журнал ffmpeg
	if opts.watermark and not Path(opts.watermark).is_file():
		raise ValueError(
			f"Файл вотермарка не найден: {opts.watermark} — проверьте "
			"путь в разделе «Вотермарк»."
		)
	info = probe_video(opts.input, opts.ffprobe_bin)
	# все дальнейшие расчёты — от рабочей (обрезанной) версии
	work_info = trimmed_info(info, opts.trim_start, opts.trim_end)
	with tempfile.TemporaryDirectory() as tmp:
		still_path: str | None = None
		if opts.intro or opts.cover:
			still_path = os.path.join(tmp, "still.png")
			prepare_still(
				opts.input, opts.intro_source, work_info, still_path,
				opts.ffmpeg_bin, start_offset=opts.trim_start,
			)
		main_output = opts.output if not opts.cover else os.path.join(tmp, "main.mp4")
		_run_main(opts, work_info, still_path, main_output, on_progress)
		if opts.cover:
			_attach_cover(opts.ffmpeg_bin, main_output, str(still_path), opts.output)
		_save_preview(opts, work_info, still_path)
	logger.info("Готово: %s", opts.output)
