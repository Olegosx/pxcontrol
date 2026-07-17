"""Тесты чистых функций подготовки видео (портированы из makeVideo)."""

from __future__ import annotations

import random
from dataclasses import asdict
from pathlib import Path

import pytest

from pxcontrol.engine.services.video import PresetFields
from pxcontrol.engine.video.filtergraph import (
	WatermarkOptions,
	_enable_expr,
	_overlay_position,
	build_filter_complex,
)
from pxcontrol.engine.video.frames import resolve_timestamp
from pxcontrol.engine.video.pipeline import ProcessingOptions
from pxcontrol.engine.video.probe import VideoInfo, _parse_fps

WM = WatermarkOptions(
	corner="tr", margin=24, opacity=1.0, scale=0.15, start=None, end=None
)
INFO = VideoInfo(width=1920, height=1080, duration=100.0, fps=25.0, has_audio=True)


def _options(**overrides: object) -> ProcessingOptions:
	"""Хелпер: ProcessingOptions из умолчаний пресета.

	Единственный источник умолчаний — ``PresetFields``; фабрика повторяет
	отображение полей из ``VideoService._build_options``.
	"""
	fields = asdict(PresetFields(name="тест"))
	fields.pop("name")
	fields.pop("subdir")  # уровень сервиса: конвейер получает готовый путь вывода
	fields["watermark"] = fields.pop("watermark_path")
	merged: dict[str, object] = {"input": "a", "output": "b", **fields, **overrides}
	return ProcessingOptions(**merged)  # type: ignore[arg-type]


def _build(**kwargs: object) -> object:
	"""Хелпер: вызывает build_filter_complex со значениями по умолчанию."""
	defaults: dict[str, object] = {
		"fps": "25.00000", "width": 1920, "height": 1080, "duration": 100.0,
		"hold": 1.0, "xfade": 0.5, "still_index": None,
		"wm": None, "wm_index": None, "has_audio": False,
		"fade_in": 0.0, "fade_out": 0.0,
	}
	defaults.update(kwargs)
	return build_filter_complex(**defaults)  # type: ignore[arg-type]


# --- граф фильтров ------------------------------------------------------------


def test_overlay_position_corners() -> None:
	"""Каждый угол даёт корректное выражение позиции."""
	assert _overlay_position("tl", 10) == "10:10"
	assert _overlay_position("tr", 10) == "W-w-10:10"
	assert _overlay_position("bl", 10) == "10:H-h-10"
	assert _overlay_position("br", 10) == "W-w-10:H-h-10"


def test_overlay_position_unknown_corner_raises() -> None:
	"""Неизвестный угол вызывает ValueError."""
	with pytest.raises(ValueError):
		_overlay_position("zz", 10)


def test_enable_expr_applies_offset() -> None:
	"""Границы окна показа сдвигаются на длительность заставки."""
	wm = WatermarkOptions(
		corner="tr", margin=24, opacity=1.0, scale=0.15, start=2.0, end=5.0
	)
	assert _enable_expr(wm, offset=1.0) == ":enable='between(t,3.000,6.000)'"
	assert _enable_expr(WM, offset=0.0) == ""


def test_intro_adds_xfade_with_offset() -> None:
	"""Заставка добавляет xfade с offset, равным времени удержания кадра."""
	graph = _build(still_index=1, hold=1.0, xfade=0.5)
	assert "xfade=transition=fade:duration=0.5:offset=1.0" in graph.filter_complex
	assert graph.video_label == "[base]"


def test_watermark_adds_overlay() -> None:
	"""Вотермарк масштабируется по эталону с сохранением пропорций (h=-2)."""
	graph = _build(wm=WM, wm_index=1)
	assert "[main]split[bg][wm_ref]" in graph.filter_complex
	assert "[1:v][wm_ref]scale=w=rw*0.15:h=-2[wm_s]" in graph.filter_complex
	assert "colorchannelmixer=aa=1.0" in graph.filter_complex
	assert "overlay=W-w-24:24:format=yuv444" in graph.filter_complex
	assert "scale2ref" not in graph.filter_complex  # устарел, искажал пропорции
	assert graph.video_label == "[vout]"


