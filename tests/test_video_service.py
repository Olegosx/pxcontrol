"""Тесты VideoService: пресеты и запуск подготовки (без реального ffmpeg)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Channel
from pxcontrol.engine.services.settings import (
	CHANNEL_DEFAULT_PRESET,
	VIDEO_PROCESSED_DIR,
	SettingsService,
)
from pxcontrol.engine.services.video import PresetFields, VideoError, VideoService
from pxcontrol.engine.video import ProcessingOptions

FIELDS = PresetFields(
	name="Бренд",
	trim_start=3.5,
	trim_end=1.5,
	fade_in=0.5,
	fade_out=1.0,
	watermark_path="/tmp/logo.png",
	wm_corner="br",
	wm_opacity=0.8,
	wm_start_offset=2.0,
	wm_end_offset=15.0,
	wm_fade=1.5,
	intro=True,
	intro_source="time:5.0",
	cover=True,
	video_bitrate_kbps=2500,
	meta_comment="https://t.me/mych — мой канал",
)


class _FakeProcessor:
	"""Подмена process(): фиксирует параметры, создаёт файл результата."""

	def __init__(self) -> None:
		self.calls: list[ProcessingOptions] = []

	def __call__(
		self,
		options: ProcessingOptions,
		on_progress: object = None,
	) -> None:
		self.calls.append(options)
		if callable(on_progress):
			on_progress(0.5)
			on_progress(1.0)
		Path(options.output).parent.mkdir(parents=True, exist_ok=True)
		Path(options.output).write_bytes(b"video")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'video.db'}")
	await database.init()
	yield database
	await database.close()


async def test_preset_crud(db: Database) -> None:
	"""Пресет создаётся, читается для правки, обновляется и удаляется."""
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	preset = await service.save_preset(FIELDS)
	assert preset.name == "Бренд"
	fields = await service.get_preset_fields(preset.id)
	assert fields.intro_source == "time:5.0" and fields.wm_opacity == 0.8
	assert fields.video_bitrate_kbps == 2500
	assert fields.trim_start == 3.5 and fields.trim_end == 1.5
	assert fields.fade_in == 0.5 and fields.fade_out == 1.0
	assert fields.subdir == "Бренд"  # авто из имени при создании
	updated = await service.save_preset(PresetFields(name="Бренд-2", no_audio=True), preset.id)
	assert updated.name == "Бренд-2"
	await service.delete_preset(preset.id)
	assert await service.list_presets() == []


def test_sanitize_subdir_strips_forbidden() -> None:
	"""Разделители путей и спецсимволы ОС вычищаются, края обрезаются."""
	from pxcontrol.engine.services.video import sanitize_subdir

	assert sanitize_subdir("Мой/канал\\..") == "Мойканал"
	assert sanitize_subdir('a:b*c?d"e<f>g|h') == "abcdefgh"
	assert sanitize_subdir("  .обычное имя.  ") == "обычное имя"
	assert sanitize_subdir("") == ""


async def test_preset_subdir_auto_on_create_and_editable(db: Database) -> None:
	"""Создание с пустой подпапкой — авто из имени; правка не перетирается."""
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	preset = await service.save_preset(PresetFields(name="Канал/Тест"))
	fields = await service.get_preset_fields(preset.id)
	assert fields.subdir == "КаналТест"  # авто + очистка
	# явная правка сохраняется как есть (в т.ч. пустая — «без подпапки»)
	await service.save_preset(PresetFields(name="Канал/Тест", subdir=""), preset.id)
	assert (await service.get_preset_fields(preset.id)).subdir == ""


async def test_prepare_uses_processed_dir_setting_and_subdir(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Результат кладётся в <настройка video_processed_dir>/<подпапка пресета>."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	settings = SettingsService(db)
	await settings.set(VIDEO_PROCESSED_DIR, str(tmp_path / "мои-результаты"))
	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	output = await service.prepare(str(source), PresetFields(name="Тест", subdir="паб"))
	assert Path(output).parent == tmp_path / "мои-результаты" / "паб"


async def test_dirs_for_and_processed_dir_for_channel(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""dirs_for создаёт папки; папка результатов канала — из его пресета."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	settings = SettingsService(db)
	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	dirs = await service.dirs_for("суб")
	assert dirs.source == str(tmp_path / "media" / "source" / "суб")
	assert Path(dirs.processed).is_dir() and Path(dirs.published).is_dir()
	# канал без пресета — корень результатов
	async with db.session_factory() as session:
		channel = Channel(title="Канал", tg_chat_id="-1001")
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
	root = await service.processed_dir_for_channel(channel.id)
	assert root == str(tmp_path / "media" / "processed")
	# канал с пресетом — подпапка пресета
	preset = await service.save_preset(PresetFields(name="Суб", subdir="суб"))
	await settings.set_for(CHANNEL_DEFAULT_PRESET, channel.id, preset.id)
	assert (await service.processed_dir_for_channel(channel.id)).endswith("/суб")


async def test_list_processed_shows_whole_subdir(db: Database, tmp_path: Path) -> None:
	"""Список — все видео подпапки (новые сверху), посторонние файлы — мимо."""
	settings = SettingsService(db)
	await settings.set(VIDEO_PROCESSED_DIR, str(tmp_path / "результаты"))
	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	folder = tmp_path / "результаты" / "паб"
	folder.mkdir(parents=True)
	old, new = folder / "старое.mp4", folder / "новое.mkv"
	old.write_bytes(b"a" * 10)
	new.write_bytes(b"b" * 20)
	os.utime(old, (1_700_000_000, 1_700_000_000))
	os.utime(new, (1_800_000_000, 1_800_000_000))
	(folder / "новое.png").write_bytes(b"preview")  # превью — не видео
	(folder / "заметка.txt").write_bytes(b"note")

	listing = await service.list_processed("паб")
	assert listing.directory == str(folder)
	assert [item.name for item in listing.items] == ["новое.mkv", "старое.mp4"]
	assert listing.items[0].size_bytes == 20
	assert listing.items[0].path == str(new)
	# несуществующая подпапка — пустой список, а не ошибка
	empty = await service.list_processed("ещё-нет")
	assert empty.items == []


async def test_delete_processed_removes_preview_and_guards_root(
	db: Database, tmp_path: Path
) -> None:
	"""Удаление уносит превью; файл вне папки результатов не трогается."""
	settings = SettingsService(db)
	await settings.set(VIDEO_PROCESSED_DIR, str(tmp_path / "результаты"))
	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	folder = tmp_path / "результаты" / "паб"
	folder.mkdir(parents=True)
	video, preview = folder / "ролик.mp4", folder / "ролик.png"
	video.write_bytes(b"video")
	preview.write_bytes(b"preview")
	await service.delete_processed(str(video))
	assert not video.exists() and not preview.exists()

	outsider = tmp_path / "чужое.mp4"
	outsider.write_bytes(b"video")
	with pytest.raises(VideoError, match="только файлы из папки результатов"):
		await service.delete_processed(str(outsider))
	assert outsider.exists()


async def test_delete_preset_clears_channel_defaults(db: Database) -> None:
	"""Удаление пресета снимает его у каналов (ADR-0013, вариант «а»)."""
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	preset = await service.save_preset(FIELDS)
	keep = await service.save_preset(PresetFields(name="Другой"))
	async with db.session_factory() as session:
		channel = Channel(title="Канал", tg_chat_id="-1001")
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
	settings = SettingsService(db)
	await settings.set_for(CHANNEL_DEFAULT_PRESET, channel.id, preset.id)
	await service.delete_preset(preset.id)
	assert await settings.get_for(CHANNEL_DEFAULT_PRESET, channel.id) is None
	# другой пресет не задет
	assert [p.id for p in await service.list_presets()] == [keep.id]


async def test_prepare_maps_fields_to_options(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Подготовка применяет переданные поля (пресет в БД не нужен)."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "исходник.mp4"
	source.write_bytes(b"src")
	processor = _FakeProcessor()
	service = VideoService(db, "ffmpeg", processor=processor)

	output = await service.prepare(str(source), FIELDS)

	assert Path(output).is_file()
	options = processor.calls[0]
	assert options.input == str(source)
	assert options.trim_start == 3.5 and options.trim_end == 1.5
	assert options.fade_in == 0.5 and options.fade_out == 1.0
	assert options.watermark == "/tmp/logo.png"
	assert options.wm_corner == "br" and options.wm_opacity == 0.8
	assert options.wm_start_offset == 2.0 and options.wm_end_offset == 15.0
	assert options.wm_fade == 1.5
	assert options.intro and options.intro_source == "time:5.0"
	assert options.cover is True
	assert options.video_bitrate_kbps == 2500
	assert options.meta_comment == "https://t.me/mych — мой канал"
	assert "processed" in options.output and "исходник_Бренд_" in options.output


async def test_prepare_reports_progress(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Колбэк прогресса пробрасывается до процессора и получает доли."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	received: list[float] = []
	await service.prepare(str(source), PresetFields(name="Простой"), on_progress=received.append)
	assert received == [0.5, 1.0]


async def test_prepare_intro_source_override(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Подмена источника кадра действует на один запуск."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	processor = _FakeProcessor()
	service = VideoService(db, "ffmpeg", processor=processor)
	fields = PresetFields(name="Выбор", intro=True, intro_source="random-choice")
	await service.prepare(str(source), fields, intro_source="image:/x/кадр.png")
	assert processor.calls[0].intro_source == "image:/x/кадр.png"


async def test_extract_random_frames(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Кандидаты: количество, диапазон 5–95 %, размер кадра, смена партии."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.probe_video",
		lambda _p, _b: VideoInfo(1280, 720, 100.0, 25.0, True),
	)

	def _fake_extract(
		_src: str,
		timestamp: float,
		out: str,
		width: int,
		height: int,
		_bin: str = "ffmpeg",
	) -> None:
		assert (width, height) == (1920, 1080)  # финальный размер кадра
		assert 5.0 <= timestamp <= 95.0
		Path(out).write_bytes(b"png")

	monkeypatch.setattr("pxcontrol.engine.services.video.extract_still", _fake_extract)
	source = tmp_path / "v.mp4"
	source.write_bytes(b"v")
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	frames = await service.extract_random_frames(str(source), 4)
	assert len(frames) == 4
	assert [f.timestamp for f in frames] == sorted(f.timestamp for f in frames)
	assert all(Path(f.path).is_file() for f in frames)
	first_dir = Path(frames[0].path).parent
	second = await service.extract_random_frames(str(source), 2)
	assert len(second) == 2
	assert not first_dir.exists()  # старая партия удалена


async def test_extract_random_frames_respects_trim(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Кандидаты — из обрезанного диапазона; время — от обрезанной версии."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.probe_video",
		lambda _p, _b: VideoInfo(1280, 720, 100.0, 25.0, True),
	)
	extracted: list[float] = []

	def _fake_extract(
		_src: str,
		timestamp: float,
		out: str,
		_w: int,
		_h: int,
		_bin: str = "ffmpeg",
	) -> None:
		extracted.append(timestamp)
		Path(out).write_bytes(b"png")

	monkeypatch.setattr("pxcontrol.engine.services.video.extract_still", _fake_extract)
	source = tmp_path / "v.mp4"
	source.write_bytes(b"v")
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	frames = await service.extract_random_frames(str(source), 6, trim_start=20.0, trim_end=30.0)
	# рабочая версия — 50 с: подписи в её времени, извлечение — со сдвигом
	for frame, raw in zip(frames, extracted, strict=True):
		assert 2.5 <= frame.timestamp <= 47.5  # 5–95 % от 50 с
		assert raw == pytest.approx(20.0 + frame.timestamp)
	with pytest.raises(VideoError, match="не оставляет"):
		await service.extract_random_frames(str(source), 2, trim_start=70.0, trim_end=40.0)


async def test_ffmpeg_provider_picks_up_changes(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Путь к ffmpeg спрашивается у провайдера на каждом запуске.

	Так смена пути в настройках приложения подхватывается без
	перезапуска (ADR-0013).
	"""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	current = {"path": "/opt/one/ffmpeg"}
	processor = _FakeProcessor()
	service = VideoService(db, lambda: current["path"], processor=processor)
	await service.prepare(str(source), PresetFields(name="а"))
	assert processor.calls[0].ffmpeg_bin == "/opt/one/ffmpeg"
	current["path"] = "/opt/two/ffmpeg"  # «сменили в настройках»
	await service.prepare(str(source), PresetFields(name="б"))
	assert processor.calls[1].ffmpeg_bin == "/opt/two/ffmpeg"


