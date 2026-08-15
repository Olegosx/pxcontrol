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


async def test_list_processed_recurses_into_batch_subdirs(db: Database, tmp_path: Path) -> None:
	"""Результаты пакета (вложенная папка) видны с относительным именем."""
	settings = SettingsService(db)
	await settings.set(VIDEO_PROCESSED_DIR, str(tmp_path / "результаты"))
	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	folder = tmp_path / "результаты" / "паб"
	batch = folder / "20260815-1200_пакет"
	batch.mkdir(parents=True)
	flat, nested = folder / "обычное.mp4", batch / "пакетное.mp4"
	flat.write_bytes(b"a")
	nested.write_bytes(b"b")
	os.utime(flat, (1_700_000_000, 1_700_000_000))
	os.utime(nested, (1_800_000_000, 1_800_000_000))

	listing = await service.list_processed("паб")
	assert [item.name for item in listing.items] == [
		"20260815-1200_пакет/пакетное.mp4",
		"обычное.mp4",
	]
	assert listing.items[0].path == str(nested)


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


async def test_prepare_sanitizes_preset_name_in_filename(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Имя пресета в имени файла чистится и не ломает разбор суффикса.

	Разделители путей не создают лишних каталогов (раньше «Канал/Тест»
	ронял ffmpeg в самом конце кодирования), а «_» меняется на «-»,
	чтобы title_from_filename срезал суффикс _<пресет>_<штамп> целиком.
	"""
	from pxcontrol.engine.services.captions import title_from_filename

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	settings = SettingsService(db)
	await settings.set(VIDEO_PROCESSED_DIR, str(tmp_path / "res"))
	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	source = tmp_path / "Lara Croft.mp4"
	source.write_bytes(b"src")
	output = await service.prepare(str(source), PresetFields(name="Канал/Тест_HD", subdir="паб"))
	assert Path(output).parent == tmp_path / "res" / "паб"
	assert "_КаналТест-HD_" in Path(output).name
	assert title_from_filename(output) == "Lara Croft"


async def test_prepare_extra_subdir_for_batch(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Подпапка пакета вкладывается в подпапку пресета (и чистится)."""
	monkeypatch.setattr("pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media")
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	output = await service.prepare(
		str(source),
		PresetFields(name="Тест", subdir="паб"),
		extra_subdir="2026/пакет",  # разделитель пути вычищается
	)
	assert Path(output).parent == tmp_path / "media" / "processed" / "паб" / "2026пакет"
	# пустая подпапка пакета — прежнее поведение
	plain = await service.prepare(str(source), PresetFields(name="Тест", subdir="паб"))
	assert Path(plain).parent == tmp_path / "media" / "processed" / "паб"


# --- сканирование исходников (пакетная обработка) ---------------------------


async def test_scan_sources_finds_videos_recursively(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Рекурсивный поиск: фильтр расширений, скрытое и служебное — мимо."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.probe_video",
		lambda path, _b: VideoInfo(1920, 1080, 42.0, 25.0, True),
	)
	root = tmp_path / "исходники"
	(root / "вложенная").mkdir(parents=True)
	(root / ".скрытая").mkdir()
	(root / "а.mp4").write_bytes(b"v" * 10)
	(root / "вложенная" / "б.MOV").write_bytes(b"v" * 20)  # регистр не важен
	(root / ".скрытая" / "мимо.mp4").write_bytes(b"v")  # скрытая папка
	(root / ".тайное.mp4").write_bytes(b"v")  # скрытый файл
	(root / "заметка.txt").write_bytes(b"t")  # не видео
	(root / "недописанное.mp4.part").write_bytes(b"v")  # черновик ffmpeg

	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	progress: list[tuple[int, int]] = []
	found = await service.scan_sources(str(root), on_progress=lambda i, n: progress.append((i, n)))

	assert [video.name for video in found] == ["а.mp4", "вложенная/б.MOV"]
	assert found[0].size_bytes == 10 and found[0].duration_s == 42.0
	assert found[1].path == str(root / "вложенная" / "б.MOV")
	assert progress == [(1, 2), (2, 2)]


async def test_scan_sources_excludes_results_dirs_and_marks_unreadable(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Папки результатов/опубликованных не обходятся; битый файл — с None."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)

	def _probe(path: str, _b: str) -> VideoInfo:
		if "битое" in path:
			raise RuntimeError("ffprobe завершился с ошибкой")
		return VideoInfo(1920, 1080, 42.0, 25.0, True)

	monkeypatch.setattr("pxcontrol.engine.services.video.probe_video", _probe)
	root = tmp_path / "медиа"
	settings = SettingsService(db)
	await settings.set(VIDEO_PROCESSED_DIR, str(root / "processed"))
	(root / "processed").mkdir(parents=True)
	(root / "processed" / "готовое.mp4").write_bytes(b"v")  # уже результат
	(root / "хорошее.mp4").write_bytes(b"v")
	(root / "битое.mp4").write_bytes(b"v")

	service = VideoService(db, "ffmpeg", settings=settings, processor=_FakeProcessor())
	found = await service.scan_sources(str(root))
	assert [video.name for video in found] == ["битое.mp4", "хорошее.mp4"]
	assert found[0].duration_s is None  # нечитаемый — с пометкой, не ошибка
	assert found[1].duration_s == 42.0


async def test_scan_sources_requires_existing_dir_and_ffmpeg(db: Database, tmp_path: Path) -> None:
	"""Понятные ошибки: нет папки, нет ffmpeg."""
	service = VideoService(db, "/нет/такого/ffmpeg", processor=_FakeProcessor())
	with pytest.raises(VideoError, match="Папка не найдена"):
		await service.scan_sources(str(tmp_path / "нет"))
	with pytest.raises(VideoError, match="ffmpeg"):
		await service.scan_sources(str(tmp_path))


async def test_scan_ready_lists_videos_without_probe(db: Database, tmp_path: Path) -> None:
	"""Готовая папка: рекурсивный список с размером, без ffprobe и исключений."""
	root = tmp_path / "результаты" / "паб" / "пакет"
	(root / "вложенная").mkdir(parents=True)
	(root / "а.mp4").write_bytes(b"v" * 10)
	(root / "вложенная" / "б.mkv").write_bytes(b"v" * 20)
	(root / ".скрытое.mp4").write_bytes(b"v")
	(root / "черновик.mp4.part").write_bytes(b"v")
	(root / "превью.png").write_bytes(b"p")

	# ffmpeg сознательно «не найден»: scan_ready он не нужен
	service = VideoService(db, "/нет/такого/ffmpeg", processor=_FakeProcessor())
	found = await service.scan_ready(str(root))
	assert [(video.name, video.size_bytes) for video in found] == [
		("а.mp4", 10),
		("вложенная/б.mkv", 20),
	]
	with pytest.raises(VideoError, match="Папка не найдена"):
		await service.scan_ready(str(tmp_path / "нет"))
