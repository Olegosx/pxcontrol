"""Сервис подписей к постам: поля со словарями, шаблоны, сборка текста.

Поле канала («Genre», «Year»…) хранит свой словарь значений один раз;
шаблоны — именованные наборы полей с порядком. Сборка подписи — чистые
функции: жирное название (Markdown, Telethon парсит его по умолчанию)
и строки «Поле: значения» (с решётками или без).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import (
	CaptionField,
	CaptionTemplate,
	CaptionTemplateField,
	CaptionValue,
)

logger = logging.getLogger(__name__)

#: Суффикс имён файлов нашего конвейера: _<пресет>_<ГГГГММДД-ЧЧММСС>.
_PIPELINE_SUFFIX = re.compile(r"_[^_]+_\d{8}-\d{6}$")


class CaptionsError(Exception):
	"""Ошибка работы с подписями (с понятным человеку текстом)."""


@dataclass(frozen=True)
class FieldDto:
	"""Поле подписи со словарём значений (для интерфейса)."""

	id: int
	name: str
	hashtag: bool
	multiple: bool
	values: list[str]


@dataclass(frozen=True)
class TemplateFieldDto:
	"""Поле в составе шаблона: само поле и включённость по умолчанию."""

	field: FieldDto
	enabled: bool


@dataclass(frozen=True)
class TemplateDto:
	"""Шаблон подписи: имя и упорядоченный состав полей."""

	id: int
	name: str
	last_used_at: datetime | None
	fields: list[TemplateFieldDto]


@dataclass(frozen=True)
class CaptionLine:
	"""Строка подписи для сборки: имя поля, оформление, значения."""

	name: str
	hashtag: bool
	values: list[str]


# --- чистые функции сборки ---------------------------------------------------


def hashtag(value: str) -> str:
	"""Превращает значение в хэштег: «Tomb Raider» → «#TombRaider».

	Слова склеиваются с заглавной буквы (пробелы и знаки в хэштеге
	Telegram не допускает); не-буквенные символы отбрасываются.
	"""
	words = [w for w in re.split(r"[^\w]+|_", value) if w]
	return "#" + "".join(w[:1].upper() + w[1:] for w in words)


def build_caption(title: str, lines: list[CaptionLine]) -> str:
	"""Собирает текст подписи: жирное название + строки полей.

	Строки без значений пропускаются. Разметка — Markdown
	(``**название**``), Telethon применяет её по умолчанию.
	"""
	rows = [f"**{title.strip()}**"] if title.strip() else []
	for line in lines:
		values = [v for v in (raw.strip() for raw in line.values) if v]
		if not values:
			continue
		rendered = ", ".join(
			hashtag(v) if line.hashtag else v for v in values
		)
		rows.append(f"{line.name}: {rendered}")
	return "\n".join(rows)


def title_from_filename(path: str) -> str:
	"""Название поста из имени файла (без суффикса нашего конвейера)."""
	stem = Path(path).stem
	return _PIPELINE_SUFFIX.sub("", stem).strip()


# --- сервис -------------------------------------------------------------------


class CaptionsService:
	"""Поля, словари и шаблоны подписей каналов."""

	def __init__(self, db: Database) -> None:
		self._db = db

	# --- поля и словари ---------------------------------------------------

	async def list_fields(self, channel_id: int) -> list[FieldDto]:
		"""Возвращает поля канала со словарями значений."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(
				select(CaptionField)
				.options(selectinload(CaptionField.values))
				.where(CaptionField.channel_id == channel_id)
				.order_by(CaptionField.id)
			)).scalars().all()
			return [self._field_dto(f) for f in rows]

	async def add_field(
		self, channel_id: int, name: str, hashtag: bool, multiple: bool
	) -> FieldDto:
		"""Добавляет поле в пул канала.

		Raises:
			CaptionsError: Пустое имя или поле с таким именем уже есть.
		"""
		name = name.strip()
		if not name:
			raise CaptionsError("У поля должно быть имя.")
		async with self._db.session_factory() as session:
			exists = (await session.execute(
				select(CaptionField).where(
					CaptionField.channel_id == channel_id,
					CaptionField.name == name,
				)
			)).scalar_one_or_none()
			if exists is not None:
				raise CaptionsError(f"Поле «{name}» уже есть у канала.")
			field = CaptionField(
				channel_id=channel_id, name=name,
				hashtag=hashtag, multiple=multiple,
			)
			session.add(field)
			await session.commit()
			await session.refresh(field)
		logger.info("Поле подписи «%s» добавлено (канал id=%s).", name, channel_id)
		return FieldDto(field.id, field.name, field.hashtag, field.multiple, [])

	async def delete_field(self, field_id: int) -> None:
		"""Удаляет поле, его словарь и строки состава шаблонов."""
		async with self._db.session_factory() as session:
			await session.execute(
				delete(CaptionTemplateField)
				.where(CaptionTemplateField.field_id == field_id)
			)
			await session.execute(
				delete(CaptionValue).where(CaptionValue.field_id == field_id)
			)
			await session.execute(
				delete(CaptionField).where(CaptionField.id == field_id)
			)
			await session.commit()

	# --- шаблоны -----------------------------------------------------------

	async def list_templates(self, channel_id: int) -> list[TemplateDto]:
		"""Возвращает шаблоны канала с полным составом полей."""
		async with self._db.session_factory() as session:
			rows = (await session.execute(
				select(CaptionTemplate)
				.options(
					selectinload(CaptionTemplate.fields)
					.selectinload(CaptionTemplateField.field)
					.selectinload(CaptionField.values)
				)
				.where(CaptionTemplate.channel_id == channel_id)
				.order_by(CaptionTemplate.id)
			)).scalars().all()
			return [self._template_dto(t) for t in rows]

	async def save_template(
		self, channel_id: int, name: str, field_ids: list[int],
		template_id: int | None = None,
	) -> TemplateDto:
		"""Создаёт или перезаписывает шаблон (состав — в порядке списка).

		Raises:
			CaptionsError: Пустое имя, пустой состав или шаблон не найден.
		"""
		name = name.strip()
		if not name:
			raise CaptionsError("У шаблона должно быть имя.")
		if not field_ids:
			raise CaptionsError("Выберите хотя бы одно поле для шаблона.")
		async with self._db.session_factory() as session:
			template = await self._get_or_create_template(
				session, channel_id, name, template_id
			)
			saved_id = template.id
			await session.execute(
				delete(CaptionTemplateField)
				.where(CaptionTemplateField.template_id == saved_id)
			)
			for position, field_id in enumerate(field_ids):
				session.add(CaptionTemplateField(
					template_id=saved_id, field_id=field_id,
					position=position, enabled=True,
				))
			await session.commit()
		logger.info("Шаблон подписи «%s» сохранён (канал id=%s).", name, channel_id)
		templates = await self.list_templates(channel_id)
		return next(t for t in templates if t.id == saved_id)

	async def delete_template(self, template_id: int) -> None:
		"""Удаляет шаблон и его состав (словари полей не трогаются)."""
		async with self._db.session_factory() as session:
			await session.execute(
				delete(CaptionTemplateField)
				.where(CaptionTemplateField.template_id == template_id)
			)
			await session.execute(
				delete(CaptionTemplate).where(CaptionTemplate.id == template_id)
			)
			await session.commit()

	async def record_usage(
		self, template_id: int, used_values: dict[int, list[str]]
	) -> None:
		"""Фиксирует использование шаблона: словари пополняются сами.

		``used_values`` — значения по id полей; новые (без учёта регистра)
		добавляются в словарь. Шаблону отмечается момент использования —
		для предвыбора в диалоге.
		"""
		async with self._db.session_factory() as session:
			for field_id, values in used_values.items():
				known = {
					v.lower() for (v,) in await session.execute(
						select(CaptionValue.value)
						.where(CaptionValue.field_id == field_id)
					)
				}
				for value in dict.fromkeys(v.strip() for v in values):
					if value and value.lower() not in known:
						session.add(CaptionValue(field_id=field_id, value=value))
						known.add(value.lower())
			template = await session.get(CaptionTemplate, template_id)
			if template is not None:
				template.last_used_at = datetime.now(UTC)
			await session.commit()

	# --- внутреннее ---------------------------------------------------------

	@staticmethod
	async def _get_or_create_template(
		session: AsyncSession, channel_id: int, name: str, template_id: int | None
	) -> CaptionTemplate:
		"""Находит шаблон для перезаписи или создаёт новый.

		Raises:
			CaptionsError: Шаблон для обновления не найден.
		"""
		if template_id is None:
			template = CaptionTemplate(channel_id=channel_id, name=name)
			session.add(template)
			await session.flush()
			return template
		existing = await session.get(CaptionTemplate, template_id)
		if existing is None:
			raise CaptionsError("Шаблон не найден — обновите список.")
		existing.name = name
		return existing

	@staticmethod
	def _field_dto(field: CaptionField) -> FieldDto:
		return FieldDto(
			field.id, field.name, field.hashtag, field.multiple,
			[v.value for v in field.values],
		)

	@classmethod
	def _template_dto(cls, template: CaptionTemplate) -> TemplateDto:
		return TemplateDto(
			template.id, template.name, template.last_used_at,
			[
				TemplateFieldDto(cls._field_dto(row.field), row.enabled)
				for row in template.fields
			],
		)
