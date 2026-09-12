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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import SUBDIR_MAX_CHARS, VideoPreset
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.captions import FORBIDDEN_NAME_CHARS
from pxcontrol.engine.services.settings import (
	COMMUNITY_DEFAULT_PRESET,
	VIDEO_PROCESSED_DIR,
	VIDEO_PUBLISHED_DIR,
	VIDEO_QUEUED_DIR,
	VIDEO_SOURCE_DIR,
	SettingKey,
	SettingsService,
)
from pxcontrol.engine.telegram.types import limit_gb, userbot_max_file_bytes
from pxcontrol.engine.video import ProcessingOptions, process
from pxcontrol.engine.video.constants import (
	AUDIO_KBPS,
	DEFAULT_RESOLUTION,
	preview_path,
)
from pxcontrol.engine.video.ffmpeg import FfmpegSource, ffmpeg_source
from pxcontrol.engine.video.frames import extract_candidates
from pxcontrol.engine.video.pipeline import ProgressCallback
from pxcontrol.engine.video.probe import (
	VideoInfo,
	ffprobe_bin_for,
	probe_video,
	trimmed_info,
)
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
class SourceAdvice:
	"""Сведения об исходнике для подсказок в карточке файла.

	Собираются одной пробой ffprobe: интерфейсу нужны и размеры кадра
	(предупреждение об апскейле), и совет по качеству.

	Attributes:
		width: ширина исходного кадра в пикселях (отображаемая — флаг
			поворота учитывает ``probe_video``).
		height: высота исходного кадра в пикселях (отображаемая).
		bitrate: рекомендация битрейта, если файл больше лимита Telegram;
			None — файл в лимит укладывается или совет невозможен.
	"""

	width: int
	height: int
	bitrate: BitrateAdvice | None


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


#: Расширения видеофайлов: по ним работают сканирования папок
#: (``scan_sources``/``scan_ready``) и фильтры файловых диалогов
#: (:func:`video_dialog_filter`). Прочее (превью .png, черновики) — мимо.
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})


def video_dialog_filter() -> str:
	"""Фильтр файлового диалога «Видео (…)», собранный из ``VIDEO_SUFFIXES``.

	Единая точка для диалогов выбора видео («Видео», «Публикация»):
	новое расширение движка попадает в фильтры само, без ручной
	синхронизации строк.
	"""
	patterns = " ".join(f"*{suffix}" for suffix in sorted(VIDEO_SUFFIXES))
	return f"Видео ({patterns})"


@dataclass(frozen=True)
class VideoFile:
	"""Видеофайл на диске: всё, что известно о нём без пробы ffprobe.

	Один тип на обе задачи — список готовых видео на странице «Видео»
	и источник пакетной отправки (ADR-0015). Прежде их было два
	(``ProcessedVideo`` и ``ReadyVideo``), причём второй — строгое
	подмножество первого: одно понятие с двумя именами и двумя
	сканерами папки, которые приходилось править парой.

	От :class:`FoundVideo` отличается по существу: там есть данные
	пробы ffprobe (длительность, размер кадра) — ради них сканирование
	источников идёт минутами, а здесь достаточно обхода каталога.

	Attributes:
		name: путь относительно папки списка (файл в подпапке пакета
			показывается как «пакет/файл.mp4»).
		path: полный путь (контракт с публикацией — путь к файлу).
		size_bytes: размер файла (в том числе для пометки «больше
			лимита сообщества»).
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
		frame: размеры кадра (ширина, высота) с учётом флага поворота;
			None — файл не прочитался. Пара едет вместе с находкой,
			чтобы карточка пакета сказала об апскейле, не запуская
			ffprobe второй раз: сканирование уже прощупало файл.
	"""

	name: str
	path: str
	size_bytes: int
	duration_s: float | None
	frame: tuple[int, int] | None


#: Поля пресета, которых у конвейера нет: имя набора и подпапка
#: результатов — это устройство хранения, а не параметры обработки.
_PRESET_ONLY_FIELDS = frozenset({"name", "subdir"})

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
	items: list[VideoFile]


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
_INTRO_KINDS_WITHOUT_VALUE = frozenset(
	{IntroSourceKind.RANDOM_MIDDLE, IntroSourceKind.RANDOM_CHOICE}
)


