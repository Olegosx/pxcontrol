"""Запуск ffmpeg/ffprobe: единый обработчик ошибок и трансляция прогресса.

Единственное место, где конвейер обращается к ``subprocess``: одинаковый
лог команды и одинаковый перевод ненулевого кода возврата в ``RuntimeError``
с текстом stderr — раньше этот код был скопирован в четырёх модулях.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

#: Колбэк прогресса: доля готовности 0.0..1.0.
ProgressCallback = Callable[[float], None]

#: Сколько последних строк журнала ffmpeg попадает в текст ошибки.
_ERROR_TAIL_LINES = 3

#: Предел длины текста ошибки — защита интерфейса от многоэкранного дампа.
_ERROR_TAIL_CHARS = 400

#: Признаки строки с причиной ошибки в журнале ffmpeg.
_ERROR_MARKERS = ("error", "invalid", "no such", "not found", "denied", "failed")


def _error_summary(stderr: str) -> str:
	"""Короткая суть ошибки из журнала ffmpeg.

	Текст исключения показывается пользователю как есть, поэтому дамп
	журнала (экраны версии, конфигурации, потоков) в него не попадает:
	берётся первая строка с признаком ошибки (ffmpeg называет причину —
	«Invalid PNG signature», «No such file…» — раньше, чем завершается)
	и последние строки итога. Полный журнал вызывающий пишет в лог.
	"""
	lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
	tail = lines[-_ERROR_TAIL_LINES:]
	cause = next(
		(line for line in lines if any(marker in line.lower() for marker in _ERROR_MARKERS)),
		None,
	)
	if cause is not None and cause not in tail:
		tail = [cause, *tail]
	summary = " · ".join(tail)
	if len(summary) > _ERROR_TAIL_CHARS:
		summary = f"{summary[:_ERROR_TAIL_CHARS]}…"
	return summary


#: Источник пути к ffmpeg: готовая строка или провайдер (путь из настроек).
FfmpegSource = str | Callable[[], str]


def ffmpeg_source(source: FfmpegSource) -> Callable[[], str]:
	"""Нормализует источник пути к ffmpeg: строка → константный провайдер.

	Сервисы принимают и строку (тесты, простые случаи), и провайдер —
	путь из настроек приложения, смена которого подхватывается
	без перезапуска.
	"""
	return source if callable(source) else (lambda: source)


def run_tool(cmd: list[str], what: str, timeout: float | None = None) -> str:
	"""Запускает ffmpeg/ffprobe и возвращает stdout.

	Args:
		cmd: полная команда (первый элемент — путь к бинарю).
		what: короткое человекочитаемое имя операции для лога и ошибки.
		timeout: предел ожидания в секундах (None — без предела). Шаги
			без прогресса и отмены обязаны его задавать: зависший процесс
			(файл на отвалившемся сетевом диске) иначе заблокировал бы
			обработчик очереди навсегда.

	Raises:
		RuntimeError: Инструмент завершился с ненулевым кодом или не успел
			за ``timeout`` (процесс убит).
	"""
	tool = Path(cmd[0]).name
	logger.debug("%s (%s): %s", tool, what, " ".join(cmd))
	# кодировка явная: ffmpeg пишет журнал в UTF-8 (пути с кириллицей —
	# норма проекта), а локаль системы бывает иной (Windows: cp1251)
	try:
		result = subprocess.run(
			cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
		)
	except subprocess.TimeoutExpired as exc:
		logger.error("%s (%s) не завершился за %.0f с — процесс убит.", tool, what, exc.timeout)
		raise RuntimeError(
			f"{tool} ({what}) не ответил за {exc.timeout:.0f} с — процесс "
			"остановлен (файл или диск недоступен)."
		) from exc
	if result.returncode != 0:
		logger.error(
			"%s (%s) завершился с ошибкой, полный вывод:\n%s",
			tool,
			what,
			result.stderr.strip(),
		)
		raise RuntimeError(f"{tool} ({what}) завершился с ошибкой: {_error_summary(result.stderr)}")
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

	Отмена: исключение из ``on_progress`` убивает процесс (контракт
	очереди обработки). Колбэк вызывается и при молчании ffmpeg
	(раз в ``_CANCEL_POLL_SECONDS`` с последней долей) — иначе зависший
	процесс, переставший писать прогресс, было бы не отменить.

	Raises:
		RuntimeError: Если ffmpeg завершился с ненулевым кодом.
	"""
	logger.debug("ffmpeg (%s): %s", what, " ".join(cmd))
	proc = subprocess.Popen(
		cmd,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",  # журнал ffmpeg — UTF-8 независимо от локали системы
		errors="replace",
	)
	# контекст закрывает каналы и дожидается процесса даже при исключении;
	# kill в except не даёт «осиротевшему» ffmpeg дописывать файл, если
	# упало чтение прогресса (например, колбэк вызывающей стороны)
	with proc:
		if proc.stdout is None or proc.stderr is None:  # для mypy: оба — PIPE
			proc.kill()
			raise RuntimeError(f"ffmpeg ({what}): каналы процесса не открылись.")
		stderr_pipe = proc.stderr
		stderr_chunks: list[str] = []

		def _read_stderr() -> None:
			try:
				stderr_chunks.append(stderr_pipe.read())
			except (ValueError, OSError):
				# канал закрыт при отмене/kill — штатный исход, не ошибка;
				# без except трейсбек потока ушёл бы в stderr мимо logging
				logger.debug("Канал stderr ffmpeg закрыт до конца чтения.", exc_info=True)

		reader = threading.Thread(target=_read_stderr, name="ffmpeg-stderr", daemon=True)
		reader.start()
		try:
			_stream_progress(proc, proc.stdout, on_progress, total_seconds)
		except BaseException:
			proc.kill()
			raise
		proc.wait()
	reader.join(timeout=10.0)
	if proc.returncode != 0:
		stderr = "".join(stderr_chunks)
		logger.error(
			"ffmpeg (%s) завершился с ошибкой, полный вывод:\n%s",
			what,
			stderr.strip(),
		)
		summary = _error_summary(stderr) or "журнал ffmpeg недоступен"
		raise RuntimeError(f"ffmpeg ({what}) завершился с ошибкой: {summary}")


