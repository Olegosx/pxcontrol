"""Обещанная клавиатура: хранение до момента, когда её можно применить (ADR-0031).

Кнопки ставит только бот и только **после** публикации. Пост, у которого
есть своя строка (элемент очереди отправки), возит клавиатуру рядом
с собой — колонкой. А у отложенной записи своей строки нет: она живёт
на сервере Telegram (ADR-0010). Поэтому обещание держится отдельной
записью — сообщество, номер отложки, время, текст для опознания
и сама клавиатура.

Сервис занят только хранением: он ничего не ставит и никуда не ходит.
Применение — найти вышедший пост и дорисовать кнопки — работа отправки
и появится следующим этапом. Здесь же живут те переходы обещания, которые
описаны матрицей маршрутов: перенос времени обещание не рвёт, удаление
поста его снимает, неудачная попытка остаётся в записи.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import PromisedMarkup
from pxcontrol.engine.db.types import as_utc_optional
from pxcontrol.engine.telegram.markup import (
	MarkupError,
	PostMarkup,
	markup_from_json,
	markup_to_json,
	validate_markup,
)

logger = logging.getLogger(__name__)


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


class MarkupsService:
	"""Хранилище обещанных клавиатур (ADR-0031)."""

	def __init__(self, db: Database) -> None:
		self._db = db

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

	@staticmethod
	def _dto(row: PromisedMarkup) -> PromisedMarkupDto | None:
		"""Снимок записи обещания (None — клавиатура в записи не разобралась).

		Повреждённую запись не показываем и не применяем: без клавиатуры
		обещание бессмысленно, а причина уже в журнале (``markup_from_json``).
		"""
		markup = markup_from_json(row.markup)
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
		)
