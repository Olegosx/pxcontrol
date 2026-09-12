"""Извлечение кадра-заставки и подготовка картинки (порт из makeVideo)."""

from __future__ import annotations

import random
from pathlib import Path

from pxcontrol.engine.video.constants import scaled_size
from pxcontrol.engine.video.ffmpeg import run_tool
from pxcontrol.engine.video.probe import VideoInfo

# Границы средней части видео для режима random-middle (доли длительности).
MIDDLE_FROM = 0.25
MIDDLE_TO = 0.75

# Диапазон случайных кадров-кандидатов (режим random-choice): шире середины,
# чтобы пользователю было из чего выбирать.
CHOICE_FROM = 0.05
CHOICE_TO = 0.95

#: Предел ожидания извлечения одного кадра: перемотка в длинном
#: ролике занимает секунды, две минуты — запас на медленный диск.
_STILL_TIMEOUT_S = 120.0


def resolve_timestamp(source: str, info: VideoInfo) -> float:
	"""Вычисляет момент времени (сек), из которого брать кадр заставки.

	Args:
		source: режим источника — 'random-middle', 'random-choice'
			или 'time:СЕК' (протокол ``IntroSourceKind`` сервиса видео).
		info: метаданные видео.

	Raises:
		ValueError: Если режим источника не распознан.
	"""
	if source == "random-middle":
		return random.uniform(info.duration * MIDDLE_FROM, info.duration * MIDDLE_TO)
	if source == "random-choice":
		# случайный из 5–95 %: этим режимом сервис набирает кадры-кандидаты,
		# он же — запасное поведение без человека (автоматика)
		return random.uniform(info.duration * CHOICE_FROM, info.duration * CHOICE_TO)
	# число разбирается с доменным текстом: сырое «could not convert…»
	# выбивалось бы из стиля модуля (каждая ошибка названа по-русски)
	if source.startswith("time:"):
		try:
			return float(source.split(":", 1)[1])
		except ValueError as exc:
			raise ValueError(f"Момент кадра не число: {source}") from exc
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
		ffmpeg_bin,
		"-y",
		"-ss",
		f"{timestamp:.3f}",
		"-i",
		input_path,
		"-frames:v",
		"1",
		"-vf",
		_fit_pad_filter(width, height),
		output_path,
	]
	# один кадр — секунды; предел ловит зависший ffmpeg (недоступный диск)
	run_tool(cmd, f"извлечение кадра на {timestamp:.3f} с", timeout=_STILL_TIMEOUT_S)


def still_from_video(
	input_path: str,
	timestamp: float,
	info: VideoInfo,
	output_path: str,
	target_resolution: int | None,
	ffmpeg_bin: str = "ffmpeg",
	start_offset: float = 0.0,
) -> None:
	"""Достаёт кадр видео под размер итогового кадра.

	Здесь живёт правило, общее для заставки и для выбора кадра
	человеком: размер считается от **обрезанной** версии (``info``),
	а извлекается кадр из **исходника** — поэтому момент сдвигается
	на ``start_offset`` (сколько обрезано в начале). Разойдись эти две
	реализации, и склейка xfade упала бы на несовпадении размеров
	входов — а поймать это тестом врозь было бы нечем.
	"""
	width, height = scaled_size(info.width, info.height, target_resolution)
	extract_still(input_path, start_offset + timestamp, output_path, width, height, ffmpeg_bin)


def extract_candidates(
	input_path: str,
	info: VideoInfo,
	count: int,
	out_dir: str,
	target_resolution: int | None,
	ffmpeg_bin: str = "ffmpeg",
	start_offset: float = 0.0,
) -> list[tuple[float, str]]:
	"""Извлекает ``count`` случайных кадров для выбора заставки человеком.

	Моменты — от обрезанной версии (``info``), извлечение — из исходника
	со сдвигом ``start_offset``: то же правило, что у заставки
	(:func:`still_from_video`).

	Returns:
		Пары «момент в обрезанной версии, путь к файлу кадра»,
		по возрастанию момента.

	Raises:
		ValueError: Обрезка не оставляет от ролика ничего.
		RuntimeError: ffmpeg не смог извлечь кадр.
	"""
	stamps = sorted(resolve_timestamp("random-choice", info) for _ in range(count))
	frames: list[tuple[float, str]] = []
	for index, timestamp in enumerate(stamps):
		path = str(Path(out_dir) / f"frame_{index:02d}.png")
		still_from_video(
			input_path, timestamp, info, path, target_resolution, ffmpeg_bin, start_offset
		)
		frames.append((timestamp, path))
	return frames


def prepare_still(
	input_path: str,
	source: str,
	info: VideoInfo,
	output_path: str,
	target_resolution: int | None,
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
	``target_resolution`` — ступень разрешения итога (None — «как
	в оригинале»); она обязана совпадать со ступенью основного видео:
	склейка xfade требует точного равенства размеров.

	Raises:
		RuntimeError: Если ffmpeg не смог подготовить картинку.
		ValueError: Режим источника не распознан или картинка не найдена.
	"""
	if source.startswith("image:"):
		image_path = source.split(":", 1)[1]
		# проверка до ffmpeg: несуществующий файл дал бы дамп журнала
		# вместо точной причины
		if not Path(image_path).is_file():
			raise ValueError(f"Картинка для заставки не найдена: {image_path}")
		width, height = scaled_size(info.width, info.height, target_resolution)
		extract_still(image_path, 0.0, output_path, width, height, ffmpeg_bin)
		return
	timestamp = resolve_timestamp(source, info)
	still_from_video(
		input_path, timestamp, info, output_path, target_resolution, ffmpeg_bin, start_offset
	)
