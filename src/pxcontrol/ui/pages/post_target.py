"""Адресат поста: сообщество и тема форума — общий блок форм публикации.

Куда уходит пост, спрашивают две формы раздела «Публикация» (ADR-0032):
«Новый пост» и «Пакет». Вопрос у них один и тот же, и правила ответа
тоже: в списке только включённые сообщества, выбор хранится по id
(названия в Telegram не уникальны), ряд темы форума виден только форуму
с публикатором, закрытые темы участнику не предлагаются (ADR-0022),
а ответ движка, пришедший для уже переключённого сообщества,
отбрасывается — иначе данные сообщества A легли бы в виджеты
сообщества B.

Пока форма была одна, всё это жило внутри неё. С появлением второй
формы правила должны жить в одном месте, иначе они начнут расходиться
по мелочи — как уже начинали расходиться тексты рядов темы до того, как
их свели в общий :func:`~pxcontrol.ui.pages.common.topic_row`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.settings import PUBLISH_LAST_COMMUNITY_ID
from pxcontrol.engine.telegram.types import ForumTopicInfo
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.common import (
	DtoComboBox,
	closed_topics_hint,
	community_combo_label,
	noop,
	topic_label,
	topic_row,
	visible_topics,
)

#: Подсказка ряда темы форума — одна на обе формы.
TOPIC_TOOLTIP = (
	"Тема, в которую уйдёт пост; «Общая лента» — General. "
	"Список читается из Telegram при выборе сообщества."
)


class CommunityChoice(DtoComboBox[CommunityDto]):
	"""Выбор сообщества для публикации: только включённые, выбор по id.

	Выключенные сообщества в списке не показываются — фильтр
	презентационный, само правило держит движок (``PostsService``
	откажет выключенному). Предвыбор (:meth:`want`) применяется сразу,
	если список уже загружен, и при ближайшей загрузке, если нет;
	несбывшийся предвыбор на свежем списке забывается — иначе он
	«выстрелил» бы позже внезапной сменой сообщества.
	"""

	#: Выбор изменился (рукой, предвыбором или пересборкой списка).
	chosen = Signal()

	def __init__(self, parent: QWidget, worker: EngineWorker) -> None:
		super().__init__(parent)
		self._worker = worker
		self._wanted: int | None = None
		self.currentIndexChanged.connect(lambda _index: self.chosen.emit())

	def reload(self, on_error: Callable[[str], None]) -> None:
		"""Просит у движка свежий список сообществ."""
		run_in_engine(
			self._worker,
			self._worker.engine.communities.list_communities(),
			self,
			self._show,
			on_error,
		)

	def restore_last(self) -> None:
		"""Предвыбирает сообщество прошлой публикации (настройка приложения).

		Сбой чтения настройки молчалив: предвыбор — удобство, без него
		форма просто открывается на первом сообществе списка.
		"""
		run_in_engine(
			self._worker,
			self._worker.engine.settings.get(PUBLISH_LAST_COMMUNITY_ID),
			self,
			self.want,
			noop,
		)

	def remember_last(self) -> None:
		"""Запоминает выбранное сообщество как сообщество прошлой публикации."""
		community = self.current()
		if community is None:
			return
		run_in_engine(
			self._worker,
			self._worker.engine.settings.set(PUBLISH_LAST_COMMUNITY_ID, community.id),
			self,
			noop,
			noop,
		)

	def want(self, community_id: int | None) -> None:
		"""Предвыбирает сообщество по id (None — ничего не менять)."""
		if community_id is None:
			return
		self._wanted = community_id
		self._apply_wanted()

	def current(self) -> CommunityDto | None:
		"""Выбранное сообщество или None (список пуст либо ещё не загружен)."""
		return self.selected()

	def is_stale(self, community_id: int) -> bool:
		"""Пришёл ли ответ движка для уже переключённого сообщества.

		Пока движок занят (очередь отправки в том же цикле, ADR-0016),
		ответы задерживаются: без этой проверки подсказки и пределы
		сообщества A перезаписали бы уже показанные данные сообщества B.
		"""
		return not self.is_current_id(community_id)

	def _show(self, communities: list[CommunityDto]) -> None:
		"""Пересобирает список, сохраняя выбор по id сообщества."""
		self.set_items(
			[community for community in communities if community.enabled],
			label=community_combo_label,
			key=lambda community: community.id,
		)
		# успешный предвыбор сам излучает сигнал смены (контракт
		# ``DtoComboBox.select``); сообщить о пересборке нужно только
		# когда применять было нечего или предвыбор не сбылся
		if not self._apply_wanted():
			self._wanted = None
			self.chosen.emit()

	def _apply_wanted(self) -> bool:
		"""Применяет отложенный предвыбор (True — применён)."""
		wanted = self._wanted
		if wanted is None:
			return False
		if self.select(lambda community: community.id == wanted):
			self._wanted = None
			return True
		return False


class TopicChoice:
	"""Ряд выбора темы форума: список тем читается у движка по сообществу.

	Темы читает только userbot (у Bot API метода нет): форум лишь
	с ботом публикует в общую ленту — ряд скрыт. Закрытые темы видит
	и выбирает только админ (ADR-0022): участнику они не предлагаются,
	а их число названо подписью ряда.
	"""

	def __init__(
		self,
		page: QWidget,
		layout: QVBoxLayout,
		worker: EngineWorker,
		choice: CommunityChoice,
		*,
		on_failed: Callable[[str], None] | None = None,
	) -> None:
		"""Args:
		page: страница-владелец (владелец колбэков движка).
		layout: компоновка, в которую встаёт ряд.
		worker: мост к движку.
		choice: выбор сообщества — по нему проверяется, не устарел ли
			ответ движка.
		on_failed: темы не прочитались — владелец говорит человеку, что
			пост уйдёт в общую ленту (текст причины приходит аргументом).
		"""
		self._page = page
		self._worker = worker
		self._choice = choice
		self._on_failed = on_failed
		row = topic_row(page, layout, tooltip=TOPIC_TOOLTIP)
		self._box, self._combo, self._hint = row.box, row.combo, row.hint
		self._box.setVisible(False)

	def update_for(self, community: CommunityDto | None) -> None:
		"""Показывает и наполняет ряд под выбранное сообщество."""
		if community is None or not community.forum or not community.capabilities.userbot:
			self._box.setVisible(False)
			self._combo.set_items([], label=topic_label)
			return
		self._box.setVisible(True)
		self._hint.setText("")
		self._combo.set_items([], label=topic_label)
		run_in_engine(
			self._worker,
			self._worker.engine.posts.list_topics(community.id),
			self._page,
			partial(self._show_topics, community),
			partial(self._failed, community.id),
		)

	def topic_id(self) -> int | None:
		"""Выбранная тема; ряд скрыт или «Общая лента» — None."""
		if not self._box.isVisibleTo(self._page):
			return None
		topic = self._combo.selected()
		return topic.id if topic is not None else None

	def _show_topics(self, community: CommunityDto, topics: list[ForumTopicInfo]) -> None:
		"""Наполняет список тем с учётом роли публикатора (ADR-0022)."""
		if self._choice.is_stale(community.id):
			return
		shown, closed = visible_topics(topics, community.default_role)
		if closed:
			self._hint.setText(closed_topics_hint(closed))
		self._combo.set_items(shown, label=topic_label, key=lambda topic: topic.id)

	def _failed(self, community_id: int, message: str) -> None:
		"""Темы не прочитались — пост уйдёт в общую ленту, честно предупредив."""
		if self._choice.is_stale(community_id):
			return
		self._box.setVisible(False)
		if self._on_failed is not None:
			self._on_failed(message)