def build_intro_source(kind: IntroSourceKind, value: str = "") -> str:
	"""Собирает строку ``intro_source`` («вид» или «вид:значение»)."""
	if kind in _INTRO_KINDS_WITHOUT_VALUE:
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
		# молча подменять режим нельзя: человек с сохранённой картинкой
		# получил бы чужой случайный кадр и ни следа о причине
		logger.warning("Пресет: неизвестный источник заставки %r — берём случайный кадр.", source)
		return IntroSourceKind.RANDOM_MIDDLE, ""


#: Стандартные имена папок видео в media/ (настройка пуста — берутся они).
#: Публичный: страница «Настройки» строит из них подсказки — имена
#: не должны разъезжаться между движком и интерфейсом.
VIDEO_DIR_DEFAULTS: dict[SettingKey[str], str] = {
	VIDEO_SOURCE_DIR: "source",
	VIDEO_PROCESSED_DIR: "processed",
	VIDEO_PUBLISHED_DIR: "published",
	VIDEO_QUEUED_DIR: "queued",
}


def video_base_dir(settings: SettingsService, key: SettingKey[str]) -> Path:
	"""Действующий корень папки видео: настройка или стандарт в media/.

	Единственный источник правила «настройка пуста — media/<имя>»:
	им пользуются и подготовка видео (``VideoService``), и перенос
	после публикации (``PostsService``) — понимание, где лежат папки,
	не должно разъезжаться между сервисами.
	"""
	custom = settings.cached(key)
	return Path(custom) if custom else media_dir() / VIDEO_DIR_DEFAULTS[key]


def prune_empty_dirs(start: Path, root: Path, mirror_root: Path | None = None) -> None:
	"""Удаляет опустевшие папки от ``start`` вверх до ``root`` (корень цел).

	Уборка за ушедшим файлом (ADR-0016, «Уборка опустевших папок»):
	каждая папка удаляется, только если она пуста — ``rmdir`` атомарно
	отказывает непустой, поэтому гонка с параллельной записью безопасна,
	а первая непустая папка останавливает подъём.

	``mirror_root`` — корень зеркального дерева папки очереди: пока
	в зеркале уровня лежат файлы, элементы очереди могут вернуть их
	обратно (отмена, снятие ошибки), и уровень сохраняется. Любая ошибка
	файловой системы просто прекращает уборку — папка остаётся, это
	безопасный исход.
	"""
	try:
		current = start.resolve()
		resolved_root = root.resolve()
	except OSError:
		return
	while current != resolved_root and resolved_root in current.parents:
		if mirror_root is not None:
			mirror = mirror_root / current.relative_to(resolved_root)
			try:
				if mirror.is_dir() and any(mirror.iterdir()):
					return
			except OSError:
				return
		try:
			current.rmdir()
		except OSError:
			return  # непуста или занята — выше подниматься нет смысла
		current = current.parent


def sanitize_subdir(name: str) -> str:
	"""Очищает имя подпапки: без разделителей путей и спецсимволов ОС.

	Крайние точки и пробелы срезаются (Windows их не терпит в именах),
	результат ограничен ``SUBDIR_MAX_CHARS`` (длина колонки).
	Пустой результат — «без подпапки».
	"""
	# единый перечень запрещённых символов — captions.FORBIDDEN_NAME_CHARS
	cleaned = "".join(ch for ch in name if ch not in FORBIDDEN_NAME_CHARS)
	return cleaned.strip(" .")[:SUBDIR_MAX_CHARS]


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

	``target_resolution``: ступень разрешения итогового кадра — число
	по короткой стороне (``RESOLUTION_STEPS``); None — «как в оригинале»,
	кадр не масштабируется.
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
	target_resolution: int | None = DEFAULT_RESOLUTION
	meta_comment: str | None = None  # тег comment: «ссылка на канал — описание»
	subdir: str = ""  # подпапка внутри базовых папок видео (пусто — без неё)


#: Формат штампа времени конвейера: имена результатов
#: (``<исходник>_<пресет>_<штамп>.mp4``) и подпапок пакетов. Связан
#: контрактом с ``_PIPELINE_SUFFIX`` сервиса подписей
#: (``title_from_filename`` вырезает суффикс по этому виду) — связка
#: закреплена тестом ``test_title_from_filename_matches_pipeline_stamp``.
PIPELINE_STAMP_FORMAT = "%Y%m%d-%H%M%S"


