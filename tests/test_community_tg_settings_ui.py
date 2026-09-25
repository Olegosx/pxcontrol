"""Экран «Настройки › В Telegram» и редакторы значений (ADR-0043) — Qt без экрана."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import fields, replace
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402 — после выбора платформы

from pxcontrol.engine.community_settings.catalog import SPECS  # noqa: E402
from pxcontrol.engine.community_settings.model import (  # noqa: E402
	ChangeResult,
	CommunitySettings,
	LinkedChat,
	PhotoValue,
	ReactionsValue,
	SettingsContext,
	SettingValue,
)
from pxcontrol.engine.community_settings.rules import Availability  # noqa: E402
from pxcontrol.engine.services.community_settings import (  # noqa: E402
	NO_EXECUTOR,
	SettingsSaved,
	SettingsView,
)
from pxcontrol.engine.telegram.rights import ALL_MEMBER_RIGHTS, MemberRights  # noqa: E402
from pxcontrol.engine.telegram.types import (  # noqa: E402
	ChatReactionsMode,
	CommunityKind,
	OwnerKind,
)
from pxcontrol.ui.pages.common import UnsavedChanges  # noqa: E402
from pxcontrol.ui.pages.community_tg_settings import (  # noqa: E402
	BOT_NOTES,
	TelegramSettingsScreen,
	executor_caption,
	save_summary,
)
from pxcontrol.ui.pages.setting_editors import (  # noqa: E402
	EDITORS,
	PERMISSION_LABELS,
	EditorContext,
	build_editor,
)

GROUP = SettingsContext(CommunityKind.GROUP, reaction_catalog=("👍", "🔥"), reactions_max=11)


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


@pytest.fixture
def host(qapp: QApplication) -> Iterator[QWidget]:
	widget = QWidget()
	yield widget
	widget.deleteLater()


# --- чистое ----------------------------------------------------------------------------


def test_every_value_kind_has_an_editor_and_labels_are_complete() -> None:
	from pxcontrol.engine.community_settings.model import ValueKind

	assert set(EDITORS) == set(ValueKind)
	assert list(PERMISSION_LABELS) == [field.name for field in fields(MemberRights)]


def test_save_summary() -> None:
	labels = {"title": "Название", "slowmode": "Медленный режим"}
	ok = SettingsSaved((ChangeResult("title"), ChangeResult("slowmode")))
	assert save_summary(ok, labels) == (True, "Применено изменений: 2.")
	assert save_summary(SettingsSaved(()), labels) == (True, "Изменений нет.")
	partial = SettingsSaved((ChangeResult("title"), ChangeResult("slowmode", "Не отправлено: …")))
	complete, text = save_summary(partial, labels)
	assert not complete and text == "«Медленный режим»: Не отправлено: …"


def test_executor_caption_explains_bot_limits() -> None:
	bot = SettingsView(1, "бот", OwnerKind.BOT)
	assert "userbot" in executor_caption(bot)
	assert "@ub" in executor_caption(SettingsView(1, "@ub", OwnerKind.USER))


# --- редакторы ------------------------------------------------------------------------------


VALUES: dict[str, SettingValue] = {
	"noforwards": True,
	"title": "Группа",
	"about": "описание",
	"slowmode": 30,
	"permissions": replace(ALL_MEMBER_RIGHTS, send_polls=False),
	"reactions": ReactionsValue(ChatReactionsMode.SOME, ("🔥",), 3),
	"photo": PhotoValue(True),
	"linked_chat": LinkedChat("-1005", "Обсуждение"),
}


@pytest.mark.parametrize("key", list(VALUES))
def test_editor_round_trip(host: QWidget, key: str) -> None:
	"""Показанное значение читается обратно без изменений — показ не правка."""
	editor = build_editor(SPECS[key], VALUES[key], EditorContext(GROUP), host)
	assert editor.value() == VALUES[key]


def test_choice_editor_keeps_value_outside_catalog(host: QWidget) -> None:
	"""Значение, выставленное другим клиентом, не теряется и не выглядит правкой."""
	year = 31536000
	editor = build_editor(SPECS["ttl"], year, EditorContext(GROUP), host)
	assert editor.value() == year


def test_reactions_editor_modes(host: QWidget) -> None:
	editor = build_editor(
		SPECS["reactions"], ReactionsValue(ChatReactionsMode.ALL), EditorContext(GROUP), host
	)
	assert editor.value() == ReactionsValue(ChatReactionsMode.ALL)
	editor._mode.setCurrentIndex(1)  # «только выбранные»
	editor._pills["👍"].setChecked(True)
	assert editor.value() == ReactionsValue(ChatReactionsMode.SOME, ("👍",))


def test_editor_signals_only_on_edit(host: QWidget) -> None:
	editor = build_editor(SPECS["noforwards"], False, EditorContext(GROUP), host)
	seen: list[bool] = []
	editor.changed.connect(lambda: seen.append(True))
	editor.set_value(True)
	assert seen == [], "показ — не правка"
	editor._switch.setChecked(False)
	assert seen == [True]


# --- экран ------------------------------------------------------------------------------------


def _view(**access: Availability) -> SettingsView:
	values: dict[str, SettingValue] = {
		"title": "Группа",
		"about": "",
		"slowmode": 0,
		"forum": False,
		"permissions": ALL_MEMBER_RIGHTS,
	}
	specs = tuple(spec for spec in SPECS.values() if spec.key in values)
	rights = {key: access.get(key, Availability(True)) for key in values}
	return SettingsView(1, "@ub", OwnerKind.USER, CommunitySettings(values, GROUP), specs, rights)


def _screen(host: QWidget) -> TelegramSettingsScreen:
	community: Any = SimpleNamespace(id=1, title="Группа", username=None)
	return TelegramSettingsScreen(Any, community, host)  # type: ignore[arg-type]


def test_screen_builds_form_by_catalog_and_tracks_edits(host: QWidget) -> None:
	screen = _screen(host)
	assert isinstance(screen, UnsavedChanges)
	screen.show_view(_view(forum=Availability(False, "Нужно право «владелец».")))
	assert list(screen._editors) == ["title", "about", "permissions", "slowmode", "forum"]
	assert not screen._editors["forum"].isEnabled()
	assert not screen.dirty
	screen._editors["slowmode"]._combo.setCurrentIndex(2)
	assert screen.dirty
	screen.discard()
	assert not screen.dirty and screen._editors["slowmode"].value() == 0


def test_screen_without_executor_shows_reason(host: QWidget) -> None:
	screen = _screen(host)
	requested: list[bool] = []
	screen.members_requested.connect(lambda: requested.append(True))
	screen.show_view(SettingsView(1, reason=NO_EXECUTOR))
	assert screen._editors == {} and not screen.dirty


def test_bot_note_under_permissions(host: QWidget) -> None:
	screen = _screen(host)
	view = replace(_view(), executor_kind=OwnerKind.BOT)
	note = screen._note(SPECS["permissions"], view, None)
	assert note == BOT_NOTES["permissions"]
	assert screen._note(SPECS["forum"], view, "причина") == "причина"