async def test_prepare_validations(db: Database, tmp_path: Path) -> None:
	"""Понятные ошибки: нет файла, нет ffmpeg."""
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	fields = PresetFields(name="x")
	with pytest.raises(VideoError, match="Файл не найден"):
		await service.prepare(str(tmp_path / "нет.mp4"), fields)

	source = tmp_path / "есть.mp4"
	source.write_bytes(b"src")
	no_ffmpeg = VideoService(db, "/нет/такого/ffmpeg", processor=_FakeProcessor())
	with pytest.raises(VideoError, match="ffmpeg"):
		await no_ffmpeg.prepare(str(source), fields)


async def test_prepare_wraps_processor_errors(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Падение ffmpeg превращается в VideoError с текстом причины."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")

	def _boom(_options: ProcessingOptions, _on_progress: object = None) -> None:
		raise RuntimeError("ffmpeg (обработка видео) завершился с ошибкой: тест")

	service = VideoService(db, "ffmpeg", processor=_boom)
	with pytest.raises(VideoError, match="Обработка не удалась"):
		await service.prepare(str(source), PresetFields(name="Пустой"))


# --- рекомендация битрейта -------------------------------------------------


def test_recommended_bitrate_formula() -> None:
	"""Битрейт даёт размер «лимит минус 1 %» за вычетом аудио 192 кбит/с."""
	from pxcontrol.engine.services.video import recommended_bitrate_kbps

	# лимит 2 000 000 000 байт, час видео: бюджет 0.99 × 8 / 3600 / 1000
	kbps = recommended_bitrate_kbps(3600.0, 2_000_000_000)
	assert kbps == int(2_000_000_000 * 0.99 * 8 / 3600 / 1000) - 192
	# итоговый размер (видео + аудио) не превышает лимит
	total_bits = (kbps + 192) * 1000 * 3600
	assert total_bits / 8 <= 2_000_000_000
	with pytest.raises(VideoError, match="ffprobe"):
		recommended_bitrate_kbps(0.0, 2_000_000_000)
	with pytest.raises(VideoError, match="длинное"):
		recommended_bitrate_kbps(10**9, 2_000_000_000)  # вечное видео


