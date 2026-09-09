"""Тесты подписей: чистая сборка текста и сервис полей/шаблонов."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community
from pxcontrol.engine.services.captions import (
	BRACKETS_STEP,
	DATE_STEP,
	DIGIT_WORDS_STEP,
	TITLE_STEP_PRESETS,
	CaptionLine,
	CaptionsError,
	CaptionsService,
	TitleCaseMode,
	TitleParseRules,
	build_caption,
	compile_step,
	hashtag,
	parse_title,
	title_from_filename,
)
from pxcontrol.engine.services.video import PIPELINE_STAMP_FORMAT

# --- чистые функции ---------------------------------------------------------


def test_hashtag_normalization() -> None:
	"""Слова склеиваются с заглавной, лишние символы отбрасываются."""
	assert hashtag("Tomb Raider") == "#TombRaider"
	assert hashtag("sci-fi") == "#SciFi"
	assert hashtag("2026") == "#2026"
	assert hashtag("uno") == "#Uno"


def test_build_caption_full() -> None:
	"""Название жирным, решётки/текст по полю, пустые строки — вон."""
	text = build_caption(
		"Lara Croft",
		[
			CaptionLine("Year", hashtag=False, values=["2026"]),
			CaptionLine("Genre", hashtag=True, values=["action", "sci-fi"]),
			CaptionLine("Author", hashtag=True, values=["  "]),  # пусто — пропуск
		],
	)
	assert text == ("**Lara Croft**\nYear: 2026\nGenre: #Action, #SciFi")


def test_build_caption_without_title() -> None:
	"""Без названия подпись начинается сразу с полей."""
	text = build_caption("", [CaptionLine("Year", False, ["2026"])])
	assert text == "Year: 2026"


def test_build_caption_without_field_name() -> None:
	"""show_name=False — строка из одних значений, без префикса «Имя: »."""
	text = build_caption(
		"Lara Croft",
		[
			CaptionLine("Genre", hashtag=True, values=["action", "sci-fi"], show_name=False),
			CaptionLine("Year", hashtag=False, values=["2026"]),
			CaptionLine("Tags", hashtag=True, values=[""], show_name=False),  # пусто — пропуск
		],
	)
	assert text == ("**Lara Croft**\n#Action, #SciFi\nYear: 2026")


def test_title_from_filename_matches_pipeline_stamp() -> None:
	"""Связка форматов: суффикс с штампом PIPELINE_STAMP_FORMAT вырезается.

	Формат штампа живёт в сервисе видео, вырезающее его регулярное
	выражение — здесь: тест ловит их молчаливое расхождение.
	"""
	stamp = datetime(2026, 8, 26, 12, 30, 45).strftime(PIPELINE_STAMP_FORMAT)
	assert title_from_filename(f"/x/Имя ролика_пресет_{stamp}.mp4") == "Имя ролика"


def test_title_from_filename_strips_pipeline_suffix() -> None:
	"""Суффикс конвейера _<пресет>_<штамп> отрезается, чужие имена — как есть."""
	assert title_from_filename("/x/Lara Croft_test_20260713-223049.mp4") == "Lara Croft"
	assert title_from_filename("/x/Просто видео.mp4") == "Просто видео"


# --- разбор имени файла в название (пакетная публикация) ---------------------


def test_parse_title_applies_steps_in_order() -> None:
	"""Шаги идут по очереди: каждый — по результату предыдущего."""
	rules = TitleParseRules(steps=("_", DATE_STEP, DIGIT_WORDS_STEP))
	assert parse_title("Фильм_2024-01-31_1080_финал", rules) == "Фильм финал"
	# порядок важен: даты после замены разделителей уже не видны
	late_dates = TitleParseRules(steps=("_", DATE_STEP))
	assert parse_title("Фильм_2024_01_31", late_dates) == "Фильм 2024 01 31"
	early_dates = TitleParseRules(steps=(DATE_STEP, "_"))
	assert parse_title("Фильм_2024_01_31", early_dates) == "Фильм"


def test_parse_title_case_modes() -> None:
	"""Регистр применяется по итоговой фразе, не ломая «iPhone»."""
	every = TitleParseRules(steps=("_",), case=TitleCaseMode.EVERY_WORD)
	assert parse_title("lara_croft_tomb", every) == "Lara Croft Tomb"
	assert parse_title("обзор iPhone", TitleParseRules(case=TitleCaseMode.EVERY_WORD)) == (
		"Обзор IPhone"
	)
	first = TitleParseRules(steps=("_",), case=TitleCaseMode.FIRST_WORD)
	assert parse_title("новый_ролик_серии", first) == "Новый ролик серии"


def test_date_preset_covers_common_writings() -> None:
	"""Заготовка дат: ходовые написания, включая короткий год."""
	rules = TitleParseRules(steps=(DATE_STEP,))
	for name in (
		"Выпуск 2024-01-31 финал",
		"Выпуск 31.01.2024 финал",
		"Выпуск 31.01.24 финал",
		"Выпуск 24-01-31 финал",
		"Выпуск 20240131 финал",
	):
		assert parse_title(name, rules) == "Выпуск финал", name


def test_date_preset_keeps_plain_numbers() -> None:
	"""Одиночный год и длинные числа датой не считаются.

	«Blade Runner 2049» обязан пережить разбор: год сам по себе — часть
	названия. Разные разделители внутри одной даты («31.01-24») —
	тоже не дата.
	"""
	rules = TitleParseRules(steps=(DATE_STEP,))
	assert parse_title("Blade Runner 2049", rules) == "Blade Runner 2049"
	assert parse_title("Отчёт 123456789", rules) == "Отчёт 123456789"
	assert parse_title("Выпуск 31.01-24", rules) == "Выпуск 31.01-24"


def test_digit_words_preset_keeps_mixed_words() -> None:
	"""Заготовка «слова из цифр» не трогает смешанные слова."""
	rules = TitleParseRules(steps=("_", DIGIT_WORDS_STEP))
	assert parse_title("Ролик_2024_1080_4k_S01E02", rules) == "Ролик 4k S01E02"


def test_parse_title_broken_step_is_skipped() -> None:
	"""Битый шаг пропускается, остальные отрабатывают.

	Причину пользователь уже видит в форме (``compile_step``), а сотня
	имён из-за одной опечатки разбираться не перестаёт.
	"""
	rules = TitleParseRules(steps=("[незакрытый", "_"))
	assert parse_title("Фильм_релиз", rules) == "Фильм релиз"


def test_compile_step_reports_reason() -> None:
	"""Проверка шага: пустой — выключен, битый — понятная ошибка."""
	assert compile_step("") is None
	assert compile_step(r"\d+") is not None
	with pytest.raises(CaptionsError):
		compile_step("[незакрытый")


def test_step_presets_are_valid_expressions() -> None:
	"""Каждая заготовка помощника разбирается — в форму мусор не попадёт."""
	for label, pattern in TITLE_STEP_PRESETS:
		assert compile_step(pattern) is not None, label


def test_parse_title_empty_result_falls_back_to_raw() -> None:
	"""Шаги съели всё — возвращается исходное название, не пустота."""
	rules = TitleParseRules(steps=(BRACKETS_STEP, "(?i)ролик"))
	assert parse_title("[1080p] ролик", rules) == "[1080p] ролик"


def test_parse_title_rules_tokens_round_trip() -> None:
	"""Сериализация в токены и обратно без потерь; незнакомое — мимо."""
	rules = TitleParseRules(
		steps=("_", DATE_STEP, r"(?i)\bofficial\b"),
		case=TitleCaseMode.FIRST_WORD,
	)
	assert TitleParseRules.from_tokens(rules.to_tokens()) == rules
	assert TitleParseRules.from_tokens([]) == TitleParseRules()
	# токены будущих версий не ломают чтение (прямая совместимость)
	assert TitleParseRules.from_tokens(["новое_правило", "case:чудо"]) == TitleParseRules()


def test_parse_title_rules_convert_legacy_tokens() -> None:
	"""Настройка прежней модели читается шагами и разбирает так же.

	У каналов сохранены наборы галочек; после обновления они обязаны
	продолжать работать, а не молча обнулиться.
	"""
	legacy = TitleParseRules.from_tokens(
		["separators", "brackets", "dates", "edge_numbers", "remove:official", "case:first_word"]
	)
	assert legacy.case is TitleCaseMode.FIRST_WORD
	assert legacy.steps  # галочки превратились в выражения
	assert parse_title("01. Фильм_2024-01-31 [1080p] OFFICIAL", legacy) == "Фильм"
	# одна галочка «_ и -» покрывает оба разделителя
	both = TitleParseRules.from_tokens(["separators"])
	assert parse_title("lara_croft-tomb", both) == "lara croft tomb"


def test_sanitize_filename_limits_bytes_not_chars() -> None:
	"""Предел имени — в байтах UTF-8; обрезка не рвёт символ посередине."""
	from pxcontrol.engine.services.captions import (
		MAX_FILENAME_BYTES,
		sanitize_filename,
	)

	# латиница (1 байт/символ): входит ровно MAX_FILENAME_BYTES символов
	assert sanitize_filename("a" * 300) == "a" * MAX_FILENAME_BYTES
	# кириллица (2 байта/буква): режется по байтам, символы целы
	cut = sanitize_filename("ы" * 300)
	assert cut == "ы" * (MAX_FILENAME_BYTES // 2)
	assert len(cut.encode("utf-8")) <= MAX_FILENAME_BYTES
	# короткие имена не трогаются
	assert sanitize_filename("Обычное имя") == "Обычное имя"


# --- сервис -------------------------------------------------------------------


async def _add_community(db: Database, username: str | None = None) -> int:
	"""Заводит канал; ID чата уникален — их бывает несколько в одном тесте."""
	async with db.session_factory() as session:
		count = len((await session.execute(select(Community))).scalars().all())
		community = Community(title="Канал", tg_chat_id=f"-100{count + 1}", username=username)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def test_fields_crud_and_duplicates(db: Database) -> None:
	"""Поле создаётся, дубль имени отклоняется, удаление чистит словарь."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Genre", hashtag=True, multiple=True)
	assert field.name == "Genre" and field.values == []
	assert field.parent_field_id is None
	with pytest.raises(CaptionsError, match="уже есть"):
		await service.add_field(community_id, "Genre", hashtag=True, multiple=True)
	await service.delete_field(field.id)
	assert await service.list_fields(community_id) == []