def test_watermark_requires_input_index() -> None:
	"""Вотермарк без индекса входа — ошибка сборки графа."""
	with pytest.raises(ValueError, match="вход"):
		_build(wm=WM, wm_index=None)


def test_watermark_converts_colors_correctly() -> None:
	"""RGB→YUV вотермарка — матрицей bt709 и без потери разрешения цветности."""
	graph = _build(wm=WM, wm_index=1)
	assert "scale=out_color_matrix=bt709:out_range=tv" in graph.filter_complex
	assert "format=yuva444p" in graph.filter_complex


def test_audio_delayed_with_intro() -> None:
	"""При заставке звук задерживается (adelay), без неё идёт напрямую."""
	with_intro = _build(still_index=1, hold=1.0, has_audio=True)
	assert "adelay=1000:all=1" in with_intro.filter_complex
	assert with_intro.audio_label == "[aout]"
	plain = _build(has_audio=True)
	assert plain.audio_label == "0:a"


def test_main_chain_uses_explicit_size() -> None:
	"""Основное видео масштабируется на явный размер (общий с заставкой)."""
	graph = _build(width=608, height=1080)
	assert "[0:v]scale=608:1080," in graph.filter_complex


# --- геометрия кадра --------------------------------------------------------------


def test_fitted_size() -> None:
	"""Вписывание в FullHD: пропорции сохраняются, стороны чётные."""
	from pxcontrol.engine.video.constants import fitted_size

	assert fitted_size(1920, 1080) == (1920, 1080)  # уже FullHD
	assert fitted_size(1280, 720) == (1920, 1080)  # растяжение 16:9
	assert fitted_size(720, 1280) == (608, 1080)  # вертикальное
	assert fitted_size(3840, 2160) == (1920, 1080)  # уменьшение 4K
	assert fitted_size(853, 480) == (1920, 1080)  # почти 16:9, округление


def test_fit_pad_filter_letterboxes() -> None:
	"""Кадр заставки приводится к точному размеру с полями по центру."""
	from pxcontrol.engine.video.frames import _fit_pad_filter

	assert _fit_pad_filter(608, 1080) == (
		"scale=608:1080:force_original_aspect_ratio=decrease,"
		"pad=608:1080:(ow-iw)/2:(oh-ih)/2"
	)


# --- кадр заставки --------------------------------------------------------------


def test_resolve_timestamp_modes() -> None:
	"""Все режимы источника кадра дают ожидаемое время."""
	assert resolve_timestamp("first", INFO) == 0.0
	assert resolve_timestamp("time:5.5", INFO) == 5.5
	assert resolve_timestamp("frame:50", INFO) == 2.0  # 50 кадров при 25 fps
	random.seed(0)
	for _ in range(100):
		assert 25.0 <= resolve_timestamp("random-middle", INFO) <= 75.0
		# random-choice — кадры-кандидаты (и автоматика): из 5–95 %
		assert 5.0 <= resolve_timestamp("random-choice", INFO) <= 95.0
	with pytest.raises(ValueError):
		resolve_timestamp("bogus", INFO)


# --- окно показа вотермарка -------------------------------------------------------


def test_watermark_window_offsets_to_absolute() -> None:
	"""Отступы от краёв превращаются в абсолютное окно показа."""
	from pxcontrol.engine.video.pipeline import _watermark_options

	opts = _options(
		watermark="/x/wm.png", wm_start_offset=3.0, wm_end_offset=10.0
	)
	wm = _watermark_options(opts, INFO)  # длительность 100 с
	assert wm.start == 3.0 and wm.end == 90.0
	plain = _watermark_options(_options(watermark="/x/wm.png"), INFO)
	assert plain.start is None and plain.end is None