async def test_bitrate_advice_only_for_oversized(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Совет даётся только файлу больше лимита; учитывает Premium и обрезку."""
	from pxcontrol.engine.services.video import BitrateAdvice
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.userbot_max_file_bytes",
		lambda premium: 200_000_000 if premium else 2_000_000,
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.probe_video",
		lambda _p, _b: VideoInfo(1920, 1080, 100.0, 25.0, True),
	)
	small = tmp_path / "small.mp4"
	small.write_bytes(b"x" * 1_000_000)
	big = tmp_path / "big.mp4"
	big.write_bytes(b"x" * 2_500_000)

	premium = False
	service = VideoService(
		db,
		"ffmpeg",
		processor=_FakeProcessor(),
		userbot_premium=lambda: premium,
	)
	# файл в лимите — совета нет; несуществующий путь — тоже
	assert await service.bitrate_advice(str(small)) is None
	assert await service.bitrate_advice(str(tmp_path / "нет.mp4")) is None
	# больше лимита — совет с формулой от длительности после обрезки
	advice = await service.bitrate_advice(str(big), trim_start=40.0, trim_end=10.0)
	assert isinstance(advice, BitrateAdvice)
	expected = int(2_000_000 * 0.99 * 8 / 50.0 / 1000) - 192
	assert advice.kbps == expected
	# Premium поднимает лимит — большой файл перестаёт требовать совета
	premium = True
	assert await service.bitrate_advice(str(big)) is None
