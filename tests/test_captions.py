"""Тесты подписей: чистая сборка и разбор имени файла, сервис полей и пресетов."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Community
from pxcontrol.engine.services.captions import (
	BRACKETS_STEP,
	DATE_STEP,
	DIGIT_WORDS_STEP,
	EXTRACT_PRESETS,
	STEP_PRESETS,
	CaptionLine,
	CaptionPresetDraft,
	CaptionPresetDto,
	CaptionsError,
	CaptionsService,
	CaseMode,
	FieldDto,
	FieldEdit,
	FieldStyle,
	PresetFieldSpec,
	ReplaceStep,
	SourceRule,
	build_caption,
	check_rule,
	compile_step,
	compose_filename,
	extract_values,
	filename_source,
	hashtag,
)
from pxcontrol.engine.services.video import PIPELINE_STAMP_FORMAT
from pxcontrol.engine.telegram.rich_text import RichText, TextEntity, TextStyle

#: Пример из постановки задачи (ADR-0042): дата, номер, актёры, тайтл.
_MOVIE = "11.12.2014 627563 Bruce Lee, Jan Clod Van Dam, Ded Morozz (Prosto Film)"

#: Оформление строки названия ролика (так его заводит миграция).
_TITLE_STYLE = FieldStyle(hashtag=False, multiple=False, show_name=False, bold=True)

# --- сборка подписи ------------------------------------------------------------------


def test_hashtag_normalization() -> None:
	"""Слова склеиваются с заглавной, лишние символы отбрасываются."""
	assert hashtag("Tomb Raider") == "#TombRaider"
	assert hashtag("sci-fi") == "#SciFi"
	assert hashtag("2026") == "#2026"
	assert hashtag("uno") == "#Uno"


def test_build_caption_title_is_ordinary_bold_field() -> None:
	"""Название — обычная строка с оформлением «жирным», разметка сущностью.

	Звёздочек в тексте нет (ADR-0033); пустые поля пропускаются.
	"""
	caption = build_caption(
		[
			CaptionLine("Video", ["Lara Croft"], _TITLE_STYLE),
			CaptionLine("Year", ["2026"], FieldStyle(hashtag=False)),
			CaptionLine("Genre", ["action", "sci-fi"], FieldStyle(multiple=True)),
			CaptionLine("Author", ["  "]),  # пусто — пропуск
		]
	)
	assert caption.text == "Lara Croft\nYear: 2026\nGenre: #Action, #SciFi"
	assert caption.entities == (TextEntity(TextStyle.BOLD, 0, len("Lara Croft")),)


def test_build_caption_bold_anywhere_counts_utf16() -> None:
	"""Жирная строка не первой: смещение — в UTF-16, эмодзи занимает две единицы."""
	caption = build_caption(
		[
			CaptionLine("Mood", ["🙂"], FieldStyle(hashtag=False, show_name=False)),
			CaptionLine("Empty", []),  # пропуск не сдвигает смещения
			CaptionLine("Title", ["Фильм"], replace_style(bold=True, hashtag=False)),
		]
	)
	assert caption.text == "🙂\nTitle: Фильм"
	# «🙂» — две единицы UTF-16, плюс перевод строки
	assert caption.entities == (TextEntity(TextStyle.BOLD, 3, len("Title: Фильм")),)


def replace_style(**changes: Any) -> FieldStyle:
	"""Оформление по умолчанию с поправками."""
	from dataclasses import replace

	return replace(FieldStyle(), **changes)


def test_build_caption_without_bold_and_names() -> None:
	"""Без жирных строк разметки нет; show_name=False — только значения."""
	caption = build_caption(
		[
			CaptionLine("Genre", ["action", "sci-fi"], FieldStyle(show_name=False)),
			CaptionLine("Year", ["2026"], FieldStyle(hashtag=False)),
		]
	)
	assert caption == RichText("#Action, #SciFi\nYear: 2026")


def test_filename_source_matches_pipeline_stamp() -> None:
	"""Связка форматов: суффикс с штампом PIPELINE_STAMP_FORMAT вырезается.

	Формат штампа живёт в сервисе видео, вырезающее его регулярное
	выражение — здесь: тест ловит их молчаливое расхождение.
	"""
	stamp = datetime(2026, 8, 26, 12, 30, 45).strftime(PIPELINE_STAMP_FORMAT)
	assert filename_source(f"/x/Имя ролика_пресет_{stamp}.mp4") == "Имя ролика"


def test_filename_source_strips_pipeline_suffix_only() -> None:
	"""Суффикс конвейера отрезается, чужие имена — как есть (без расширения)."""
	assert filename_source("/x/Lara Croft_test_20260713-223049.mp4") == "Lara Croft"
	assert filename_source("/x/Просто видео.mp4") == "Просто видео"
	assert filename_source(f"/x/{_MOVIE}.mkv") == _MOVIE


# --- разбор имени файла в значения полей ------------------------------------------------


def test_task_example_splits_into_title_and_starring() -> None:
	"""Пример из постановки: тайтл из скобок, актёры списком с решётками."""
	title = extract_values(_MOVIE, SourceRule(extract=r"\(([^()]*)\)\s*$"), multiple=False)
	starring = extract_values(_MOVIE, SourceRule(extract=r"^\S+\s+\d+\s+(.+?)\s*\("), True)
	assert title == ["Prosto Film"]
	assert starring == ["Bruce Lee", "Jan Clod Van Dam", "Ded Morozz"]
	caption = build_caption(
		[
			CaptionLine("Title", title, _TITLE_STYLE),
			CaptionLine("Starring", starring, FieldStyle(multiple=True)),
		]
	)
	assert caption.text == "Prosto Film\nStarring: #BruceLee, #JanClodVanDam, #DedMorozz"


def test_no_match_means_no_value() -> None:
	"""Нет совпадения — поле пустое, а не заполнено всем именем файла."""
	rule = SourceRule(extract=r"\(([^()]*)\)\s*$")
	assert extract_values("Без скобок", rule, multiple=False) == []
	assert extract_values("Без скобок", rule, multiple=True) == []


def test_default_rule_takes_whole_name() -> None:
	"""Правило по умолчанию — имя целиком (так миграция переносит название)."""
	assert extract_values("Lara  Croft ", SourceRule(), multiple=False) == ["Lara Croft"]
	# у единственного значения разделитель не действует
	assert extract_values("Tom, Jerry", SourceRule(), multiple=False) == ["Tom, Jerry"]


def test_group_choice_value_then_first_then_whole() -> None:
	"""Группа value главнее; иначе первая группа; без групп — всё совпадение."""
	named = SourceRule(extract=r"(\d+)-(?P<value>[a-z]+)")
	assert extract_values("12-abc", named, False) == ["abc"]
	assert extract_values("12-abc", SourceRule(extract=r"(\d+)-([a-z]+)"), False) == ["12"]
	assert extract_values("12-abc", SourceRule(extract=r"\d+-[a-z]+"), False) == ["12-abc"]
	# необязательная группа не участвовала — значения нет
	assert extract_values("x", SourceRule(extract=r"x(y)?"), False) == []


def test_steps_split_case_and_dedupe() -> None:
	"""Очистка идёт до разделения, регистр — у каждого значения, повторы — мимо."""
	rule = SourceRule(
		steps=(ReplaceStep("_", " "),),
		case=CaseMode.FIRST_WORD,
		separator=r"\s*[,&]\s*",
	)
	assert extract_values("bruce_lee & jan , bruce_lee,,", rule, multiple=True) == [
		"Bruce lee",
		"Jan",
	]
	every = SourceRule(case=CaseMode.EVERY_WORD)
	# «Каждое Слово» не ломает «iPhone»: поднимается только первая буква
	assert extract_values("обзор iPhone", every, False) == ["Обзор IPhone"]


def test_broken_parts_do_not_break_parsing(caplog: pytest.LogCaptureFixture) -> None:
	"""Битое извлечение — пусто, битый шаг — пропуск, битый разделитель — одно значение.

	Разбор сотни имён не падает из-за опечатки; причина — в логе
	(и на экране пресета, :func:`check_rule`).
	"""
	caplog.set_level(logging.WARNING)
	assert extract_values("abc", SourceRule(extract="[незакрытый"), False) == []
	steps = SourceRule(steps=(ReplaceStep("[незакрытый"), ReplaceStep("_", " ")))
	assert extract_values("Фильм_релиз", steps, False) == ["Фильм релиз"]
	assert extract_values("a,b", SourceRule(separator="("), True) == ["a,b"]
	assert "не разобран" in caplog.text


def test_steps_keep_previous_chain_semantics() -> None:
	"""Цепочка замен прежняя: порядок важен, пустая замена — удаление, группы."""
	spaces = ReplaceStep("_", " ")
	late = SourceRule(steps=(spaces, ReplaceStep(DATE_STEP)))
	assert extract_values("Фильм_2024_01_31", late, False) == ["Фильм 2024 01 31"]
	early = SourceRule(steps=(ReplaceStep(DATE_STEP), spaces))
	assert extract_values("Фильм_2024_01_31", early, False) == ["Фильм"]
	glue = SourceRule(steps=(ReplaceStep("_", ""),))
	assert extract_values("Мой_ролик", glue, False) == ["Мойролик"]
	swap = SourceRule(steps=(ReplaceStep(r"(\w+)\.(\w+)", r"\2 \1"),))
	assert extract_values("ролик.мой", swap, False) == ["мой ролик"]


def test_date_preset_covers_common_writings() -> None:
	"""Заготовка дат: ходовые написания, включая короткий год."""
	rule = SourceRule(steps=(ReplaceStep(DATE_STEP, " "),))
	for name in (
		"Выпуск 2024-01-31 финал",
		"Выпуск 31.01.2024 финал",
		"Выпуск 31.01.24 финал",
		"Выпуск 24-01-31 финал",
		"Выпуск 20240131 финал",
	):
		assert extract_values(name, rule, False) == ["Выпуск финал"], name


def test_date_preset_keeps_plain_numbers() -> None:
	"""Одиночный год и длинные числа датой не считаются."""
	rule = SourceRule(steps=(ReplaceStep(DATE_STEP, " "),))
	assert extract_values("Blade Runner 2049", rule, False) == ["Blade Runner 2049"]
	assert extract_values("Отчёт 123456789", rule, False) == ["Отчёт 123456789"]
	assert extract_values("Выпуск 31.01-24", rule, False) == ["Выпуск 31.01-24"]


def test_digit_words_and_brackets_presets() -> None:
	"""«Слова из цифр» не трогают смешанные слова; скобки уходят с содержимым."""
	rule = SourceRule(steps=(ReplaceStep("_", " "), ReplaceStep(DIGIT_WORDS_STEP)))
	assert extract_values("Ролик_2024_1080_4k_S01E02", rule, False) == ["Ролик 4k S01E02"]
	brackets = SourceRule(steps=(ReplaceStep(BRACKETS_STEP),))
	assert extract_values("[1080p] ролик (official)", brackets, False) == ["ролик"]


def test_extract_presets_pick_expected_parts() -> None:
	"""Заготовки извлечения: дата отдаётся целиком, а не разделителем."""
	presets = dict(EXTRACT_PRESETS)
	assert extract_values(_MOVIE, SourceRule(presets["Первая дата"]), False) == ["11.12.2014"]
	assert extract_values(_MOVIE, SourceRule(presets["Текст в последних скобках"]), False) == [
		"Prosto Film"
	]
	assert extract_values(
		"Фильм (2020) [HD]", SourceRule(presets["Текст до первой скобки"]), False
	) == ["Фильм"]
	assert extract_values("Blade Runner 2049", SourceRule(presets["Год"]), False) == ["2049"]


def test_presets_are_valid_expressions() -> None:
	"""Каждая заготовка помощника разбирается — в форму мусор не попадёт."""
	for label, pattern in STEP_PRESETS:
		assert compile_step(ReplaceStep(pattern)) is not None, label
	for label, pattern in EXTRACT_PRESETS:
		check_rule(SourceRule(extract=pattern))  # не бросает
		assert label
	# разделители среди заготовок очистки не живут: это замена на пробел
	assert not [label for label, pattern in STEP_PRESETS if pattern in {"_", "-"}]


def test_check_rule_and_compile_step_report_reason() -> None:
	"""Проверка правила называет сломанную часть; пустой шаг — выключен."""
	assert compile_step(ReplaceStep("")) is None
	with pytest.raises(CaptionsError, match="Выражение не"):
		compile_step(ReplaceStep("[незакрытый"))
	with pytest.raises(CaptionsError, match="Замена"):
		compile_step(ReplaceStep(r"\d+", r"\9"))  # группы 9 в выражении нет
	with pytest.raises(CaptionsError, match="Замена"):
		compile_step(ReplaceStep(r"\d+", r"\g<нет>"))  # именованной группы нет
	with pytest.raises(CaptionsError, match="извлечения"):
		check_rule(SourceRule(extract="(("))
	with pytest.raises(CaptionsError, match="Разделитель"):
		check_rule(SourceRule(separator="["))
	check_rule(SourceRule())  # правило по умолчанию годное


def test_source_rule_json_round_trip_and_tolerance(caplog: pytest.LogCaptureFixture) -> None:
	"""JSON правила туда и обратно без потерь; испорченное — по умолчанию.

	Незнакомые ключи пропускаются: правило более новой версии не ломает
	старую (прямая совместимость).
	"""
	rule = SourceRule(
		extract=r"\(([^()]*)\)",
		steps=(ReplaceStep("_", " "), ReplaceStep(DATE_STEP)),
		case=CaseMode.EVERY_WORD,
		separator=";",
	)
	assert SourceRule.from_json(rule.to_json()) == rule
	assert SourceRule.from_json({}) == SourceRule()
	assert SourceRule.from_json({"extract": "x", "новое": 1}) == SourceRule(extract="x")
	caplog.set_level(logging.WARNING)
	broken = SourceRule.from_json(
		{"extract": 5, "steps": [["a", "b"], "мусор", [1, 2]], "case": "чудо"}
	)
	assert broken == SourceRule(steps=(ReplaceStep("a", "b"),))
	assert SourceRule.from_json("не объект") == SourceRule()


def test_preset_parsed_and_lines() -> None:
	"""Пресет разбирает поля с правилом и собирает строки в своём порядке."""
	title = FieldDto(1, "Title", _TITLE_STYLE, [])
	starring = FieldDto(2, "Starring", FieldStyle(multiple=True), [])
	genre = FieldDto(3, "Genre", FieldStyle(multiple=True), [])
	from pxcontrol.engine.services.captions import PresetFieldDto

	preset = CaptionPresetDto(
		1,
		"Фильм",
		None,
		[
			PresetFieldDto(title, True, SourceRule(extract=r"\(([^()]*)\)\s*$")),
			PresetFieldDto(starring, True, SourceRule(extract=r"^\S+\s+\d+\s+(.+?)\s*\(")),
			PresetFieldDto(genre, True, None),
		],
	)
	parsed = preset.parsed(_MOVIE)
	assert parsed == {1: ["Prosto Film"], 2: ["Bruce Lee", "Jan Clod Van Dam", "Ded Morozz"]}
	values = {**parsed, 3: ["action"]}
	lines = preset.lines(values, enabled=[1, 3])  # Starring отключён для этой подписи
	assert [line.name for line in lines] == ["Title", "Genre"]
	assert build_caption(lines).text == "Prosto Film\nGenre: #Action"


# --- имя файла --------------------------------------------------------------------------


def test_sanitize_filename_limits_bytes_not_chars() -> None:
	"""Предел имени — в байтах UTF-8; обрезка не рвёт символ посередине."""
	from pxcontrol.engine.services.captions import (
		MAX_FILENAME_BYTES,
		sanitize_filename,
	)

	assert sanitize_filename("a" * 300) == "a" * MAX_FILENAME_BYTES
	cut = sanitize_filename("ы" * 300)
	assert cut == "ы" * (MAX_FILENAME_BYTES // 2)
	assert len(cut.encode("utf-8")) <= MAX_FILENAME_BYTES
	assert sanitize_filename("Обычное имя") == "Обычное имя"


def test_compose_filename_keeps_unknown_placeholders() -> None:
	"""Неизвестный плейсхолдер виден как есть; пустой результат — пустая строка."""
	assert compose_filename("{Video} {quality}", {"Video": "Имя"}, ".mp4") == "Имя {quality}.mp4"
	assert compose_filename("{Video}", {"Video": ""}, ".mp4") == ""


def test_filename_complaint_matches_preset_rules() -> None:
	"""Имя, набранное человеком, проверяется по правилам сборки по пресету.

	Замок единой точки: раньше «переименовать при отправке» проверяло
	только путь, и Telegram молча урезал слишком длинное имя на сервере,
	а файловая система отвергала длинное имя сырой ошибкой.
	"""
	from pxcontrol.engine.services.captions import (
		MAX_FILENAME_BYTES,
		TELEGRAM_MAX_STEM_CHARS,
		filename_complaint,
	)

	assert filename_complaint("Обычное имя.mp4") is None
	complaint = filename_complaint("Плохое: имя?.mp4")
	assert complaint is not None and "недопустимы символы" in complaint
	long_stem = "я" * (TELEGRAM_MAX_STEM_CHARS + 1)
	complaint = filename_complaint(f"{long_stem}.mp4")
	assert complaint is not None and "предела Telegram" in complaint
	assert filename_complaint("я" * (MAX_FILENAME_BYTES // 2 + 1)) is not None
	assert filename_complaint("я" * TELEGRAM_MAX_STEM_CHARS + ".mp4") is None


# --- сервис: помощники ------------------------------------------------------------------


async def _add_community(db: Database, username: str | None = None) -> int:
	"""Заводит сообщество; ID чата уникален — их бывает несколько в одном тесте."""
	async with db.session_factory() as session:
		count = len((await session.execute(select(Community))).scalars().all())
		community = Community(title="Канал", tg_chat_id=f"-100{count + 1}", username=username)
		session.add(community)
		await session.commit()
		await session.refresh(community)
		return community.id


async def _preset(
	service: CaptionsService,
	community_id: int,
	name: str,
	fields: list[int | tuple[int, SourceRule]],
	pattern: str | None = None,
	preset_id: int | None = None,
) -> CaptionPresetDto:
	"""Сохраняет пресет: поле — id или пара «id, правило разбора»."""
	specs = tuple(
		PresetFieldSpec(*item) if isinstance(item, tuple) else PresetFieldSpec(item)
		for item in fields
	)
	return await service.save_preset(
		community_id, CaptionPresetDraft(name, specs, pattern), preset_id
	)


def _mock_quality(monkeypatch: pytest.MonkeyPatch, *, fails: bool = False) -> None:
	"""Подменяет ffprobe: кадр 1920×1080 или сбой чтения."""
	from pxcontrol.engine.video.probe import VideoInfo

	def probe(_path: str, _binary: str) -> VideoInfo:
		if fails:
			raise RuntimeError("не видео")
		return VideoInfo(1920, 1080, 60.0, 25.0, True)

	monkeypatch.setattr("pxcontrol.engine.services.captions.probe_video", probe)


# --- сервис: поля ---------------------------------------------------------------------


async def test_fields_crud_and_duplicates(db: Database) -> None:
	"""Поле создаётся с оформлением, дубль имени отклоняется, удаление чистит словарь."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Video", _TITLE_STYLE)
	assert field.name == "Video" and field.values == [] and field.style == _TITLE_STYLE
	assert field.parent_field_id is None
	with pytest.raises(CaptionsError, match="уже есть"):
		await service.add_field(community_id, "Video", FieldStyle())
	with pytest.raises(CaptionsError, match="имя"):
		await service.add_field(community_id, "  ", FieldStyle())
	await service.delete_field(field.id)
	assert await service.list_fields(community_id) == []