async def test_field_show_name_flag(db: Database) -> None:
	"""Флаг «имя в подписи»: включён по умолчанию, переключается без пересоздания."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	genre = await service.add_field(community_id, "Genre", hashtag=True, multiple=True)
	assert genre.show_name is True
	tags = await service.add_field(
		community_id, "Tags", hashtag=True, multiple=True, show_name=False
	)
	assert tags.show_name is False

	# выключение у существующего поля сохраняет словарь
	await service.add_values(genre.id, ["action"])
	updated = await service.set_field_show_name(genre.id, False)
	assert updated.show_name is False and updated.names() == ["action"]
	fields = {f.name: f for f in await service.list_fields(community_id)}
	assert fields["Genre"].show_name is False and fields["Tags"].show_name is False

	with pytest.raises(CaptionsError, match="не найдено"):
		await service.set_field_show_name(999_999, True)


async def test_template_roundtrip_and_shared_dictionary(db: Database) -> None:
	"""Шаблоны включают поля канала; словарь общий для всех шаблонов."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	genre = await service.add_field(community_id, "Genre", hashtag=True, multiple=True)
	year = await service.add_field(community_id, "Year", hashtag=False, multiple=False)
	movie = await service.save_template(community_id, "Фильм", [year.id, genre.id])
	await service.save_template(community_id, "Клип", [genre.id])
	assert [tf.field.name for tf in movie.fields] == ["Year", "Genre"]

	# использование по «Фильму» пополняет словарь, «Клип» его видит
	await service.record_usage(movie.id, {genre.id: ["action", "Action", "drama"]})
	templates = await service.list_templates(community_id)
	clip = next(t for t in templates if t.name == "Клип")
	assert next(tf for tf in clip.fields).field.names() == ["action", "drama"]
	film = next(t for t in templates if t.name == "Фильм")
	assert film.last_used_at is not None


