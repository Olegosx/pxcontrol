"""Обёртки над ffprobe: извлечение метаданных видео (порт из makeVideo)."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoInfo:
	"""Метаданные исходного видео.

	Attributes:
		width: ширина кадра в пикселях.
		height: высота кадра в пикселях.
		duration: длительность в секундах.
		fps: кадровая частота (кадров в секунду).
		has_audio: есть ли в файле звуковая дорожка.
		bitrate_kbps: битрейт видеопотока в кбит/с (None — ffprobe не отдал).
	"""

	width: int
	height: int
	duration: float
	fps: float
	has_audio: bool
	bitrate_kbps: int | None = None


def _run_ffprobe(path: str, ffprobe_bin: str) -> dict[str, Any]:
	"""Запускает ffprobe и возвращает разобранный JSON по файлу.

	Raises:
		RuntimeError: Если ffprobe завершился с ненулевым кодом.
	"""
	cmd = [
		ffprobe_bin, "-v", "error", "-print_format", "json",
		"-show_streams", "-show_format", path,
	]
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(f"ffprobe не смог прочитать '{path}': {result.stderr.strip()}")
	return dict(json.loads(result.stdout))


def _parse_fps(rate: str) -> float:
	"""Преобразует строку кадровой частоты вида '30000/1001' в число.

	Returns:
		Частота в кадрах в секунду; 0.0, если знаменатель нулевой ('0/0').
	"""
	if "/" in rate:
		num, den = rate.split("/", 1)
		den_value = float(den)
		return 0.0 if den_value == 0 else float(num) / den_value
	return float(rate)


def _parse_bitrate_kbps(video: dict[str, Any], fmt: dict[str, Any]) -> int | None:
	"""Битрейт видеопотока в кбит/с: из потока, иначе из контейнера.

	Битрейт контейнера включает и звук, поэтому это лишь приближение сверху —
	используется, когда поток своего значения не сообщает (типично для mkv/webm).
	"""
	for raw in (video.get("bit_rate"), fmt.get("bit_rate")):
		try:
			bps = int(str(raw))
		except (TypeError, ValueError):
			continue
		if bps > 0:
			return round(bps / 1000)
	return None


def probe_video(path: str, ffprobe_bin: str = "ffprobe") -> VideoInfo:
	"""Возвращает метаданные видео: размеры, длительность, fps, наличие звука.

	Raises:
		RuntimeError: Если нет видеопотока или не удалось определить fps.
	"""
	data = _run_ffprobe(path, ffprobe_bin)
	streams = data.get("streams", [])
	video = next((s for s in streams if s.get("codec_type") == "video"), None)
	if video is None:
		raise RuntimeError(f"В файле '{path}' нет видеопотока")
	fps = _parse_fps(video.get("avg_frame_rate", "0/0")) or _parse_fps(
		video.get("r_frame_rate", "0/0")
	)
	if fps <= 0:
		raise RuntimeError(f"Не удалось определить кадровую частоту для '{path}'")
	fmt = dict(data.get("format", {}))
	return VideoInfo(
		width=int(video["width"]),
		height=int(video["height"]),
		duration=float(fmt.get("duration", 0.0)),
		fps=fps,
		has_audio=any(s.get("codec_type") == "audio" for s in streams),
		bitrate_kbps=_parse_bitrate_kbps(video, fmt),
	)