async def test_update_field_style_keeps_dictionary(db: Database) -> None:
	"""Оформление меняется без пересоздания поля — словарь цел."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	genre = await service.add_field(community_id, "Genre", FieldStyle(multiple=True))
	await service.add_values(genre.id, ["action"])
	bold = FieldStyle(hashtag=False, multiple=True, show_name=False, bold=True)
	updated = await service.update_field(genre.id, FieldEdit(bold))
	assert updated.style == bold and updated.names() == ["action"]
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.update_field(999_999, FieldEdit(FieldStyle()))


async def test_dictionary_add_and_delete_values(db: Database) -> None:
	"""Редактор словаря: добавление с дедупликацией, удаление значения."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Genre", FieldStyle(multiple=True))
	updated = await service.add_values(field.id, ["action", " Action ", "", "drama"])
	assert updated.names() == ["action", "drama"]  # дубль и пустое — пропущены
	action = next(item for item in updated.values if item.value == "action")
	updated = await service.delete_value(action.id)
	assert updated.names() == ["drama"]
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.add_values(999, ["x"])
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.delete_value(999)


# --- сервис: пресеты --------------------------------------------------------------------


async def test_preset_round_trip_with_rules_and_shared_dictionary(db: Database) -> None:
	"""Пресет хранит порядок и правила; словарь общий для всех пресетов."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	video = await service.add_field(community_id, "Video", _TITLE_STYLE)
	genre = await service.add_field(community_id, "Genre", FieldStyle(multiple=True))
	rule = SourceRule(extract=r"\(([^()]*)\)", case=CaseMode.EVERY_WORD)
	movie = await _preset(service, community_id, "Фильм", [(video.id, rule), genre.id])
	await _preset(service, community_id, "Клип", [genre.id])
	assert [item.field.name for item in movie.fields] == ["Video", "Genre"]
	assert [item.rule for item in movie.fields] == [rule, None]
	assert movie.fields[0].field.style == _TITLE_STYLE

	await service.record_usage(movie.id, {genre.id: ["action", "Action", "drama"]})
	presets = {p.name: p for p in await service.list_presets(community_id)}
	assert presets["Клип"].fields[0].field.names() == ["action", "drama"]
	assert presets["Фильм"].last_used_at is not None


async def test_parsed_fields_do_not_feed_dictionary(db: Database) -> None:
	"""Значения полей с правилом разбора в словарь не попадают (ADR-0042).

	Название ролика и состав — данные конкретного файла; словарь из них
	только засорялся бы.
	"""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	video = await service.add_field(community_id, "Video", _TITLE_STYLE)
	genre = await service.add_field(community_id, "Genre", FieldStyle(multiple=True))
	preset = await _preset(service, community_id, "Фильм", [(video.id, SourceRule()), genre.id])
	await service.record_usage(preset.id, {video.id: ["Lara Croft"], genre.id: ["action"]})
	fields = {f.name: f for f in await service.list_fields(community_id)}
	assert fields["Video"].names() == []
	assert fields["Genre"].names() == ["action"]


async def test_save_preset_applies_field_edits_in_one_record(db: Database) -> None:
	"""Правки полей едут той же записью; битая — не сохраняет ничего."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title = await service.add_field(community_id, "Title", FieldStyle())
	character = await service.add_field(community_id, "Character", FieldStyle(multiple=True))
	bold = FieldStyle(hashtag=False, bold=True)
	draft = CaptionPresetDraft(
		"Аниме",
		(PresetFieldSpec(title.id), PresetFieldSpec(character.id)),
		field_edits={
			title.id: FieldEdit(bold),
			character.id: FieldEdit(FieldStyle(multiple=True), title.id),
		},
	)
	preset = await service.save_preset(community_id, draft)
	fields = {item.field.name: item.field for item in preset.fields}
	assert fields["Title"].style == bold
	assert fields["Character"].parent_field_id == title.id

	# кольцо связей: вся запись откатывается, имя пресета не меняется
	ring = CaptionPresetDraft(
		"Переименован",
		draft.fields,
		field_edits={title.id: FieldEdit(FieldStyle(), character.id)},
	)
	with pytest.raises(CaptionsError, match="кольцо"):
		await service.save_preset(community_id, ring, preset.id)
	assert (await service.get_preset(preset.id)).name == "Аниме"


