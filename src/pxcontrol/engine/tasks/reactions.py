"""Вид задачи «реакции»: выбранные пользователи ставят реакции записям (ADR-0039).

Один **запуск — проход одного пользователя**: он читает ленту, отбирает
записи по охвату и ставит на каждую случайную реакцию из выбранных
с учётом весов. Пользователи идут **по кругу** между запусками
(состояние — курсор задачи), а пауза между проходами — это интервал
расписания задачи (ADR-0038): очередь не держит слот часами ради сна,
перезапуск приложения не начинает круг заново, а журнал получает
строку на каждого пользователя.

Границы обязательны, как у любой массовой работы (ADR-0026): глубина
просмотра, потолок реакций за проход и пауза между реакциями. Массовые
реакции с пользовательских аккаунтов — та же автоматизация, за которую
Telegram ограничивает аккаунты; сдерживают её паузы, потолок и остановка
при флуд-лимите (ADR-0017).

Модуль без интерфейса и базы; выбор реакции, отбор записей и круг
по пользователям — чистые функции.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pxcontrol.engine.services.abilities import ExecutorAction
from pxcontrol.engine.services.communities import ExecutorDto
from pxcontrol.engine.tasks.model import (
	TaskContext,
	TaskError,
	TaskKind,
	TaskTitle,
	as_int,
	check_range,
)
from pxcontrol.engine.telegram.mtproto import UserbotMessageGoneError, UserbotReactionError
from pxcontrol.engine.telegram.types import (
	ChatReactions,
	ChatReactionsMode,
	ExecutorRef,
	OwnerKind,
	ReactablePost,
	ReactionsPage,
)

logger = logging.getLogger(__name__)

#: Сколько сообщений читать одним запросом (предел Telegram — 100).
PAGE_SIZE = 100

#: Сколько последних записей просматривать по умолчанию (решение
#: владельца, 21.09.2026): три запроса к Telegram на проход.
DEFAULT_DEPTH = 256

#: Пределы глубины просмотра.
DEPTH_RANGE = (1, 5000)

#: Потолок реакций за проход по умолчанию — равен глубине: второго
#: числа человек не просил, а потолок у массовой работы обязателен.
DEFAULT_LIMIT = DEFAULT_DEPTH

#: Пределы потолка реакций за проход.
LIMIT_RANGE = (1, 5000)

#: Пауза между реакциями по умолчанию, секунды: случайно в промежутке
#: (решение владельца — «около секунды»); ноль допустим для тестов
#: и означает «только зазор дорожки».
DEFAULT_PAUSE_S = (0.7, 1.5)

#: Пределы паузы между реакциями, секунды.
PAUSE_RANGE = (0.0, 60.0)

#: Сколько случайных записей брать по умолчанию в охвате «случайные».
DEFAULT_RANDOM_COUNT = 3

#: Пределы числа случайных записей.
RANDOM_COUNT_RANGE = (1, 500)

#: Пределы веса реакции, проценты.
WEIGHT_RANGE = (0, 100)

TITLE = TaskTitle(
	noun="Реакции",
	hint=(
		"Выбранные пользователи по очереди ставят реакции записям: под каждой "
		"выпадает случайная из отмеченных с учётом веса. Один запуск — проход "
		"одного пользователя; пауза между проходами — интервал расписания."
	),
)


class ReactionScope(StrEnum):
	"""Каким записям ставить реакции."""

	ALL_WITHOUT_MINE = "all_without_mine"  # все просмотренные без реакции пользователя
	LAST_POST = "last_post"  # только последняя запись
	RANDOM_WITHOUT_MINE = "random_without_mine"  # N случайных без реакции пользователя


#: Охваты по-русски — для формы и журнала.
SCOPE_TITLES: dict[ReactionScope, str] = {
	ReactionScope.ALL_WITHOUT_MINE: "все записи без реакции пользователя",
	ReactionScope.LAST_POST: "только последняя запись",
	ReactionScope.RANDOM_WITHOUT_MINE: "несколько случайных записей без реакции",
}


class ReactionsPort(Protocol):
	"""Часть шлюза Telegram, нужная этому виду (для подмены в тестах)."""

	async def userbot_available_reactions(self, account_id: int, chat_id: str) -> ChatReactions: ...

	async def userbot_reactions_page(
		self, account_id: int, chat_id: str, offset_id: int, limit: int
	) -> ReactionsPage: ...

	async def userbot_send_reaction(
		self, account_id: int, chat_id: str, message_id: int, emojis: Sequence[str]
	) -> None: ...

	def userbot_premium(self, account_id: int | None) -> bool: ...


@dataclass(frozen=True)
class ReactionChoice:
	"""Реакция из набора задачи и её вес (относительный, в процентах)."""

	emoji: str
	weight: int = 100


@dataclass(frozen=True)
class ReactionsParams:
	"""Параметры задачи реакций.

	Attributes:
		users: пользователи пула, которые ставят реакции, по кругу.
		reactions: набор реакций с весами.
		scope: каким записям ставить.
		random_count: сколько случайных записей (только для охвата
			«случайные»).
		depth: сколько последних записей просматривать.
		limit: потолок реакций за один проход.
		pause_min_s: нижняя граница паузы между реакциями, секунды.
		pause_max_s: верхняя граница паузы.
		premium_double: аккаунт с Premium ставит две разные реакции.
	"""

	users: tuple[ExecutorRef, ...] = ()
	reactions: tuple[ReactionChoice, ...] = ()
	scope: ReactionScope = ReactionScope.ALL_WITHOUT_MINE
	random_count: int = DEFAULT_RANDOM_COUNT
	depth: int = DEFAULT_DEPTH
	limit: int = DEFAULT_LIMIT
	pause_min_s: float = DEFAULT_PAUSE_S[0]
	pause_max_s: float = DEFAULT_PAUSE_S[1]
	premium_double: bool = False


@dataclass(frozen=True)
class ReactionsReport:
	"""Итог одного прохода.

	Attributes:
		executor: кто ставил (человеческое имя).
		scanned: сколько сообщений просмотрено.
		candidates: сколько записей подошло под охват.
		reacted: скольким записям реакции поставлены (просмотр — 0).
		skipped: сколько записей Telegram не принял (эмодзи не разрешён,
			предел реакций, запись исчезла) — пропуск, а не сбой.
		by_emoji: сколько раз выпала каждая реакция.
		limited: проход остановился о потолок за проход.
		exhausted: лента кончилась раньше глубины.
	"""

	executor: str = ""
	scanned: int = 0
	candidates: int = 0
	reacted: int = 0
	skipped: int = 0
	by_emoji: dict[str, int] = field(default_factory=dict)
	limited: bool = False
	exhausted: bool = False


def reactions_summary(report: ReactionsReport, *, dry_run: bool) -> str:
	"""Итог прохода одной строкой."""
	who = f" ({report.executor})" if report.executor else ""
	if dry_run:
		return f"Подходящих записей: {report.candidates} из {report.scanned} просмотренных{who}."
	parts = [f"Реакций поставлено: {report.reacted} из {report.candidates} подходящих{who}"]
	if report.by_emoji:
		parts.append(" ".join(f"{emoji} {count}" for emoji, count in report.by_emoji.items()))
	if report.skipped:
		parts.append(f"Telegram не принял: {report.skipped}")
	if report.limited:
		parts.append("сработал предел за проход")
	return ". ".join(parts) + "."


def pick_reaction(choices: Sequence[ReactionChoice], rng: random.Random) -> str | None:
	"""Случайная реакция с учётом весов; None — выбирать не из чего.

	Веса относительные: 50 и 50 — то же, что 100 и 100. Реакции с нулевым
	весом не выпадают, но остаются в наборе — так их можно временно
	отключить, не теряя из списка.
	"""
	weighted = [choice for choice in choices if choice.weight > 0]
	if not weighted:
		return None
	return rng.choices(
		[choice.emoji for choice in weighted], weights=[choice.weight for choice in weighted]
	)[0]


def pick_reactions(choices: Sequence[ReactionChoice], count: int, rng: random.Random) -> list[str]:
	"""``count`` **разных** реакций по весам (меньше — если набор мал)."""
	remaining = list(choices)
	picked: list[str] = []
	while remaining and len(picked) < count:
		emoji = pick_reaction(remaining, rng)
		if emoji is None:
			break
		picked.append(emoji)
		remaining = [choice for choice in remaining if choice.emoji != emoji]
	return picked


def select_targets(
	posts: Sequence[ReactablePost],
	scope: ReactionScope,
	*,
	random_count: int,
	limit: int,
	rng: random.Random,
) -> list[ReactablePost]:
	"""Записи, которым ставить реакции, — по охвату и потолку за проход.

	Записи с реакцией пользователя не берутся ни в одном охвате: ставить
	вторую реакцию значит заменять первую. Порядок — как в ленте (новые
	сначала), у случайного охвата — случайный.
	"""
	fresh = [post for post in posts if not post.mine]
	if scope is ReactionScope.LAST_POST:
		chosen = fresh[:1] if posts and not posts[0].mine else []
	elif scope is ReactionScope.RANDOM_WITHOUT_MINE:
		chosen = rng.sample(fresh, min(random_count, len(fresh)))
	else:
		chosen = fresh
	return chosen[:limit]


def next_user(users: Sequence[ExecutorRef], cursor: dict[str, Any] | None) -> int:
	"""Номер пользователя, чья очередь ставить реакции (по курсору задачи).

	Список пользователей мог измениться с прошлого запуска — номер
	берётся по модулю, чтобы не выпасть за край.
	"""
	if not users:
		return 0
	return as_int(cursor or {}, "next", 0) % len(users)


def rotation(users: Sequence[ExecutorRef], start: int) -> list[ExecutorRef]:
	"""Пользователи по кругу, начиная с ``start``."""
	return [users[(start + shift) % len(users)] for shift in range(len(users))]


class ReactionsTask:
	"""Спецификация вида «реакции» (контракт :class:`TaskSpec`)."""

	kind = TaskKind.REACTIONS

	def __init__(self, rng: random.Random | None = None) -> None:
		"""``rng`` — источник случайности (тесты передают свой)."""
		self._rng = rng if rng is not None else random.Random()

	def default_params(self) -> ReactionsParams:
		return ReactionsParams()

	def params_from_payload(self, payload: dict[str, Any] | None) -> ReactionsParams:
		payload = payload or {}
		raw_users = payload.get("users")
		users = tuple(
			ExecutorRef(OwnerKind.USER, int(item))
			for item in (raw_users if isinstance(raw_users, list) else [])
			if isinstance(item, int) and not isinstance(item, bool)
		)
		raw_reactions = payload.get("reactions")
		reactions = tuple(
			ReactionChoice(str(item["emoji"]), as_int(item, "weight", 100))
			for item in (raw_reactions if isinstance(raw_reactions, list) else [])
			if isinstance(item, dict) and isinstance(item.get("emoji"), str)
		)
		raw_scope = payload.get("scope")
		scope = (
			ReactionScope(raw_scope)
			if isinstance(raw_scope, str) and raw_scope in ReactionScope
			else ReactionScope.ALL_WITHOUT_MINE
		)
		return ReactionsParams(
			users=users,
			reactions=reactions,
			scope=scope,
			random_count=as_int(payload, "random_count", DEFAULT_RANDOM_COUNT),
			depth=as_int(payload, "depth", DEFAULT_DEPTH),
			limit=as_int(payload, "limit", DEFAULT_LIMIT),
			pause_min_s=_as_float(payload, "pause_min_s", DEFAULT_PAUSE_S[0]),
			pause_max_s=_as_float(payload, "pause_max_s", DEFAULT_PAUSE_S[1]),
			premium_double=bool(payload.get("premium_double", False)),
		)

	def params_to_payload(self, params: ReactionsParams) -> dict[str, Any]:
		return {
			"users": [user.id for user in params.users],
			"reactions": [{"emoji": r.emoji, "weight": r.weight} for r in params.reactions],
			"scope": str(params.scope),
			"random_count": params.random_count,
			"depth": params.depth,
			"limit": params.limit,
			"pause_min_s": params.pause_min_s,
			"pause_max_s": params.pause_max_s,
			"premium_double": params.premium_double,
		}

	def validate(self, params: ReactionsParams, *, dry_run: bool) -> None:
		if not params.users:
			raise TaskError("Выберите хотя бы одного пользователя, который будет ставить реакции.")
		if not any(choice.weight > 0 for choice in params.reactions):
			raise TaskError("Отметьте хотя бы одну реакцию с ненулевым весом.")
		for choice in params.reactions:
			check_range(f"Вес реакции {choice.emoji}", choice.weight, WEIGHT_RANGE)
		check_range("Глубина просмотра", params.depth, DEPTH_RANGE)
		check_range("Предел реакций за проход", params.limit, LIMIT_RANGE)
		check_range("Число случайных записей", params.random_count, RANDOM_COUNT_RANGE)
		check_range("Пауза между реакциями, с", params.pause_min_s, PAUSE_RANGE)
		check_range("Пауза между реакциями, с", params.pause_max_s, PAUSE_RANGE)
		if params.pause_max_s < params.pause_min_s:
			raise TaskError("Пауза между реакциями: верхняя граница меньше нижней.")

	def action(self, params: ReactionsParams, *, dry_run: bool) -> ExecutorAction:
		return ExecutorAction.REACT

	def choose_executor(
		self,
		params: ReactionsParams,
		cursor: dict[str, Any] | None,
		capable: Sequence[ExecutorDto],
	) -> ExecutorDto | None:
		"""Следующий по кругу из названных, кто сейчас способен реагировать.

		Неспособные (приостановлен, вышел, реакции запрещены ему)
		пропускаются — очередь переходит к следующему названному.
		"""
		by_owner = {executor.owner: executor for executor in capable}
		for owner in rotation(params.users, next_user(params.users, cursor)):
			executor = by_owner.get(owner)
			if executor is not None:
				return executor
		return None

	def title(self, *, dry_run: bool) -> str:
		return "Реакции: подбор записей" if dry_run else "Реакции: проход"

	def report_from_payload(self, payload: dict[str, Any]) -> ReactionsReport:
		raw = payload.get("by_emoji")
		by_emoji = (
			{str(k): v for k, v in raw.items() if isinstance(v, int)}
			if isinstance(raw, dict)
			else {}
		)
		return ReactionsReport(
			executor=str(payload.get("executor", "")),
			scanned=as_int(payload, "scanned", 0),
			candidates=as_int(payload, "candidates", 0),
			reacted=as_int(payload, "reacted", 0),
			skipped=as_int(payload, "skipped", 0),
			by_emoji=by_emoji,
			limited=bool(payload.get("limited", False)),
			exhausted=bool(payload.get("exhausted", False)),
		)

	def report_to_payload(self, report: ReactionsReport) -> dict[str, Any]:
		return {
			"executor": report.executor,
			"scanned": report.scanned,
			"candidates": report.candidates,
			"reacted": report.reacted,
			"skipped": report.skipped,
			"by_emoji": dict(report.by_emoji),
			"limited": report.limited,
			"exhausted": report.exhausted,
		}

	def summary(self, report: ReactionsReport, *, dry_run: bool) -> str:
		return reactions_summary(report, dry_run=dry_run)

	async def run(
		self, ctx: TaskContext, gateway: ReactionsPort, params: ReactionsParams
	) -> ReactionsReport:
		"""Проход одного пользователя: лента → отбор → реакции с паузами.

		Raises:
			TaskError: В сообществе реакции запрещены или ни одна
				из выбранных здесь не разрешена.
		"""
		account_id = ctx.executor.owner.id
		chat_id = ctx.community.tg_chat_id
		choices = await self._allowed_choices(ctx, gateway, params)
		count = 2 if params.premium_double and gateway.userbot_premium(account_id) else 1
		posts, scanned, exhausted = await self._read_feed(ctx, gateway, params)
		targets = select_targets(
			posts, params.scope, random_count=params.random_count, limit=params.limit, rng=self._rng
		)
		limited = len([p for p in posts if not p.mine]) > len(targets) and (
			params.scope is not ReactionScope.RANDOM_WITHOUT_MINE
		)
		ctx.log(f"просмотрено {scanned}, подходящих записей {len(targets)}")
		reacted = skipped = 0
		by_emoji: dict[str, int] = {}
		if not ctx.dry_run:
			reacted, skipped, by_emoji = await self._react(
				ctx, gateway, account_id, chat_id, targets, choices, count, params
			)
		ctx.cursor = {"next": self._after(params, ctx.executor.owner)}
		ctx.progress(1.0, None)
		return ReactionsReport(
			executor=ctx.executor.label,
			scanned=scanned,
			candidates=len(targets),
			reacted=reacted,
			skipped=skipped,
			by_emoji=by_emoji,
			limited=limited,
			exhausted=exhausted,
		)

	async def _allowed_choices(
		self, ctx: TaskContext, gateway: ReactionsPort, params: ReactionsParams
	) -> list[ReactionChoice]:
		"""Набор задачи, сверенный с тем, что разрешено в сообществе.

		Raises:
			TaskError: Реакции запрещены или пересечение пусто.
		"""
		allowed = await gateway.userbot_available_reactions(
			ctx.executor.owner.id, ctx.community.tg_chat_id
		)
		if allowed.mode is ChatReactionsMode.NONE:
			raise TaskError(f"В «{ctx.community.title}» реакции запрещены — задача невыполнима.")
		choices = [choice for choice in params.reactions if choice.weight > 0]
		if allowed.mode is ChatReactionsMode.SOME:
			permitted = {option.emoji for option in allowed.options}
			dropped = [choice.emoji for choice in choices if choice.emoji not in permitted]
			if dropped:
				ctx.log(f"в сообществе не разрешены и пропущены: {' '.join(dropped)}")
			choices = [choice for choice in choices if choice.emoji in permitted]
		if not choices:
			raise TaskError(
				f"Ни одна из выбранных реакций не разрешена в «{ctx.community.title}» — "
				"выберите другие."
			)
		return choices

	@staticmethod
	async def _read_feed(
		ctx: TaskContext, gateway: ReactionsPort, params: ReactionsParams
	) -> tuple[list[ReactablePost], int, bool]:
		"""Читает ленту на глубину просмотра: записи, просмотрено, кончилась ли."""
		account_id = ctx.executor.owner.id
		chat_id = ctx.community.tg_chat_id
		# последней записи хватит одной страницы: глубина здесь не нужна
		depth = (
			min(params.depth, PAGE_SIZE)
			if params.scope is ReactionScope.LAST_POST
			else params.depth
		)
		posts: list[ReactablePost] = []
		scanned = 0
		offset_id = 0
		exhausted = False
		while scanned < depth:
			ctx.check_stop()
			page = await gateway.userbot_reactions_page(
				account_id, chat_id, offset_id, min(PAGE_SIZE, depth - scanned)
			)
			scanned += page.scanned
			posts.extend(page.posts)
			ctx.progress(min(0.5, 0.5 * scanned / depth), f"просмотрено {scanned}")
			if page.next_offset_id is None:
				exhausted = True
				break
			offset_id = page.next_offset_id
		return posts, scanned, exhausted

	async def _react(
		self,
		ctx: TaskContext,
		gateway: ReactionsPort,
		account_id: int,
		chat_id: str,
		targets: Sequence[ReactablePost],
		choices: Sequence[ReactionChoice],
		count: int,
		params: ReactionsParams,
	) -> tuple[int, int, dict[str, int]]:
		"""Ставит реакции записям с паузами; отказ по записи — пропуск."""
		reacted = skipped = 0
		by_emoji: dict[str, int] = {}
		for number, post in enumerate(targets):
			ctx.check_stop()
			emojis = pick_reactions(choices, count, self._rng)
			try:
				await gateway.userbot_send_reaction(account_id, chat_id, post.id, emojis)
			except (UserbotReactionError, UserbotMessageGoneError) as exc:
				skipped += 1
				ctx.log(f"запись {post.id}: {exc}")
			else:
				reacted += 1
				for emoji in emojis:
					by_emoji[emoji] = by_emoji.get(emoji, 0) + 1
			ctx.progress(0.5 + 0.5 * (number + 1) / len(targets), f"реакций {reacted}")
			if number + 1 < len(targets):
				await ctx.sleep(self._rng.uniform(params.pause_min_s, params.pause_max_s))
		return reacted, skipped, by_emoji

	@staticmethod
	def _after(params: ReactionsParams, owner: ExecutorRef) -> int:
		"""Номер следующего по кругу после того, кто вёл проход."""
		if owner in params.users:
			return (params.users.index(owner) + 1) % len(params.users)
		return 0


def _as_float(payload: dict[str, Any], key: str, default: float) -> float:
	"""Число с плавающей точкой из JSON-словаря; иное — умолчание."""
	value = payload.get(key)
	return (
		float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default
	)
