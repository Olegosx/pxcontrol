"""Сервис подписей к постам: поля со словарями, шаблоны, сборка текста.

Поле канала («Genre», «Year»…) хранит свой словарь значений один раз;
шаблоны — именованные наборы полей с порядком. Сборка подписи — чистые
функции: жирное название (Markdown, Telethon парсит его по умолчанию)
и строки «Поле: значения» (с решётками или без).
"""

from __future__ import annotations

import asyncio
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
	Channel,
)
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.video.ffmpeg import FfmpegSource, ffmpeg_source
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video

logger = logging.getLogger(__name__)

#: Суффикс имён файлов нашего конвейера: _<пресет>_<ГГГГММДД-ЧЧММСС>.
_PIPELINE_SUFFIX = re.compile(r"_[^_]+_\d{8}-\d{6}$")

#: Плейсхолдер шаблона имени файла: {video}, {ИмяПоля}, {quality}, {channel}.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

#: Символы, недопустимые в именах файлов (плюс управляющие).
_FORBIDDEN_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: Предел имени файла в байтах UTF-8: лимит файловых систем байтовый
#: (ext4 — 255 байт на имя; NTFS — 255 символов), а кириллица занимает
#: два байта на букву. 240 — запас под любые ФС и перезалив.
MAX_FILENAME_BYTES = 240


class CaptionsError(EngineError):
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
	"""Шаблон подписи: имя, состав полей и шаблон имени файла."""

	id: int
	name: str
	last_used_at: datetime | None
	fields: list[TemplateFieldDto]
	filename_pattern: str | None = None


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


def sanitize_filename(name: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
	"""Чистит имя файла: недопустимые символы → пробел, предел — в байтах.

	Предел считается в байтах UTF-8 (см. :data:`MAX_FILENAME_BYTES`);
	обрезка не рвёт многобайтовый символ посередине.
	"""
	cleaned = _FORBIDDEN_IN_FILENAME.sub(" ", name)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	if len(cleaned.encode("utf-8")) <= max_bytes:
		return cleaned
	cut = cleaned.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
	return cut.strip()


# --- сервис -------------------------------------------------------------------


class CaptionsService:
	"""Поля, словари и шаблоны подписей каналов."""

	def __init__(self, db: Database, ffmpeg_path: FfmpegSource = "ffmpeg") -> None:
		self._db = db
		self._ffmpeg = ffmpeg_source(ffmpeg_path)  # провайдер пути (настройки)

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

	async def add_values(self, field_id: int, values: list[str]) -> FieldDto:
		"""Пополняет словарь поля (редактор словаря в «Полях подписи»).

		Дубли значений (без учёта регистра) и пустые строки пропускаются —
		правила те же, что при автопополнении из сборки подписи.

		Returns:
			Поле с обновлённым словарём.

		Raises:
			CaptionsError: Поле не найдено.
		"""
		async with self._db.session_factory() as session:
			if await session.get(CaptionField, field_id) is None:
				raise CaptionsError("Поле не найдено — обновите список.")
			await self._merge_values(session, field_id, values)
			await session.commit()
		return await self._get_field(field_id)

	async def delete_value(self, field_id: int, value: str) -> FieldDto:
		"""Удаляет значение из словаря поля.

		Returns:
			Поле с обновлённым словарём.

		Raises:
			CaptionsError: Поле не найдено.
		"""
		async with self._db.session_factory() as session:
			await session.execute(
				delete(CaptionValue).where(
					CaptionValue.field_id == field_id,
					CaptionValue.value == value,
				)
			)
			await session.commit()
		return await self._get_field(field_id)

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
		filename_pattern: str | None = None, template_id: int | None = None,
	) -> TemplateDto:
		"""Создаёт или перезаписывает шаблон (состав — в порядке списка).

		``filename_pattern`` — необязательный шаблон имени файла при
		отправке ({video}, {ИмяПоля}, {quality}, {channel}).

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
			template.filename_pattern = (filename_pattern or "").strip() or None
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

	async def render_filename(
		self,
		template_id: int,
		channel_id: int,
		title: str,
		used_values: dict[int, list[str]],
		media_path: str,
	) -> str:
		"""Собирает имя файла по шаблону имени выбранного шаблона подписи.

		Плейсхолдеры: ``{video}`` — название видео/поста, ``{ИмяПоля}`` —
		значения поля через запятую (без решёток), ``{quality}`` — меньшая
		сторона кадра видео (ffprobe), ``{channel}`` — @имя канала без
		``@``. Неизвестные плейсхолдеры остаются как есть — видно
		и правится руками. Название нарочно не ``{title}``: у каналов
		бывает поле «Title», и различие только регистром путало.

		Raises:
			CaptionsError: Шаблон не найден, шаблон имени не задан
				или имя получилось пустым.
		"""
		async with self._db.session_factory() as session:
			template = await session.get(CaptionTemplate, template_id)
			if template is None or not template.filename_pattern:
				raise CaptionsError("У шаблона не задан шаблон имени файла.")
			pattern = template.filename_pattern
			channel = await session.get(Channel, channel_id)
		mapping = {
			"video": title.strip(),
			# ffprobe — блокирующий подпроцесс: в отдельном потоке,
			# чтобы не останавливать цикл событий движка
			"quality": await asyncio.to_thread(self._probe_quality, media_path),
			"channel": (channel.username or "") if channel else "",
		}
		for field in await self.list_fields(channel_id):
			mapping[field.name] = ", ".join(used_values.get(field.id, []))
		rendered = _PLACEHOLDER.sub(
			lambda m: mapping.get(m.group(1), m.group(0)), pattern
		)
		# бюджет стема — предел имени минус расширение (оно едет как есть)
		suffix = Path(media_path).suffix
		stem = sanitize_filename(
			rendered, MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
		)
		if not stem:
			raise CaptionsError("Имя файла по шаблону получилось пустым.")
		return stem + suffix

	def _probe_quality(self, media_path: str) -> str:
		"""Качество видео (меньшая сторона кадра) или пустая строка."""
		try:
			info = probe_video(media_path, ffprobe_bin_for(self._ffmpeg()))
		except (OSError, RuntimeError, ValueError):
			return ""
		return str(min(info.width, info.height))

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
				await self._merge_values(session, field_id, values)
			template = await session.get(CaptionTemplate, template_id)
			if template is not None:
				template.last_used_at = datetime.now(UTC)
			await session.commit()

	# --- внутреннее ---------------------------------------------------------

	@staticmethod
	async def _merge_values(
		session: AsyncSession, field_id: int, values: list[str]
	) -> None:
		"""Добавляет в словарь поля новые значения (в открытой сессии).

		Пустые строки и дубли (без учёта регистра) пропускаются.
		"""
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

	async def _get_field(self, field_id: int) -> FieldDto:
		"""Возвращает поле со словарём значений.

		Raises:
			CaptionsError: Поле не найдено.
		"""
		async with self._db.session_factory() as session:
			field = (await session.execute(
				select(CaptionField)
				.options(selectinload(CaptionField.values))
				.where(CaptionField.id == field_id)
			)).scalar_one_or_none()
		if field is None:
			raise CaptionsError("Поле не найдено — обновите список.")
		return self._field_dto(field)

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
			template.filename_pattern,
		)