async def test_save_preset_validation(db: Database) -> None:
	"""Пустое имя/состав, повтор поля, чужое поле и битое правило отклоняются."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	other = await _add_community(db)
	field = await service.add_field(community_id, "Год", FieldStyle(hashtag=False))
	alien = await service.add_field(other, "Год", FieldStyle())
	with pytest.raises(CaptionsError, match="имя"):
		await _preset(service, community_id, " ", [field.id])
	with pytest.raises(CaptionsError, match="хотя бы одно"):
		await _preset(service, community_id, "Пустой", [])
	with pytest.raises(CaptionsError, match="дважды"):
		await _preset(service, community_id, "Дубль", [field.id, field.id])
	with pytest.raises(CaptionsError, match="не найдено у этого сообщества"):
		await _preset(service, community_id, "Чужой", [alien.id])
	with pytest.raises(CaptionsError, match="Поле №1: Выражение извлечения"):
		await _preset(service, community_id, "Битый", [(field.id, SourceRule(extract="(("))])
	with pytest.raises(CaptionsError, match="не найден"):
		await _preset(service, community_id, "Нет", [field.id], preset_id=999)
	assert await service.list_presets(community_id) == []


async def test_delete_preset_keeps_fields_and_dictionary(db: Database) -> None:
	"""Удаление пресета не трогает поля и словарь; повторное — без ошибки."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	field = await service.add_field(community_id, "Год", FieldStyle(hashtag=False))
	preset = await _preset(service, community_id, "Т", [field.id])
	await service.record_usage(preset.id, {field.id: ["2026"]})
	await service.delete_preset(preset.id)
	await service.delete_preset(preset.id)
	assert await service.list_presets(community_id) == []
	assert (await service.list_fields(community_id))[0].names() == ["2026"]
	with pytest.raises(CaptionsError, match="не найден"):
		await service.get_preset(preset.id)


