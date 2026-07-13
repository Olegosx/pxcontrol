"""Оркестрация обработки: сборка и запуск ffmpeg (порт из makeVideo).

Поведение — по SPEC.md референса makeVideo: FullHD-вписывание, вотермарк
с окном показа (отсчёт от исходного видео), заставка hold+xfade для
превью Телеграма, опциональная обложка attached_pic.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from pxcontrol.engine.video.constants import fitted_size
from pxcontrol.engine.video.filtergraph import WatermarkOptions, build_filter_complex
from pxcontrol.engine.video.frames import prepare_still
from pxcontrol.engine.video.probe import VideoInfo, probe_video

logger = logging.getLogger(__name__)

#: Колбэк прогресса: доля готовности 0.0..1.0.
ProgressCallback = Callable[[float], None]


@dataclass(frozen=True)
class ProcessingOptions:
	"""Параметры обработки одного видео (зеркалят пресет `video_presets`).

	``video_bitrate_kbps``: целевой битрейт видео в кбит/с; None — «как
	в оригинале» (битрейт исходника, а если он неизвестен — CRF 20).
	"""

	input: str
	output: str
	video_bitrate_kbps: int | None = None
	watermark: str | None = None
	wm_corner: str = "tr"
	wm_margin: int = 24
	wm_opacity: float = 1.0
	wm_scale: float = 0.15
	wm_start: float | None = None
	wm_end: float | None = None
	intro: bool = False
	intro_source: str = "random-middle"
	intro_hold: float = 1.0
	xfade: float = 0.5
	cover: bool = False
	no_audio: bool = False
	ffmpeg_bin: str = "ffmpeg"
	ffprobe_bin: str = "ffprobe"


def _fps_arg(fps: float) -> str:
	"""Форматирует кадровую частоту строкой для аргументов ffmpeg."""
	return f"{fps:.5f}"


def _watermark_options(opts: ProcessingOptions) -> WatermarkOptions:
	"""Собирает параметры вотермарка из общих опций обработки."""
	return WatermarkOptions(
		corner=opts.wm_corner,
		margin=opts.wm_margin,
		opacity=opts.wm_opacity,
		scale=opts.wm_scale,
		start=opts.wm_start,
		end=opts.wm_end,
	)


def _build_inputs(
	opts: ProcessingOptions, info: VideoInfo, still_path: str | None
) -> tuple[list[str], int | None, int | None]:
	"""Готовит список входов ffmpeg и индексы вотермарка и заставки.

	Returns:
		Кортеж (аргументы входов, индекс вотермарка, индекс заставки).
		Индекс равен None, если соответствующий вход не нужен.
	"""
	inputs = ["-i", opts.input]
	index = 1
	wm_index: int | None = None
	still_index: int | None = None
	if opts.watermark:
		inputs += ["-i", opts.watermark]
		wm_index, index = index, index + 1
	if opts.intro:
		duration = opts.intro_hold + opts.xfade + 0.1
		inputs += [
			"-loop", "1", "-framerate", _fps_arg(info.fps),
			"-t", f"{duration:.3f}", "-i", str(still_path),
		]
		still_index, index = index, index + 1
	return inputs, wm_index, still_index


def _video_quality_args(opts: ProcessingOptions, info: VideoInfo) -> list[str]:
	"""Аргументы качества видео: заданный битрейт, битрейт исходника или CRF.

	Приоритет: явный битрейт пресета → битрейт исходника («как в оригинале»,
	режим по умолчанию) → CRF 20, если ffprobe битрейт не сообщил.
	"""
	kbps = opts.video_bitrate_kbps or info.bitrate_kbps
	if kbps:
		return ["-b:v", f"{kbps}k"]
	return ["-crf", "20"]


def _assemble_command(
	ffmpeg_bin: str, inputs: list[str], filter_complex: str, video_label: str,
	audio_label: str | None, quality: list[str], output: str,
) -> list[str]:
	"""Собирает полную команду ffmpeg для основной обработки."""
	cmd = [ffmpeg_bin, "-y", *inputs, "-filter_complex", filter_complex, "-map", video_label]
	if audio_label:
		cmd += ["-map", audio_label]
	cmd += ["-c:v", "libx264", "-preset", "medium", *quality, "-pix_fmt", "yuv420p"]
	if audio_label:
		cmd += ["-c:a", "aac", "-b:a", "192k"]
	# -progress pipe:1 — ffmpeg пишет ход кодирования в stdout (для прогресс-бара)
	cmd += ["-progress", "pipe:1", "-nostats", "-movflags", "+faststart", output]
	return cmd


def _progress_seconds(line: str) -> float | None:
	"""Извлекает секунды из строки прогресса ffmpeg.

	Поле ``out_time_ms`` исторически содержит МИКРОсекунды (причуда ffmpeg,
	проверено на 8.0); ``out_time_us`` — его честный синоним.
	"""
	for key in ("out_time_us=", "out_time_ms="):
		if line.startswith(key):
			value = line[len(key):].strip()
			try:
				return int(value) / 1_000_000
			except ValueError:
				return None
	return None


def _run_ffmpeg_streaming(
	cmd: list[str],
	what: str,
	total_seconds: float,
	on_progress: ProgressCallback | None,
) -> None:
	"""Запускает ffmpeg, транслируя ход кодирования в колбэк.

	Raises:
		RuntimeError: Если ffmpeg завершился с ненулевым кодом.
	"""
	logger.debug("ffmpeg (%s): %s", what, " ".join(cmd))
	proc = subprocess.Popen(
		cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
	)
	assert proc.stdout is not None  # stdout=PIPE
	for line in proc.stdout:
		seconds = _progress_seconds(line)
		if seconds is not None and on_progress is not None and total_seconds > 0:
			on_progress(min(seconds / total_seconds, 1.0))
	stderr = proc.stderr.read() if proc.stderr is not None else ""
	proc.wait()
	if proc.returncode != 0:
		raise RuntimeError(f"ffmpeg ({what}) завершился с ошибкой: {stderr.strip()}")


def _run_ffmpeg(cmd: list[str], what: str) -> None:
	"""Запускает ffmpeg и поднимает исключение при ошибке.

	Raises:
		RuntimeError: Если ffmpeg завершился с ненулевым кодом.
	"""
	logger.debug("ffmpeg (%s): %s", what, " ".join(cmd))
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(f"ffmpeg ({what}) завершился с ошибкой: {result.stderr.strip()}")


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
	graph = build_filter_complex(
		fps=_fps_arg(info.fps), width=width, height=height, has_intro=opts.intro,
		hold=opts.intro_hold, xfade=opts.xfade, still_index=still_index,
		has_watermark=bool(opts.watermark), wm=_watermark_options(opts),
		wm_index=wm_index, has_audio=has_audio,
	)
	cmd = _assemble_command(
		opts.ffmpeg_bin, inputs, graph.filter_complex, graph.video_label,
		graph.audio_label, _video_quality_args(opts, info), output,
	)
	# длительность итога = исходник + удержание кадра заставки (см. SPEC)
	total = info.duration + (opts.intro_hold if opts.intro else 0.0)
	_run_ffmpeg_streaming(cmd, "обработка видео", total, on_progress)


def _attach_cover(
	ffmpeg_bin: str, video_path: str, cover_path: str, output: str
) -> None:
	"""Вшивает картинку как обложку mp4 (attached_pic) без перекодирования."""
	cmd = [
		ffmpeg_bin, "-y", "-i", video_path, "-i", cover_path,
		"-map", "0", "-map", "1", "-c", "copy", "-c:v:1", "png",
		"-disposition:v:1", "attached_pic", output,
	]
	_run_ffmpeg(cmd, "вшивание обложки")


def process(
	opts: ProcessingOptions, on_progress: ProgressCallback | None = None
) -> None:
	"""Обрабатывает одно видео по заданным параметрам (блокирующе).

	Вызывающая сторона отвечает за вынос в поток/executor — модуль
	сознательно синхронный, как и его тесты.

	Args:
		opts: параметры обработки.
		on_progress: необязательный колбэк хода кодирования (0.0..1.0).

	Raises:
		RuntimeError: Если ffprobe/ffmpeg завершились с ошибкой.
		ValueError: Если режим источника кадра не распознан.
	"""
	info = probe_video(opts.input, opts.ffprobe_bin)
	with tempfile.TemporaryDirectory() as tmp:
		still_path: str | None = None
		if opts.intro or opts.cover:
			still_path = os.path.join(tmp, "still.png")
			prepare_still(opts.input, opts.intro_source, info, still_path, opts.ffmpeg_bin)
		main_output = opts.output if not opts.cover else os.path.join(tmp, "main.mp4")
		_run_main(opts, info, still_path, main_output, on_progress)
		if opts.cover:
			_attach_cover(opts.ffmpeg_bin, main_output, str(still_path), opts.output)
	logger.info("Готово: %s", opts.output)