def batch_subdir_name(root: str, now: datetime | None = None) -> str:
	"""Имя подпапки пакета обработки: «штамп_имя-папки-источника».

	Правило раскладки результатов пакета на диске (ADR-0014, п. 4) —
	контракт движка, а не интерфейса: штамп даёт уникальность
	и хронологическую сортировку, имя папки-источника — узнаваемость.
	Спецсимволы вычищает движок при применении (``sanitize_subdir``).
	"""
	stamp = (now or datetime.now()).strftime(PIPELINE_STAMP_FORMAT)
	return f"{stamp}_{Path(root).name}"


def _preset_field_names() -> list[str]:
	"""Имена полей пресета — контракт «PresetFields зеркалит video_presets».

	Единая точка соответствия для обоих направлений (сохранение и чтение):
	новое поле достаточно добавить в dataclass и в модель — маппинг
	подхватит его сам, без правки в двух местах разными способами.
	"""
	return [field.name for field in dataclass_fields(PresetFields)]


def _preset_values(fields: PresetFields) -> dict[str, Any]:
	"""Значения полей пресета для записи в ORM (парный — чтение в
	:meth:`VideoService.get_preset_fields`)."""
	return {name: getattr(fields, name) for name in _preset_field_names()}


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

	def _bitrate_for(
		self, info: VideoInfo, limit: int, trim_start: float, trim_end: float
	) -> BitrateAdvice:
		"""Совет по битрейту для исходника, не влезающего в лимит.

		Общий шаг двух подсказок (:meth:`bitrate_advice` для очереди
		обработки и :meth:`source_advice` для карточки файла). Сами
		подсказки различаются тем, как относятся к невозможности совета:
		одна поднимает ошибку, другая молча показывает размеры кадра —
		и это различие сознательное, закреплённое тестом.

		Raises:
			VideoError: Даже минимальный битрейт не впишет видео в лимит.
		"""
		duration = trimmed_info(info, trim_start, trim_end).duration
		return BitrateAdvice(limit_gb(limit), recommended_bitrate_kbps(duration, limit))

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
		# is_file/stat — обращения к диску: вне цикла событий движка
		if not await asyncio.to_thread(path.is_file):
			return None
		limit = userbot_max_file_bytes(self._userbot_premium())
		if (await asyncio.to_thread(path.stat)).st_size <= limit:
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
		return self._bitrate_for(info, limit, trim_start, trim_end)

	async def source_advice(
		self, source_path: str, trim_start: float = 0.0, trim_end: float = 0.0
	) -> SourceAdvice | None:
		"""Размеры кадра исходника и совет по битрейту — одной пробой ffprobe.

		Для карточки файла на странице «Видео»: по размерам она говорит
		об апскейле, по совету — подставляет качество. Отдельно от
		:meth:`bitrate_advice` (им пользуется очередь обработки, которой
		размеры кадра не нужны и лишняя проба ни к чему).

		Returns:
			Сведения об исходнике; None — файла нет или он не читается:
			подсказки вспомогательные, шуметь ошибкой из-за них незачем.
		"""
		path = Path(source_path)
		# is_file/stat — обращения к диску: вне цикла событий движка
		if not await asyncio.to_thread(path.is_file):
			return None
		try:
			info = await asyncio.to_thread(
				probe_video, source_path, ffprobe_bin_for(self._ffmpeg())
			)
		except (OSError, RuntimeError, ValueError):
			logger.warning(
				"Подсказки по исходнику: файл %s не прочитан.", source_path, exc_info=True
			)
			return None
		limit = userbot_max_file_bytes(self._userbot_premium())
		bitrate: BitrateAdvice | None = None
		if (await asyncio.to_thread(path.stat)).st_size > limit:
			try:
				bitrate = self._bitrate_for(info, limit, trim_start, trim_end)
			except (VideoError, ValueError):
				# «не вписать даже минимальным качеством» и «обрезка съела
				# всё видео» — не повод скрывать размеры кадра: обе причины
				# скажет честной ошибкой сама обработка
				logger.info("Совет по битрейту для %s невозможен.", source_path, exc_info=True)
		return SourceAdvice(info.width, info.height, bitrate)

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
		values = _preset_values(fields)
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
		return PresetFields(**{name: getattr(preset, name) for name in _preset_field_names()})

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
			# диск (в т.ч. сетевой/заснувший) — вне цикла событий движка,
			# как у соседей list_processed/scan_sources
			await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
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
		items = await asyncio.to_thread(self._scan_videos, directory)
		items.sort(key=lambda item: item.modified_at, reverse=True)
		return ProcessedListing(str(directory), items)

	@staticmethod
	def _scan_videos(directory: Path) -> list[VideoFile]:
		"""Блокирующий рекурсивный обход папки с видео (в потоке).

		Один обход на оба списка — готовые результаты и источник пакета
		отправки: прежде это были два почти одинаковых сканера, и любая
		правка правил обхода требовала помнить про второй.
		Несуществующая папка — пустой список: подпапка результатов
		создаётся при первой обработке, и до неё показывать нечего.
		Порядок — как отдал обход; сортировку выбирает вызывающий.
		"""
		if not directory.is_dir():
			return []
		items: list[VideoFile] = []
		for path in _walk_videos(directory):
			try:
				stat = path.stat()
			except OSError:  # файл исчез между обходом и stat()
				logger.warning("Файл %s исчез во время обхода — пропущен.", path)
				continue
			items.append(
				VideoFile(
					path.relative_to(directory).as_posix(),
					str(path),
					stat.st_size,
					datetime.fromtimestamp(stat.st_mtime),
				)
			)
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
		# is_dir и which — обращения к диску: вне цикла событий движка
		if not await asyncio.to_thread(directory.is_dir):
			raise VideoError(f"Папка не найдена: {root}")
		await asyncio.to_thread(self._require_ffmpeg)
		return await asyncio.to_thread(self._scan_sources, directory, on_progress)

	async def scan_ready(self, root: str) -> list[VideoFile]:
		"""Рекурсивно ищет видео в готовой папке (для пакетной отправки).

		В отличие от :meth:`scan_sources` папки результатов не исключаются
		(они и есть источник пакета отправки, ADR-0015) и ffprobe
		не вызывается — только имя, путь и размер. Скрытые файлы и папки,
		недописанные ``.part`` пропускаются. Обход блокирующий —
		выполняется в отдельном потоке.

		Raises:
			VideoError: Папка не существует.
		"""
		directory = Path(root)
		if not await asyncio.to_thread(directory.is_dir):  # диск — вне цикла
			raise VideoError(f"Папка не найдена: {root}")
		return await asyncio.to_thread(self._scan_videos, directory)

	async def ready_from_paths(self, paths: list[str]) -> list[VideoFile]:
		"""Готовые видео из явного списка путей (выбор на странице «Видео»).

		Парный вход к :meth:`scan_ready` — источник пакета публикации
		держит движок (ADR-0015), интерфейс передаёт только пути. Папка
		не сканируется, размеры читаются здесь; исчезнувшие файлы
		пропускаются с предупреждением в лог. Порядок — по имени файла,
		как при обработке: список «Готовых видео» показывает новые сверху,
		и без сортировки раскладка отдала бы ранние слоты последним
		обработанным.
		"""

		def build() -> list[VideoFile]:
			files: list[VideoFile] = []
			for path in paths:
				try:
					stat = Path(path).stat()
				except OSError:
					logger.warning("Пакет публикации: файл %s исчез — пропущен.", path)
					continue
				files.append(
					VideoFile(
						Path(path).name,
						path,
						stat.st_size,
						datetime.fromtimestamp(stat.st_mtime),
					)
				)
			files.sort(key=lambda video: video.name.casefold())
			return files

		return await asyncio.to_thread(build)

	def _scan_sources(self, directory: Path, on_progress: ScanProgress | None) -> list[FoundVideo]:
		"""Блокирующий обход папки и пробы файлов (выполняется в потоке)."""
		excluded = [
			video_base_dir(self._settings, key).resolve()
			for key in (VIDEO_PROCESSED_DIR, VIDEO_PUBLISHED_DIR, VIDEO_QUEUED_DIR)
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
			frame: tuple[int, int] | None
			try:
				info = probe_video(str(path), ffprobe)
			except (OSError, RuntimeError, ValueError):
				# нечитаемый файл показывается в списке с пометкой —
				# решать, что с ним делать, будет человек
				logger.warning("Сканирование: файл %s не прочитан ffprobe.", path, exc_info=True)
				duration, frame = None, None
			else:
				duration, frame = info.duration, (info.width, info.height)
			found.append(
				FoundVideo(path.relative_to(directory).as_posix(), str(path), size, duration, frame)
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
		await asyncio.to_thread(self._remove_and_prune, target, root)
		logger.info("Готовое видео удалено: %s", path)

	@staticmethod
	def _remove_with_preview(target: Path) -> None:
		"""Блокирующее удаление файла и его превью (выполняется в потоке).

		Raises:
			VideoError: Удаление не удалось (права, файл занят).
		"""
		try:
			target.unlink(missing_ok=True)
			preview_path(target).unlink(missing_ok=True)
		except OSError as exc:
			raise VideoError(f"Не удалось удалить файл: {exc.strerror or exc}") from exc

	def _remove_and_prune(self, target: Path, root: Path) -> None:
		"""Удаление с уборкой опустевших папок результатов (в потоке).

		Уровень убирается, только если его зеркало в дереве очереди пусто
		или отсутствует: элементы очереди могут вернуть файлы при отмене
		(ADR-0016, «Уборка опустевших папок»).
		"""
		self._remove_with_preview(target)
		queued_root = video_base_dir(self._settings, VIDEO_QUEUED_DIR)
		prune_empty_dirs(target.parent, root, mirror_root=queued_root)

	async def processed_dir_for_community(self, community_id: int) -> str:
		"""Папка результатов канала: подпапка его пресета по умолчанию.

		Для диалога выбора видео на «Публикации». Нет пресета (или он
		удалён) — корень папки результатов.
		"""
		subdir = ""
		preset_id = await self._settings.get_for(COMMUNITY_DEFAULT_PRESET, community_id)
		if preset_id is not None:
			async with self._db.session_factory() as session:
				preset = await session.get(VideoPreset, preset_id)
			if preset is not None:
				subdir = preset.subdir
		return (await self.dirs_for(subdir)).processed

	async def shutdown(self) -> None:
		"""Убирает временную папку кадров-кандидатов (при остановке движка)."""
		if self._candidates_dir is not None:
			await asyncio.to_thread(shutil.rmtree, self._candidates_dir, ignore_errors=True)
			self._candidates_dir = None

	async def delete_preset(self, preset_id: int) -> None:
		"""Удаляет пресет и снимает его у каналов, где он был по умолчанию.

		Целостность настройки-ссылки держит сервис (ADR-0013, вариант «а»):
		внешнего ключа у строки настройки нет, поэтому ссылки чистятся здесь.
		"""
		# порядок важен: сначала снять ссылки, потом удалить пресет — сбой
		# между шагами оставит пресет без ссылок (безвредно), а не ссылки
		# каналов на несуществующий пресет
		await self._settings.drop_community_value(COMMUNITY_DEFAULT_PRESET, preset_id)
		async with self._db.session_factory() as session:
			await session.execute(delete(VideoPreset).where(VideoPreset.id == preset_id))
			await session.commit()

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
		await self.ensure_ready([source_path])
		source = Path(source_path)
		# сборка включает создание папки результата (диск) — вне цикла
		options = await asyncio.to_thread(
			self._build_options, source, fields, intro_source, extra_subdir
		)
		logger.info("Обработка видео: %s (параметры «%s»)…", source.name, fields.name)
		try:
			await asyncio.to_thread(self._processor, options, on_progress)
		except (RuntimeError, ValueError, OSError) as exc:
			# OSError — диск полон, права, сетевой диск: доменный текст
			# вместо «внутренней ошибки» в карточке очереди
			raise VideoError(f"Обработка не удалась: {exc}") from exc
		logger.info("Видео готово: %s", options.output)
		return options.output

	async def extract_random_frames(
		self,
		source_path: str,
		count: int = 6,
		trim_start: float = 0.0,
		trim_end: float = 0.0,
		*,
		target_resolution: int | None,
	) -> list[FrameCandidate]:
		"""Выдёргивает случайные кадры-кандидаты заставки (5–95 % длительности).

		Кадры пишутся PNG точно в размере итогового кадра — выбранный файл
		уходит в обработку как есть (``image:<путь>``), без повторного
		извлечения. Поэтому ступень разрешения обязательна и должна быть
		той же, с какой пойдёт обработка: иначе склейка заставки с видео
		(xfade) упрётся в расхождение размеров. Партией владеет сервис:
		предыдущая удаляется при каждом новом запросе, так что за сессию
		живёт максимум одна папка.
		При обрезке (``trim_start``/``trim_end``) кандидаты берутся только
		из обрезанного диапазона, время — от обрезанной версии.

		Raises:
			VideoError: Файл/ffmpeg не найдены, обрезка съедает всё видео
				или извлечение упало.
		"""
		await self.ensure_ready([source_path])

		def _fresh_candidates_dir() -> str:
			# rmtree/mkdtemp — диск: вне цикла событий движка
			if self._candidates_dir is not None:
				shutil.rmtree(self._candidates_dir, ignore_errors=True)
			return tempfile.mkdtemp(prefix="pxcontrol-frames-")

		self._candidates_dir = await asyncio.to_thread(_fresh_candidates_dir)
		try:
			return await asyncio.to_thread(
				self._extract_candidates,
				source_path,
				count,
				self._candidates_dir,
				trim_start,
				trim_end,
				target_resolution,
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
		target_resolution: int | None,
	) -> list[FrameCandidate]:
		"""Блокирующее извлечение кадров (выполняется в отдельном потоке).

		Raises:
			ValueError: Обрезка не оставляет от ролика ничего.
		"""
		info = probe_video(source_path, ffprobe_bin_for(self._ffmpeg()))
		work_info = trimmed_info(info, trim_start, trim_end)
		# правило «размер от обрезанной версии, кадр из исходника» —
		# в чистом модуле, одно на заставку и на выбор кадра человеком
		frames = extract_candidates(
			source_path,
			work_info,
			count,
			out_dir,
			target_resolution,
			self._ffmpeg(),
			trim_start,
		)
		return [FrameCandidate(timestamp, path) for timestamp, path in frames]

	async def ensure_ready(self, source_paths: Sequence[str]) -> None:
		"""Пакетно проверяет: ffmpeg доступен, каждый исходник существует.

		Обращения к диску (``which`` перебирает каталоги PATH, ``is_file`` —
		stat) идут одним заходом в отдельном потоке: постановка пакета
		из десятков файлов не держит цикл событий движка (норма
		«файловые операции — вне цикла», аудит 26.08); ffmpeg
		проверяется один раз на пакет, а не на файл.

		Raises:
			VideoError: ffmpeg или один из файлов не найдены.
		"""

		def _check_all() -> None:
			self._require_ffmpeg()
			for source_path in source_paths:
				if not Path(source_path).is_file():
					raise VideoError(f"Файл не найден: {source_path}")

		await asyncio.to_thread(_check_all)

	def _require_ffmpeg(self) -> None:
		"""Проверяет, что на месте оба инструмента: ffmpeg и ffprobe.

		Путь к ffprobe производный — «сосед ffmpeg». Раньше проверялся
		только ffmpeg, и отсутствие ffprobe выглядело как «в папке
		полно битых файлов»: проба падала на каждом файле по отдельности,
		а настоящая причина была видна только в журнале.

		Raises:
			VideoError: Инструмент не найден.
		"""
		for tool in (self._ffmpeg(), ffprobe_bin_for(self._ffmpeg())):
			if shutil.which(tool) is None:
				raise VideoError(
					f"Не найден {Path(tool).name} («{tool}») — установите его "
					"или укажите путь к ffmpeg в «Настройки → Общие»."
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
		stamp = datetime.now().strftime(PIPELINE_STAMP_FORMAT)
		# имя пресета — свободный текст: чистим спецсимволы ОС (как подпапку)
		# и меняем «_» на «-», чтобы суффикс _<пресет>_<штамп> оставался
		# разборчивым для title_from_filename (captions)
		preset_part = sanitize_subdir(fields.name).replace("_", "-").strip(" .") or "preset"
		output = out_dir / f"{source.stem}_{preset_part}_{stamp}.mp4"
		# имена полей пресета и параметров обработки совпадают, поэтому
		# перечислять два десятка присваиваний не нужно: новое поле
		# пресета доходит до конвейера само. В сторону БД то же самое
		# уже сделано интроспекцией (см. _preset_field_names)
		pipeline_fields = {
			field.name: getattr(fields, field.name)
			for field in dataclass_fields(fields)
			if field.name not in _PRESET_ONLY_FIELDS
		}
		# заставку вызывающий может задать поверх пресета (выбранный кадр)
		pipeline_fields["intro_source"] = intro_source or fields.intro_source
		return ProcessingOptions(
			input=str(source),
			output=str(output),
			ffmpeg_bin=self._ffmpeg(),
			ffprobe_bin=ffprobe_bin_for(self._ffmpeg()),
			**pipeline_fields,
		)
