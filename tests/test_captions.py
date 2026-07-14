"""Тесты подписей: чистая сборка текста и сервис полей/шаблонов."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Channel
from pxcontrol.engine.services.captions import (
	CaptionLine,
	CaptionsError,
	CaptionsService,
	build_caption,
	hashtag,
	title_from_filename,
)

# --- чистые функции ---------------------------------------------------------


def test_hashtag_normalization() -> None:
	"""Слова склеиваются с заглавной, лишние символы отбрасываются."""
	assert hashtag("Tomb Raider") == "#TombRaider"
	assert hashtag("sci-fi") == "#SciFi"
	assert hashtag("2026") == "#2026"
	assert hashtag("uno") == "#Uno"


def test_build_caption_full() -> None:
	"""Название жирным, решётки/текст по полю, пустые строки — вон."""
	text = build_caption("Lara Croft", [
		CaptionLine("Year", hashtag=False, values=["2026"]),
		CaptionLine("Genre", hashtag=True, values=["action", "sci-fi"]),
		CaptionLine("Author", hashtag=True, values=["  "]),  # пусто — пропуск
	])
	assert text == (
		"**Lara Croft**\n"
		"Year: 2026\n"
		"Genre: #Action, #SciFi"
	)


def test_build_caption_without_title() -> None:
	"""Без названия подпись начинается сразу с полей."""
	text = build_caption("", [CaptionLine("Year", False, ["2026"])])
	assert text == "Year: 2026"


def test_title_from_filename_strips_pipeline_suffix() -> None:
	"""Суффикс конвейера _<пресет>_<штамп> отрезается, чужие имена — как есть."""
	assert title_from_filename(
		"/x/Lara Croft_test_20260713-223049.mp4"
	) == "Lara Croft"
	assert title_from_filename("/x/Просто видео.mp4") == "Просто видео"


# --- сервис -------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
	"""Временная БД с применёнными миграциями."""
	database = Database(f"sqlite+aiosqlite:///{tmp_path / 'captions.db'}")
	await database.init()
	yield database
	await database.close()


async def _add_channel(db: Database, username: str | None = None) -> int:
	async with db.session_factory() as session:
		channel = Channel(title="Канал", tg_chat_id="-1001", username=username)
		session.add(channel)
		await session.commit()
		await session.refresh(channel)
		return channel.id


async def test_fields_crud_and_duplicates(db: Database) -> None:
	"""Поле создаётся, дубль имени отклоняется, удаление чистит словарь."""
	service = CaptionsService(db)
	channel_id = await _add_channel(db)
	field = await service.add_field(channel_id, "Genre", hashtag=True, multiple=True)
	assert field.name == "Genre" and field.values == []
	with pytest.raises(CaptionsError, match="уже есть"):
		await service.add_field(channel_id, "Genre", hashtag=True, multiple=True)
	await service.delete_field(field.id)
	assert await service.list_fields(channel_id) == []


async def test_template_roundtrip_and_shared_dictionary(db: Database) -> None:
	"""Шаблоны включают поля канала; словарь общий для всех шаблонов."""
	service = CaptionsService(db)
	channel_id = await _add_channel(db)
	genre = await service.add_field(channel_id, "Genre", hashtag=True, multiple=True)
	year = await service.add_field(channel_id, "Year", hashtag=False, multiple=False)
	movie = await service.save_template(channel_id, "Фильм", [year.id, genre.id])
	await service.save_template(channel_id, "Клип", [genre.id])
	assert [tf.field.name for tf in movie.fields] == ["Year", "Genre"]

	# использование по «Фильму» пополняет словарь, «Клип» его видит
	await service.record_usage(movie.id, {genre.id: ["action", "Action", "drama"]})
	templates = await service.list_templates(channel_id)
	clip = next(t for t in templates if t.name == "Клип")
	assert next(tf for tf in clip.fields).field.values == ["action", "drama"]
	film = next(t for t in templates if t.name == "Фильм")
	assert film.last_used_at is not None


async def test_render_filename(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Имя файла: плейсхолдеры, качество, канал, очистка символов."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: VideoInfo(1920, 1080, 60.0, 25.0, True),
	)
	service = CaptionsService(db)
	channel_id = await _add_channel(db, username="mych")
	author = await service.add_field(channel_id, "Author", hashtag=True, multiple=False)
	genre = await service.add_field(channel_id, "Genre", hashtag=True, multiple=True)
	template = await service.save_template(
		channel_id, "Фильм", [author.id, genre.id],
		"{Author}, {video} ({Genre}) {quality} (@{channel})",
	)
	assert template.filename_pattern is not None
	name = await service.render_filename(
		template.id, channel_id, "Lara: Croft",
		{author.id: ["Best"], genre.id: ["action", "drama"]},
		"/x/видео.mp4",
	)
	# двоеточие из названия вычищено, качество и канал подставлены
	assert name == "Best, Lara Croft (action, drama) 1080 (@mych).mp4"


async def test_render_filename_edge_cases(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Не-видео — без качества; неизвестный плейсхолдер остаётся как есть."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: (_ for _ in ()).throw(RuntimeError("не видео")),
	)
	service = CaptionsService(db)
	channel_id = await _add_channel(db)  # канал без username
	field = await service.add_field(channel_id, "Год", hashtag=False, multiple=False)
	template = await service.save_template(
		channel_id, "Т", [field.id], "{video} {quality} {Нет} ({Год})"
	)
	name = await service.render_filename(
		template.id, channel_id, "Имя", {field.id: ["2026"]}, "/x/файл.zip"
	)
	assert name == "Имя {Нет} (2026).zip"
	no_pattern = await service.save_template(channel_id, "Без", [field.id])
	with pytest.raises(CaptionsError, match="не задан шаблон имени"):
		await service.render_filename(
			no_pattern.id, channel_id, "х", {}, "/x/ф.mp4"
		)


async def test_template_validation_and_delete(db: Database) -> None:
	"""Пустое имя/состав отклоняются; удаление шаблона не трогает словарь."""
	service = CaptionsService(db)
	channel_id = await _add_channel(db)
	field = await service.add_field(channel_id, "Год", hashtag=False, multiple=False)
	with pytest.raises(CaptionsError, match="имя"):
		await service.save_template(channel_id, " ", [field.id])
	with pytest.raises(CaptionsError, match="хотя бы одно"):
		await service.save_template(channel_id, "Пустой", [])
	template = await service.save_template(channel_id, "Т", [field.id])
	await service.record_usage(template.id, {field.id: ["2026"]})
	await service.delete_template(template.id)
	assert await service.list_templates(channel_id) == []
	assert (await service.list_fields(channel_id))[0].values == ["2026"]