async def test_render_filename(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Имя файла: плейсхолдеры, качество, канал, очистка символов."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: VideoInfo(1920, 1080, 60.0, 25.0, True),
	)
	service = CaptionsService(db)
	community_id = await _add_community(db, username="mych")
	author = await service.add_field(community_id, "Author", hashtag=True, multiple=False)
	genre = await service.add_field(community_id, "Genre", hashtag=True, multiple=True)
	template = await service.save_template(
		community_id,
		"Фильм",
		[author.id, genre.id],
		"{Author}, {video} ({Genre}) {quality} (@{channel})",
	)
	assert template.filename_pattern is not None
	name = await service.render_filename(
		template.id,
		community_id,
		"Lara: Croft",
		{author.id: ["Best"], genre.id: ["action", "drama"]},
		"/x/видео.mp4",
	)
	# двоеточие из названия вычищено, качество и канал подставлены
	assert name == "Best, Lara Croft (action, drama) 1080 (@mych).mp4"


async def test_render_filename_builtin_wins_over_field_namesake(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Поле-тёзка «video» не подменяет встроенный плейсхолдер названия."""
	from pxcontrol.engine.video.probe import VideoInfo

	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: VideoInfo(1920, 1080, 60.0, 25.0, True),
	)
	service = CaptionsService(db)
	community_id = await _add_community(db, username="mych")
	namesake = await service.add_field(community_id, "video", hashtag=True, multiple=False)
	template = await service.save_template(community_id, "Тёзка", [namesake.id], "{video}")
	name = await service.render_filename(
		template.id, community_id, "Название поста", {namesake.id: ["значение-поля"]}, "/x/в.mp4"
	)
	assert name == "Название поста.mp4"  # встроенный приоритетнее поля


async def test_render_filename_fits_telegram_limit(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Длинное имя режется до законченного слова в пределах лимита Telegram."""
	from pxcontrol.engine.services.captions import TELEGRAM_MAX_STEM_CHARS

	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: (_ for _ in ()).throw(RuntimeError("не видео")),
	)
	service = CaptionsService(db)
	community_id = await _add_community(db, username="nature_docs")
	tags = await service.add_field(community_id, "Tags", hashtag=True, multiple=True)
	template = await service.save_template(
		community_id, "Т", [tags.id], "{video},@{channel},{Tags}"
	)
	values = [
		"4K",
		"8K",
		"Sunrise",
		"Mountain",
		"Twilight",
		"Sunsets",
		"Wildlife",
		"Lake",
		"Meadows",
	]
	name = await service.render_filename(
		template.id,
		community_id,
		"WinterMorningLights",
		{tags.id: values},
		"/x/v.mp4",
	)
	stem = name.removesuffix(".mp4")
	assert len(stem) <= TELEGRAM_MAX_STEM_CHARS
	# начало нетронуто; срез пришёлся на запятую после «Sunsets» —
	# висячая запятая убрана, имя кончается законченным словом
	assert stem == ("WinterMorningLights,@nature_docs,4K, 8K, Sunrise, Mountain, Twilight, Sunsets")


