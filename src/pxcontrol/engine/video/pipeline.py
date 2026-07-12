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
from dataclasses import dataclass

from pxcontrol.engine.video.filtergraph import WatermarkOptions, build_filter_complex
from pxcontrol.engine.video.frames import prepare_still
from pxcontrol.engine.video.probe import VideoInfo, probe_video

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingOptions:
	"""Параметры обработки одного видео (зеркалят пресет `video_presets`)."""

	input: str
	output: str
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


def _assemble_command(
	ffmpeg_bin: str, inputs: list[str], filter_complex: str, video_label: str,
	audio_label: str | None, output: str,
) -> list[str]:
	"""Собирает полную команду ffmpeg для основной обработки."""
	cmd = [ffmpeg_bin, "-y", *inputs, "-filter_complex", filter_complex, "-map", video_label]
	if audio_label:
		cmd += ["-map", audio_label]
	cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
	if audio_label:
		cmd += ["-c:a", "aac", "-b:a", "192k"]
	cmd += ["-movflags", "+faststart", output]
	return cmd


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
	opts: ProcessingOptions, info: VideoInfo, still_path: str | None, output: str
) -> None:
	"""Запускает основную обработку: FullHD, вотермарк, заставка, кодирование."""
	inputs, wm_index, still_index = _build_inputs(opts, info, still_path)
	has_audio = info.has_audio and not opts.no_audio
	graph = build_filter_complex(
		fps=_fps_arg(info.fps), has_intro=opts.intro,
		hold=opts.intro_hold, xfade=opts.xfade, still_index=still_index,
		has_watermark=bool(opts.watermark), wm=_watermark_options(opts),
		wm_index=wm_index, has_audio=has_audio,
	)
	cmd = _assemble_command(
		opts.ffmpeg_bin, inputs, graph.filter_complex,
		graph.video_label, graph.audio_label, output,
	)
	_run_ffmpeg(cmd, "обработка видео")


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


def process(opts: ProcessingOptions) -> None:
	"""Обрабатывает одно видео по заданным параметрам (блокирующе).

	Вызывающая сторона отвечает за вынос в поток/executor — модуль
	сознательно синхронный, как и его тесты.

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
		_run_main(opts, info, still_path, main_output)
		if opts.cover:
			_attach_cover(opts.ffmpeg_bin, main_output, str(still_path), opts.output)
	logger.info("Готово: %s", opts.output)
