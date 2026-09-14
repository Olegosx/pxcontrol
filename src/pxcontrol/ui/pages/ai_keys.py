"""Категория настроек «Ключи ИИ»: ключи провайдеров для генерации контента.

Прежде жила на странице «Аккаунты» вместе с ботами и userbot-аккаунтами;
те переехали в раздел «Пользователи и боты» (ADR-0029), а ключи ИИ —
реквизиты внешнего провайдера, как ключ API Telegram, — остались
в настройках своей категорией.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PushButton, ScrollArea, SubtitleLabel

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.accounts import AiKeyDto
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	FormDialog,
	bind,
	clear_layout,
	confirm_delete,
	error_reporter,
	exec_dialog,
	page_layout,
	require_filled,
	row_card,
	show_success,
)


class AiKeysPanel(ScrollArea):
	"""Список ключей ИИ: добавление, удаление; значения показаны маской (ADR-0009)."""

	def __init__(self, worker: EngineWorker, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setObjectName("ai_keys")
		self._worker = worker
		self._show_error = error_reporter(self)
		self._build()
		self._reload()

	def _build(self) -> None:
		"""Заголовок с кнопкой «Добавить» и список карточек."""
		layout = page_layout(self, spacing=density.spacing().wide_spacing)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel("Ключи ИИ", self))
		header.addStretch()
		add_button = PushButton(FluentIcon.ADD, "Добавить", self)
		add_button.clicked.connect(self._on_add)
		header.addWidget(add_button)
		layout.addLayout(header)
		self._rows = QVBoxLayout()
		self._rows.setSpacing(density.spacing().list_spacing)
		layout.addLayout(self._rows)
		layout.addStretch()

	def _reload(self) -> None:
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.list_ai_keys(),
			self,
			self._show_keys,
			self._show_error,
		)

	def _show_keys(self, keys: list[AiKeyDto]) -> None:
		"""Перерисовывает список ключей (значения замаскированы)."""
		clear_layout(self._rows)
		if not keys:
			self._rows.addWidget(
				CaptionLabel("Пока нет ключей — нужны для генерации контента.", self)
			)
			return
		for key in keys:
			self._rows.addWidget(
				row_card(
					self,
					key.label or key.provider,
					f"{key.provider} · {key.key_masked}",
					on_delete=bind(self._delete_key, key),
				)
			)

	def _on_add(self) -> None:
		dialog = FormDialog(
			"Новый ключ ИИ (Anthropic)",
			[("label", "Название"), ("api_key", "Ключ API (sk-ant-…)")],
			self.window(),
			validator=require_filled("label", "api_key", message="Заполните оба поля."),
		)
		if not exec_dialog(dialog):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.add_ai_key(dialog.value("label"), dialog.value("api_key")),
			self,
			self._on_key_added,
			self._show_error,
		)

	def _on_key_added(self, key: AiKeyDto) -> None:
		show_success(self, "Ключ сохранён", key.label)
		self._reload()

	def _delete_key(self, key: AiKeyDto) -> None:
		if not confirm_delete(self, f"Удалить ключ «{key.label}»?"):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.accounts.delete_ai_key(key.id),
			self,
			self._reload,
			self._show_error,
		)
