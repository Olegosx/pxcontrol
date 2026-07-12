"""Обработка видео через ffmpeg (каркас).

Полная логика будет перенесена из соседнего проекта makeVideo (референс):
приведение к FullHD, вотермарк, заставка для превью, обложка.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class VideoProcessor:
	"""Готовит видео к публикации, вызывая ffmpeg как подпроцесс."""

	def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
		self._ffmpeg = ffmpeg_path

	async def to_fullhd(self, src: str, dst: str) -> None:
		"""Вписывает видео в 1920×1080 без полей (минимальный шаг каркаса).

		Args:
			src: Путь к исходному файлу.
			dst: Путь к результату.

		Raises:
			RuntimeError: Если ffmpeg завершился с ненулевым кодом.
		"""
		args = [
			self._ffmpeg, "-y", "-i", src,
			"-vf", "scale=1920:1080:force_original_aspect_ratio=decrease",
			dst,
		]
		logger.info("ffmpeg: приведение к FullHD %s → %s", src, dst)
		proc = await asyncio.create_subprocess_exec(
			*args,
			stdout=asyncio.subprocess.DEVNULL,
			stderr=asyncio.subprocess.PIPE,
		)
		_, stderr = await proc.communicate()
		if proc.returncode != 0:
			detail = stderr.decode(errors="replace")
			raise RuntimeError(f"ffmpeg завершился с кодом {proc.returncode}: {detail}")