#: Период «пустого» вызова колбэка прогресса при молчании ffmpeg (секунды).
_CANCEL_POLL_SECONDS = 1.0


def _stream_progress(
	proc: subprocess.Popen[str],
	stdout: IO[str],
	on_progress: ProgressCallback | None,
	total_seconds: float,
) -> None:
	"""Транслирует прогресс ffmpeg, не завися от его разговорчивости.

	Строки читает отдельный поток через очередь: основной цикл
	просыпается не реже раза в ``_CANCEL_POLL_SECONDS`` и повторяет
	колбэку последнюю долю — давая ему шанс отменить зависший процесс.
	"""
	lines: queue.Queue[str | None] = queue.Queue()

	def _pump() -> None:
		try:
			for line in stdout:
				lines.put(line)
		except (ValueError, OSError):
			# канал закрыт при отмене/kill — штатный исход, не ошибка
			logger.debug("Канал прогресса ffmpeg закрыт до конца чтения.", exc_info=True)
		finally:
			lines.put(None)  # конец потока (или канал закрыт после kill)

	threading.Thread(target=_pump, name="ffmpeg-progress", daemon=True).start()
	progress = 0.0
	while True:
		try:
			line = lines.get(timeout=_CANCEL_POLL_SECONDS)
		except queue.Empty:
			if proc.poll() is not None:
				return
			if on_progress is not None:
				on_progress(progress)
			continue
		if line is None:
			return
		seconds = _progress_seconds(line)
		if seconds is not None and on_progress is not None and total_seconds > 0:
			progress = min(seconds / total_seconds, 1.0)
			on_progress(progress)


def _progress_seconds(line: str) -> float | None:
	"""Извлекает секунды из строки прогресса ffmpeg.

	Поле ``out_time_ms`` исторически содержит МИКРОсекунды (причуда ffmpeg,
	проверено на 8.0); ``out_time_us`` — его честный синоним.
	"""
	for key in ("out_time_us=", "out_time_ms="):
		if line.startswith(key):
			value = line[len(key) :].strip()
			try:
				return int(value) / 1_000_000
			except ValueError:
				return None
	return None
