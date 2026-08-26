"""Сервис постов: fire-and-forget, источник истины — сам канал (ADR-0010).

Публикация любого контента — единой сущностью ``PostDraft``. Транспорт
выбирается по возможностям канала: userbot в приоритете (ADR-0011),
бот — запасной путь. «Сейчас» — обычная отправка, отложенно — запись
прямо в канале (её хранит и публикует сервер Telegram). Локальной
таблицы постов нет; страница «Расписание» читает отложенные из Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pxcontrol.engine.db.database import Database
from pxcontrol.engine.db.models import Channel
from pxcontrol.engine.errors import EngineError
from pxcontrol.engine.services.settings import (
	CHANNEL_ENABLED,
	VIDEO_PROCESSED_DIR,
	VIDEO_PUBLISHED_DIR,
	VIDEO_QUEUED_DIR,
	SettingKey,
	SettingsService,
)
from pxcontrol.engine.services.video import video_base_dir
from pxcontrol.engine.telegram.mtproto import (
	UserbotNotConnectedError,
	UserbotUnavailableError,
)

# лимит Bot API живёт в telegram/types.py; здесь — явный реэкспорт
# (интерфейс исторически берёт его из сервиса постов)
from pxcontrol.engine.telegram.types import (
	BOT_MAX_FILE_BYTES as BOT_MAX_FILE_BYTES,
)
from pxcontrol.engine.telegram.types import (
	MediaKind,
	OutgoingPost,
	ScheduledMessage,
	userbot_max_file_bytes,
)
from pxcontrol.engine.video.ffmpeg import FfmpegSource, ffmpeg_source, run_tool
from pxcontrol.engine.video.frames import resolve_timestamp
from pxcontrol.engine.video.probe import ffprobe_bin_for, probe_video

logger = logging.getLogger(__name__)

#: Минимальный запас до времени публикации (Telegram не берёт «почти сейчас»).
MIN_SCHEDULE_AHEAD = timedelta(seconds=60)


#: Миниатюра видео для Telegram: вписывается в квадрат, JPEG-качество ffmpeg.
_THUMB_BOX_PX = 320
_THUMB_JPEG_QUALITY = "4"

#: Длина превью текста отложенной записи на странице «Расписание».
_SCHEDULED_PREVIEW_CHARS = 80

#: Колбэк прогресса загрузки: доля 0.0..1.0.
ProgressCallback = Callable[[float], None]


@dataclass(frozen=True)
class PublishCapabilities:
	"""Возможности публикации канала (из способов администрирования).

	Attributes:
		userbot: полный набор — любые типы, до 2 ГБ, «сейчас» и отложенные.
		bot: запасной путь — текст и медиа до 50 МБ, только «сейчас».
	"""

	userbot: bool
	bot: bool


def publish_capabilities(bot_assigned: bool, userbot_admin: bool) -> PublishCapabilities:
	"""Возможности публикации по способам администрирования канала.

	Единственный источник правды для движка и интерфейса; приоритет
	транспорта — MTProto (ADR-0011).
	"""
	return PublishCapabilities(userbot=userbot_admin, bot=bot_assigned)


class PostError(EngineError):
	"""Ошибка создания/отправки поста (с понятным человеку текстом)."""


def text_preview(text: str, limit: int) -> str:
	"""Обрезает текст до ``limit`` символов, длинный — с «…» на конце.

	Общий помощник коротких заголовков/превью (очередь отправки,
	список отложенных): лимит зависит от места показа.
	"""
	if len(text) <= limit:
		return text
	return f"{text[: limit - 1]}…"


@dataclass(frozen=True)
class PostDraft:
	"""Черновик публикации — единая сущность для всех типов контента.

	Attributes:
		channel_id: подключённый канал (id в нашей БД).
		text: текст поста или подпись к медиа.
		media_path: путь к файлу вложения (None — чистый текст).
		media_kind: тип вложения.
		when: момент публикации (None — «сейчас»).
		rename_to: новое имя файла (без пути) перед отправкой; вместе
			с файлом переименовывается его кадр-превью (сосед ``.png``).
	"""

	channel_id: int
	text: str = ""
	media_path: str | None = None
	media_kind: MediaKind = MediaKind.NONE
	when: datetime | None = None
	rename_to: str | None = None


def _free_name(target: Path) -> Path:
	"""Свободное имя рядом с занятым: «имя (2).mp4», «имя (3).mp4»…"""
	if not target.exists():
		return target
	counter = 2
	while True:
		candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
		if not candidate.exists():
			return candidate
		counter += 1


def refresh_draft_media(draft: PostDraft) -> PostDraft:
	"""Возвращает черновик с учётом уже выполненного переименования файла.

	Нужен при повторной отправке после ошибки: переименование выполняется
	до загрузки, поэтому неудачная попытка могла оставить файл уже под
	новым именем. Если исходного пути больше нет, а файл с именем
	``rename_to`` в той же папке есть — черновик указывает на него,
	и повторное переименование снимается.
	"""
	if draft.media_path is None or not draft.rename_to:
		return draft
	source = Path(draft.media_path)
	if source.is_file():
		return draft
	target = source.with_name(draft.rename_to)
	if target.is_file():
		return replace(draft, media_path=str(target), rename_to=None)
	return draft


class _PostPort(Protocol):
	"""Часть шлюза Telegram, нужная сервису (для подмены в тестах)."""

	def userbot_premium(self) -> bool: ...

	async def send_text(self, token: str, chat_id: str, text: str) -> int: ...

	async def publish(
		self,
		chat_id: str,
		post: OutgoingPost,
		on_progress: ProgressCallback | None,
	) -> None: ...

	async def send_media(
		self, token: str, chat_id: str, kind: MediaKind, path: str, caption: str
	) -> int: ...

	async def get_scheduled(self, chat_id: str) -> list[ScheduledMessage]: ...


@dataclass(frozen=True)
class ScheduledPostDto:
	"""Отложенная запись канала (прочитана из Telegram) для интерфейса.

	``channel_id`` — id канала в нашей БД: по нему интерфейс фильтрует
	список по каналам (название для этого не годится — не уникально).
	"""

	channel_id: int
	channel_title: str
	text_preview: str
	scheduled_at: datetime


class PostsService:
	"""Публикация постов: userbot в приоритете, бот — запасной путь."""

	def __init__(
		self,
		db: Database,
		gateway: _PostPort,
		ffmpeg_path: FfmpegSource = "ffmpeg",
		settings: SettingsService | None = None,
	) -> None:
		"""``settings`` — общий сервис настроек движка; None — свой
		экземпляр поверх той же БД (для тестов это эквивалентно:
		настройки каналов не кэшируются)."""
		self._db = db
		self._gateway = gateway
		self._ffmpeg = ffmpeg_source(ffmpeg_path)  # провайдер пути (настройки)
		self._settings = settings if settings is not None else SettingsService(db)

	async def publish(self, draft: PostDraft, on_progress: ProgressCallback | None = None) -> None:
		"""Публикует черновик: userbot в приоритете, бот — запасной путь.

		Единый вход для всех типов контента. Транспорт выбирается по
		возможностям канала (:func:`publish_capabilities`): userbot —
		полный набор; только бот — текст и медиа до 50 МБ, «сейчас».
		``on_progress`` получает долю загрузки файла 0.0..1.0
		(бот-путь прогресс не отдаёт).

		Raises:
			PostError: Черновик/канал/файл не годятся, канал выключен
				или у канала нет способа публикации.
			UserbotUnavailableError: Userbot отвалился по дороге.
		"""
		self.validate_draft(draft)
		channel = await self._get_channel(draft.channel_id)
		if not await self._settings.get_for(CHANNEL_ENABLED, draft.channel_id):
			# правило системы, не интерфейса: любой будущий вход в публикацию
			# (автопостинг из источников) не должен писать в выключенный канал
			raise PostError(
				f"Канал «{channel.title}» выключен — включите его на странице «Каналы»."
			)
		caps = publish_capabilities(channel.bot is not None, channel.userbot_admin)
		self._check_transport(caps, draft)
		# переименование — после всех проверок, способных отклонить черновик:
		# отклонённая публикация не должна оставлять файл переименованным
		media_path = draft.media_path
		if media_path is not None and draft.rename_to:
			media_path = self._apply_rename(media_path, draft.rename_to)
		if caps.userbot:
			await self._publish_userbot(channel, draft, media_path, on_progress)
		else:
			await self._publish_bot(channel, draft, media_path)
		if draft.media_kind is MediaKind.VIDEO and media_path is not None:
			await self._move_to_published(media_path)
		logger.info(
			"Пост (%s) → «%s» (%s, %s).",
			draft.media_kind if draft.media_path else "текст",
			channel.title,
			"userbot" if caps.userbot else "бот",
			f"отложено на {draft.when}" if draft.when else "опубликовано",
		)

	def _check_transport(self, caps: PublishCapabilities, draft: PostDraft) -> None:
		"""Проверки транспорта, способные отклонить черновик.

		Выполняются до побочных эффектов публикации (переименование файла):
		отклонённый черновик не должен менять ничего на диске.

		Raises:
			PostError: Нет способа публикации, отложенный пост через бота
				или файл больше лимита выбранного транспорта.
		"""
		if not caps.userbot and not caps.bot:
			raise PostError(
				"У канала нет способа публикации — проверьте доступы на странице «Каналы»."
			)
		media_path = draft.media_path
		if caps.userbot:
			limit = userbot_max_file_bytes(self._gateway.userbot_premium())
			if media_path is not None and self._file_size(media_path) > limit:
				raise PostError(
					f"Файл больше {limit // 10**9} ГБ — лимит Telegram на файл "
					"для этого аккаунта. Уменьшите файл (например, битрейтом "
					"на странице «Видео»)."
				)
			return
		if draft.when is not None:
			raise PostError(
				"Отложенные посты требуют userbot-админа в канале — "
				"через бота доступно только «сейчас»."
			)
		if media_path is not None and self._file_size(media_path) > BOT_MAX_FILE_BYTES:
			raise PostError(
				f"Файл больше {BOT_MAX_FILE_BYTES // 2**20} МБ — лимит "
				"отправки ботом. Добавьте userbot администратором канала "
				"или уменьшите файл."
			)

	@staticmethod
	def _file_size(media_path: str) -> int:
		"""Размер файла для проверки лимитов транспорта.

		Raises:
			PostError: Файл исчез или недоступен (сетевой диск, права) —
				доменный текст вместо сырой «внутренней ошибки» в очереди.
		"""
		try:
			return Path(media_path).stat().st_size
		except OSError as exc:
			raise PostError(f"Файл недоступен: {exc.strerror or exc} — {media_path}") from exc

	async def _publish_userbot(
		self,
		channel: Channel,
		draft: PostDraft,
		media_path: str | None,
		on_progress: ProgressCallback | None,
	) -> None:
		"""Полный путь через userbot (MTProto): всё, включая отложенные.

		Лимит размера файла проверен раньше (:meth:`_check_transport`).
		"""
		with tempfile.TemporaryDirectory() as tmp:
			thumb: str | None = None
			if draft.media_kind is MediaKind.VIDEO and media_path:
				thumb = await asyncio.to_thread(self._video_thumbnail, media_path, tmp)
			post = OutgoingPost(
				text=draft.text,
				media_path=media_path,
				media_kind=draft.media_kind,
				when=draft.when,
				thumb_path=thumb,
			)
			await self._gateway.publish(channel.tg_chat_id, post, on_progress)

	async def _publish_bot(
		self, channel: Channel, draft: PostDraft, media_path: str | None
	) -> None:
		"""Запасной путь через бота: текст и медиа до 50 МБ, только «сейчас».

		Отложенность и лимит размера проверены раньше
		(:meth:`_check_transport`).
		"""
		if channel.bot is None:  # publish() сюда без бота не приводит
			raise PostError("У канала не назначен бот — переподключите канал.")
		if media_path is None:
			await self._gateway.send_text(channel.bot.token, channel.tg_chat_id, draft.text)
			return
		await self._gateway.send_media(
			channel.bot.token,
			channel.tg_chat_id,
			draft.media_kind,
			media_path,
			draft.text,
		)

	async def _move_to_published(self, media_path: str) -> None:
		"""Переносит опубликованное видео из результатов в опубликованные.

		Правило — «зеркалим относительный путь»: файл из
		``<результаты>/<подпапка>/…`` переезжает в
		``<опубликованные>/<подпапка>/…`` (вместе с соседом-превью ``.png``).
		Файл вне папки результатов не трогается. Перенос вспомогательный:
		любой сбой — предупреждение в лог, публикацию не роняет (пост уже
		ушёл; у отложенных файл уже загружен на сервер Telegram).
		"""
		source = Path(media_path)
		# основной путь — из папки очереди (ADR-0016: постановка перенесла
		# файл туда); прямой вызов publish() минуя очередь — из результатов
		rel = self._relative_to_root(source, VIDEO_QUEUED_DIR) or self._relative_to_root(
			source, VIDEO_PROCESSED_DIR
		)
		if rel is None:
			return  # видео не из наших папок — оставляем на месте
		target = video_base_dir(self._settings, VIDEO_PUBLISHED_DIR) / rel
		try:
			# перенос между дисками — это копирование гигабайтов: в отдельном
			# потоке, чтобы не останавливать цикл событий движка
			await asyncio.to_thread(self._move_with_preview, source, target)
			logger.info("Опубликованное видео перенесено: %s → %s", source, target)
		except OSError:
			logger.warning(
				"Не удалось перенести опубликованное видео %s — файл остался в папке результатов.",
				media_path,
				exc_info=True,
			)

	@staticmethod
	def _move_with_preview(source: Path, target: Path) -> None:
		"""Блокирующий перенос файла с соседом-превью ``.png`` (в потоке)."""
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.move(str(source), str(target))
		preview = source.with_suffix(".png")
		if preview.is_file():
			shutil.move(str(preview), str(target.with_suffix(".png")))

	def _relative_to_root(self, path: Path, key: SettingKey[str]) -> Path | None:
		"""Путь относительно корня папки видео; None — файл вне корня."""
		root = video_base_dir(self._settings, key)
		try:
			return path.resolve().relative_to(root.resolve())
		except ValueError:
			return None

	async def stash_for_queue(self, media_path: str) -> str:
		"""Переносит файл результата в папку очереди отправки (ADR-0016).

		Зеркалит относительный путь (``queued/<подпапка>/<файл>``), вместе
		с файлом переезжает кадр-превью. Файл ждущего поста уходит
		из «Готовых видео» — его нельзя случайно удалить или поставить
		повторно. Файл вне папки результатов (произвольное вложение
		с диска) не трогается.

		Returns:
			Путь файла в папке очереди (или исходный, если файл не наш).

		Raises:
			PostError: В папке очереди уже есть файл с таким относительным
				именем или перенос не удался (права, диск).
		"""
		source = Path(media_path)
		rel = self._relative_to_root(source, VIDEO_PROCESSED_DIR)
		if rel is None:
			return media_path
		target = video_base_dir(self._settings, VIDEO_QUEUED_DIR) / rel
		if target.exists():
			raise PostError(
				f"В папке очереди уже есть файл «{rel}» — переименуйте "
				"результат или дождитесь отправки тёзки."
			)
		try:
			await asyncio.to_thread(self._move_with_preview, source, target)
		except OSError as exc:
			raise PostError(
				f"Не удалось перенести файл в папку очереди: {exc.strerror or exc}"
			) from exc
		return str(target)

	async def unstash_from_queue(self, media_path: str) -> str:
		"""Возвращает файл из папки очереди в результаты (ADR-0016).

		Вызывается при отмене или снятии элемента без отправки: файл снова
		«готовый». Коллизия имён (результат с тем же именем появился
		заново) решается суффиксом « (2)» — возврат не должен падать;
		сбой переноса не роняет операцию (файл остаётся в папке очереди,
		предупреждение в лог). Файл вне папки очереди не трогается.

		Returns:
			Путь файла в папке результатов (или исходный).
		"""
		source = Path(media_path)
		rel = self._relative_to_root(source, VIDEO_QUEUED_DIR)
		if rel is None:
			return media_path
		target = _free_name(video_base_dir(self._settings, VIDEO_PROCESSED_DIR) / rel)
		try:
			await asyncio.to_thread(self._move_with_preview, source, target)
		except OSError:
			logger.warning(
				"Не удалось вернуть файл %s из папки очереди — он остался там.",
				media_path,
				exc_info=True,
			)
			return media_path
		return str(target)

	@staticmethod
	def _apply_rename(media_path: str, rename_to: str) -> str:
		"""Переименовывает файл (и его кадр-превью) перед отправкой.

		Returns:
			Путь к файлу под новым именем (папка не меняется).

		Raises:
			PostError: Имя содержит путь, целевое имя занято или
				переименовать не удалось (права, диск).
		"""
		PostsService.check_rename_name(rename_to)
		source = Path(media_path)
		try:
			target = source.with_name(rename_to)
		except ValueError as exc:  # страховка: Path строже наших проверок
			raise PostError(f"Имя «{rename_to}» не годится для файла.") from exc
		if target == source:
			return str(source)
		if target.exists():
			raise PostError(f"Файл «{rename_to}» уже существует — смените имя.")
		try:
			source.rename(target)
		except OSError as exc:
			raise PostError(
				f"Не удалось переименовать файл в «{rename_to}»: {exc.strerror or exc}"
			) from exc
		preview = source.with_suffix(".png")
		try:
			if preview.is_file():
				preview.rename(target.with_suffix(".png"))
		except OSError as exc:
			# пара «файл + превью» переименовывается атомарно: без отката
			# превью осталось бы под старым стемом и потерялось бы при
			# переносе в «опубликованные» (поиск соседа идёт по новому)
			with suppress(OSError):
				target.rename(source)
			raise PostError(
				f"Не удалось переименовать превью файла «{rename_to}» — "
				f"переименование отменено: {exc.strerror or exc}"
			) from exc
		logger.info("Файл переименован: %s → %s", source.name, target.name)
		return str(target)

	def _video_thumbnail(self, video_path: str, tmp_dir: str) -> str | None:
		"""Готовит JPEG-миниатюру видео для Telegram (вписана в 320×320).

		Источник: кадр-превью конвейера (сосед видео с расширением .png),
		а без него — случайный кадр из середины видео. Миниатюра —
		вспомогательная: любой сбой не мешает публикации (None + лог).
		"""
		thumb = str(Path(tmp_dir) / "thumb.jpg")
		preview = Path(video_path).with_suffix(".png")
		try:
			if preview.is_file():
				_make_thumbnail(str(preview), thumb, self._ffmpeg())
			else:
				info = probe_video(video_path, ffprobe_bin_for(self._ffmpeg()))
				timestamp = resolve_timestamp("random-middle", info)
				_make_thumbnail(video_path, thumb, self._ffmpeg(), timestamp)
		except (OSError, RuntimeError, ValueError):
			logger.warning(
				"Миниатюра для %s не получилась — публикуем без неё.",
				video_path,
				exc_info=True,
			)
			return None
		return thumb

	async def userbot_limit_gb(self) -> int:
		"""Лимит userbot на файл в целых ГБ — для подсказок интерфейса."""
		return userbot_max_file_bytes(self._gateway.userbot_premium()) // 10**9

	async def userbot_limit_bytes(self) -> int:
		"""Точный лимит userbot на файл в байтах (2000/4000 МиБ по Premium).

		Для пометки «больше лимита канала» в пакете отправки (ADR-0015):
		округление до целых ГБ здесь дало бы ложные пометки у файлов
		между 2 ГБ и фактическими 2000 МиБ.
		"""
		return userbot_max_file_bytes(self._gateway.userbot_premium())

	async def channel_title(self, channel_id: int) -> str:
		"""Название канала (для заголовков элементов очереди отправки).

		Raises:
			PostError: Канал не найден.
		"""
		return (await self._get_channel(channel_id)).title

	@staticmethod
	def check_rename_name(rename_to: str) -> None:
		"""Отклоняет негодное имя для «переименовать при отправке».

		Raises:
			PostError: Имя содержит путь или служебное («.», «..»).
		"""
		if "/" in rename_to or "\\" in rename_to:
			raise PostError("Новое имя файла не должно содержать путь.")
		if rename_to in (".", ".."):
			raise PostError("Укажите настоящее имя файла («.» и «..» — служебные).")

	@staticmethod
	def validate_draft(draft: PostDraft) -> None:
		"""Отклоняет пустой черновик, битый путь, негодное имя переименования
		и время «почти сейчас».

		Публичная: очередь отправки проверяет черновик при постановке,
		чтобы ошибка всплыла сразу, а не при отправке.

		Raises:
			PostError: Черновик не готов к отправке.
		"""
		if not draft.text and draft.media_path is None:
			raise PostError("Пост пуст — добавьте текст или файл.")
		if draft.rename_to:
			PostsService.check_rename_name(draft.rename_to)
		if draft.media_path is not None and draft.media_kind is MediaKind.NONE:
			raise PostError("У вложения не указан тип контента.")
		if draft.media_path is not None and not Path(draft.media_path).is_file():
			raise PostError(f"Файл не найден: {draft.media_path}")
		when = draft.when
		if when is not None and when.astimezone(UTC) - datetime.now(UTC) < MIN_SCHEDULE_AHEAD:
			raise PostError("Время публикации должно быть хотя бы на минуту в будущем.")

	async def list_scheduled(self) -> list[ScheduledPostDto]:
		"""Собирает отложенные записи активных userbot-каналов из Telegram.

		Опрашиваются только каналы с userbot-админом: у бот-канала
		отложенных быть не может (Bot API их не умеет, ADR-0010/0011).
		Выключенные каналы (настройка ``enabled`` = False) не опрашиваются.
		Ошибка одного канала не роняет весь список — канал пропускается
		с предупреждением в логе.

		Raises:
			UserbotNotConnectedError: Userbot не подключён вовсе — без него
				опрашивать нечего, ошибка общая для всех каналов.
		"""
		enabled = await self._settings.get_for_all(CHANNEL_ENABLED)
		async with self._db.session_factory() as session:
			channels = (
				(
					await session.execute(
						select(Channel).where(Channel.userbot_admin).order_by(Channel.id)
					)
				)
				.scalars()
				.all()
			)
		items: list[ScheduledPostDto] = []
		for channel in channels:
			if not enabled.get(channel.id, CHANNEL_ENABLED.default):
				continue
			try:
				messages = await self._gateway.get_scheduled(channel.tg_chat_id)
			except UserbotNotConnectedError:
				raise
			except UserbotUnavailableError as exc:
				logger.warning("Отложенные канала «%s» не прочитаны: %s", channel.title, exc)
				continue
			for message in messages:
				items.append(self._dto(channel, message))
		items.sort(key=lambda item: item.scheduled_at)
		return items

	async def scheduled_times(self, channel_id: int) -> list[datetime]:
		"""Моменты существующих отложек канала (для раскладки пакета).

		Пакетная отправка (ADR-0015) пропускает занятые слоты — сюда
		отдаются времена уже созданных в Telegram отложенных записей.
		Канал без userbot-админа отложек иметь не может — пустой список.

		Raises:
			PostError: Канал не найден.
			UserbotUnavailableError: Отложки прочитать не удалось —
				вызывающая сторона решает, продолжать ли без них.
		"""
		channel = await self._get_channel(channel_id)
		if not channel.userbot_admin:
			return []
		messages = await self._gateway.get_scheduled(channel.tg_chat_id)
		return [message.scheduled_at for message in messages]

	async def _get_channel(self, channel_id: int) -> Channel:
		"""Возвращает канал с ботом или объясняет, что канал не найден."""
		async with self._db.session_factory() as session:
			channel = (
				await session.execute(
					select(Channel)
					.options(selectinload(Channel.bot))
					.where(Channel.id == channel_id)
				)
			).scalar_one_or_none()
		if channel is None:
			raise PostError("Канал не найден — обновите список каналов.")
		return channel

	@staticmethod
	def _dto(channel: Channel, message: ScheduledMessage) -> ScheduledPostDto:
		"""Готовит запись для интерфейса: канал, короткий текст, время."""
		text = message.text or "(медиа без текста)"
		preview = text_preview(text, _SCHEDULED_PREVIEW_CHARS)
		return ScheduledPostDto(channel.id, channel.title, preview, message.scheduled_at)


def _make_thumbnail(
	source_path: str,
	output_jpg: str,
	ffmpeg_bin: str = "ffmpeg",
	timestamp: float = 0.0,
) -> None:
	"""Делает JPEG-миниатюру для Telegram: кадр, вписанный в 320×320.

	Пропорции кадра сохраняются (Telegram растягивает миниатюру до
	пропорций видео — квадратный кроп исказил бы картинку). Источник —
	картинка или видео (кадр берётся в момент ``timestamp``). Живёт
	на слое публикации: чистый модуль ``engine/video`` про Telegram
	не знает.

	Raises:
		RuntimeError: Если ffmpeg не смог сделать миниатюру.
	"""
	box = _THUMB_BOX_PX
	cmd = [
		ffmpeg_bin,
		"-y",
		"-ss",
		f"{timestamp:.3f}",
		"-i",
		source_path,
		"-frames:v",
		"1",
		"-vf",
		f"scale={box}:{box}:force_original_aspect_ratio=decrease",
		"-q:v",
		_THUMB_JPEG_QUALITY,
		output_jpg,
	]
	# один кадр — секунды; предел ловит зависший ffmpeg (недоступный диск)
	run_tool(cmd, "миниатюра видео", timeout=120.0)
