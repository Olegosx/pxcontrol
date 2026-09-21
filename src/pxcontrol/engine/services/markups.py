"""Обещанная клавиатура: хранение до момента, когда её можно применить (ADR-0031).

Кнопки ставит только бот и только **после** публикации. Пост, у которого
есть своя строка (элемент очереди отправки), возит клавиатуру рядом
с собой — колонкой. А у отложенной записи своей строки нет: она живёт
на сервере Telegram (ADR-0010). Поэтому обещание держится отдельной
записью — сообщество, номер отложки, время, текст для опознания
и сама клавиатура.

Кроме хранения сервис **применяет** обещания: у отложенного поста
кнопки можно поставить лишь после выхода, поэтому по обещаниям ходит
дозор — периодическая задача движка. Его шаги: дождаться времени
публикации, опознать вышедший пост (точное совпадение текста и даты —
ADR-0031, п. 9), дорисовать клавиатуру ботом, снять обещание.

Дозор написан осторожным: промах опаснее отсутствия кнопок, поэтому
двусмысленность («нашлось два поста с таким текстом») трактуется как
«не нашлось»; приостановленный бот — повод подождать, а не считать
попытку; флуд-лимит прекращает проход, а не молотит дальше.

**Срок обещания — сутки** (:data:`APPLY_MAX_AGE`), и считается он
временем, а не числом попыток. Прежнее правило «пять попыток подряд»
означало пять минут: сеть, мигнувшая на десять, хоронила кнопки
у вышедшего поста, хотя ничего непоправимого не случилось. Время —
честная мера: за сутки поправимое (нет связи, бот на паузе, право
вернули) успевает поправиться, а непоправимое остаётся непоправимым.
По истечении срока обещание отпускается: запись в журнал и причина
в самом обещании, чтобы человек увидел её на карточке поста.

Здесь же живут переходы обещания из матрицы маршрутов: перенос времени
обещание не рвёт, удаление поста его снимает, неудачная попытка остаётся
в записи.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community, CommunityExecutor, PromisedMarkup
from pxcontrol.engine.db.types import as_utc, as_utc_optional
from pxcontrol.engine.errors import user_message
from pxcontrol.engine.periodic import PeriodicTask
from pxcontrol.engine.services.abilities import ExecutorAction
from pxcontrol.engine.services.community_rights import bot_ref, ranked_executors
from pxcontrol.engine.telegram.bot_api import BotMessageGoneError
from pxcontrol.engine.telegram.lane import LaneLiveState
from pxcontrol.engine.telegram.markup import (
	MarkupError,
	PostMarkup,
	markup_from_json,
	markup_to_json,
	validate_markup,
)
from pxcontrol.engine.telegram.types import BotRef, ExecutorRef, OwnerKind, TelegramFloodError

logger = logging.getLogger(__name__)

#: Как часто дозор проверяет обещания. Минута — компромисс: кнопки
#: появляются вскоре после выхода поста, а дорожка аккаунта (ADR-0024)
#: от такого темпа не страдает — проход обычно не делает ни одного
#: запроса (обещаний нет).
APPLY_TICK_S = 60

#: Сколько свежих записей просматривать, опознавая вышедший пост.
#: Отложка выходит в назначенную минуту, и за время до прихода дозора
#: канал успевает получить единицы постов; сотня — с большим запасом.
HISTORY_LOOKUP_LIMIT = 100

#: Запас назад при поиске: сервер публикует отложку в названную минуту,
#: но дата поста может отличаться на секунды.
LOOKUP_MARGIN = timedelta(minutes=2)

#: Сколько дозор ходит за обещанием, прежде чем отпустить его. Отсчёт —
#: от ожидаемого выхода поста (у поста «сейчас» — от самого обещания).
#: Сутки: за это время переживается всё поправимое — обрыв связи,
#: приостановленный бот, отобранное и возвращённое право; дальше
#: причина уже не заминка, и ходить за обещанием вечно незачем.
APPLY_MAX_AGE = timedelta(days=1)

#: Предел ожидания дозора при остановке движка (ADR-0020).
_SHUTDOWN_TIMEOUT_S = 10.0


def promise_deadline(promise: PromisedMarkupDto) -> datetime:
	"""До какого момента дозор ходит за обещанием.

	Отсчёт — от ожидаемого выхода поста; у поста «сейчас» такого
	времени нет, и считаем от самого обещания. Чистая функция:
	правило срока проверяется тестом без базы и сети.
	"""
	return (promise.when or promise.created_at) + APPLY_MAX_AGE


def promise_expired(promise: PromisedMarkupDto, moment: datetime) -> bool:
	"""Вышел ли срок обещания (дозор за ним больше не ходит).

	Срок меряется настенным временем, а дозор живёт, только пока
	запущено приложение, — и это не одно и то же. Поставили посты
	с кнопками на пятницу, приложение до воскресенья не открывали:
	срок вышел, хотя **ни одной попытки не было**, и кнопки пропали бы
	молча. Поэтому просрочка отпускает обещание лишь после настоящей
	попытки (``attempts``): у каждого обещания есть хотя бы один шанс,
	и он приходится на первый запуск после срока.
	"""
	return promise.attempts > 0 and moment > promise_deadline(promise)


class _MarkupPort(Protocol):
	"""Часть шлюза, нужная дозору кнопок (разделение интерфейсов)."""

	async def userbot_find_published(
		self, account_id: int, chat_id: str, text: str, after: datetime, limit: int
	) -> int | None: ...

	async def bot_edit_markup(
		self, bot: BotRef, chat_id: str, message_id: int, markup: PostMarkup | None
	) -> None: ...

	def live_states(self) -> dict[ExecutorRef, LaneLiveState]: ...


@dataclass(frozen=True)
class PromisedMarkupDto:
	"""Обещание клавиатуры для поста, которого у нас нет.

	Attributes:
		id: номер записи обещания.
		community_id: сообщество, где выйдет пост.
		scheduled_message_id: номер отложенной записи на сервере
			(None — отложки нет: пост «сейчас» или уже вышел).
		message_id: номер вышедшего поста (None — он ещё не вышел):
			по нему повторяют неудавшуюся правку клавиатуры.
		when: ожидаемый момент публикации (None — «сейчас»).
		match_text: текст или подпись поста для точного опознания
			вышедшего поста (ADR-0031, п. 9).
		markup: сама клавиатура.
		attempts: сколько раз пытались применить.
		error: текст последней неудачи (None — ещё не пытались
			или прошлая попытка не оставила следа).
		created_at: когда обещание записано — от него считается срок
			у поста «сейчас», которому ожидаемого времени не назначали.
	"""

	id: int
	community_id: int
	scheduled_message_id: int | None
	message_id: int | None
	when: datetime | None
	match_text: str
	markup: PostMarkup
	attempts: int
	error: str | None
	created_at: datetime


class MarkupsService:
	"""Хранилище обещанных клавиатур и дозор, который их применяет (ADR-0031)."""

	def __init__(self, db: Database, gateway: _MarkupPort | None = None) -> None:
		"""``gateway`` — шлюз Telegram; None — только хранение, без дозора
		(в тестах хранилища сеть не нужна)."""
		self._db = db
		self._gateway = gateway
		self._poller = PeriodicTask(
			self.apply_due,
			name="Дозор кнопок",
			interval_s=APPLY_TICK_S,
			shutdown_timeout_s=_SHUTDOWN_TIMEOUT_S,
		)
		#: обещания, срок которых уже отмечен в этом запуске (см. _release)
		self._released: set[int] = set()

	async def promise(
		self,
		community_id: int,
		markup: PostMarkup,
		*,
		match_text: str,
		when: datetime | None = None,
		scheduled_message_id: int | None = None,
		message_id: int | None = None,
	) -> int:
		"""Запоминает, что посту обещана клавиатура.

		Клавиатура проверяется здесь же: в базе не должно оказаться
		того, что Telegram всё равно не примет или молча обрежет —
		иначе человек узнал бы о потере кнопок уже из канала.

		Args:
			community_id: сообщество, где выйдет пост.
			markup: клавиатура (пустая недопустима — обещать нечего).
			match_text: текст или подпись поста для опознания.
			when: ожидаемый момент публикации (None — «сейчас»).
			scheduled_message_id: номер отложенной записи, если она есть.
			message_id: номер **вышедшего** поста, если пост уже
				опубликован, а кнопки поставить не удалось.

		Returns:
			Номер записи обещания.

		Raises:
			MarkupError: Клавиатура пуста или не проходит пределы Telegram.
		"""
		if not markup:
			raise MarkupError("Клавиатура пуста — обещать нечего.")
		validate_markup(markup)
		row = PromisedMarkup(
			community_id=community_id,
			scheduled_message_id=scheduled_message_id,
			message_id=message_id,
			when=when,
			match_text=match_text,
			markup=markup_to_json(markup),
		)
		async with self._db.session_factory() as session:
			session.add(row)
			await session.commit()
			promise_id = row.id
		logger.info(
			"Сообществу id=%s обещана клавиатура (%d кнопок): обещание id=%s, отложка %s.",
			community_id,
			len(markup.buttons),
			promise_id,
			scheduled_message_id or "—",
		)
		return promise_id

	async def pending(self, community_id: int | None = None) -> list[PromisedMarkupDto]:
		"""Неприменённые обещания: все или одного сообщества.

		Порядок — по ожидаемому времени публикации, затем по номеру:
		первым идёт то, чей пост выйдет раньше.
		"""
		query = select(PromisedMarkup).order_by(PromisedMarkup.when, PromisedMarkup.id)
		if community_id is not None:
			query = query.where(PromisedMarkup.community_id == community_id)
		async with self._db.session_factory() as session:
			rows = (await session.execute(query)).scalars().all()
		return [dto for row in rows if (dto := self._dto(row)) is not None]

	async def reschedule(self, promise_id: int, when: datetime | None) -> None:
		"""Переносит обещание на новое время публикации.

		Перенос времени отложенной записи обещание не рвёт: клавиатуру
		обещали посту, а не дате (матрица нестандартных телодвижений).
		"""
		async with self._db.session_factory() as session:
			await session.execute(
				update(PromisedMarkup).where(PromisedMarkup.id == promise_id).values(when=when)
			)
			await session.commit()
		logger.info("Обещание id=%s перенесено на %s.", promise_id, when or "«сейчас»")

	async def promised_ids(self, community_id: int) -> set[int]:
		"""Номера отложенных записей сообщества, которым обещаны кнопки.

		Нужно вкладке «Отложено»: человек должен видеть, что у записи
		будут кнопки, хотя сейчас их нет и быть не может (ADR-0031).
		"""
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(PromisedMarkup.scheduled_message_id).where(
						PromisedMarkup.community_id == community_id,
						PromisedMarkup.scheduled_message_id.is_not(None),
					)
				)
			).scalars()
		return {int(value) for value in rows if value is not None}

	async def post_promises(self, community_id: int) -> dict[int, str]:
		"""Вышедшие посты сообщества, которым кнопки обещаны, но не стоят.

		Номер поста → текст последней неудачи (пустая строка — попыток
		ещё не было). Нужно экрану «Опубликовано» (ADR-0032): пост вышел,
		а клавиатуры под ним нет — человек должен видеть, что она обещана,
		и чем кончилась последняя попытка, а не гадать, куда делись кнопки.
		"""
		async with self._db.session_factory() as session:
			rows = (
				await session.execute(
					select(PromisedMarkup.message_id, PromisedMarkup.error).where(
						PromisedMarkup.community_id == community_id,
						PromisedMarkup.message_id.is_not(None),
					)
				)
			).all()
		return {int(message_id): error or "" for message_id, error in rows if message_id}

	async def retarget(
		self,
		community_id: int,
		scheduled_message_id: int,
		*,
		when: datetime | None = None,
		match_text: str | None = None,
	) -> bool:
		"""Ведёт обещание за правкой его отложенной записи.

		Правка отложки меняет время и текст поста — а обещание опознаёт
		вышедший пост как раз по ним (ADR-0031, п. 9). Без переноса дозор
		искал бы вчерашний текст на вчерашнее время и не нашёл бы ничего:
		пост вышел бы без кнопок, хотя всё было в порядке.

		Args:
			community_id: сообщество записи.
			scheduled_message_id: номер отложенной записи.
			when: новое время публикации (None — не менять).
			match_text: новый текст для опознания (None — не менять).

		Returns:
			True — обещание нашлось и обновлено; False — его не было
			(у поста просто нет кнопок, и это не ошибка).
		"""
		values: dict[str, object] = {}
		if when is not None:
			values["when"] = when
		if match_text is not None:
			values["match_text"] = match_text
		if not values:
			return False
		async with self._db.session_factory() as session:
			result = await session.execute(
				update(PromisedMarkup)
				.where(
					PromisedMarkup.community_id == community_id,
					PromisedMarkup.scheduled_message_id == scheduled_message_id,
				)
				.values(**values)
			)
			await session.commit()
		moved = int(getattr(result, "rowcount", 0) or 0) > 0
		if moved:
			logger.info(
				"Обещание кнопок отложки %s в сообществе id=%s поехало за правкой.",
				scheduled_message_id,
				community_id,
			)
		return moved

	async def remember_post(self, promise_id: int, message_id: int) -> None:
		"""Запоминает опознанный пост за обещанием.

		Нужно до попытки поставить клавиатуру, а не после удачи: если
		правка не пройдёт (у бота отняли право, он на паузе), запись
		с номером поста покажет причину на «Опубликовано» — экран
		отбирает обещания именно по номеру (ADR-0031, п. 12). Заодно
		следующая попытка не перечитывает историю сообщества: пост
		уже найден, а дозор просыпается раз в минуту целые сутки.
		"""
		async with self._db.session_factory() as session:
			await session.execute(
				update(PromisedMarkup)
				.where(PromisedMarkup.id == promise_id)
				.values(message_id=message_id)
			)
			await session.commit()

	async def fail(self, promise_id: int, error: str) -> None:
		"""Записывает неудачную попытку применить клавиатуру.

		Обещание остаётся: кнопок у поста просто нет, и это исход,
		а не сбой поста — человек увидит причину и сможет повторить.
		"""
		async with self._db.session_factory() as session:
			await session.execute(
				update(PromisedMarkup)
				.where(PromisedMarkup.id == promise_id)
				.values(attempts=PromisedMarkup.attempts + 1, error=error)
			)
			await session.commit()
		logger.info("Обещание id=%s: попытка не удалась (%s).", promise_id, error)

	async def drop(self, promise_id: int) -> None:
		"""Снимает обещание (применено или пост исчез).

		Применённое обещание не хранится: в базе остаётся только то,
		чего в Telegram ещё нет (ADR-0031, п. 8).
		"""
		async with self._db.session_factory() as session:
			await session.execute(delete(PromisedMarkup).where(PromisedMarkup.id == promise_id))
			await session.commit()
		logger.info("Обещание id=%s снято.", promise_id)

	async def drop_post(self, community_id: int, message_id: int) -> None:
		"""Снимает обещание кнопок вышедшего поста: с ними разобрались.

		Зовётся крючком движка, когда человек поставил или снял кнопки
		руками либо удалил сам пост (ADR-0032, подача A4): дозору
		повторять больше нечего, а оставленное обещание он бы применил
		поверх решения человека.
		"""
		async with self._db.session_factory() as session:
			result = await session.execute(
				delete(PromisedMarkup).where(
					PromisedMarkup.community_id == community_id,
					PromisedMarkup.message_id == message_id,
				)
			)
			await session.commit()
		if int(getattr(result, "rowcount", 0) or 0):
			logger.info(
				"Обещание кнопок поста %s в сообществе id=%s снято: с кнопками разобрались.",
				message_id,
				community_id,
			)

	async def drop_scheduled(self, community_id: int, message_ids: Sequence[int]) -> int:
		"""Снимает обещания названных отложенных записей сообщества.

		Зовётся, когда отложки не стало: её удалили, опубликовали
		«сейчас» или она исчезла на сервере. Возвращает число снятых —
		по нему видно, было ли что снимать.
		"""
		if not message_ids:
			return 0
		async with self._db.session_factory() as session:
			result = await session.execute(
				delete(PromisedMarkup).where(
					PromisedMarkup.community_id == community_id,
					PromisedMarkup.scheduled_message_id.in_(list(message_ids)),
				)
			)
			await session.commit()
		dropped = int(getattr(result, "rowcount", 0) or 0)
		if dropped:
			logger.info(
				"Сообщество id=%s: снято обещаний клавиатуры — %d (отложки исчезли).",
				community_id,
				dropped,
			)
		return dropped

	# --- применение (дозор) ---------------------------------------------------

	def start_polling(self) -> None:
		"""Запускает дозор кнопок (при старте движка)."""
		if self._gateway is None:
			return
		self._poller.start()

	async def shutdown(self) -> None:
		"""Гасит дозор кооперативно (ADR-0020).

		Между обещаниями задача выходит сразу, начатое обращение
		к Telegram дожидается конца; не успевшая за страховочный срок —
		отменяется как последнее средство.
		"""
		await self._poller.shutdown()

	async def apply_due(self, now: datetime | None = None) -> int:
		"""Один проход дозора: ставит кнопки там, где это уже можно.

		Обещание берётся в работу, когда его пост мог появиться в канале:
		у отложенного — после названного времени, у вышедшего (правка
		не прошла с первого раза) — сразу. Флуд-лимит прекращает проход:
		остальные обещания дождутся следующего.

		Returns:
			Сколько обещаний применено.
		"""
		if self._gateway is None:
			return 0
		moment = now or datetime.now(UTC)
		applied = 0
		for promise in await self.pending():
			if self._poller.stopping:
				break
			if promise_expired(promise, moment):
				await self._release(promise)
				continue
			if not self._is_due(promise, moment):
				continue
			try:
				if await self._apply_one(promise, moment):
					applied += 1
			except TelegramFloodError as exc:
				logger.info("Дозор кнопок отступает: %s", exc)
				break
		return applied

	async def drop_scheduled_quiet(self, community_id: int, message_ids: list[int]) -> None:
		"""То же, что :meth:`drop_scheduled`, но без возврата числа.

		Подпись под крючок движка: вызывающему (посты) знать, было ли
		что снимать, незачем — у поста могло и не быть кнопок.
		"""
		await self.drop_scheduled(community_id, message_ids)

	@staticmethod
	def _is_due(promise: PromisedMarkupDto, moment: datetime) -> bool:
		"""Пора ли браться за обещание: пост уже мог появиться в ленте."""
		return promise.when is None or promise.when <= moment

	async def _release(self, promise: PromisedMarkupDto) -> None:
		"""Отпускает обещание, у которого вышел срок (:data:`APPLY_MAX_AGE`).

		Запись остаётся в базе: человек должен увидеть на карточке поста,
		что кнопки обещаны и почему их нет (ADR-0032). Причина
		дописывается один раз за запуск приложения — набор отпущенных
		живёт в памяти: перезапуск перепишет тот же текст, и это дешевле
		колонки в базе ради одной пометки.
		"""
		if promise.id in self._released:
			return
		self._released.add(promise.id)
		reason = promise.error or "вышедший пост не опознан"
		text = f"срок обещания вышел (сутки), кнопки так и не встали: {reason}"
		async with self._db.session_factory() as session:
			await session.execute(
				update(PromisedMarkup).where(PromisedMarkup.id == promise.id).values(error=text)
			)
			await session.commit()
		logger.warning(
			"Обещание id=%s отпущено: %s. Пост остался без кнопок — поставьте их "
			"правкой поста на «Опубликовано».",
			promise.id,
			text,
		)

	async def _apply_one(self, promise: PromisedMarkupDto, moment: datetime) -> bool:
		"""Пытается поставить кнопки одному посту.

		Returns:
			True — кнопки поставлены и обещание снято; False — ещё рано,
			некому или не вышло (причина осталась в записи и в журнале).

		Raises:
			TelegramFloodError: Telegram просит подождать — проход
				прекращается целиком (не наше дело перебирать дальше).
		"""
		place = await self._place(promise.community_id)
		if place is None:
			return False
		chat_id, account_id, bot = place
		message_id = promise.message_id
		if message_id is None:
			after = (promise.when or moment) - LOOKUP_MARGIN
			message_id = await self._gateway.userbot_find_published(  # type: ignore[union-attr]
				account_id, chat_id, promise.match_text, after, HISTORY_LOOKUP_LIMIT
			)
			if message_id is not None:
				await self.remember_post(promise.id, message_id)
		if message_id is None:
			await self.fail(promise.id, "вышедший пост не опознан")
			return False
		try:
			await self._gateway.bot_edit_markup(bot, chat_id, message_id, promise.markup)  # type: ignore[union-attr]
		except TelegramFloodError:
			raise
		except BotMessageGoneError:
			# пост удалили из другого клиента: ставить кнопки некуда,
			# и ходить за этим обещанием сутки незачем
			await self.drop(promise.id)
			logger.info(
				"Обещание id=%s снято: поста id=%s в сообществе id=%s больше нет.",
				promise.id,
				message_id,
				promise.community_id,
			)
			return False
		except Exception as exc:  # noqa: BLE001 — исход обещания, а не дозора
			await self.fail(promise.id, user_message(exc))
			return False
		await self.drop(promise.id)
		logger.info(
			"Кнопки поставлены посту id=%s в сообществе id=%s (обещание id=%s).",
			message_id,
			promise.community_id,
			promise.id,
		)
		return True

	async def _place(self, community_id: int) -> tuple[str, int, BotRef] | None:
		"""Куда и кем ставить кнопки (None — сейчас некем).

		Приостановленный или отвязанный бот, пропавший публикатор —
		повод подождать, а не тратить попытку: человек вернёт их сам
		(матрица маршрутов, ADR-0029).
		"""
		async with self._db.session_factory() as session:
			# связи подгружаются явно: ленивая загрузка в асинхронном
			# режиме запрещена, а возможности сообщества считаются
			# по боту и публикатору
			community = (
				await session.execute(
					select(Community)
					.options(
						selectinload(Community.default_bot),
						selectinload(Community.default_account),
						selectinload(Community.executors).selectinload(
							CommunityExecutor.tg_account
						),
						selectinload(Community.executors).selectinload(CommunityExecutor.bot),
					)
					.where(Community.id == community_id)
				)
			).scalar_one_or_none()
			if community is None:
				return None
			# кого спросить — решает диспетчер по пулу (ADR-0036): бот
			# с правом править чужое ставит кнопки, свободный состоящий
			# пользователь опознаёт вышедший пост
			live = self._gateway.live_states()  # type: ignore[union-attr]
			bots = ranked_executors(community, ExecutorAction.EDIT_OTHERS, live, kind=OwnerKind.BOT)
			if not bots:
				logger.info(
					"Сообщество id=%s пока не может принять кнопки (бот или право) — жду.",
					community_id,
				)
				return None
			readers = ranked_executors(
				community, ExecutorAction.READ_HISTORY, live, kind=OwnerKind.USER
			)
			if not readers:
				logger.info(
					"У сообщества id=%s нет пользователя — вышедший пост опознать нечем, жду.",
					community_id,
				)
				return None
			return community.tg_chat_id, int(readers[0].tg_account_id or 0), bot_ref(bots[0])

	@staticmethod
	def _dto(row: PromisedMarkup) -> PromisedMarkupDto | None:
		"""Снимок записи обещания (None — клавиатура в записи не разобралась).

		Повреждённую запись не показываем и не применяем: без клавиатуры
		обещание бессмысленно, а причина уже в журнале (``markup_from_json``).
		"""
		markup = markup_from_json(row.markup, f"обещание кнопок id={row.id}")
		if markup is None:
			logger.warning("Обещание id=%s без разборчивой клавиатуры — пропущено.", row.id)
			return None
		return PromisedMarkupDto(
			id=row.id,
			community_id=row.community_id,
			scheduled_message_id=row.scheduled_message_id,
			message_id=row.message_id,
			when=as_utc_optional(row.when),
			match_text=row.match_text,
			markup=markup,
			attempts=row.attempts,
			error=row.error,
			created_at=as_utc(row.created_at),
		)
