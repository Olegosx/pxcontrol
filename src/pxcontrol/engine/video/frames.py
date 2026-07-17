"""Извлечение кадра-заставки и подготовка картинки (порт из makeVideo)."""

from __future__ import annotations

import random
from pathlib import Path

from pxcontrol.engine.video.constants import fitted_size
from pxcontrol.engine.video.ffmpeg import run_tool
from pxcontrol.engine.video.probe import VideoInfo

# Границы средней части видео для режима random-middle (доли длительности).
MIDDLE_FROM = 0.25
MIDDLE_TO = 0.75

# Диапазон случайных кадров-кандидатов (режим random-choice): шире середины,
# чтобы пользователю было из чего выбирать.
CHOICE_FROM = 0.05
CHOICE_TO = 0.95


def resolve_timestamp(source: str, info: VideoInfo) -> float:
	"""Вычисляет момент времени (сек), из которого брать кадр заставки.

	Args:
		source: режим источника — 'random-middle', 'random-choice',
			'first', 'time:СЕК' или 'frame:N'.
		info: метаданные видео.

	Raises:
		ValueError: Если режим источника не распознан.
	"""
	if source == "first":
		return 0.0
	if source == "random-middle":
		return random.uniform(info.duration * MIDDLE_FROM, info.duration * MIDDLE_TO)
	if source == "random-choice":
		# случайный из 5–95 %: этим режимом сервис набирает кадры-кандидаты,
		# он же — запасное поведение без человека (автоматика)
		return random.uniform(info.duration * CHOICE_FROM, info.duration * CHOICE_TO)
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
	run_tool(cmd, f"извлечение кадра на {timestamp:.3f} с")


def prepare_still(
	input_path: str,
	source: str,
	info: VideoInfo,
	output_path: str,
	ffmpeg_bin: str = "ffmpeg",
	start_offset: float = 0.0,
) -> None:
	"""Готовит картинку-заставку точно под размер итогового кадра.

	Поддерживает режим 'image:ПУТЬ' (чужая картинка вписывается с чёрными
	полями) и все режимы извлечения кадра из видео
	(см. :func:`resolve_timestamp`).

	``info`` — метаданные рабочей (обрезанной) версии: режимы времени
	и кадра считаются от неё. ``start_offset`` — смещение начала рабочей
	версии в исходном файле (обрезка в начале): кадр извлекается
	из исходника, поэтому момент сдвигается на это смещение.

	Raises:
		RuntimeError: Если ffmpeg не смог подготовить картинку.
		ValueError: Режим источника не распознан или картинка не найдена.
	"""
	width, height = fitted_size(info.width, info.height)
	if source.startswith("image:"):
		image_path = source.split(":", 1)[1]
		# проверка до ffmpeg: несуществующий файл дал бы дамп журнала
		# вместо точной причины
		if not Path(image_path).is_file():
			raise ValueError(f"Картинка для заставки не найдена: {image_path}")
		extract_still(image_path, 0.0, output_path, width, height, ffmpeg_bin)
		return
	timestamp = resolve_timestamp(source, info)
	extract_still(
		input_path, start_offset + timestamp, output_path, width, height, ffmpeg_bin
	)
