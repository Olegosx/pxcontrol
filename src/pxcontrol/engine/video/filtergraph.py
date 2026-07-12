"""Сборка графа фильтров ffmpeg (-filter_complex).

Порт из makeVideo (референс).

Граф собирается из независимых участков:
  * приведение основного видео к FullHD;
  * заставка: кадр держится сплошняком, затем растворяется (xfade) в видео;
  * вотермарк: масштабирование под кадр, прозрачность и наложение в угол с
    опциональным окном показа;
  * задержка звука на длительность заставки, чтобы дорожка совпала с видео.
"""

import logging
from dataclasses import dataclass

from pxcontrol.engine.video.constants import FULLHD_FIT, TARGET_PIX_FMT

logger = logging.getLogger(__name__)

# Выражения позиции overlay по углу: W/H — размеры фона, w/h — размеры вотермарка.
CORNER_POSITIONS = {
	"tl": "{m}:{m}",
	"tr": "W-w-{m}:{m}",
	"bl": "{m}:H-h-{m}",
	"br": "W-w-{m}:H-h-{m}",
}

# Условный «бесконечный» конец окна показа вотермарка, если задан только старт.
_OPEN_END = 1_000_000.0


@dataclass(frozen=True)
class WatermarkOptions:
	"""Параметры вотермарка.

	Attributes:
		corner: угол — 'tl', 'tr', 'bl' или 'br'.
		margin: отступ от края в пикселях.
		opacity: прозрачность от 0 (невидим) до 1 (непрозрачен).
		scale: ширина вотермарка как доля ширины кадра (например 0.15).
		start: момент появления в секундах по исходному видео (None — с начала).
		end: момент скрытия в секундах по исходному видео (None — до конца).
	"""

	corner: str
	margin: int
	opacity: float
	scale: float
	start: float | None
	end: float | None


@dataclass(frozen=True)
class FilterGraph:
	"""Результат сборки графа фильтров.

	Attributes:
		filter_complex: строка для аргумента -filter_complex.
		video_label: метка итогового видеопотока для -map.
		audio_label: метка итогового звукового потока для -map (None — без звука).
	"""

	filter_complex: str
	video_label: str
	audio_label: str | None


def _prep(label_in: str, fps: str, label_out: str) -> str:
	"""Приводит поток к единому fps, формату пикселей и SAR для стыковки в xfade."""
	return (
		f"{label_in}fps={fps},format={TARGET_PIX_FMT},setsar=1,"
		f"setpts=PTS-STARTPTS{label_out}"
	)


def _main_chain(fps: str) -> str:
	"""Цепочка приведения основного видео к FullHD и единому формату."""
	return f"[0:v]{FULLHD_FIT}," + _prep("", fps, "[main]")


def _intro_chains(fps: str, hold: float, xfade: float, still_index: int) -> list[str]:
	"""Цепочки заставки: подготовка кадра и растворение его в основное видео.

	Кадр (clip A) держится сплошняком hold секунд, затем за xfade секунд
	перетекает в основное видео (clip B). offset=hold — момент старта растворения,
	поэтому кадр t=0 остаётся чистым и уходит в превью Телеграма.
	"""
	return [
		_prep(f"[{still_index}:v]", fps, "[still]"),
		f"[still][main]xfade=transition=fade:duration={xfade}:offset={hold}[base]",
	]


def _overlay_position(corner: str, margin: int) -> str:
	"""Возвращает выражение x:y для overlay по углу и отступу.

	Raises:
		ValueError: если угол не из набора tl/tr/bl/br.
	"""
	if corner not in CORNER_POSITIONS:
		raise ValueError(f"Неизвестный угол вотермарка: {corner}")
	return CORNER_POSITIONS[corner].format(m=margin)


def _enable_expr(wm: WatermarkOptions, offset: float) -> str:
	"""Возвращает фрагмент ':enable=...' окна показа вотермарка или пустую строку.

	Время задаётся по исходному видео, поэтому к границам прибавляется offset —
	длительность заставки в начале (0, если заставки нет).
	"""
	if wm.start is None and wm.end is None:
		return ""
	start = (wm.start or 0.0) + offset
	end = (wm.end if wm.end is not None else _OPEN_END) + offset
	return f":enable='between(t,{start:.3f},{end:.3f})'"


def _watermark_chains(
	wm: WatermarkOptions, wm_index: int, base: str, offset: float
) -> tuple[list[str], str]:
	"""Цепочки вотермарка: масштабирование под кадр, прозрачность, наложение.

	scale2ref масштабирует вотермарк относительно фона (main_w — ширина фона),
	сохраняя пропорции вотермарка (ow/a). Затем colorchannelmixer задаёт
	прозрачность, и overlay кладёт картинку в нужный угол.
	"""
	position = _overlay_position(wm.corner, wm.margin)
	enable = _enable_expr(wm, offset)
	chains = [
		f"[{wm_index}:v]{base}scale2ref=w=main_w*{wm.scale}:h=ow/a[wm_s][bg]",
		f"[wm_s]format=rgba,colorchannelmixer=aa={wm.opacity}[wm_a]",
		f"[bg][wm_a]overlay={position}{enable}[vout]",
	]
	return chains, "[vout]"


def _audio_chains(has_intro: bool, hold: float) -> tuple[list[str], str]:
	"""Цепочки звука: задержка на длительность заставки или прямое отображение."""
	if has_intro:
		delay_ms = int(round(hold * 1000))
		return [f"[0:a]adelay={delay_ms}:all=1[aout]"], "[aout]"
	return [], "0:a"


def build_filter_complex(
	*,
	fps: str,
	has_intro: bool,
	hold: float,
	xfade: float,
	still_index: int | None,
	has_watermark: bool,
	wm: WatermarkOptions | None,
	wm_index: int | None,
	has_audio: bool,
) -> FilterGraph:
	"""Собирает граф фильтров из участков и возвращает метки потоков для -map.

	Args:
		fps: кадровая частота строкой (например '29.97003').
		has_intro: включена ли заставка.
		hold: сколько секунд держать кадр заставки сплошняком.
		xfade: длительность растворения заставки в видео.
		still_index: индекс входа с картинкой-заставкой (если есть заставка).
		has_watermark: накладывать ли вотермарк.
		wm: параметры вотермарка (если he накладывается — может быть None).
		wm_index: индекс входа с картинкой-вотермарком.
		has_audio: переносить ли звук.

	Returns:
		FilterGraph со строкой -filter_complex и метками видео/звука.
	"""
	chains = [_main_chain(fps)]
	base, offset = "[main]", 0.0
	if has_intro:
		if still_index is None:
			raise ValueError("Заставка включена, но вход с кадром не задан")
		chains += _intro_chains(fps, hold, xfade, still_index)
		base, offset = "[base]", hold
	if has_watermark:
		if wm is None or wm_index is None:
			raise ValueError("Вотермарк включён, но его параметры не заданы")
		wm_chains, video_label = _watermark_chains(wm, wm_index, base, offset)
		chains += wm_chains
	else:
		video_label = base
	audio_label: str | None = None
	if has_audio:
		audio_chains, audio_label = _audio_chains(has_intro, hold)
		chains += audio_chains
	return FilterGraph(";".join(chains), video_label, audio_label)
