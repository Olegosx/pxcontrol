"""Сервис подготовки видео: пресеты (БД) + чистый модуль обработки.

Граница слоёв: этот сервис знает про БД и пути приложения, а модуль
``engine/video`` — только про файлы и параметры. Публикация видео —
отдельная зона (PostsService), контракт между ними — путь к файлу.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import VideoPreset
from pxcontrol.engine.video import ProcessingOptions, process
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
class PresetFields:
	"""Поля пресета для создания/правки (зеркалят таблицу video_presets)."""

	name: str
	watermark_path: str | None = None
	wm_corner: str = "tr"
	wm_margin: int = 24
	wm_opacity: float = 1.0
	wm_scale: float = 0.15
	intro: bool = False
	intro_source: str = "random-middle"
	intro_hold: float = 1.0
	xfade: float = 0.5
	cover: bool = False
	no_audio: bool = False


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
	return " · ".join(parts)


class VideoService:
	"""Пресеты обработки и запуск подготовки видео."""

	def __init__(
		self,
		db: Database,
		ffmpeg_path: str,
		processor: Callable[[ProcessingOptions], None] = process,
	) -> None:
		self._db = db
		self._ffmpeg = ffmpeg_path
		self._processor = processor  # подменяется в тестах

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
			intro=preset.intro, intro_source=preset.intro_source,
			intro_hold=preset.intro_hold, xfade=preset.xfade,
			cover=preset.cover, no_audio=preset.no_audio,
		)

	async def delete_preset(self, preset_id: int) -> None:
		"""Удаляет пресет по идентификатору."""
		async with self._db.session_factory() as session:
			await session.execute(
				delete(VideoPreset).where(VideoPreset.id == preset_id)
			)
			await session.commit()

	# --- подготовка ----------------------------------------------------------

	async def prepare(self, source_path: str, preset_id: int) -> str:
		"""Готовит видео по пресету; возвращает путь к результату.

		Обработка блокирующая (ffmpeg) и выполняется в отдельном потоке,
		чтобы не останавливать цикл событий движка.

		Raises:
			VideoError: Файл/пресет/ffmpeg не найдены или обработка упала.
		"""
		source = Path(source_path)
		if not source.is_file():
			raise VideoError(f"Файл не найден: {source_path}")
		if shutil.which(self._ffmpeg) is None:
			raise VideoError(
				f"Не найден ffmpeg («{self._ffmpeg}») — установите его "
				"или укажите путь в FFMPEG_PATH."
			)
		options = await self._build_options(source, preset_id)
		logger.info("Обработка видео: %s (пресет id=%s)…", source.name, preset_id)
		try:
			await asyncio.to_thread(self._processor, options)
		except (RuntimeError, ValueError) as exc:
			raise VideoError(f"Обработка не удалась: {exc}") from exc
		logger.info("Видео готово: %s", options.output)
		return options.output

	async def _build_options(
		self, source: Path, preset_id: int
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
		ffprobe = self._ffprobe_bin()
		return ProcessingOptions(
			input=str(source), output=str(output),
			watermark=preset.watermark_path, wm_corner=preset.wm_corner,
			wm_margin=preset.wm_margin, wm_opacity=preset.wm_opacity,
			wm_scale=preset.wm_scale, wm_start=preset.wm_start,
			wm_end=preset.wm_end, intro=preset.intro,
			intro_source=preset.intro_source, intro_hold=preset.intro_hold,
			xfade=preset.xfade, cover=preset.cover, no_audio=preset.no_audio,
			ffmpeg_bin=self._ffmpeg, ffprobe_bin=ffprobe,
		)

	def _ffprobe_bin(self) -> str:
		"""ffprobe ищем рядом с заданным ffmpeg (или в PATH)."""
		ffmpeg = Path(self._ffmpeg)
		if ffmpeg.is_absolute():
			return str(ffmpeg.with_name("ffprobe"))
		return "ffprobe"