async def test_render_filename_cuts_long_title_at_word(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Огромное название без полей режется по границе слова, без огрызков."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: (_ for _ in ()).throw(RuntimeError("не видео")),
	)
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Год", hashtag=False, multiple=False)
	template = await service.save_template(community_id, "Т", [field.id], "{video}")
	name = await service.render_filename(
		template.id, community_id, "Длинное Слово " * 20, {}, "/x/v.mp4"
	)
	stem = name.removesuffix(".mp4")
	assert len(stem) <= 78
	assert stem.endswith("Слово") or stem.endswith("Длинное")
	# срез не оставил обрубка: стем состоит из целых слов исходника
	assert all(w in ("Длинное", "Слово") for w in stem.split())


async def test_render_filename_edge_cases(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Не-видео — без качества; неизвестный плейсхолдер остаётся как есть."""
	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: (_ for _ in ()).throw(RuntimeError("не видео")),
	)
	service = CaptionsService(db)
	community_id = await _add_community(db)  # канал без username
	field = await service.add_field(community_id, "Год", hashtag=False, multiple=False)
	template = await service.save_template(
		community_id, "Т", [field.id], "{video} {quality} {Нет} ({Год})"
	)
	name = await service.render_filename(
		template.id, community_id, "Имя", {field.id: ["2026"]}, "/x/файл.zip"
	)
	assert name == "Имя {Нет} (2026).zip"
	no_pattern = await service.save_template(community_id, "Без", [field.id])
	with pytest.raises(CaptionsError, match="не задан шаблон имени"):
		await service.render_filename(no_pattern.id, community_id, "х", {}, "/x/ф.mp4")


async def test_dictionary_add_and_delete_values(db: Database) -> None:
	"""Редактор словаря: добавление с дедупликацией, удаление значения."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Genre", hashtag=True, multiple=True)
	updated = await service.add_values(field.id, ["action", " Action ", "", "drama"])
	assert updated.names() == ["action", "drama"]  # дубль и пустое — пропущены
	action = next(item for item in updated.values if item.value == "action")
	updated = await service.delete_value(action.id)
	assert updated.names() == ["drama"]
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.add_values(999, ["x"])
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.delete_value(999)