def test_watermark_zero_offsets_mean_no_limit() -> None:
	"""Нулевые отступы равносильны незаданным: окно не ограничено."""
	from pxcontrol.engine.video.pipeline import _watermark_options

	wm = _watermark_options(
		_options(watermark="/x/wm.png", wm_start_offset=0.0, wm_end_offset=0.0),
		INFO,
	)
	assert wm.start is None and wm.end is None


def test_watermark_fade_adds_loop_and_fades() -> None:
	"""Плавность: картинка зацикливается, fade по альфе на краях окна."""
	wm = WatermarkOptions(
		corner="tr", margin=24, opacity=0.8, scale=0.15,
		start=2.0, end=5.0, fade=1.0,
	)
	graph = _build(wm=wm, wm_index=1)
	assert "loop=loop=-1:size=1,fps=25.00000" in graph.filter_complex
	# поток обязательно конечен: бесконечный не даёт ffmpeg завершиться
	assert "trim=duration=100.000" in graph.filter_complex
	assert "fade=t=in:st=2.000:d=1.000:alpha=1" in graph.filter_complex
	assert "fade=t=out:st=4.000:d=1.000:alpha=1" in graph.filter_complex
	# со заставкой времена переходов сдвигаются на удержание кадра
	shifted = _build(wm=wm, wm_index=1, still_index=2, hold=1.0)
	assert "fade=t=in:st=3.000" in shifted.filter_complex
	assert "fade=t=out:st=5.000" in shifted.filter_complex


def test_watermark_without_fade_keeps_single_frame() -> None:
	"""Нулевая плавность — прежний дешёвый путь без loop и fade."""
	graph = _build(wm=WM, wm_index=1)
	assert "loop=" not in graph.filter_complex
	assert "fade=" not in graph.filter_complex


def test_watermark_fade_must_fit_window() -> None:
	"""Переходы, не помещающиеся в окно показа, — ошибка до кодирования."""
	from pxcontrol.engine.video.pipeline import _watermark_options

	opts = _options(
		watermark="/x/wm.png",
		wm_start_offset=45.0, wm_end_offset=45.0, wm_fade=6.0,  # окно 10 с < 12 с
	)
	with pytest.raises(ValueError, match="Плавность"):
		_watermark_options(opts, INFO)
	ok = _watermark_options(
		_options(
			watermark="/x/wm.png",
			wm_start_offset=45.0, wm_end_offset=45.0, wm_fade=5.0,
		),
		INFO,
	)
	assert ok.fade == 5.0


def test_watermark_window_degenerate_raises() -> None:
	"""Отступы больше длительности ролика — понятная ошибка до кодирования."""
	from pxcontrol.engine.video.pipeline import _watermark_options

	opts = _options(
		watermark="/x/wm.png",
		wm_start_offset=60.0, wm_end_offset=50.0,  # 60 ≥ 100−50
	)
	with pytest.raises(ValueError, match="не помещаются"):
		_watermark_options(opts, INFO)
	# без вотермарка те же отступы безвредны (валидировать нечего)
	no_wm = _options(wm_start_offset=60.0, wm_end_offset=50.0)
	assert _watermark_options(no_wm, INFO).start == 60.0


# --- затухание на краях -----------------------------------------------------------


def test_fade_edges_video_and_audio() -> None:
	"""Затухание: видео — из чёрного и в чёрное, звук — afade к тем же краям."""
	graph = _build(fade_in=0.5, fade_out=1.0, has_audio=True)
	assert "fade=t=in:st=0:d=0.500" in graph.filter_complex
	assert "fade=t=out:st=99.000:d=1.000" in graph.filter_complex  # 100 − 1
	assert "afade=t=in:st=0.000:d=0.500" in graph.filter_complex
	assert "afade=t=out:st=99.000:d=1.000" in graph.filter_complex
	assert graph.video_label == "[vfinal]" and graph.audio_label == "[aout]"


