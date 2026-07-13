"""Извлечение кадра-заставки и подготовка картинки (порт из makeVideo)."""

from __future__ import annotations

import logging
import random
import subprocess

from pxcontrol.engine.video.constants import fitted_size
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


def _fit_pad_filter(width: int, height: int) -> str:
	"""Фильтр: вписать в точный размер кадра, недостающее добить чёрными полями.

	Размер кадра заставки обязан совпадать с основным видео (требование
	xfade). Для кадра из того же видео поля не появляются; для чужой
	картинки с иными пропорциями — letterbox по центру.
	"""
	return (
		f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
		f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
	)


def extract_still(
	input_path: str,
	timestamp: float,
	output_path: str,
	width: int,
	height: int,
	ffmpeg_bin: str = "ffmpeg",
) -> None:
	"""Извлекает один кадр в момент timestamp, приведённый к размеру кадра.

	Raises:
		RuntimeError: Если ffmpeg не смог извлечь кадр.
	"""
	cmd = [
		ffmpeg_bin, "-y", "-ss", f"{timestamp:.3f}", "-i", input_path,
		"-frames:v", "1", "-vf", _fit_pad_filter(width, height), output_path,
	]
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(
			f"Не удалось извлечь кадр на {timestamp:.3f}с: {result.stderr.strip()}"
		)


def make_thumbnail(
	source_path: str,
	output_jpg: str,
	ffmpeg_bin: str = "ffmpeg",
	timestamp: float = 0.0,
) -> None:
	"""Делает JPEG-миниатюру для Telegram: кадр, вписанный в 320×320.

	Пропорции кадра сохраняются (Telegram растягивает миниатюру до
	пропорций видео — квадратный кроп исказил бы картинку). Источник —
	картинка или видео (кадр берётся в момент ``timestamp``).

	Raises:
		RuntimeError: Если ffmpeg не смог сделать миниатюру.
	"""
	cmd = [
		ffmpeg_bin, "-y", "-ss", f"{timestamp:.3f}", "-i", source_path,
		"-frames:v", "1",
		"-vf", "scale=320:320:force_original_aspect_ratio=decrease",
		"-q:v", "4", output_jpg,
	]
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(
			f"Не удалось сделать миниатюру из '{source_path}': "
			f"{result.stderr.strip()}"
		)


def prepare_still(
	input_path: str,
	source: str,
	info: VideoInfo,
	output_path: str,
	ffmpeg_bin: str = "ffmpeg",
) -> None:
	"""Готовит картинку-заставку точно под размер итогового кадра.

	Поддерживает режим 'image:ПУТЬ' (чужая картинка вписывается с чёрными
	полями) и все режимы извлечения кадра из видео
	(см. :func:`resolve_timestamp`).

	Raises:
		RuntimeError: Если ffmpeg не смог подготовить картинку.
		ValueError: Если режим источника не распознан.
	"""
	width, height = fitted_size(info.width, info.height)
	if source.startswith("image:"):
		extract_still(source.split(":", 1)[1], 0.0, output_path, width, height, ffmpeg_bin)
		return
	timestamp = resolve_timestamp(source, info)
	extract_still(input_path, timestamp, output_path, width, height, ffmpeg_bin)