async def test_render_filename_respects_limits(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Сплошное слово — жёсткий срез по лимиту Telegram; байтовый предел цел."""
	from pxcontrol.engine.services.captions import (
		MAX_FILENAME_BYTES,
		TELEGRAM_MAX_STEM_CHARS,
	)

	monkeypatch.setattr(
		"pxcontrol.engine.services.captions.probe_video",
		lambda _p, _b: (_ for _ in ()).throw(RuntimeError("не видео")),
	)
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Год", hashtag=False, multiple=False)
	template = await service.save_template(community_id, "Т", [field.id], "{video}")
	# сплошная латиница без разделителей: границы слова нет — срез ровно
	# по лимиту Telegram (длиннее сервер изменил бы имя сам)
	name = await service.render_filename(template.id, community_id, "a" * 200, {}, "/x/ф.mp4")
	assert name == "a" * TELEGRAM_MAX_STEM_CHARS + ".mp4"
	# эмодзи — 4 байта на символ: байтовый предел ФС строже символьного
	long_name = await service.render_filename(
		template.id, community_id, "\U0001f600" * 100, {}, "/x/ф.mp4"
	)
	assert long_name.endswith(".mp4")
	assert len(long_name.encode("utf-8")) <= MAX_FILENAME_BYTES
	assert len(long_name.removesuffix(".mp4")) <= TELEGRAM_MAX_STEM_CHARS


async def test_template_validation_and_delete(db: Database) -> None:
	"""Пустое имя/состав отклоняются; удаление шаблона не трогает словарь."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Год", hashtag=False, multiple=False)
	with pytest.raises(CaptionsError, match="имя"):
		await service.save_template(community_id, " ", [field.id])
	with pytest.raises(CaptionsError, match="хотя бы одно"):
		await service.save_template(community_id, "Пустой", [])
	template = await service.save_template(community_id, "Т", [field.id])
	await service.record_usage(template.id, {field.id: ["2026"]})
	await service.delete_template(template.id)
	assert await service.list_templates(community_id) == []
	assert (await service.list_fields(community_id))[0].names() == ["2026"]


# --- связанные словари (персонаж внутри тайтла) ------------------------------


async def _linked_fields(service: CaptionsService, community_id: int) -> tuple[int, int]:
	"""Готовит пару полей «Title» и зависимый от него «Character»."""
	title = await service.add_field(community_id, "Title", hashtag=True, multiple=False)
	character = await service.add_field(community_id, "Character", hashtag=True, multiple=True)
	linked = await service.set_field_parent(character.id, title.id)
	assert linked.parent_field_id == title.id
	return title.id, character.id


async def test_usage_binds_new_values_to_selected_parent(db: Database) -> None:
	"""Персонажи привязываются к выбранному тайтлу; тёзка — своя запись."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	template = await service.save_template(community_id, "Фильм", [title_id, character_id])
	await service.record_usage(
		template.id, {title_id: ["TombRider"], character_id: ["Lara", "Zip"]}
	)
	await service.record_usage(template.id, {title_id: ["Fallout"], character_id: ["Lara"]})
	fields = {f.name: f for f in await service.list_fields(community_id)}
	titles = {item.id: item.value for item in fields["Title"].values}
	bound = sorted(
		(item.value, titles[item.parent_id])
		for item in fields["Character"].values
		if item.parent_id is not None
	)
	# тёзки из разных тайтлов — разные записи словаря (одна на свой тайтл)
	assert bound == [("Lara", "Fallout"), ("Lara", "TombRider"), ("Zip", "TombRider")]


async def test_available_filters_by_parent(db: Database) -> None:
	"""Словарь зависимого поля фильтруется по выбранному значению родителя."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	template = await service.save_template(community_id, "Фильм", [title_id, character_id])
	await service.record_usage(template.id, {title_id: ["TombRider"], character_id: ["Lara"]})
	await service.record_usage(template.id, {title_id: ["Fallout"], character_id: ["Vault Boy"]})
	# значение без привязки видно при любом выборе: иначе его не выбрать
	await service.add_values(character_id, ["Ничей"])
	fields = {f.name: f for f in await service.list_fields(community_id)}
	tomb = next(i for i in fields["Title"].values if i.value == "TombRider")
	character = fields["Character"]
	assert [i.value for i in character.available([tomb.id])] == ["Lara", "Ничей"]
	assert [i.value for i in character.available([])] == ["Ничей"]
	# независимое поле фильтру не подчиняется — отдаёт весь словарь
	assert [i.value for i in fields["Title"].available([])] == ["Fallout", "TombRider"]


