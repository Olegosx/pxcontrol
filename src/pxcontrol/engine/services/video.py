"""Сервис подготовки видео: пресеты (БД) + чистый модуль обработки.

Граница слоёв: этот сервис знает про БД и пути приложения, а модуль
``engine/video`` — только про файлы и параметры. Публикация видео —
отдельная зона (PostsService), контракт между ними — путь к файлу.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import VideoPreset
from pxcontrol.engine.video import ProcessingOptions, process
from pxcontrol.engine.video.constants import fitted_size
from pxcontrol.engine.video.frames import CHOICE_FROM, CHOICE_TO, extract_still
from pxcontrol.engine.video.pipeline import ProgressCallback
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video
from pxcontrol.paths import media_dir

logger = logging.getLogger(__name__)


class VideoError(Exception):
	"""Ошибка подготовки видео (с понятным человеку текстом)."""


@dataclass(frozen=True)
class PresetDto:
	"""Пресет обработки для интерфейса."""

	id: int
	name: str
	summary: str


@dataclass(frozen=True)
class FrameCandidate:
	"""Кадр-кандидат заставки: момент времени и путь к готовому PNG."""

	timestamp: float
	path: str


@dataclass(frozen=True)
class PresetFields:
	"""Поля пресета для создания/правки (зеркалят таблицу video_presets).

	``video_bitrate_kbps``: целевой битрейт видео в кбит/с;
	None — «как в оригинале» (по умолчанию).
	"""

	name: str
	watermark_path: str | None = None
	wm_corner: str = "tr"
	wm_margin: int = 24
	wm_opacity: float = 1.0
	wm_scale: float = 0.15
	wm_start_offset: float | None = None  # показать через N сек от начала
	wm_end_offset: float | None = None  # скрыть за N сек до конца
	wm_fade: float = 0.0  # плавность появления/исчезания (сек; 0 — резко)
	intro: bool = False
	intro_source: str = "random-middle"
	intro_hold: float = 1.0
	xfade: float = 0.5
	cover: bool = False
	no_audio: bool = False
	video_bitrate_kbps: int | None = None


def _summary(preset: VideoPreset) -> str:
	"""Короткое описание пресета для карточки."""
	parts = ["FullHD"]
	if preset.watermark_path:
		parts.append(f"вотермарк ({preset.wm_corner})")
	if preset.intro:
		parts.append("заставка")
	if preset.cover:
		parts.append("обложка")
	if preset.no_audio:
		parts.append("без звука")
	if preset.video_bitrate_kbps:
		parts.append(f"{preset.video_bitrate_kbps / 1000:g} Мбит/с")
	return " · ".join(parts)


class VideoService:
	"""Пресеты обработки и запуск подготовки видео."""

	def __init__(
		self,
		db: Database,
		ffmpeg_path: str,
		processor: Callable[[ProcessingOptions, ProgressCallback | None], None] = process,
	) -> None:
		self._db = db
		self._ffmpeg = ffmpeg_path
		self._processor = processor  # подменяется в тестах
		self._candidates_dir: str | None = None  # партия кадров-кандидатов

	# --- пресеты -----------------------------------------------------------

	async def list_presets(self) -> list[PresetDto]:
		"""Возвращает все пресеты обработки."""
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(select(VideoPreset).order_by(VideoPreset.id))
			).scalars()
			return [PresetDto(p.id, p.name, _summary(p)) for p in rows]

	async def save_preset(
		self, fields: PresetFields, preset_id: int | None = None
	) -> PresetDto:
		"""Создаёт пресет или обновляет существующий (``preset_id``).

		Raises:
			VideoError: Пресет для обновления не найден.
		"""
		async with self._db.session_factory() as session:
			if preset_id is None:
				preset = VideoPreset(**vars(fields))
				session.add(preset)
			else:
				existing = await session.get(VideoPreset, preset_id)
				if existing is None:
					raise VideoError("Пресет не найден — обновите список.")
				for key, value in vars(fields).items():
					setattr(existing, key, value)
				preset = existing
			await session.commit()
			await session.refresh(preset)
		logger.info("Пресет «%s» сохранён (id=%s).", preset.name, preset.id)
		return PresetDto(preset.id, preset.name, _summary(preset))

	async def get_preset_fields(self, preset_id: int) -> PresetFields:
		"""Возвращает поля пресета для диалога правки.

		Raises:
			VideoError: Пресет не найден.
		"""
		async with self._db.session_factory() as session:
			preset = await session.get(VideoPreset, preset_id)
		if preset is None:
			raise VideoError("Пресет не найден — обновите список.")
		return PresetFields(
			name=preset.name, watermark_path=preset.watermark_path,
			wm_corner=preset.wm_corner, wm_margin=preset.wm_margin,
			wm_opacity=preset.wm_opacity, wm_scale=preset.wm_scale,
			wm_start_offset=preset.wm_start_offset,
			wm_end_offset=preset.wm_end_offset, wm_fade=preset.wm_fade,
			intro=preset.intro, intro_source=preset.intro_source,
			intro_hold=preset.intro_hold, xfade=preset.xfade,
			cover=preset.cover, no_audio=preset.no_audio,
			video_bitrate_kbps=preset.video_bitrate_kbps,
		)

	async def delete_preset(self, preset_id: int) -> None:
		"""Удаляет пресет по идентификатору."""
		async with self._db.session_factory() as session:
			await session.execute(
				delete(VideoPreset).where(VideoPreset.id == preset_id)
			)
			await session.commit()

	# --- подготовка ----------------------------------------------------------

	async def prepare(
		self,
		source_path: str,
		preset_id: int,
		intro_source: str | None = None,
		on_progress: ProgressCallback | None = None,
	) -> str:
		"""Готовит видео по пресету; возвращает путь к результату.

		Обработка блокирующая (ffmpeg) и выполняется в отдельном потоке,
		чтобы не останавливать цикл событий движка. ``on_progress``
		вызывается из этого потока с долей готовности 0.0..1.0.
		``intro_source`` подменяет источник кадра заставки только для
		этого запуска (выбор кадра из кандидатов), пресет не меняется.

		Raises:
			VideoError: Файл/пресет/ffmpeg не найдены или обработка упала.
		"""
		self._require_ready(source_path)
		source = Path(source_path)
		options = await self._build_options(source, preset_id, intro_source)
		logger.info("Обработка видео: %s (пресет id=%s)…", source.name, preset_id)
		try:
			await asyncio.to_thread(self._processor, options, on_progress)
		except (RuntimeError, ValueError) as exc:
			raise VideoError(f"Обработка не удалась: {exc}") from exc
		logger.info("Видео готово: %s", options.output)
		return options.output

	async def extract_random_frames(
		self, source_path: str, count: int = 6
	) -> list[FrameCandidate]:
		"""Выдёргивает случайные кадры-кандидаты заставки (5–95 % длительности).

		Кадры пишутся PNG точно в размере итогового кадра — выбранный файл
		уходит в обработку как есть (``image:<путь>``), без повторного
		извлечения. Партией владеет сервис: предыдущая удаляется при каждом
		новом запросе, так что за сессию живёт максимум одна папка.

		Raises:
			VideoError: Файл/ffmpeg не найдены или извлечение упало.
		"""
		self._require_ready(source_path)
		if self._candidates_dir is not None:
			shutil.rmtree(self._candidates_dir, ignore_errors=True)
		self._candidates_dir = tempfile.mkdtemp(prefix="pxcontrol-frames-")
		try:
			return await asyncio.to_thread(
				self._extract_candidates, source_path, count, self._candidates_dir
			)
		except (RuntimeError, ValueError, OSError) as exc:
			raise VideoError(f"Не удалось извлечь кадры: {exc}") from exc

	def _extract_candidates(
		self, source_path: str, count: int, out_dir: str
	) -> list[FrameCandidate]:
		"""Блокирующее извлечение кадров (выполняется в отдельном потоке)."""
		info = probe_video(source_path, ffprobe_bin_for(self._ffmpeg))
		width, height = fitted_size(info.width, info.height)
		stamps = sorted(
			random.uniform(info.duration * CHOICE_FROM, info.duration * CHOICE_TO)
			for _ in range(count)
		)
		frames: list[FrameCandidate] = []
		for index, timestamp in enumerate(stamps):
			path = str(Path(out_dir) / f"frame_{index:02d}.png")
			extract_still(source_path, timestamp, path, width, height, self._ffmpeg)
			frames.append(FrameCandidate(timestamp, path))
		return frames

	def _require_ready(self, source_path: str) -> None:
		"""Проверяет, что исходник существует и ffmpeg доступен.

		Raises:
			VideoError: Файл или ffmpeg не найдены.
		"""
		if not Path(source_path).is_file():
			raise VideoError(f"Файл не найден: {source_path}")
		if shutil.which(self._ffmpeg) is None:
			raise VideoError(
				f"Не найден ffmpeg («{self._ffmpeg}») — установите его "
				"или укажите путь в FFMPEG_PATH."
			)

	async def _build_options(
		self, source: Path, preset_id: int, intro_source: str | None = None
	) -> ProcessingOptions:
		"""Собирает параметры обработки из пресета БД."""
		async with self._db.session_factory() as session:
			preset = await session.get(VideoPreset, preset_id)
		if preset is None:
			raise VideoError("Пресет не найден — обновите список.")
		out_dir = media_dir() / "processed"
		out_dir.mkdir(parents=True, exist_ok=True)
		stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
		output = out_dir / f"{source.stem}_{preset.name}_{stamp}.mp4"
		ffprobe = ffprobe_bin_for(self._ffmpeg)
		return ProcessingOptions(
			input=str(source), output=str(output),
			watermark=preset.watermark_path, wm_corner=preset.wm_corner,
			wm_margin=preset.wm_margin, wm_opacity=preset.wm_opacity,
			wm_scale=preset.wm_scale, wm_start_offset=preset.wm_start_offset,
			wm_end_offset=preset.wm_end_offset, wm_fade=preset.wm_fade,
			intro=preset.intro,
			intro_source=intro_source or preset.intro_source,
			intro_hold=preset.intro_hold,
			xfade=preset.xfade, cover=preset.cover, no_audio=preset.no_audio,
			video_bitrate_kbps=preset.video_bitrate_kbps,
			ffmpeg_bin=self._ffmpeg, ffprobe_bin=ffprobe,
		)
