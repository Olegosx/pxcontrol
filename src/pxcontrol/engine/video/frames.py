"""Извлечение кадра-заставки и подготовка картинки (порт из makeVideo)."""

from __future__ import annotations

import logging
import random
import subprocess

from pxcontrol.engine.video.constants import FULLHD_FIT
from pxcontrol.engine.video.probe import VideoInfo

logger = logging.getLogger(__name__)

# Границы средней части видео для режима random-middle (доли длительности).
MIDDLE_FROM = 0.25
MIDDLE_TO = 0.75


def resolve_timestamp(source: str, info: VideoInfo) -> float:
	"""Вычисляет момент времени (сек), из которого брать кадр заставки.

	Args:
		source: режим источника — 'random-middle', 'first', 'time:СЕК'
			или 'frame:N'.
		info: метаданные видео.

	Raises:
		ValueError: Если режим источника не распознан.
	"""
	if source == "first":
		return 0.0
	if source == "random-middle":
		return random.uniform(info.duration * MIDDLE_FROM, info.duration * MIDDLE_TO)
	if source.startswith("time:"):
		return float(source.split(":", 1)[1])
	if source.startswith("frame:"):
		return int(source.split(":", 1)[1]) / info.fps
	raise ValueError(f"Неизвестный источник кадра: {source}")


def extract_still(
	input_path: str, timestamp: float, output_path: str, ffmpeg_bin: str = "ffmpeg"
) -> None:
	"""Извлекает один кадр в момент timestamp, вписанный в FullHD.

	Raises:
		RuntimeError: Если ffmpeg не смог извлечь кадр.
	"""
	cmd = [
		ffmpeg_bin, "-y", "-ss", f"{timestamp:.3f}", "-i", input_path,
		"-frames:v", "1", "-vf", FULLHD_FIT, output_path,
	]
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(
			f"Не удалось извлечь кадр на {timestamp:.3f}с: {result.stderr.strip()}"
		)


def prepare_still(
	input_path: str,
	source: str,
	info: VideoInfo,
	output_path: str,
	ffmpeg_bin: str = "ffmpeg",
) -> None:
	"""Готовит картинку-заставку по выбранному источнику, вписанную в FullHD.

	Поддерживает режим 'image:ПУТЬ' (масштабирование готовой картинки) и все
	режимы извлечения кадра из видео (см. :func:`resolve_timestamp`).

	Raises:
		RuntimeError: Если ffmpeg не смог подготовить картинку.
		ValueError: Если режим источника не распознан.
	"""
	if source.startswith("image:"):
		extract_still(source.split(":", 1)[1], 0.0, output_path, ffmpeg_bin)
		return
	timestamp = resolve_timestamp(source, info)
	extract_still(input_path, timestamp, output_path, ffmpeg_bin)