async def test_deleting_parent_value_removes_children(db: Database) -> None:
	"""Удаление тайтла уносит его персонажей; чужие остаются."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	template = await service.save_template(community_id, "Фильм", [title_id, character_id])
	await service.record_usage(template.id, {title_id: ["TombRider"], character_id: ["Lara"]})
	await service.record_usage(template.id, {title_id: ["Fallout"], character_id: ["Vault Boy"]})
	fields = {f.name: f for f in await service.list_fields(community_id)}
	tomb = next(i for i in fields["Title"].values if i.value == "TombRider")
	titles = await service.delete_value(tomb.id)
	assert titles.names() == ["Fallout"]
	character = next(f for f in await service.list_fields(community_id) if f.name == "Character")
	assert character.names() == ["Vault Boy"]


async def test_manual_binding_and_adoption(db: Database) -> None:
	"""Привязка руками, отвязка и усыновление значения без родителя."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	titles = await service.add_values(title_id, ["TombRider"])
	tomb = titles.values[0]
	characters = await service.add_values(character_id, ["Lara"])
	lara = characters.values[0]
	assert lara.parent_id is None

	characters = await service.assign_value_parent(lara.id, tomb.id)
	assert characters.values[0].parent_id == tomb.id
	characters = await service.assign_value_parent(lara.id, None)
	assert characters.values[0].parent_id is None

	# значение без привязки, использованное вместе с тайтлом, усыновляется:
	# новой записи не появляется, у прежней проставляется родитель
	template = await service.save_template(community_id, "Фильм", [title_id, character_id])
	await service.record_usage(template.id, {title_id: ["TombRider"], character_id: ["Lara"]})
	characters = next(f for f in await service.list_fields(community_id) if f.name == "Character")
	assert characters.names() == ["Lara"]
	assert characters.values[0].parent_id == tomb.id


async def test_parent_validation_and_unlink(db: Database) -> None:
	"""Негодный родитель отклоняется; снятие связи чистит привязки значений."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	other_community = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	with pytest.raises(CaptionsError, match="само от себя"):
		await service.set_field_parent(character_id, character_id)
	with pytest.raises(CaptionsError, match="кольцо"):
		await service.set_field_parent(title_id, character_id)
	alien = await service.add_field(other_community, "Title", hashtag=True, multiple=False)
	with pytest.raises(CaptionsError, match="не найдено у этого канала"):
		await service.set_field_parent(character_id, alien.id)
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.set_field_parent(999, title_id)

	titles = await service.add_values(title_id, ["TombRider"])
	await service.add_values(character_id, ["Lara"], titles.values[0].id)
	# снятие связи обнуляет привязки: они указывали в словарь прежнего родителя
	characters = await service.set_field_parent(character_id, None)
	assert characters.parent_field_id is None
	assert characters.values[0].parent_id is None
	with pytest.raises(CaptionsError, match="не зависит от другого поля"):
		await service.assign_value_parent(characters.values[0].id, titles.values[0].id)


async def test_delete_parent_field_keeps_dependent_dictionary(db: Database) -> None:
	"""Удаление родительского поля не стирает словарь зависимого.

	Зависимое поле становится независимым (SET NULL у связи полей),
	его значения отвязываются — как при снятии связи через
	set_field_parent, а не гибнут каскадом parent_value_id.
	"""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	titles = await service.add_values(title_id, ["TombRider"])
	await service.add_values(character_id, ["Lara"], titles.values[0].id)

	await service.delete_field(title_id)

	fields = await service.list_fields(community_id)
	assert [f.name for f in fields] == ["Character"]  # Title удалён
	character = fields[0]
	assert character.parent_field_id is None  # поле стало независимым
	assert character.names() == ["Lara"]  # словарь цел
	assert character.values[0].parent_id is None  # привязка снята, не каскад
