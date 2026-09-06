"""Переименование сущности: channels → communities (этап 0 ADR-0021).

Сообщество — подключённый канал или группа Telegram (пока только
каналы; вид `kind` появится следующим этапом). Здесь — только имена:
таблицы ``channels`` → ``communities`` и ``channel_settings`` →
``community_settings``, колонки ``channel_id`` → ``community_id``
(настройки сообщества и очередь отправки), хранимое имя ключа
app-настройки ``publish_last_channel_id`` →
``publish_last_community_id``. Колонки ``channel_id`` таблиц подписей
(``caption_fields``, ``caption_templates``) переименовываются той же
миграцией. Поведение не меняется.

SQLite (с версии 3.25) переименовывает таблицы и колонки на месте
(``ALTER TABLE … RENAME``), ссылки внешних ключей в зависимых таблицах
обновляются автоматически — пересборка таблиц (паттерн ``copy_from``
из c1a4b83f7e29) здесь не нужна.

Revision ID: a9d4c37e8b15
Revises: e6b9d43a7f21
Create Date: 2026-09-06
"""

from __future__ import annotations

from alembic import op

revision = "a9d4c37e8b15"
down_revision = "e6b9d43a7f21"
branch_labels = None
depends_on = None


def upgrade() -> None:
	op.rename_table("channels", "communities")
	op.rename_table("channel_settings", "community_settings")
	# RENAME COLUMN — native SQLite: первичный ключ и внешний ключ
	# колонки переопределяются самим SQLite, без пересборки таблицы
	op.execute("ALTER TABLE community_settings RENAME COLUMN channel_id TO community_id")
	op.execute("ALTER TABLE publish_queue_items RENAME COLUMN channel_id TO community_id")
	op.execute("ALTER TABLE caption_fields RENAME COLUMN channel_id TO community_id")
	op.execute("ALTER TABLE caption_templates RENAME COLUMN channel_id TO community_id")
	# имя ключа настройки хранится строкой (ADR-0013) — переносим данные
	op.execute(
		"UPDATE app_settings SET name = 'publish_last_community_id' "
		"WHERE name = 'publish_last_channel_id'"
	)


def downgrade() -> None:
	op.execute(
		"UPDATE app_settings SET name = 'publish_last_channel_id' "
		"WHERE name = 'publish_last_community_id'"
	)
	op.execute("ALTER TABLE caption_templates RENAME COLUMN community_id TO channel_id")
	op.execute("ALTER TABLE caption_fields RENAME COLUMN community_id TO channel_id")
	op.execute("ALTER TABLE publish_queue_items RENAME COLUMN community_id TO channel_id")
	op.execute("ALTER TABLE community_settings RENAME COLUMN community_id TO channel_id")
	op.rename_table("community_settings", "channel_settings")
	op.rename_table("communities", "channels")
