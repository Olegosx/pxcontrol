"""Тесты VideoService: пресеты и запуск подготовки (без реального ffmpeg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.services.video import PresetFields, VideoError, VideoService
from pxcontrol.engine.video import ProcessingOptions

FIELDS = PresetFields(
	name="Бренд", watermark_path="/tmp/logo.png", wm_corner="br",
	wm_opacity=0.8, intro=True, intro_source="time:5.0", cover=True,
	video_bitrate_kbps=2500,
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
	assert "вотермарк (br)" in preset.summary and "заставка" in preset.summary
	assert "2.5 Мбит/с" in preset.summary
	fields = await service.get_preset_fields(preset.id)
	assert fields.intro_source == "time:5.0" and fields.wm_opacity == 0.8
	assert fields.video_bitrate_kbps == 2500
	updated = await service.save_preset(
		PresetFields(name="Бренд-2", no_audio=True), preset.id
	)
	assert updated.name == "Бренд-2" and "без звука" in updated.summary
	await service.delete_preset(preset.id)
	assert await service.list_presets() == []


async def test_prepare_maps_preset_to_options(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Подготовка: пресет из БД корректно превращается в ProcessingOptions."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "исходник.mp4"
	source.write_bytes(b"src")
	processor = _FakeProcessor()
	service = VideoService(db, "ffmpeg", processor=processor)
	preset = await service.save_preset(FIELDS)

	output = await service.prepare(str(source), preset.id)

	assert Path(output).is_file()
	options = processor.calls[0]
	assert options.input == str(source)
	assert options.watermark == "/tmp/logo.png"
	assert options.wm_corner == "br" and options.wm_opacity == 0.8
	assert options.intro and options.intro_source == "time:5.0"
	assert options.cover is True
	assert options.video_bitrate_kbps == 2500
	assert "processed" in options.output and "исходник" in options.output


async def test_prepare_reports_progress(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Колбэк прогресса пробрасывается до процессора и получает доли."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	preset = await service.save_preset(PresetFields(name="Простой"))
	received: list[float] = []
	await service.prepare(str(source), preset.id, on_progress=received.append)
	assert received == [0.5, 1.0]


async def test_prepare_intro_source_override(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Подмена источника кадра действует на один запуск, пресет не меняется."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")
	processor = _FakeProcessor()
	service = VideoService(db, "ffmpeg", processor=processor)
	preset = await service.save_preset(PresetFields(
		name="Выбор", intro=True, intro_source="random-choice",
	))
	await service.prepare(str(source), preset.id, intro_source="image:/x/кадр.png")
	assert processor.calls[0].intro_source == "image:/x/кадр.png"
	fields = await service.get_preset_fields(preset.id)
	assert fields.intro_source == "random-choice"  # пресет не тронут


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
		_src: str, timestamp: float, out: str, width: int, height: int,
		_bin: str = "ffmpeg",
	) -> None:
		assert (width, height) == (1920, 1080)  # финальный размер кадра
		assert 5.0 <= timestamp <= 95.0
		Path(out).write_bytes(b"png")

	monkeypatch.setattr(
		"pxcontrol.engine.services.video.extract_still", _fake_extract
	)
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


async def test_prepare_validations(db: Database, tmp_path: Path) -> None:
	"""Понятные ошибки: нет файла, нет ffmpeg, нет пресета."""
	service = VideoService(db, "ffmpeg", processor=_FakeProcessor())
	with pytest.raises(VideoError, match="Файл не найден"):
		await service.prepare(str(tmp_path / "нет.mp4"), 1)

	source = tmp_path / "есть.mp4"
	source.write_bytes(b"src")
	no_ffmpeg = VideoService(db, "/нет/такого/ffmpeg", processor=_FakeProcessor())
	with pytest.raises(VideoError, match="ffmpeg"):
		await no_ffmpeg.prepare(str(source), 1)


async def test_prepare_wraps_processor_errors(
	db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Падение ffmpeg превращается в VideoError с текстом причины."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.shutil.which", lambda _b: "/usr/bin/ffmpeg"
	)
	monkeypatch.setattr(
		"pxcontrol.engine.services.video.media_dir", lambda: tmp_path / "media"
	)
	source = tmp_path / "src.mp4"
	source.write_bytes(b"src")

	def _boom(_options: ProcessingOptions, _on_progress: object = None) -> None:
		raise RuntimeError("ffmpeg (обработка видео) завершился с ошибкой: тест")

	service = VideoService(db, "ffmpeg", processor=_boom)
	preset = await service.save_preset(PresetFields(name="Пустой"))
	with pytest.raises(VideoError, match="Обработка не удалась"):
		await service.prepare(str(source), preset.id)
