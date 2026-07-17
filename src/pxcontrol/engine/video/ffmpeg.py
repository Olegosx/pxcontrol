"""Запуск ffmpeg/ffprobe: единый обработчик ошибок и трансляция прогресса.

Единственное место, где конвейер обращается к ``subprocess``: одинаковый
лог команды и одинаковый перевод ненулевого кода возврата в ``RuntimeError``
с текстом stderr — раньше этот код был скопирован в четырёх модулях.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Колбэк прогресса: доля готовности 0.0..1.0.
ProgressCallback = Callable[[float], None]

#: Источник пути к ffmpeg: готовая строка или провайдер (путь из настроек).
FfmpegSource = str | Callable[[], str]


def ffmpeg_source(source: FfmpegSource) -> Callable[[], str]:
	"""Нормализует источник пути к ffmpeg: строка → константный провайдер.

	Сервисы принимают и строку (тесты, простые случаи), и провайдер —
	путь из настроек приложения, смена которого подхватывается
	без перезапуска.
	"""
	return source if callable(source) else (lambda: source)


def run_tool(cmd: list[str], what: str) -> str:
	"""Запускает ffmpeg/ffprobe и возвращает stdout.

	Args:
		cmd: полная команда (первый элемент — путь к бинарю).
		what: короткое человекочитаемое имя операции для лога и ошибки.

	Raises:
		RuntimeError: Инструмент завершился с ненулевым кодом.
	"""
	tool = Path(cmd[0]).name
	logger.debug("%s (%s): %s", tool, what, " ".join(cmd))
	result = subprocess.run(cmd, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(
			f"{tool} ({what}) завершился с ошибкой: {result.stderr.strip()}"
		)
	return result.stdout


def run_streaming(
	cmd: list[str],
	what: str,
	total_seconds: float,
	on_progress: ProgressCallback | None,
) -> None:
	"""Запускает ffmpeg, транслируя ход кодирования в колбэк.

	Команда должна писать прогресс в stdout (``-progress pipe:1``).
	stderr (журнал ffmpeg) читается параллельным потоком: буфер канала ОС
	конечен (~64 КБ), и болтливый ffmpeg (покадровые предупреждения
	фильтров), заполнив его, замер бы на записи — а мы вечно ждали бы
	строк прогресса из stdout (взаимная блокировка).

	Raises:
		RuntimeError: Если ffmpeg завершился с ненулевым кодом.
	"""
	logger.debug("ffmpeg (%s): %s", what, " ".join(cmd))
	proc = subprocess.Popen(
		cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
	)
	if proc.stdout is None or proc.stderr is None:  # для mypy: оба — PIPE
		raise RuntimeError(f"ffmpeg ({what}): каналы процесса не открылись.")
	stderr_pipe = proc.stderr
	stderr_chunks: list[str] = []
	reader = threading.Thread(
		target=lambda: stderr_chunks.append(stderr_pipe.read()),
		name="ffmpeg-stderr", daemon=True,
	)
	reader.start()
	for line in proc.stdout:
		seconds = _progress_seconds(line)
		if seconds is not None and on_progress is not None and total_seconds > 0:
			on_progress(min(seconds / total_seconds, 1.0))
	proc.wait()
	reader.join(timeout=10.0)
	if proc.returncode != 0:
		stderr = "".join(stderr_chunks)
		raise RuntimeError(f"ffmpeg ({what}) завершился с ошибкой: {stderr.strip()}")


def _progress_seconds(line: str) -> float | None:
	"""Извлекает секунды из строки прогресса ffmpeg.

	Поле ``out_time_ms`` исторически содержит МИКРОсекунды (причуда ffmpeg,
	проверено на 8.0); ``out_time_us`` — его честный синоним.
	"""
	for key in ("out_time_us=", "out_time_ms="):
		if line.startswith(key):
			value = line[len(key):].strip()
			try:
				return int(value) / 1_000_000
			except ValueError:
				return None
	return None