def test_fade_in_audio_starts_after_intro() -> None:
	"""С заставкой звук сдвинут на hold — afade начинается вместе со звуком."""
	graph = _build(
		still_index=1, hold=1.0, fade_in=0.5, fade_out=0.0, has_audio=True
	)
	assert "adelay=1000:all=1" in graph.filter_complex
	# картинка появляется из чёрного с нуля (накрывает заставку)…
	assert "fade=t=in:st=0:d=0.500" in graph.filter_complex
	# …а звук — с начала дорожки, после тишины adelay
	assert "afade=t=in:st=1.000:d=0.500" in graph.filter_complex


def test_no_fade_keeps_labels() -> None:
	"""Нулевое затухание не добавляет фильтров и не меняет метки."""
	graph = _build(has_audio=True)
	assert "fade=" not in graph.filter_complex
	assert graph.video_label == "[main]" and graph.audio_label == "0:a"


def test_fades_must_fit_duration(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Затухания, не помещающиеся в итог, — ошибка до кодирования."""
	from pxcontrol.engine.video import pipeline

	launched: list[str] = []
	monkeypatch.setattr(
		pipeline, "run_streaming",
		lambda cmd, what, total, cb: launched.append(what),
	)
	opts = _options(fade_in=60.0, fade_out=60.0)  # 120 с > 100 с
	with pytest.raises(ValueError, match="Затухания"):
		pipeline._run_main(opts, INFO, None, "out.mp4", None)
	assert launched == []  # до запуска ffmpeg не дошло


# --- обрезка с краёв --------------------------------------------------------------


def test_trim_adds_input_seek_args() -> None:
	"""Обрезка даёт -ss/-t перед входом; без неё аргументов нет."""
	from pxcontrol.engine.video.pipeline import _build_inputs
	from pxcontrol.engine.video.probe import trimmed_info

	work = trimmed_info(INFO, 3.5, 1.5)  # 100 − 3.5 − 1.5 = 95
	inputs, _wm, _still = _build_inputs(
		_options(trim_start=3.5, trim_end=1.5), work, None
	)
	assert inputs[:5] == ["-ss", "3.500", "-t", "95.000", "-i"]
	plain, _wm, _still = _build_inputs(_options(), INFO, None)
	assert plain[:2] == ["-i", "a"]
	# обрезан только конец: без -ss, но с ограничением длительности
	tail_only, _wm, _still = _build_inputs(
		_options(trim_end=10.0), trimmed_info(INFO, 0.0, 10.0), None
	)
	assert tail_only[:3] == ["-t", "90.000", "-i"]


def test_trimmed_info_and_degenerate_trim() -> None:
	"""Рабочая длительность — без краёв; обрезка больше ролика — ошибка."""
	from pxcontrol.engine.video.probe import trimmed_info

	assert trimmed_info(INFO, 0.0, 0.0) is INFO  # без обрезки — как есть
	work = trimmed_info(INFO, 3.0, 2.0)
	assert work.duration == 95.0 and work.fps == INFO.fps
	with pytest.raises(ValueError, match="не оставляет"):
		trimmed_info(INFO, 60.0, 40.0)


def test_watermark_window_counts_from_trimmed() -> None:
	"""Отступы окна вотермарка считаются от обрезанной длительности."""
	from pxcontrol.engine.video.pipeline import _watermark_options
	from pxcontrol.engine.video.probe import trimmed_info

	work = trimmed_info(INFO, 10.0, 10.0)  # рабочая версия — 80 с
	wm = _watermark_options(
		_options(
			watermark="/x/wm.png", trim_start=10.0, trim_end=10.0,
			wm_start_offset=5.0, wm_end_offset=5.0,
		),
		work,
	)
	assert wm.start == 5.0 and wm.end == 75.0  # 80 − 5, не 100 − 5


def test_prepare_still_shifts_by_trim(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	"""Кадр заставки: момент — от обрезанной версии, извлечение — из исходника."""
	from pxcontrol.engine.video import frames

	captured: list[float] = []
	monkeypatch.setattr(
		frames, "extract_still",
		lambda _src, ts, _out, _w, _h, _bin: captured.append(ts),
	)
	frames.prepare_still("in.mp4", "time:5", INFO, "out.png", start_offset=3.5)
	assert captured == [8.5]  # 5-я секунда обрезанной = 8.5 исходника
	captured.clear()
	# своя картинка от обрезки не зависит (файл должен существовать)
	image = tmp_path / "кадр.png"
	image.write_bytes(b"png")
	frames.prepare_still(
		"in.mp4", f"image:{image}", INFO, "out.png", start_offset=3.5
	)
	assert captured == [0.0]


# --- метаданные -------------------------------------------------------------------


def test_assemble_command_meta_comment() -> None:
	"""Комментарий попадает в команду тегом comment; пустой — не пишется."""
	from pxcontrol.engine.video.pipeline import _assemble_command

	with_meta = _assemble_command(
		"ffmpeg", ["-i", "a.mp4"], "graph", "[v]", None, ["-crf", "20"],
		"https://t.me/mych — канал", "out.mp4",
	)
	assert "-metadata" in with_meta
	assert "comment=https://t.me/mych — канал" in with_meta
	without = _assemble_command(
		"ffmpeg", ["-i", "a.mp4"], "graph", "[v]", None, ["-crf", "20"],
		None, "out.mp4",
	)
	assert "-metadata" not in without


def test_attach_cover_moves_moov_to_front(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Вшивание обложки ставит +faststart — moov в начало (для перезалива/стрима)."""
	from pxcontrol.engine.video import pipeline

	captured: list[str] = []
	monkeypatch.setattr(
		pipeline, "run_tool", lambda cmd, what: captured.extend(cmd)
	)
	pipeline._attach_cover("ffmpeg", "main.mp4", "cover.png", "out.mp4")
	assert "-movflags" in captured
	assert captured[captured.index("-movflags") + 1] == "+faststart"


# --- разбор прогресса ffmpeg -----------------------------------------------------


def test_progress_seconds() -> None:
	"""Строки out_time_us/out_time_ms дают секунды (оба поля — микросекунды)."""
	from pxcontrol.engine.video.ffmpeg import _progress_seconds

	assert _progress_seconds("out_time_us=2920000\n") == pytest.approx(2.92)
	assert _progress_seconds("out_time_ms=2920000\n") == pytest.approx(2.92)
	assert _progress_seconds("progress=continue\n") is None
	assert _progress_seconds("out_time_us=N/A\n") is None


def _fake_ffmpeg(tmp_path: Path, body: str) -> list[str]:
	"""Скрипт-подделка ffmpeg: команда для run_streaming без бинаря."""
	import sys

	script = tmp_path / "fake_ffmpeg.py"
	script.write_text(body, encoding="utf-8")
	return [sys.executable, str(script)]


def test_error_summary_keeps_only_tail() -> None:
	"""В текст ошибки попадает хвост журнала ffmpeg, а не весь дамп."""
	from pxcontrol.engine.video.ffmpeg import _error_summary

	dump = "\n".join([f"болтовня {i}" for i in range(50)] + [
		"", "  Error opening input file /tmp/нет.png.  ",
		"Error opening input files: No such file or directory",
	])
	summary = _error_summary(dump)
	assert "No such file or directory" in summary
	assert "болтовня 5" not in summary  # начало дампа отрезано
	assert len(summary) < 500


def test_missing_watermark_fails_before_ffmpeg(tmp_path: Path) -> None:
	"""Несуществующий вотермарк — точная ошибка до запуска ffmpeg."""
	from pxcontrol.engine.video import pipeline

	opts = _options(watermark=str(tmp_path / "нет-такого.png"))
	with pytest.raises(ValueError, match="вотермарка не найден"):
		pipeline.process(opts)


def test_missing_intro_image_fails_before_ffmpeg(tmp_path: Path) -> None:
	"""Несуществующая картинка заставки — точная ошибка до ffmpeg."""
	from pxcontrol.engine.video.frames import prepare_still

	with pytest.raises(ValueError, match="Картинка для заставки не найдена"):
		prepare_still(
			"input.mp4", f"image:{tmp_path / 'нет.png'}", INFO, "out.png"
		)


def test_run_streaming_survives_chatty_stderr(tmp_path: Path) -> None:
	"""Болтливый stderr (больше буфера канала ОС) не блокирует кодирование.

	До параллельного чтения stderr процесс замирал навсегда: ffmpeg ждал
	освобождения буфера stderr, а мы — строк прогресса из stdout.
	"""
	from pxcontrol.engine.video.ffmpeg import run_streaming

	cmd = _fake_ffmpeg(tmp_path, (
		"import sys\n"
		"sys.stderr.write('предупреждение фильтра\\n' * 20000)\n"  # ~800 КБ
		"sys.stderr.flush()\n"
		"sys.stdout.write('out_time_us=1000000\\nout_time_us=2000000\\n')\n"
	))
	received: list[float] = []
	run_streaming(cmd, "тест", total_seconds=2.0, on_progress=received.append)
	assert received == [pytest.approx(0.5), pytest.approx(1.0)]


def test_run_streaming_reports_stderr_on_failure(tmp_path: Path) -> None:
	"""Ненулевой код возврата — RuntimeError с текстом stderr."""
	from pxcontrol.engine.video.ffmpeg import run_streaming

	cmd = _fake_ffmpeg(tmp_path, (
		"import sys\n"
		"sys.stderr.write('битый вход: поток не распознан\\n')\n"
		"sys.exit(1)\n"
	))
	with pytest.raises(RuntimeError, match="битый вход"):
		run_streaming(cmd, "тест", total_seconds=1.0, on_progress=None)


# --- разбор fps ------------------------------------------------------------------


def test_parse_fps() -> None:
	"""Дроби, целые и '0/0' разбираются корректно."""
	assert _parse_fps("30000/1001") == pytest.approx(29.97002997)
	assert _parse_fps("25/1") == 25.0
	assert _parse_fps("24") == 24.0
	assert _parse_fps("0/0") == 0.0


# --- качество видео ---------------------------------------------------------------


def test_parse_bitrate_prefers_stream_over_format() -> None:
	"""Битрейт берётся из видеопотока, контейнер — запасной вариант."""
	from pxcontrol.engine.video.probe import _parse_bitrate_kbps

	assert _parse_bitrate_kbps({"bit_rate": "2500000"}, {"bit_rate": "9"}) == 2500
	assert _parse_bitrate_kbps({}, {"bit_rate": "1500000"}) == 1500
	assert _parse_bitrate_kbps({"bit_rate": "N/A"}, {}) is None
	assert _parse_bitrate_kbps({}, {}) is None


def test_video_quality_args_modes() -> None:
	"""Приоритет: битрейт пресета → битрейт исходника → CRF."""
	from pxcontrol.engine.video.pipeline import _video_quality_args

	def _info(kbps: int | None) -> VideoInfo:
		return VideoInfo(
			width=1920, height=1080, duration=10.0, fps=25.0,
			has_audio=True, bitrate_kbps=kbps,
		)

	assert _video_quality_args(
		_options(video_bitrate_kbps=3000), _info(1500)
	) == ["-b:v", "3000k"]
	assert _video_quality_args(
		_options(video_bitrate_kbps=None), _info(1500)
	) == ["-b:v", "1500k"]
	assert _video_quality_args(
		_options(video_bitrate_kbps=None), _info(None)
	) == ["-crf", "20"]
