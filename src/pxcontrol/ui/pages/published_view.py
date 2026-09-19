"""Вид ленты сообщества: что уже вышло (экран «Опубликовано», ADR-0032).

Последняя стадия пути поста и единственная, где истина живёт целиком
в Telegram: своей таблицы постов у приложения нет (ADR-0010), поэтому
лента **читается** — страницами по полсотни, от новых к старым, тем же
способом, что история у обслуживания (ADR-0026). Каждая страница —
один запрос через дорожку аккаунта (ADR-0024): следующая читается
по требованию человека, а не «вся лента сразу».

Зачем стадия понадобилась: кнопки под постом бот дорисовывает **после**
выхода (ADR-0031), и человек обязан видеть, поставились они или нет.
Поэтому на карточке рядом с обычным «когда вышел» стоит состояние
кнопок — и выполненное обещание, и невыполненное с причиной.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, FluentIcon, PushButton

from pxcontrol.engine import EngineWorker
from pxcontrol.engine.services.communities import CommunityDto
from pxcontrol.engine.services.posts import (
	PublishedList,
	PublishedPostDto,
	PublishedRef,
	group_albums,
)
from pxcontrol.engine.telegram.types import MediaKind
from pxcontrol.ui import density
from pxcontrol.ui.async_bridge import run_in_engine
from pxcontrol.ui.pages.card_list import CardList
from pxcontrol.ui.pages.common import (
	bind,
	confirm_delete,
	error_reporter,
	format_local,
	kind_label,
	open_link,
	plural,
)
from pxcontrol.ui.pages.post_target import CommunityChoice
from pxcontrol.ui.pages.published_edit import mount_published_editor

#: Сколько лента считается свежей: повторный показ экрана в этот срок
#: не перечитывает её заново. Минута — как у отложенных записей: ушёл
#: и вернулся, а лента за это время меняется редко.
FRESH_FOR_S = 60.0


def markup_note(item: PublishedPostDto) -> str:
	"""Состояние кнопок под постом — самое важное на этой стадии.

	Кнопки ставит бот и только после выхода поста (ADR-0031), поэтому
	у поста есть три честных состояния: кнопки стоят, кнопки обещаны
	и ещё не встали, кнопок не обещали вовсе. Молчать о втором нельзя —
	человек решит, что кнопки потерялись.
	"""
	if item.buttons:
		return f"кнопки: {item.buttons}"
	if item.markup_error is None:
		return ""
	if item.markup_error:
		return f"кнопки обещаны, не поставлены ({item.markup_error})"
	return "кнопки обещаны, ждут бота"


def content_note(item: PublishedPostDto) -> str:
	"""Что внутри поста: текст, вложение или альбом из скольких файлов.

	Альбом Telegram отдаёт несколькими записями, а читателю показывает
	одной публикацией — карточка называет его альбомом и числом файлов,
	а не видом первого вложения (ADR-0033, C4).
	"""
	if item.is_album:
		size = item.album_size
		return f"альбом: {size} {plural(size, 'файл', 'файла', 'файлов')}"
	if item.media_kind is MediaKind.NONE:
		return "текст"
	return kind_label(item.media_kind).lower()


def published_subtitle(item: PublishedPostDto) -> str:
	"""Подпись карточки: когда вышел, что внутри, сколько просмотров.

	Момент хранится в UTC (как отдаёт Telegram) и показывается
	в местном времени — как во всех списках приложения.
	"""
	parts = [f"вышел: {format_local(item.published_at)}", content_note(item)]
	if item.views is not None:
		parts.append(f"{item.views} {plural(item.views, 'просмотр', 'просмотра', 'просмотров')}")
	note = markup_note(item)
	if note:
		parts.append(note)
	return " · ".join(parts)


def feed_summary(shown: int, more: bool) -> str:
	"""Итоговая строка под лентой: сколько прочитано и есть ли ещё.

	Числа ленты — всегда «сколько прочитали», а не «сколько есть»:
	сколько всего постов в сообществе, Telegram одним ответом
	не говорит, и придумывать общее число значило бы соврать.
	"""
	if not shown:
		return "Постов не прочитано."
	tail = " · дальше есть" if more else " · это вся лента"
	return f"Прочитано {shown} {plural(shown, 'пост', 'поста', 'постов')}{tail}"


def published_signature(item: PublishedPostDto) -> tuple[Any, ...]:
	"""Отпечаток карточки: всё, что она показывает."""
	return (
		item.text_preview,
		item.buttons,
		item.views,
		item.markup_error,
		item.media_kind,
		# альбом на границе страниц дорастает при дочитывании —
		# карточка обязана пересобраться, а не остаться «3 файла»
		item.album_size,
	)


class PublishedView(QWidget):
	"""Лента одного сообщества: карточки вышедших постов и чтение страницами.

	Сообщество выбирается здесь же: лента у каждого своя, и сводить
	их в общий список незачем — посты разных сообществ не сравнивают,
	а чтение стоит по запросу на сообщество.
	"""

	def __init__(self, worker: EngineWorker, parent: QWidget) -> None:
		super().__init__(parent)
		self._worker = worker
		self._show_error = error_reporter(self)
		self._items: list[PublishedPostDto] = []
		self._next_offset: int | None = None
		#: сообщество, чью страницу читаем прямо сейчас (None — не читаем).
		#: Не просто «занято»: ответ брошенного чтения нельзя путать
		#: с ответом нужного — см. :meth:`_read`
		self._loading_for: int | None = None
		self._loaded_at: float | None = None
		self._community_id: int | None = None
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(density.spacing().row_spacing)
		self._build_head(layout)
		self._status = CaptionLabel("", self)
		self._status.setWordWrap(True)
		layout.addWidget(self._status)
		box = QVBoxLayout()
		box.setSpacing(density.spacing().list_spacing)
		layout.addLayout(box)
		self._build_footer(layout)
		# растяжка снизу: без неё лишнюю высоту экрана забирают подписи
		# с переносом слов, и список расползается пустотами
		layout.addStretch()

		self._list = CardList(
			self,
			box,
			subtitle=published_subtitle,
			signature=published_signature,
			key=lambda item: item.message_id,
			actions=self._actions,
			# «Открыть» есть только у поста со ссылкой — от этого и зависит правый край
			actions_signature=lambda item: (bool(item.link),),
			# правка — прямо в карточке, как у очереди и отложенных:
			# текст меняет публикатор, кнопки — бот (ADR-0032, подача A4)
			editable=lambda _item: True,
			fill_body=self._fill_editor,
		)
		self._render()

	# --- сборка ------------------------------------------------------------------

	def _build_head(self, layout: QVBoxLayout) -> None:
		"""Строка выбора сообщества и кнопка перечитывания ленты."""
		row = QHBoxLayout()
		self._community = CommunityChoice(self, self._worker)
		self._community.chosen.connect(self._on_community_changed)
		row.addWidget(self._community, stretch=1)
		refresh = PushButton(FluentIcon.SYNC, "Обновить", self)
		refresh.setToolTip("Перечитать ленту сообщества из Telegram с самых новых постов")
		refresh.clicked.connect(self.reload)
		row.addWidget(refresh)
		layout.addLayout(row)

	def _build_footer(self, layout: QVBoxLayout) -> None:
		"""Итог по прочитанному и кнопка дочитывания следующей страницы."""
		row = QHBoxLayout()
		self._summary = BodyLabel("", self)
		self._summary.setWordWrap(True)
		row.addWidget(self._summary, stretch=1)
		self._more = PushButton("Показать ещё", self)
		self._more.setToolTip("Прочитать следующую страницу ленты (один запрос к Telegram)")
		self._more.clicked.connect(self._load_more)
		row.addWidget(self._more)
		layout.addLayout(row)

	# --- жизнь экрана -----------------------------------------------------------

	def activate(self) -> None:
		"""Экран показан: обновить список сообществ и ленту, если она устарела."""
		self._community.reload(self._show_error)
		if self._community_id is not None and not self._fresh():
			self.reload()

	def reload(self) -> None:
		"""Перечитывает ленту с самых новых постов (кнопка «Обновить»)."""
		community = self._community.current()
		if community is None:
			return
		self._items = []
		self._next_offset = None
		self._read(community, offset_id=0)

	def _fresh(self) -> bool:
		"""Свежая ли лента (моложе :data:`FRESH_FOR_S`)."""
		return self._loaded_at is not None and monotonic() - self._loaded_at < FRESH_FOR_S

	def _on_community_changed(self) -> None:
		"""Сменилось сообщество — читаем его ленту с начала."""
		community = self._community.current()
		community_id = community.id if community is not None else None
		if community_id == self._community_id:
			return
		self._community_id = community_id
		self._items = []
		self._next_offset = None
		self._loaded_at = None
		self._render()
		if community is not None:
			self._read(community, offset_id=0)

	def _load_more(self) -> None:
		"""Дочитывает следующую страницу ленты (один запрос)."""
		community = self._community.current()
		if community is not None and self._next_offset is not None:
			self._read(community, offset_id=self._next_offset)

	# --- чтение -------------------------------------------------------------------

	def _read(self, community: CommunityDto, offset_id: int) -> None:
		"""Читает страницу ленты этого сообщества.

		Второй запрос **того же** сообщества поверх первого не идёт:
		двойное нажатие «Показать ещё» не должно удваивать обращения
		к Telegram. А вот смена сообщества читать обязана: её ответ
		человек ждёт на экране, и прежнее чтение ему больше не нужно —
		его ответ отбросит проверка актуальности.
		"""
		if self._loading_for == community.id:
			return
		self._loading_for = community.id
		self._status.setText("Читаю ленту из Telegram…")
		self._more.setEnabled(False)
		run_in_engine(
			self._worker,
			self._worker.engine.posts.list_published(community.id, offset_id),
			self,
			partial(self._on_page, community.id),
			partial(self._on_failed, community.id),
		)

	def _on_page(self, community_id: int, page: PublishedList) -> None:
		"""Страница прочитана: дописываем её в ленту (если сообщество то же)."""
		self._finish_read(community_id)
		if self._community.is_stale(community_id):
			return
		# альбом может лечь на границу страниц: хвост дочитан сейчас,
		# голова прочитана раньше — сводим их в одну карточку
		known = {message_id for item in self._items for message_id in item.message_ids}
		self._items.extend(item for item in page.items if item.message_id not in known)
		self._items = group_albums(self._items)
		self._next_offset = page.next_offset_id
		self._loaded_at = monotonic()
		self._status.setText("")
		self._render()

	def _on_failed(self, community_id: int, message: str) -> None:
		"""Чтение не удалось: причина остаётся на экране, лента — как была.

		Ошибка пишется строкой, а не всплывающей плашкой: человек
		смотрит на пустой список и должен видеть, почему он пуст,
		даже когда плашка уже погасла.
		"""
		self._finish_read(community_id)
		if self._community.is_stale(community_id):
			return
		self._status.setText(f"Лента не прочитана: {message}")
		self._render()

	def _finish_read(self, community_id: int) -> None:
		"""Снимает признак чтения — только если вернулось то самое чтение.

		Ответ брошенного сообщества приходит после того, как чтение
		нового уже началось: снять признак по нему значило бы решить,
		что новое чтение кончилось, и оставить экран с надписью
		«Читаю ленту…» навсегда.
		"""
		if self._loading_for == community_id:
			self._loading_for = None

	# --- показ ---------------------------------------------------------------------

	def _render(self) -> None:
		"""Перерисовывает карточки, итог и доступность дочитывания."""
		self._list.sync(self._items)
		self._summary.setText(feed_summary(len(self._items), self._next_offset is not None))
		self._more.setEnabled(self._loading_for is None and self._next_offset is not None)

	def _fill_editor(
		self, item: PublishedPostDto, body: QVBoxLayout, collapse: Callable[[], None]
	) -> None:
		"""Наполняет раскрытую карточку формой правки поста."""
		mount_published_editor(self._worker, self, item, body, collapse, self.reload)

	def _actions(self, item: PublishedPostDto, parent: QWidget) -> list[QWidget]:
		"""Кнопки карточки: открыть пост в Telegram и удалить его.

		Ссылка строится по @имени сообщества, у приватного её нет —
		и кнопки тогда нет: неработающая кнопка хуже её отсутствия.
		"""
		widgets: list[QWidget] = []
		if item.link:
			open_button = PushButton("Открыть", parent)
			open_button.setToolTip("Открыть пост в Telegram")
			open_button.clicked.connect(bind(open_link, item.link))
			widgets.append(open_button)
		delete = PushButton("Удалить", parent)
		delete.setToolTip("Удалить пост из сообщества — необратимо")
		delete.clicked.connect(bind(self._delete, item))
		widgets.append(delete)
		return widgets

	def _delete(self, item: PublishedPostDto) -> None:
		"""Удаляет пост из сообщества — с подтверждением, действие необратимо.

		У альбома удаляются **все** его записи: половина альбома в ленте
		хуже, чем его отсутствие, — и человек узнаёт об этом из вопроса,
		а не по остатку из семи файлов.
		"""
		album = (
			f" Это альбом: удалятся все {item.album_size} "
			f"{plural(item.album_size, 'запись', 'записи', 'записей')} поста."
			if item.is_album
			else ""
		)
		if not confirm_delete(
			self,
			f"Удалить пост «{item.text_preview}» из «{item.community_title}»? "
			f"Он исчезнет у всех читателей, вернуть его нельзя.{album}",
		):
			return
		run_in_engine(
			self._worker,
			self._worker.engine.posts.delete_published(
				PublishedRef(item.community_id, item.message_id), item.message_ids
			),
			self,
			lambda *_a: self.reload(),
			self._on_delete_failed,
		)

	def _on_delete_failed(self, message: str) -> None:
		"""Отказ удаления: причина на экране и перечитанная лента.

		Поста могло уже не быть (удалён с телефона) — тогда карточка
		обязана исчезнуть, а не остаться с кнопками в никуда.
		"""
		self._show_error(message)
		self.reload()
