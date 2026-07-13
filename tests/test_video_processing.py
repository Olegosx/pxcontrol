"""Тесты чистых функций подготовки видео (портированы из makeVideo)."""

from __future__ import annotations

import random

import pytest

from pxcontrol.engine.video.filtergraph import (
	WatermarkOptions,
	_enable_expr,
	_overlay_position,
	build_filter_complex,
)
from pxcontrol.engine.video.frames import resolve_timestamp
from pxcontrol.engine.video.probe import VideoInfo, _parse_fps

WM = WatermarkOptions(
	corner="tr", margin=24, opacity=1.0, scale=0.15, start=None, end=None
)
INFO = VideoInfo(width=1920, height=1080, duration=100.0, fps=25.0, has_audio=True)


def _build(**kwargs: object) -> object:
	"""Хелпер: вызывает build_filter_complex со значениями по умолчанию."""
	defaults: dict[str, object] = dict(
		fps="25.00000", width=1920, height=1080, has_intro=False, hold=1.0,
		xfade=0.5, still_index=None, has_watermark=False, wm=None,
		wm_index=None, has_audio=False,
	)
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
	graph = _build(has_intro=True, still_index=1, hold=1.0, xfade=0.5)
	assert "xfade=transition=fade:duration=0.5:offset=1.0" in graph.filter_complex
	assert graph.video_label == "[base]"


def test_watermark_adds_overlay() -> None:
	"""Вотермарк масштабируется по эталону с сохранением пропорций (h=-2)."""
	graph = _build(has_watermark=True, wm=WM, wm_index=1)
	assert "[main]split[bg][wm_ref]" in graph.filter_complex
	assert "[1:v][wm_ref]scale=w=rw*0.15:h=-2[wm_s]" in graph.filter_complex
	assert "colorchannelmixer=aa=1.0" in graph.filter_complex
	assert "overlay=W-w-24:24" in graph.filter_complex
	assert "scale2ref" not in graph.filter_complex  # устарел, искажал пропорции
	assert graph.video_label == "[vout]"


def test_audio_delayed_with_intro() -> None:
	"""При заставке звук задерживается (adelay), без неё идёт напрямую."""
	with_intro = _build(has_intro=True, still_index=1, hold=1.0, has_audio=True)
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
	with pytest.raises(ValueError):
		resolve_timestamp("bogus", INFO)


# --- разбор прогресса ffmpeg -----------------------------------------------------


def test_progress_seconds() -> None:
	"""Строки out_time_us/out_time_ms дают секунды (оба поля — микросекунды)."""
	from pxcontrol.engine.video.pipeline import _progress_seconds

	assert _progress_seconds("out_time_us=2920000\n") == pytest.approx(2.92)
	assert _progress_seconds("out_time_ms=2920000\n") == pytest.approx(2.92)
	assert _progress_seconds("progress=continue\n") is None
	assert _progress_seconds("out_time_us=N/A\n") is None


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
	from pxcontrol.engine.video.pipeline import (
		ProcessingOptions,
		_video_quality_args,
	)

	def _opts(kbps: int | None) -> ProcessingOptions:
		return ProcessingOptions(input="a", output="b", video_bitrate_kbps=kbps)

	def _info(kbps: int | None) -> VideoInfo:
		return VideoInfo(
			width=1920, height=1080, duration=10.0, fps=25.0,
			has_audio=True, bitrate_kbps=kbps,
		)

	assert _video_quality_args(_opts(3000), _info(1500)) == ["-b:v", "3000k"]
	assert _video_quality_args(_opts(None), _info(1500)) == ["-b:v", "1500k"]
	assert _video_quality_args(_opts(None), _info(None)) == ["-crf", "20"]
