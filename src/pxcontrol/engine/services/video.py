"""Сервис подготовки видео: пресеты (БД) + чистый модуль обработки.

Граница слоёв: этот сервис знает про БД и пути приложения, а модуль
``engine/video`` — только про файлы и параметры. Публикация видео —
отдельная зона (PostsService), контракт между ними — путь к файлу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import VideoPreset
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.settings import (
	CHANNEL_DEFAULT_PRESET,
	VIDEO_PROCESSED_DIR,
	VIDEO_PUBLISHED_DIR,
	VIDEO_SOURCE_DIR,
	SettingKey,
	SettingsService,
)
from pxcontrol.engine.telegram.types import userbot_max_file_bytes
from pxcontrol.engine.video import ProcessingOptions, process
from pxcontrol.engine.video.constants import AUDIO_KBPS, fitted_size
from pxcontrol.engine.video.ffmpeg import FfmpegSource, ffmpeg_source
from pxcontrol.engine.video.frames import extract_still, resolve_timestamp
from pxcontrol.engine.video.pipeline import ProgressCallback
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video, trimmed_info
from pxcontrol.paths import media_dir

logger = logging.getLogger(__name__)


class VideoError(EngineError):
	"""Ошибка подготовки видео (с понятным человеку текстом)."""


#: Целевая доля лимита Telegram: 1 % запаса на контейнер и колебания
#: кодека (итог должен быть «лимит минус 1 %»).
_TARGET_SIZE_RATIO = 0.99

#: Порог осмысленного битрейта видео: ниже ~100 кбит/с H.264 в FullHD
#: даёт кашу из артефактов — честнее отказать, чем выдать нечитаемый файл.
_MIN_VIDEO_KBPS = 100


@dataclass(frozen=True)
class BitrateAdvice:
	"""Рекомендация битрейта для исходника больше лимита Telegram.

	Attributes:
		limit_gb: лимит аккаунта в целых ГБ (2; 4 — с Premium).
		kbps: битрейт видео, дающий размер «лимит минус 1 %».
	"""

	limit_gb: int
	kbps: int

	@property
	def mbps(self) -> float:
		"""Значение для поля «Качество, Мбит/с» (вниз до 0,01)."""
		return self.kbps // 10 / 100


def recommended_bitrate_kbps(duration_s: float, max_bytes: int) -> int:
	"""Битрейт видео (кбит/с), дающий файл размером «лимит минус 1 %».

	Из бюджета вычитается фиксированный битрейт аудио конвейера
	(:data:`~pxcontrol.engine.video.constants.AUDIO_KBPS`); запас в 1 %
	покрывает накладные расходы контейнера MP4.

	Raises:
		VideoError: Длительность неположительна или бюджета не хватает
			даже на минимальный битрейт видео.
	"""
	if duration_s <= 0:
		raise VideoError("Длительность видео неизвестна — ffprobe не помог.")
	total_kbps = max_bytes * _TARGET_SIZE_RATIO * 8 / duration_s / 1000
	video_kbps = int(total_kbps) - AUDIO_KBPS
	if video_kbps < _MIN_VIDEO_KBPS:
		raise VideoError(
			"Видео слишком длинное: в лимит Telegram не уложиться даже с минимальным качеством."
		)
	return video_kbps


@dataclass(frozen=True)
class PresetDto:
	"""Пресет обработки для интерфейса."""

	id: int
	name: str


@dataclass(frozen=True)
class FrameCandidate:
	"""Кадр-кандидат заставки: момент времени и путь к готовому PNG."""

	timestamp: float
	path: str


#: Расширения видеофайлов в списке результатов (те же, что в диалоге
#: выбора исходника). Прочее (превью .png, черновики) списку не место.
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})


@dataclass(frozen=True)
class ProcessedVideo:
	"""Готовое видео в папке результатов (для списка на странице «Видео»).

	Attributes:
		name: путь относительно папки списка (файл в подпапке пакета
			показывается как «пакет/файл.mp4»).
		path: полный путь (контракт с публикацией — путь к файлу).
		size_bytes: размер файла.
		modified_at: время последнего изменения (местное).
	"""

	name: str
	path: str
	size_bytes: int
	modified_at: datetime


@dataclass(frozen=True)
class FoundVideo:
	"""Видео, найденное при сканировании папки (для пакетной обработки).

	Attributes:
		name: путь относительно выбранной папки (для показа в списке).
		path: полный путь к файлу.
		size_bytes: размер файла.
		duration_s: длительность в секундах; None — файл не прочитался
			ffprobe (такой в обработке всё равно упадёт).
	"""

	name: str
	path: str
	size_bytes: int
	duration_s: float | None


#: Колбэк хода сканирования папки: (прочитано файлов, всего файлов).
#: Вызывается из рабочего потока — интерфейс доставляет через сигнал Qt.
ScanProgress = Callable[[int, int], None]


@dataclass(frozen=True)
class ProcessedListing:
	"""Содержимое папки результатов: сама папка и её видео.

	Папка возвращается вместе со списком, чтобы интерфейс мог показать
	её путь, даже когда видео в ней ещё нет.
	"""

	directory: str
	items: list[ProcessedVideo]


@dataclass(frozen=True)
class VideoDirs:
	"""Действующие папки видео (с учётом подпапки пресета).

	Attributes:
		source: исходники для обработки.
		processed: результаты обработки.
		published: опубликованные (файл переезжает сюда после публикации).
	"""

	source: str
	processed: str
	published: str


class IntroSourceKind(StrEnum):
	"""Вид источника кадра заставки (протокол поля ``intro_source``).

	Хранимый формат — строка «вид» или «вид:значение»; собирать и
	разбирать её напрямую нельзя — только :func:`build_intro_source`
	и :func:`parse_intro_source`, иначе интерфейс и движок разъедутся.
	"""

	RANDOM_MIDDLE = "random-middle"  # случайный кадр из середины
	RANDOM_CHOICE = "random-choice"  # случайные кадры на выбор пользователю
	TIME = "time"  # момент времени, значение — секунды
	IMAGE = "image"  # своя картинка, значение — путь к PNG


#: Виды источника кадра без значения (поле «секунды/путь» не нужно).
INTRO_KINDS_WITHOUT_VALUE = frozenset(
	{IntroSourceKind.RANDOM_MIDDLE, IntroSourceKind.RANDOM_CHOICE}
)


def build_intro_source(kind: IntroSourceKind, value: str = "") -> str:
	"""Собирает строку ``intro_source`` («вид» или «вид:значение»)."""
	if kind in INTRO_KINDS_WITHOUT_VALUE:
		return str(kind)
	return f"{kind}:{value.strip()}"


def parse_intro_source(source: str) -> tuple[IntroSourceKind, str]:
	"""Разбирает строку ``intro_source`` на вид и значение.

	Неизвестный вид (битые данные) — откат к случайному кадру из середины.
	"""
	kind, _sep, value = source.partition(":")
	try:
		return IntroSourceKind(kind), value
	except ValueError:
		return IntroSourceKind.RANDOM_MIDDLE, ""


#: Стандартные имена папок видео в media/ (настройка пуста — берутся они).
_VIDEO_DIR_DEFAULTS: dict[SettingKey[str], str] = {
	VIDEO_SOURCE_DIR: "source",
	VIDEO_PROCESSED_DIR: "processed",
	VIDEO_PUBLISHED_DIR: "published",
}


def video_base_dir(settings: SettingsService, key: SettingKey[str]) -> Path:
	"""Действующий корень папки видео: настройка или стандарт в media/.

	Единственный источник правила «настройка пуста — media/<имя>»:
	им пользуются и подготовка видео (``VideoService``), и перенос
	после публикации (``PostsService``) — понимание, где лежат папки,
	не должно разъезжаться между сервисами.
	"""
	custom = settings.cached(key)
	return Path(custom) if custom else media_dir() / _VIDEO_DIR_DEFAULTS[key]


#: Символы, недопустимые в имени подпапки (разделители путей и спецсимволы ОС).
_SUBDIR_FORBIDDEN = '/\\:*?"<>|'


def sanitize_subdir(name: str) -> str:
	"""Очищает имя подпапки: без разделителей путей и спецсимволов ОС.

	Крайние точки и пробелы срезаются (Windows их не терпит в именах),
	результат ограничен 128 символами (длина колонки). Пустой результат —
	«без подпапки».
	"""
	cleaned = "".join(ch for ch in name if ch not in _SUBDIR_FORBIDDEN)
	return cleaned.strip(" .")[:128]


def _is_hidden(name: str) -> bool:
	"""Скрытый файл или папка: имя начинается с точки (соглашение Unix)."""
	return name.startswith(".")


def _walk_videos(directory: Path, excluded: list[Path] | None = None) -> list[Path]:
	"""Рекурсивно собирает видеофайлы папки (отсортированы по пути).

	Скрытые файлы и папки пропускаются; папки из ``excluded`` (уже
	развёрнутые ``resolve()``) не обходятся вовсе. ``os.walk`` вместо
	``rglob``: список папок правится на месте — исключённые ветки
	отсекаются без захода внутрь.
	"""
	roots = excluded or []
	found: list[Path] = []
	for dirpath, dirnames, filenames in os.walk(directory):
		current = Path(dirpath)
		dirnames[:] = sorted(
			name
			for name in dirnames
			if not _is_hidden(name) and not _is_under((current / name).resolve(), roots)
		)
		for name in sorted(filenames):
			if _is_hidden(name) or Path(name).suffix.lower() not in VIDEO_SUFFIXES:
				continue
			found.append(current / name)
	return found


def _is_under(path: Path, roots: list[Path]) -> bool:
	"""Лежит ли развёрнутый путь внутри одного из корней (или равен ему)."""
	return any(path == root or root in path.parents for root in roots)


@dataclass(frozen=True)
class PresetFields:
	"""Поля пресета для создания/правки (зеркалят таблицу video_presets).

	``video_bitrate_kbps``: целевой битрейт видео в кбит/с;
	None — «как в оригинале» (по умолчанию).
	"""

	name: str
	trim_start: float = 0.0  # отрезать N сек в начале (0 — не резать)
	trim_end: float = 0.0  # отрезать N сек в конце (0 — не резать)
	fade_in: float = 0.0  # появление из чёрного, сек (0 — без эффекта)
	fade_out: float = 0.0  # уход в чёрное, сек (0 — без эффекта)
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
	meta_comment: str | None = None  # тег comment: «ссылка на канал — описание»
	subdir: str = ""  # подпапка внутри базовых папок видео (пусто — без неё)


class VideoService:
	"""Пресеты обработки и запуск подготовки видео."""

	def __init__(
		self,
		db: Database,
		ffmpeg_path: FfmpegSource,
		settings: SettingsService | None = None,
		processor: Callable[[ProcessingOptions, ProgressCallback | None], None] = process,
		userbot_premium: Callable[[], bool] = lambda: False,
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно:
		настройки каналов не кэшируются). ``userbot_premium`` — провайдер
		статуса Premium userbot (определяет лимит файла для рекомендации
		битрейта)."""
		self._db = db
		self._ffmpeg = ffmpeg_source(ffmpeg_path)  # провайдер: путь из настроек
		self._settings = settings if settings is not None else SettingsService(db)
		self._processor = processor  # подменяется в тестах
		self._userbot_premium = userbot_premium
		self._candidates_dir: str | None = None  # партия кадров-кандидатов

	async def bitrate_advice(
		self, source_path: str, trim_start: float = 0.0, trim_end: float = 0.0
	) -> BitrateAdvice | None:
		"""Рекомендация битрейта, если исходник больше лимита Telegram.

		None — файла нет, он не читается ffprobe (подсказка вспомогательная)
		или укладывается в лимит аккаунта (2000/4000 МиБ по статусу
		Premium). Длительность считается после обрезки краёв.

		Raises:
			VideoError: Даже минимальный битрейт не впишет видео в лимит.
		"""
		path = Path(source_path)
		if not path.is_file():
			return None
		limit = userbot_max_file_bytes(self._userbot_premium())
		if path.stat().st_size <= limit:
			return None
		try:
			info = await asyncio.to_thread(
				probe_video, source_path, ffprobe_bin_for(self._ffmpeg())
			)
		except (OSError, RuntimeError, ValueError):
			logger.warning(
				"Рекомендация битрейта: исходник %s не прочитан.",
				source_path,
				exc_info=True,
			)
			return None
		duration = trimmed_info(info, trim_start, trim_end).duration
		return BitrateAdvice(limit // 10**9, recommended_bitrate_kbps(duration, limit))

	# --- пресеты -----------------------------------------------------------

	async def list_presets(self) -> list[PresetDto]:
		"""Возвращает все пресеты обработки."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(select(VideoPreset).order_by(VideoPreset.id))).scalars()
			return [PresetDto(p.id, p.name) for p in rows]

	async def save_preset(self, fields: PresetFields, preset_id: int | None = None) -> PresetDto:
		"""Создаёт пресет или обновляет существующий (``preset_id``).

		Raises:
			VideoError: Пресет для обновления не найден.
		"""
		values = dict(vars(fields))
		if preset_id is None and not values["subdir"]:
			# авто-умолчание при создании: подпапка из имени пресета
			values["subdir"] = sanitize_subdir(fields.name)
		else:
			values["subdir"] = sanitize_subdir(values["subdir"])
		async with self._db.session_factory() as session:
			if preset_id is None:
				preset = VideoPreset(**values)
				session.add(preset)
			else:
				existing = await session.get(VideoPreset, preset_id)
				if existing is None:
					raise VideoError("Пресет не найден — обновите список.")
				for key, value in values.items():
					setattr(existing, key, value)
				preset = existing
			await session.commit()
			await session.refresh(preset)
		logger.info("Пресет «%s» сохранён (id=%s).", preset.name, preset.id)
		return PresetDto(preset.id, preset.name)

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
			name=preset.name,
			trim_start=preset.trim_start,
			trim_end=preset.trim_end,
			fade_in=preset.fade_in,
			fade_out=preset.fade_out,
			watermark_path=preset.watermark_path,
			wm_corner=preset.wm_corner,
			wm_margin=preset.wm_margin,
			wm_opacity=preset.wm_opacity,
			wm_scale=preset.wm_scale,
			wm_start_offset=preset.wm_start_offset,
			wm_end_offset=preset.wm_end_offset,
			wm_fade=preset.wm_fade,
			intro=preset.intro,
			intro_source=preset.intro_source,
			intro_hold=preset.intro_hold,
			xfade=preset.xfade,
			cover=preset.cover,
			no_audio=preset.no_audio,
			video_bitrate_kbps=preset.video_bitrate_kbps,
			meta_comment=preset.meta_comment,
			subdir=preset.subdir,
		)

	# --- папки видео -----------------------------------------------------------

	async def dirs_for(self, subdir: str) -> VideoDirs:
		"""Действующие папки видео для подпапки пресета (создаются на месте).

		Интерфейс открывает в них файловые диалоги; пустая подпапка —
		корни базовых папок.
		"""
		cleaned = sanitize_subdir(subdir)
		dirs = []
		for key in (VIDEO_SOURCE_DIR, VIDEO_PROCESSED_DIR, VIDEO_PUBLISHED_DIR):
			path = video_base_dir(self._settings, key) / cleaned
			path.mkdir(parents=True, exist_ok=True)
			dirs.append(str(path))
		return VideoDirs(*dirs)

	async def list_processed(self, subdir: str) -> ProcessedListing:
		"""Готовые видео из подпапки папки результатов (новые — первыми).

		Показывается вся подпапка, а не только последний результат: файлы
		накапливаются между запусками, и страница «Видео» — естественное
		место, где их видно, откуда их публикуют и удаляют. Обход
		рекурсивный: пакетная обработка кладёт результаты в подпапку
		пакета (ADR-0014), и они должны быть видны в общем списке —
		имя такого файла показывается с подпапкой («пакет/файл.mp4»).

		Чтение каталога блокирующее — выполняется в отдельном потоке,
		чтобы не останавливать цикл событий движка.
		"""
		directory = video_base_dir(self._settings, VIDEO_PROCESSED_DIR) / sanitize_subdir(subdir)
		items = await asyncio.to_thread(self._scan_processed, directory)
		return ProcessedListing(str(directory), items)

	@staticmethod
	def _scan_processed(directory: Path) -> list[ProcessedVideo]:
		"""Блокирующий рекурсивный обход папки результатов (в потоке).

		Несуществующая папка — пустой список: подпапка создаётся при
		первой обработке, и до неё показывать нечего.
		"""
		if not directory.is_dir():
			return []
		items: list[ProcessedVideo] = []
		for path in _walk_videos(directory):
			try:
				stat = path.stat()
			except OSError:  # файл исчез между обходом и stat()
				continue
			items.append(
				ProcessedVideo(
					path.relative_to(directory).as_posix(),
					str(path),
					stat.st_size,
					datetime.fromtimestamp(stat.st_mtime),
				)
			)
		items.sort(key=lambda item: item.modified_at, reverse=True)
		return items

	# --- сканирование исходников (пакетная обработка) --------------------------

	async def scan_sources(
		self, root: str, on_progress: ScanProgress | None = None
	) -> list[FoundVideo]:
		"""Рекурсивно ищет видео в папке и читает их длительность (ffprobe).

		Для диалога пакетной обработки (ADR-0014): пользователь выбирает
		из найденного, что отправить в очередь. Папки результатов
		и опубликованных исключаются — иначе выбор корня ``media/``
		повторно обработал бы уже готовые файлы. Скрытые файлы и папки
		(имя с точки) пропускаются. Обход и пробы блокирующие —
		выполняются в отдельном потоке; ``on_progress`` вызывается
		из него после каждого прочитанного файла.

		Raises:
			VideoError: Папка не существует или ffmpeg/ffprobe не найдены.
		"""
		directory = Path(root)
		if not directory.is_dir():
			raise VideoError(f"Папка не найдена: {root}")
		self._require_ffmpeg()
		return await asyncio.to_thread(self._scan_sources, directory, on_progress)

	def _scan_sources(self, directory: Path, on_progress: ScanProgress | None) -> list[FoundVideo]:
		"""Блокирующий обход папки и пробы файлов (выполняется в потоке)."""
		excluded = [
			video_base_dir(self._settings, key).resolve()
			for key in (VIDEO_PROCESSED_DIR, VIDEO_PUBLISHED_DIR)
		]
		files = _walk_videos(directory, excluded)
		ffprobe = ffprobe_bin_for(self._ffmpeg())
		found: list[FoundVideo] = []
		for index, path in enumerate(files, start=1):
			try:
				size = path.stat().st_size
			except OSError:  # файл исчез между обходом и stat()
				continue
			duration: float | None
			try:
				duration = probe_video(str(path), ffprobe).duration
			except (OSError, RuntimeError, ValueError):
				# нечитаемый файл показывается в списке с пометкой —
				# решать, что с ним делать, будет человек
				logger.warning("Сканирование: файл %s не прочитан ffprobe.", path, exc_info=True)
				duration = None
			found.append(
				FoundVideo(path.relative_to(directory).as_posix(), str(path), size, duration)
			)
			if on_progress is not None:
				on_progress(index, len(files))
		return found

	async def delete_processed(self, path: str) -> None:
		"""Удаляет готовое видео с диска вместе с кадром-превью (сосед .png).

		Удалять разрешено только внутри папки результатов: путь извне
		отклоняется, чтобы промах интерфейса не стёр чужой файл.

		Raises:
			VideoError: Файл вне папки результатов или удалить не удалось.
		"""
		target = Path(path)
		root = video_base_dir(self._settings, VIDEO_PROCESSED_DIR)
		try:
			target.resolve().relative_to(root.resolve())
		except ValueError as exc:
			raise VideoError("Удалять можно только файлы из папки результатов обработки.") from exc
		await asyncio.to_thread(self._remove_with_preview, target)
		logger.info("Готовое видео удалено: %s", path)

	@staticmethod
	def _remove_with_preview(target: Path) -> None:
		"""Блокирующее удаление файла и его превью (выполняется в потоке).

		Raises:
			VideoError: Удаление не удалось (права, файл занят).
		"""
		try:
			target.unlink(missing_ok=True)
			target.with_suffix(".png").unlink(missing_ok=True)
		except OSError as exc:
			raise VideoError(f"Не удалось удалить файл: {exc.strerror or exc}") from exc

	async def processed_dir_for_channel(self, channel_id: int) -> str:
		"""Папка результатов канала: подпапка его пресета по умолчанию.

		Для диалога выбора видео на «Публикации». Нет пресета (или он
		удалён) — корень папки результатов.
		"""
		subdir = ""
		preset_id = await self._settings.get_for(CHANNEL_DEFAULT_PRESET, channel_id)
		if preset_id is not None:
			async with self._db.session_factory() as session:
				preset = await session.get(VideoPreset, preset_id)
			if preset is not None:
				subdir = preset.subdir
		return (await self.dirs_for(subdir)).processed

	async def shutdown(self) -> None:
		"""Убирает временную папку кадров-кандидатов (при остановке движка)."""
		if self._candidates_dir is not None:
			shutil.rmtree(self._candidates_dir, ignore_errors=True)
			self._candidates_dir = None

	async def delete_preset(self, preset_id: int) -> None:
		"""Удаляет пресет и снимает его у каналов, где он был по умолчанию.

		Целостность настройки-ссылки держит сервис (ADR-0013, вариант «а»):
		внешнего ключа у строки настройки нет, поэтому ссылки чистятся здесь.
		"""
		async with self._db.session_factory() as session:
			await session.execute(delete(VideoPreset).where(VideoPreset.id == preset_id))
			await session.commit()
		await self._settings.drop_channel_value(CHANNEL_DEFAULT_PRESET, preset_id)

	# --- подготовка ----------------------------------------------------------

	async def prepare(
		self,
		source_path: str,
		fields: PresetFields,
		intro_source: str | None = None,
		on_progress: ProgressCallback | None = None,
		extra_subdir: str = "",
	) -> str:
		"""Готовит видео по переданным параметрам; возвращает путь к результату.

		Параметры приходят готовыми (состояние панели на странице «Видео»):
		обработка применяет ровно то, что на экране, не заглядывая в пресеты
		БД. Обработка блокирующая (ffmpeg) и выполняется в отдельном потоке,
		чтобы не останавливать цикл событий движка. ``on_progress``
		вызывается из этого потока с долей готовности 0.0..1.0.
		``intro_source`` подменяет источник кадра заставки только для
		этого запуска (выбор кадра из кандидатов). ``extra_subdir`` —
		подпапка внутри подпапки пресета: пакетная обработка (ADR-0014)
		складывает результаты пакета в его собственную папку.

		Raises:
			VideoError: Файл/ffmpeg не найдены или обработка упала.
		"""
		self._require_ready(source_path)
		source = Path(source_path)
		options = self._build_options(source, fields, intro_source, extra_subdir)
		logger.info("Обработка видео: %s (параметры «%s»)…", source.name, fields.name)
		try:
			await asyncio.to_thread(self._processor, options, on_progress)
		except (RuntimeError, ValueError) as exc:
			raise VideoError(f"Обработка не удалась: {exc}") from exc
		logger.info("Видео готово: %s", options.output)
		return options.output

	async def extract_random_frames(
		self,
		source_path: str,
		count: int = 6,
		trim_start: float = 0.0,
		trim_end: float = 0.0,
	) -> list[FrameCandidate]:
		"""Выдёргивает случайные кадры-кандидаты заставки (5–95 % длительности).

		Кадры пишутся PNG точно в размере итогового кадра — выбранный файл
		уходит в обработку как есть (``image:<путь>``), без повторного
		извлечения. Партией владеет сервис: предыдущая удаляется при каждом
		новом запросе, так что за сессию живёт максимум одна папка.
		При обрезке (``trim_start``/``trim_end``) кандидаты берутся только
		из обрезанного диапазона, время — от обрезанной версии.

		Raises:
			VideoError: Файл/ffmpeg не найдены, обрезка съедает всё видео
				или извлечение упало.
		"""
		self._require_ready(source_path)
		if self._candidates_dir is not None:
			shutil.rmtree(self._candidates_dir, ignore_errors=True)
		self._candidates_dir = tempfile.mkdtemp(prefix="pxcontrol-frames-")
		try:
			return await asyncio.to_thread(
				self._extract_candidates,
				source_path,
				count,
				self._candidates_dir,
				trim_start,
				trim_end,
			)
		except (RuntimeError, ValueError, OSError) as exc:
			raise VideoError(f"Не удалось извлечь кадры: {exc}") from exc

	def _extract_candidates(
		self,
		source_path: str,
		count: int,
		out_dir: str,
		trim_start: float,
		trim_end: float,
	) -> list[FrameCandidate]:
		"""Блокирующее извлечение кадров (выполняется в отдельном потоке).

		Raises:
			ValueError: Обрезка не оставляет от ролика ничего.
		"""
		info = probe_video(source_path, ffprobe_bin_for(self._ffmpeg()))
		work_info = trimmed_info(info, trim_start, trim_end)
		width, height = fitted_size(work_info.width, work_info.height)
		stamps = sorted(resolve_timestamp("random-choice", work_info) for _ in range(count))
		frames: list[FrameCandidate] = []
		for index, timestamp in enumerate(stamps):
			path = str(Path(out_dir) / f"frame_{index:02d}.png")
			# извлечение — из исходника, время кандидата — от обрезанной версии
			extract_still(
				source_path,
				trim_start + timestamp,
				path,
				width,
				height,
				self._ffmpeg(),
			)
			frames.append(FrameCandidate(timestamp, path))
		return frames

	def ensure_ready(self, source_path: str) -> None:
		"""Проверяет пригодность исходника к обработке (для очереди).

		Очередь обработки (ADR-0014) зовёт эту проверку при постановке
		элемента: битый путь честно отклоняется сразу, а не в момент,
		когда до него дойдёт обработка.

		Raises:
			VideoError: Файл или ffmpeg не найдены.
		"""
		self._require_ready(source_path)

	def _require_ready(self, source_path: str) -> None:
		"""Проверяет, что исходник существует и ffmpeg доступен.

		Raises:
			VideoError: Файл или ffmpeg не найдены.
		"""
		if not Path(source_path).is_file():
			raise VideoError(f"Файл не найден: {source_path}")
		self._require_ffmpeg()

	def _require_ffmpeg(self) -> None:
		"""Проверяет доступность ffmpeg (ffprobe лежит рядом с ним).

		Raises:
			VideoError: ffmpeg не найден.
		"""
		if shutil.which(self._ffmpeg()) is None:
			raise VideoError(
				f"Не найден ffmpeg («{self._ffmpeg()}») — установите его "
				"или укажите путь в «Настройки → Общие»."
			)

	def _build_options(
		self,
		source: Path,
		fields: PresetFields,
		intro_source: str | None = None,
		extra_subdir: str = "",
	) -> ProcessingOptions:
		"""Собирает параметры обработки из переданных полей."""
		out_dir = video_base_dir(self._settings, VIDEO_PROCESSED_DIR) / sanitize_subdir(
			fields.subdir
		)
		extra = sanitize_subdir(extra_subdir)
		if extra:
			out_dir = out_dir / extra
		out_dir.mkdir(parents=True, exist_ok=True)
		stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
		# имя пресета — свободный текст: чистим спецсимволы ОС (как подпапку)
		# и меняем «_» на «-», чтобы суффикс _<пресет>_<штамп> оставался
		# разборчивым для title_from_filename (captions)
		preset_part = sanitize_subdir(fields.name).replace("_", "-").strip(" .") or "preset"
		output = out_dir / f"{source.stem}_{preset_part}_{stamp}.mp4"
		ffprobe = ffprobe_bin_for(self._ffmpeg())
		return ProcessingOptions(
			input=str(source),
			output=str(output),
			trim_start=fields.trim_start,
			trim_end=fields.trim_end,
			fade_in=fields.fade_in,
			fade_out=fields.fade_out,
			watermark=fields.watermark_path,
			wm_corner=fields.wm_corner,
			wm_margin=fields.wm_margin,
			wm_opacity=fields.wm_opacity,
			wm_scale=fields.wm_scale,
			wm_start_offset=fields.wm_start_offset,
			wm_end_offset=fields.wm_end_offset,
			wm_fade=fields.wm_fade,
			intro=fields.intro,
			intro_source=intro_source or fields.intro_source,
			intro_hold=fields.intro_hold,
			xfade=fields.xfade,
			cover=fields.cover,
			no_audio=fields.no_audio,
			video_bitrate_kbps=fields.video_bitrate_kbps,
			meta_comment=fields.meta_comment,
			ffmpeg_bin=self._ffmpeg(),
			ffprobe_bin=ffprobe,
		)
