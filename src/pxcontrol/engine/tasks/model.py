"""Задачи сообщества: общие типы и контракт вида задачи (ADR-0038).

**Задача** — сохранённая настройка сообщества: вид (что делаем),
параметры, расписание, включена ли. **Запуск** — одно выполнение
задачи: кто запустил, какой исполнитель вёл, чем кончилось, отчёт.
**Задание** — элемент очереди каркаса (:mod:`pxcontrol.engine.jobs`),
который несёт запуск, пока тот выполняется. Три слова — три разные
вещи, и путать их нельзя: задача живёт в базе, задание — в памяти,
запуск — строка журнала.

Каждый **вид** задачи описан одним объектом-спецификацией
(:class:`TaskSpec`): параметры и их проверка, нужное исполнителю право,
само выполнение шагами и отчёт с его переводом в JSON и обратно.
Сервис задач (:mod:`pxcontrol.engine.services.tasks`) о видах ничего
не знает, кроме этого контракта: постановка, очередь, журнал,
расписание и интерфейс у всех видов общие.

Модуль чистый: ни сети, ни базы, ни моделей — только типы и контракт.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.abilities import ExecutorAction
from pxcontrol.engine.services.communities import CommunityDto, ExecutorDto


class TaskError(EngineError):
	"""Ошибка задачи сообщества (с понятным человеку текстом)."""


class TaskKind(StrEnum):
	"""Вид задачи сообщества — значения колонки ``community_tasks.kind``.

	Новый вид — новый модуль в :mod:`pxcontrol.engine.tasks` и строка
	в реестре видов; сервис, очередь, журнал и интерфейс общие.
	"""

	SERVICE_MESSAGES = "service_messages"  # служебные записи в ленте (ADR-0026)
	DELETED_ACCOUNTS = "deleted_accounts"  # удалённые аккаунты среди участников
	REACTIONS = "reactions"  # реакции на записи выбранными пользователями (ADR-0039)


class TaskTrigger(StrEnum):
	"""Кто запустил задачу — значения колонки ``task_runs.trigger``."""

	MANUAL = "manual"  # человек нажал кнопку
	SCHEDULE = "schedule"  # планировщик по расписанию


class RunOutcome(StrEnum):
	"""Исход запуска — значения колонки ``task_runs.outcome``.

	Строка запуска заводится **в начале** работы со значением
	``RUNNING`` и дописывается в конце: если приложение упало посреди
	запуска, в журнале останется честное «начат, не завершён».
	Отмена человеком и остановка движка — разные исходы (ADR-0025):
	первая — решение, вторая — обстоятельство.
	"""

	RUNNING = "running"
	DONE = "done"
	ERROR = "error"
	CANCELLED = "cancelled"  # отменил человек
	INTERRUPTED = "interrupted"  # прервано остановкой движка


#: Одно событие журнала запуска: когда и что случилось.
RunEvent = tuple[datetime, str]

_P = TypeVar("_P")
_R = TypeVar("_R")


class TaskContext:
	"""Что вид задачи получает от сервиса на время одного запуска.

	Снимки сообщества и исполнителя, признак «без изменений», состояние
	вида между запусками и четыре обратных вызова: ход работы для
	карточки, событие в журнал запуска, проверка «пора остановиться»
	и прерываемая пауза. Сам вид ни очереди, ни базы, ни исполнителя
	выбирать не может — это забота сервиса.

	Attributes:
		community: сообщество-цель (снимок: задача работает по
			``tg_chat_id`` и от строки в базе не зависит).
		executor: исполнитель, чьими руками идёт работа (пользователь).
		dry_run: «без изменений» — только посмотреть и посчитать.
		cursor: состояние вида между запусками (JSON-словарь; None —
			ещё не было). Вид пишет сюда новое состояние — сервис
			сохраняет его вместе с исходом запуска.
	"""

	def __init__(
		self,
		community: CommunityDto,
		executor: ExecutorDto,
		*,
		dry_run: bool,
		cursor: dict[str, Any] | None,
		progress: Callable[[float, str | None], None],
		log: Callable[[str], None],
		check_stop: Callable[[], None],
		sleep: Callable[[float], Awaitable[None]],
	) -> None:
		self.community = community
		self.executor = executor
		self.dry_run = dry_run
		self.cursor = cursor
		self._progress = progress
		self._log = log
		self._check_stop = check_stop
		self._sleep = sleep

	def progress(self, fraction: float, note: str | None = None) -> None:
		"""Сообщает долю выполнения и пометку состояния для карточки."""
		self._progress(fraction, note)

	def log(self, text: str) -> None:
		"""Пишет событие в журнал запуска (и в журнал приложения)."""
		self._log(text)

	def check_stop(self) -> None:
		"""Прерывает работу между шагами по отмене или остановке.

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
		"""
		self._check_stop()

	async def sleep(self, seconds: float) -> None:
		"""Пауза между шагами, которую прерывает остановка движка (ADR-0020)."""
		if seconds > 0:
			await self._sleep(seconds)


class TaskSpec(Protocol[_P, _R]):
	"""Контракт вида задачи: параметры, право, выполнение, отчёт.

	Реализуется модулем вида; экземпляр регистрируется в реестре видов
	(:data:`pxcontrol.engine.tasks.SPECS`). Обе структуры — параметры
	и отчёт — неизменяемые датаклассы вида с переводом в JSON и обратно:
	параметры хранятся в строке задачи, отчёт — в строке запуска.
	"""

	kind: TaskKind

	def default_params(self) -> _P:
		"""Параметры новой задачи этого вида."""
		...

	def params_from_payload(self, payload: dict[str, Any] | None) -> _P:
		"""Параметры из колонки JSON; незнакомое и битое — к умолчаниям."""
		...

	def params_to_payload(self, params: _P) -> dict[str, Any]:
		"""Параметры в колонку JSON."""
		...

	def validate(self, params: _P, *, dry_run: bool) -> None:
		"""Проверяет параметры перед постановкой.

		Raises:
			TaskError: Параметры негодны (граница нарушена, нечего делать).
		"""
		...

	def action(self, params: _P, *, dry_run: bool) -> ExecutorAction:
		"""Какое право нужно исполнителю для такого запуска."""
		...

	def choose_executor(
		self, params: _P, cursor: dict[str, Any] | None, capable: Sequence[ExecutorDto]
	) -> ExecutorDto | None:
		"""Кто из способных поведёт этот запуск; None — никто не годится.

		``capable`` — исполнители-пользователи с нужным правом в порядке
		диспетчера (ADR-0036). Большинству видов подходит первый
		(:func:`first_capable`); вид с названными исполнителями выбирает
		среди них по своему состоянию (реакции — по кругу).
		"""
		...

	def title(self, *, dry_run: bool) -> str:
		"""Что делает запуск, по-русски («Чистка служебных записей»)."""
		...

	def report_from_payload(self, payload: dict[str, Any]) -> _R:
		"""Отчёт из колонки JSON строки запуска."""
		...

	def report_to_payload(self, report: _R) -> dict[str, Any]:
		"""Отчёт в колонку JSON."""
		...

	def summary(self, report: _R, *, dry_run: bool) -> str:
		"""Итог запуска одной строкой — для журнала и экрана."""
		...

	async def run(self, ctx: TaskContext, gateway: Any, params: _P) -> _R:
		"""Выполняет один запуск шагами; между шагами спрашивает остановку.

		Raises:
			JobCancelled: Отмену запросил человек или останавливается движок.
			UserbotUnavailableError: Telegram отказал (в том числе
				флуд-лимитом) — запуск прекращается, отчёт не строится.
		"""
		...


@dataclass(frozen=True)
class TaskTitle:
	"""Название вида задачи по-русски для экрана и журнала."""

	noun: str  # «Служебные записи»
	hint: str  # объяснение человеку, что это и зачем


def first_capable(capable: Sequence[ExecutorDto]) -> ExecutorDto | None:
	"""Выбор исполнителя по умолчанию: первый в порядке диспетчера."""
	return capable[0] if capable else None


def check_range(what: str, value: float, limits: tuple[float, float]) -> None:
	"""Проверяет, что число в допустимых границах.

	Raises:
		TaskError: Значение вне границ.
	"""
	low, high = limits
	if not low <= value <= high:
		raise TaskError(f"{what}: допустимо от {low:g} до {high:g}, а указано {value:g}.")


def as_int(payload: dict[str, Any], key: str, default: int) -> int:
	"""Целое из JSON-словаря; отсутствие или не число — умолчание.

	Битое значение не роняет чтение задачи: запись могла сделать
	другая версия приложения, и честнее вернуть умолчание, чем
	отказать в открытии вкладки.
	"""
	value = payload.get(key)
	return value if isinstance(value, int) and not isinstance(value, bool) else default
