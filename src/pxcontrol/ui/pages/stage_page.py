"""Экран стадии раздела «Публикация»: общая рамка и правило жизни.

Стадий у поста несколько (ADR-0032), и у каждой свой экран. Общего
у экранов две вещи: шапка (заголовок стадии и строка-подсказка, чем эта
стадия отличается от соседних) и правило «экран видно — экран работает».

Правило вынесено сюда, а не оставлено каждому экрану, потому что цена
у него одна на всех: невидимый экран не должен опрашивать ни движок,
ни Telegram. До ADR-0032 это правило жило внутри страницы «Расписание»
и переключало её вкладки; с разъездом стадий по экранам оно стало общим.
"""

from __future__ import annotations

from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ScrollArea, SubtitleLabel

from pxcontrol.ui.pages.common import page_layout
from pxcontrol.ui.pages.publish_stages import PublishStage, stage_hint, stage_title


class StagePage(ScrollArea):
	"""Экран стадии: заголовок, подсказка и правило «виден — работает».

	Тело ставится наследником (:meth:`mount`), работа включается
	и гасится по видимости (:meth:`set_active`). Правило общее, потому
	что цена у него одна на всех: невидимый экран не должен опрашивать
	ни движок, ни Telegram — обход отложенных на скрытом экране ловил бы
	флуд-лимит, а он замораживает дорожку аккаунта вместе с публикацией
	(ADR-0024).
	"""

	def __init__(self, stage: PublishStage, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName(stage.value)
		self.stage = stage
		self._layout = page_layout(self)
		self._layout.addWidget(SubtitleLabel(stage_title(stage), self))
		hint = CaptionLabel(stage_hint(stage), self)
		hint.setWordWrap(True)
		self._layout.addWidget(hint)

	def body_layout(self) -> QVBoxLayout:
		"""Компоновка под шапкой — для экрана, который кладёт блоки сам.

		Форма поста состоит из десятка блоков подряд (адресат, текст,
		файлы, время, кнопки), и заворачивать их в одно тело незачем:
		ей нужна та же компоновка, что у шапки.
		"""
		return self._layout

	def mount(self, body: QWidget) -> None:
		"""Ставит тело экрана под шапку (наследник зовёт это один раз)."""
		self._layout.addWidget(body, stretch=1)
		self._layout.addStretch()

	def set_active(self, active: bool) -> None:
		"""Экран показан или скрыт — наследник включает и гасит свою работу."""

	def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — API Qt
		"""Экран показан: работа включается."""
		super().showEvent(event)
		self.set_active(True)

	def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 — API Qt
		"""Экран скрыт: работа гаснет."""
		super().hideEvent(event)
		self.set_active(False)
