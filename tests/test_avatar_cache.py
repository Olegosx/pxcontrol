"""Кэш картинок аватаров: файл читается один раз на путь и версию файла."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pxcontrol.ui.pages import common  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
	app = QApplication.instance() or QApplication([])
	assert isinstance(app, QApplication)
	yield app


def _png(path: Path, color: str) -> None:
	image = QImage(8, 8, QImage.Format.Format_RGB32)
	image.fill(QColor(color))
	assert image.save(str(path))


def test_same_file_is_read_once(qapp: QApplication, tmp_path: Path) -> None:
	del qapp
	common._avatar_image.cache_clear()  # noqa: SLF001
	path = tmp_path / "avatar.png"
	_png(path, "#ff0000")
	first = common.avatar_image(str(path))
	second = common.avatar_image(str(path))
	assert first is not None and first is second
	info = common._avatar_image.cache_info()  # noqa: SLF001
	assert (info.misses, info.hits) == (1, 1)


def test_changed_file_is_reread(qapp: QApplication, tmp_path: Path) -> None:
	del qapp
	common._avatar_image.cache_clear()  # noqa: SLF001
	path = tmp_path / "avatar.png"
	_png(path, "#ff0000")
	before = common.avatar_image(str(path))
	_png(path, "#0000ff")
	stamp = path.stat().st_mtime_ns + 1_000_000_000  # заведомо другая версия файла
	os.utime(path, ns=(stamp, stamp))
	after = common.avatar_image(str(path))
	assert before is not None and after is not None and after is not before
	assert after.pixelColor(0, 0) == QColor("#0000ff")


def test_missing_or_broken_file_gives_none(qapp: QApplication, tmp_path: Path) -> None:
	del qapp
	assert common.avatar_image(str(tmp_path / "нет.png")) is None
	broken = tmp_path / "broken.png"
	broken.write_bytes(b"not an image")
	assert common.avatar_image(str(broken)) is None
