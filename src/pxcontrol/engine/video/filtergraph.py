"""Сборка графа фильтров ffmpeg (-filter_complex).

Порт из makeVideo (референс).

Граф собирается из независимых участков:
  * приведение основного видео к FullHD;
  * заставка: кадр держится сплошняком, затем растворяется (xfade) в видео;
  * вотермарк: масштабирование под кадр, прозрачность и наложение в угол с
    опциональным окном показа;
  * задержка звука на длительность заставки, чтобы дорожка совпала с видео.
"""

from dataclasses import dataclass

from pxcontrol.engine.video.constants import TARGET_COLOR_MATRIX, TARGET_PIX_FMT

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
		fade: плавность появления/исчезания на краях окна (сек; 0 — резко).
	"""

	corner: str
	margin: int
	opacity: float
	scale: float
	start: float | None
	end: float | None
	fade: float = 0.0


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


def _main_chain(fps: str, width: int, height: int) -> str:
	"""Цепочка приведения основного видео к целевому размеру и формату.

	Размер задаётся явными числами (см. fitted_size): кадр заставки
	готовится под тот же размер, а xfade требует точного совпадения.
	"""
	return f"[0:v]scale={width}:{height}," + _prep("", fps, "[main]")


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


def _watermark_fades(wm: WatermarkOptions, offset: float) -> str:
	"""Фрагменты fade по альфе на краях окна показа (или пустая строка).

	Переход есть только на краю с заданным отступом; времена сдвигаются
	на длительность заставки, как и окно показа.
	"""
	if wm.fade <= 0:
		return ""
	parts = ""
	if wm.start is not None:
		parts += f",fade=t=in:st={wm.start + offset:.3f}:d={wm.fade:.3f}:alpha=1"
	if wm.end is not None:
		start_out = wm.end + offset - wm.fade
		parts += f",fade=t=out:st={start_out:.3f}:d={wm.fade:.3f}:alpha=1"
	return parts


def _watermark_chains(
	wm: WatermarkOptions, wm_index: int, base: str, offset: float, fps: str,
	duration: float,
) -> tuple[list[str], str]:
	"""Цепочки вотермарка: масштабирование под кадр, прозрачность, наложение.

	Фон дублируется (split): одна копия служит эталоном размера для scale
	(rw — ширина эталона, высота -2 сохраняет пропорции вотермарка), вторая
	идёт под наложение. scale2ref не используется: он устарел и в ffmpeg 8
	искажает размер и пропорции. colorchannelmixer задаёт прозрачность.

	Плавность (fade по альфе) требует кадров во времени — у статичной
	картинки кадр один, поэтому при переходах она зацикливается фильтром
	loop в поток. Поток обязательно ограничивается trim по длительности
	итога: бесконечный вторичный вход overlay не даёт ffmpeg завершиться
	(кодирование продолжается вечно — проверено).

	Цвета: RGB→YUV — явно матрицей bt709 (по умолчанию ffmpeg берёт bt601,
	и плеер сдвигал оттенки), смешивание — в yuv444 (по умолчанию yuv420:
	цветность в четверть разрешения, и тонкие штрихи вотермарка перенимали
	цвет соседей — красный зеленел).
	"""
	position = _overlay_position(wm.corner, wm.margin)
	enable = _enable_expr(wm, offset)
	fades = _watermark_fades(wm, offset)
	wm_in = f"[{wm_index}:v]"
	chains = [f"{base}split[bg][wm_ref]"]
	if fades:
		# trim ровно по длительности: длиннее — наложение продлит ролик
		# повтором последнего кадра фона, короче — прикроет repeatlast
		chains.append(
			f"{wm_in}loop=loop=-1:size=1,fps={fps},"
			f"setpts=N/FRAME_RATE/TB,trim=duration={duration:.3f}[wm_v]"
		)
		wm_in = "[wm_v]"
	chains += [
		f"{wm_in}[wm_ref]scale=w=rw*{wm.scale}:h=-2[wm_s]",
		f"[wm_s]format=rgba,colorchannelmixer=aa={wm.opacity}{fades},"
		f"scale=out_color_matrix={TARGET_COLOR_MATRIX}:out_range=tv,"
		f"format=yuva444p[wm_a]",
		f"[bg][wm_a]overlay={position}:format=yuv444{enable}[vout]",
	]
	return chains, "[vout]"


def _fade_filters(fade_in: float, fade_out: float, duration: float) -> str:
	"""Фрагмент fade-фильтров видео для затухания на краях (или пусто).

	Появление — из чёрного с самого начала итога (накрывает и заставку),
	уход — в чёрное к концу итоговой длительности.
	"""
	parts = []
	if fade_in > 0:
		parts.append(f"fade=t=in:st=0:d={fade_in:.3f}")
	if fade_out > 0:
		parts.append(f"fade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}")
	return ",".join(parts)


def _audio_chains(
	has_intro: bool, hold: float, fade_in: float, fade_out: float, duration: float
) -> tuple[list[str], str]:
	"""Цепочки звука: задержка под заставку и затухание на краях.

	Затухание в начале стартует там, где начинается звук: при заставке
	дорожка сдвинута на ``hold`` (тишина adelay), и afade с нуля отыграл
	бы по тишине. Затухание в конце — к концу итоговой длительности.
	"""
	filters = []
	if has_intro:
		filters.append(f"adelay={int(round(hold * 1000))}:all=1")
	if fade_in > 0:
		start = hold if has_intro else 0.0
		filters.append(f"afade=t=in:st={start:.3f}:d={fade_in:.3f}")
	if fade_out > 0:
		filters.append(
			f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}"
		)
	if not filters:
		return [], "0:a"
	return [f"[0:a]{','.join(filters)}[aout]"], "[aout]"


def build_filter_complex(
	*,
	fps: str,
	width: int,
	height: int,
	duration: float,
	hold: float,
	xfade: float,
	still_index: int | None,
	wm: WatermarkOptions | None,
	wm_index: int | None,
	has_audio: bool,
	fade_in: float,
	fade_out: float,
) -> FilterGraph:
	"""Собирает граф фильтров из участков и возвращает метки потоков для -map.

	Участки включаются самими данными: заставка — заданным ``still_index``,
	вотермарк — заданным ``wm`` (отдельных флагов нет — нечему расходиться).

	Args:
		fps: кадровая частота строкой (например '29.97003').
		width: ширина итогового кадра (fitted_size).
		height: высота итогового кадра (fitted_size).
		duration: длительность итогового видео (сек) — ограничивает
			зацикленный поток вотермарка при плавности и задаёт конец
			затухания.
		hold: сколько секунд держать кадр заставки сплошняком.
		xfade: длительность растворения заставки в видео.
		still_index: индекс входа с картинкой-заставкой (None — без заставки).
		wm: параметры вотермарка (None — не накладывается).
		wm_index: индекс входа с картинкой-вотермарком (обязателен при ``wm``).
		has_audio: переносить ли звук.
		fade_in: затухание в начале — появление из чёрного (сек; 0 — нет).
		fade_out: затухание в конце — уход в чёрное (сек; 0 — нет).

	Returns:
		FilterGraph со строкой -filter_complex и метками видео/звука.

	Raises:
		ValueError: Задан ``wm`` без ``wm_index``.
	"""
	has_intro = still_index is not None
	chains = [_main_chain(fps, width, height)]
	base, offset = "[main]", 0.0
	if still_index is not None:
		chains += _intro_chains(fps, hold, xfade, still_index)
		base, offset = "[base]", hold
	if wm is not None:
		if wm_index is None:
			raise ValueError("Вотермарк задан, но вход с его картинкой — нет")
		wm_chains, video_label = _watermark_chains(
			wm, wm_index, base, offset, fps, duration
		)
		chains += wm_chains
	else:
		video_label = base
	fades = _fade_filters(fade_in, fade_out, duration)
	if fades:
		chains.append(f"{video_label}{fades}[vfinal]")
		video_label = "[vfinal]"
	audio_label: str | None = None
	if has_audio:
		audio_chains, audio_label = _audio_chains(
			has_intro, hold, fade_in, fade_out, duration
		)
		chains += audio_chains
	return FilterGraph(";".join(chains), video_label, audio_label)
