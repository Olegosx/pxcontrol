"""Тема и акцентный цвет приложения (QFluentWidgets, ADR-0008)."""

from __future__ import annotations

from qfluentwidgets import Theme, setTheme, setThemeColor

#: Акцентный цвет — тиловый, выбран по интерактивному макету (ADR-0008).
ACCENT_COLOR = "#14b8a6"


def apply_theme(dark: bool = True) -> None:
	"""Применяет тему и акцент. Вызывается до создания главного окна.

	Args:
		dark: ``True`` — тёмная тема (по умолчанию), ``False`` — светлая.
	"""
	setTheme(Theme.DARK if dark else Theme.LIGHT)
	setThemeColor(ACCENT_COLOR)