async def test_delete_field_leaves_presets_without_it(db: Database) -> None:
	"""Удалённое поле уходит из состава всех пресетов (каскад схемы)."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	year = await service.add_field(community_id, "Год", FieldStyle(hashtag=False))
	genre = await service.add_field(community_id, "Genre", FieldStyle())
	preset = await _preset(service, community_id, "Т", [year.id, genre.id])
	await service.delete_field(year.id)
	assert [i.field.name for i in (await service.get_preset(preset.id)).fields] == ["Genre"]


# --- сервис: имя файла ------------------------------------------------------------------


async def test_render_filename(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Имя файла: поля, качество, канал, очистка символов."""
	_mock_quality(monkeypatch)
	service = CaptionsService(db)
	community_id = await _add_community(db, username="mych")
	author = await service.add_field(community_id, "Author", FieldStyle())
	video = await service.add_field(community_id, "Video", _TITLE_STYLE)
	genre = await service.add_field(community_id, "Genre", FieldStyle(multiple=True))
	preset = await _preset(
		service,
		community_id,
		"Фильм",
		[author.id, video.id, genre.id],
		"{Author}, {Video} ({Genre}) {quality} (@{channel})",
	)
	name = await service.render_filename(
		preset.id,
		community_id,
		{author.id: ["Best"], video.id: ["Lara: Croft"], genre.id: ["action", "drama"]},
		"/x/видео.mp4",
	)
	# двоеточие из названия вычищено, качество и канал подставлены
	assert name == "Best, Lara Croft (action, drama) 1080 (@mych).mp4"


