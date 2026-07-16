"""Запуск ffmpeg/ffprobe: единый обработчик ошибок и трансляция прогресса.

Единственное место, где конвейер обращается к ``subprocess``: одинаковый
лог команды и одинаковый перевод ненулевого кода возврата в ``RuntimeError``
с текстом stderr — раньше этот код был скопирован в четырёх модулях.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Колбэк прогресса: доля готовности 0.0..1.0.
ProgressCallback = Callable[[float], None]


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

	Raises:
		RuntimeError: Если ffmpeg завершился с ненулевым кодом.
	"""
	logger.debug("ffmpeg (%s): %s", what, " ".join(cmd))
	proc = subprocess.Popen(
		cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
	)
	assert proc.stdout is not None  # stdout=PIPE
	for line in proc.stdout:
		seconds = _progress_seconds(line)
		if seconds is not None and on_progress is not None and total_seconds > 0:
			on_progress(min(seconds / total_seconds, 1.0))
	stderr = proc.stderr.read() if proc.stderr is not None else ""
	proc.wait()
	if proc.returncode != 0:
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