async def test_render_filename_builtin_wins_over_field_namesake(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Поле-тёзка «channel» не подменяет встроенный плейсхолдер."""
	_mock_quality(monkeypatch)
	service = CaptionsService(db)
	community_id = await _add_community(db, username="mych")
	namesake = await service.add_field(community_id, "channel", FieldStyle())
	preset = await _preset(service, community_id, "Тёзка", [namesake.id], "{channel}")
	name = await service.render_filename(
		preset.id, community_id, {namesake.id: ["значение-поля"]}, "/x/в.mp4"
	)
	assert name == "mych.mp4"  # встроенный приоритетнее поля


async def test_render_filename_fits_telegram_limit(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Длинное имя режется до законченного слова в пределах лимита Telegram."""
	from pxcontrol.engine.services.captions import TELEGRAM_MAX_STEM_CHARS

	_mock_quality(monkeypatch, fails=True)
	service = CaptionsService(db)
	community_id = await _add_community(db, username="nature_docs")
	video = await service.add_field(community_id, "Video", _TITLE_STYLE)
	tags = await service.add_field(community_id, "Tags", FieldStyle(multiple=True))
	preset = await _preset(
		service, community_id, "Т", [video.id, tags.id], "{Video},@{channel},{Tags}"
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
		preset.id,
		community_id,
		{video.id: ["WinterMorningLights"], tags.id: values},
		"/x/v.mp4",
	)
	stem = name.removesuffix(".mp4")
	assert len(stem) <= TELEGRAM_MAX_STEM_CHARS
	# срез пришёлся на запятую после «Sunsets» — висячая запятая убрана
	assert stem == ("WinterMorningLights,@nature_docs,4K, 8K, Sunrise, Mountain, Twilight, Sunsets")


async def test_render_filename_cuts_long_value_at_word(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Огромное значение режется по границе слова, без огрызков."""
	_mock_quality(monkeypatch, fails=True)
	service = CaptionsService(db)
	community_id = await _add_community(db)
	video = await service.add_field(community_id, "Video", _TITLE_STYLE)
	preset = await _preset(service, community_id, "Т", [video.id], "{Video}")
	name = await service.render_filename(
		preset.id, community_id, {video.id: ["Длинное Слово " * 20]}, "/x/v.mp4"
	)
	stem = name.removesuffix(".mp4")
	assert len(stem) <= 78
	assert all(w in ("Длинное", "Слово") for w in stem.split())


async def test_render_filename_edge_cases(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Не-видео — без качества; неизвестный плейсхолдер остаётся; без шаблона — ошибка."""
	_mock_quality(monkeypatch, fails=True)
	service = CaptionsService(db)
	community_id = await _add_community(db)  # сообщество без username
	field = await service.add_field(community_id, "Год", FieldStyle(hashtag=False))
	preset = await _preset(service, community_id, "Т", [field.id], "Имя {quality} {Нет} ({Год})")
	name = await service.render_filename(preset.id, community_id, {field.id: ["2026"]}, "/x/ф.zip")
	assert name == "Имя {Нет} (2026).zip"
	no_pattern = await _preset(service, community_id, "Без", [field.id])
	with pytest.raises(CaptionsError, match="не задан шаблон имени"):
		await service.render_filename(no_pattern.id, community_id, {}, "/x/ф.mp4")
	empty = await _preset(service, community_id, "Пусто", [field.id], "{Год}")
	with pytest.raises(CaptionsError, match="пустым"):
		await service.render_filename(empty.id, community_id, {}, "/x/ф.mp4")


async def test_render_filename_respects_limits(
	db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Сплошное слово — жёсткий срез по лимиту Telegram; байтовый предел цел."""
	from pxcontrol.engine.services.captions import (
		MAX_FILENAME_BYTES,
		TELEGRAM_MAX_STEM_CHARS,
	)

	_mock_quality(monkeypatch, fails=True)
	service = CaptionsService(db)
	community_id = await _add_community(db)
	video = await service.add_field(community_id, "Video", _TITLE_STYLE)
	preset = await _preset(service, community_id, "Т", [video.id], "{Video}")
	name = await service.render_filename(
		preset.id, community_id, {video.id: ["a" * 200]}, "/x/ф.mp4"
	)
	assert name == "a" * TELEGRAM_MAX_STEM_CHARS + ".mp4"
	long_name = await service.render_filename(
		preset.id, community_id, {video.id: ["\U0001f600" * 100]}, "/x/ф.mp4"
	)
	assert long_name.endswith(".mp4")
	assert len(long_name.encode("utf-8")) <= MAX_FILENAME_BYTES
	assert len(long_name.removesuffix(".mp4")) <= TELEGRAM_MAX_STEM_CHARS


# --- связанные словари (персонаж внутри тайтла) ------------------------------------------


async def _linked_fields(service: CaptionsService, community_id: int) -> tuple[int, int]:
	"""Готовит пару полей «Title» и зависимый от него «Character»."""
	title = await service.add_field(community_id, "Title", FieldStyle())
	character = await service.add_field(community_id, "Character", FieldStyle(multiple=True))
	linked = await service.update_field(character.id, FieldEdit(character.style, title.id))
	assert linked.parent_field_id == title.id
	return title.id, character.id


async def test_usage_binds_new_values_to_selected_parent(db: Database) -> None:
	"""Персонажи привязываются к выбранному тайтлу; тёзка — своя запись."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	preset = await _preset(service, community_id, "Фильм", [title_id, character_id])
	await service.record_usage(preset.id, {title_id: ["TombRider"], character_id: ["Lara", "Zip"]})
	await service.record_usage(preset.id, {title_id: ["Fallout"], character_id: ["Lara"]})
	fields = {f.name: f for f in await service.list_fields(community_id)}
	titles = {item.id: item.value for item in fields["Title"].values}
	bound = sorted(
		(item.value, titles[item.parent_id])
		for item in fields["Character"].values
		if item.parent_id is not None
	)
	assert bound == [("Lara", "Fallout"), ("Lara", "TombRider"), ("Zip", "TombRider")]


async def test_available_filters_by_parent(db: Database) -> None:
	"""Словарь зависимого поля фильтруется по выбранному значению родителя."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	preset = await _preset(service, community_id, "Фильм", [title_id, character_id])
	await service.record_usage(preset.id, {title_id: ["TombRider"], character_id: ["Lara"]})
	await service.record_usage(preset.id, {title_id: ["Fallout"], character_id: ["Vault Boy"]})
	await service.add_values(character_id, ["Ничей"])
	fields = {f.name: f for f in await service.list_fields(community_id)}
	tomb = next(i for i in fields["Title"].values if i.value == "TombRider")
	character = fields["Character"]
	assert [i.value for i in character.available([tomb.id])] == ["Lara", "Ничей"]
	assert [i.value for i in character.available([])] == ["Ничей"]
	assert [i.value for i in fields["Title"].available([])] == ["Fallout", "TombRider"]


async def test_deleting_parent_value_removes_children(db: Database) -> None:
	"""Удаление тайтла уносит его персонажей; чужие остаются."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	preset = await _preset(service, community_id, "Фильм", [title_id, character_id])
	await service.record_usage(preset.id, {title_id: ["TombRider"], character_id: ["Lara"]})
	await service.record_usage(preset.id, {title_id: ["Fallout"], character_id: ["Vault Boy"]})
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

	preset = await _preset(service, community_id, "Фильм", [title_id, character_id])
	await service.record_usage(preset.id, {title_id: ["TombRider"], character_id: ["Lara"]})
	characters = next(f for f in await service.list_fields(community_id) if f.name == "Character")
	assert characters.names() == ["Lara"]
	assert characters.values[0].parent_id == tomb.id


async def test_parent_validation_and_unlink(db: Database) -> None:
	"""Негодный родитель отклоняется; снятие связи чистит привязки значений."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	other_community = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	multi = FieldStyle(multiple=True)
	with pytest.raises(CaptionsError, match="само от себя"):
		await service.update_field(character_id, FieldEdit(multi, character_id))
	with pytest.raises(CaptionsError, match="кольцо"):
		await service.update_field(title_id, FieldEdit(FieldStyle(), character_id))
	alien = await service.add_field(other_community, "Title", FieldStyle())
	with pytest.raises(CaptionsError, match="не найдено у этого сообщества"):
		await service.update_field(character_id, FieldEdit(multi, alien.id))
	with pytest.raises(CaptionsError, match="не найдено"):
		await service.update_field(999, FieldEdit(multi, title_id))

	titles = await service.add_values(title_id, ["TombRider"])
	await service.add_values(character_id, ["Lara"], titles.values[0].id)
	characters = await service.update_field(character_id, FieldEdit(multi, None))
	assert characters.parent_field_id is None
	assert characters.values[0].parent_id is None
	with pytest.raises(CaptionsError, match="не зависит от другого поля"):
		await service.assign_value_parent(characters.values[0].id, titles.values[0].id)


async def test_style_edit_keeps_value_bindings(db: Database) -> None:
	"""Правка одного оформления связь не трогает — привязки значений целы."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	titles = await service.add_values(title_id, ["TombRider"])
	await service.add_values(character_id, ["Lara"], titles.values[0].id)
	updated = await service.update_field(
		character_id, FieldEdit(FieldStyle(multiple=True, bold=True), title_id)
	)
	assert updated.values[0].parent_id == titles.values[0].id


async def test_delete_parent_field_keeps_dependent_dictionary(db: Database) -> None:
	"""Удаление родительского поля не стирает словарь зависимого."""
	service = CaptionsService(db)
	community_id = await _add_community(db)
	title_id, character_id = await _linked_fields(service, community_id)
	titles = await service.add_values(title_id, ["TombRider"])
	await service.add_values(character_id, ["Lara"], titles.values[0].id)

	await service.delete_field(title_id)

	fields = await service.list_fields(community_id)
	assert [f.name for f in fields] == ["Character"]
	character = fields[0]
	assert character.parent_field_id is None
	assert character.names() == ["Lara"]
	assert character.values[0].parent_id is None
