# Документация pXcontrol — оглавление

Это навигационный центр по всей документации проекта. Каждый документ
помечен статусом готовности.

**Условные обозначения статуса:**
🟡 черновик-заглушка (структура есть, содержание не наполнено) ·
🟢 готов · 🔴 устарел, требует пересмотра.

---

## 01. Обзор продукта
| Документ | Назначение | Статус |
|---|---|---|
| [vision.md](01-overview/vision.md) | Видение, цели, целевая аудитория | 🟢 |
| [use-cases.md](01-overview/use-cases.md) | Сценарии использования | 🟡 |
| [glossary.md](01-overview/glossary.md) | Глоссарий терминов | 🟡 |

## 02. Архитектура
| Документ | Назначение | Статус |
|---|---|---|
| [overview.md](02-architecture/overview.md) | Общая картина системы | 🟡 |
| [components.md](02-architecture/components.md) | Состав компонентов | 🟢 |
| [data-flow.md](02-architecture/data-flow.md) | Поток данных (контент → публикация) | 🟡 |
| [runtime.md](02-architecture/runtime.md) | Порядок запуска и остановки | 🟢 |
| [tech-stack.md](02-architecture/tech-stack.md) | Технологии и обоснование | 🟢 |
| [decisions/README.md](02-architecture/decisions/README.md) | Журнал решений (ADR) | 🟢 |

## 03. Модули
| Документ | Назначение | Статус |
|---|---|---|
| [README.md](03-modules/README.md) | Карта модулей | 🟡 |
| [channels.md](03-modules/channels.md) | Управление каналами | 🟢 |
| [telegram-gateway.md](03-modules/telegram-gateway.md) | Шлюз Telegram (Bot API + MTProto) | 🟢 |
| [scheduler.md](03-modules/scheduler.md) | Расписание: отложенные записи из Telegram | 🟢 |
| [video-processing.md](03-modules/video-processing.md) | Подготовка видео | 🟢 |
| [captions.md](03-modules/captions.md) | Подписи к постам (шаблоны) | 🟢 |
| [content-sources.md](03-modules/content-sources.md) | Источники и парсинг | 🟡 |
| [ai-generation.md](03-modules/ai-generation.md) | Генерация контента ИИ | 🟡 |
| [moderation-queue.md](03-modules/moderation-queue.md) | Очередь модерации | 🟡 |

## 04. Интерфейс движка
| Документ | Назначение | Статус |
|---|---|---|
| [engine-api.md](04-api/engine-api.md) | API движка (внутренний, Python) | 🟢 |
| [data-schemas.md](04-api/data-schemas.md) | Схемы данных (датаклассы границы) | 🟢 |

## 05. Данные
| Документ | Назначение | Статус |
|---|---|---|
| [data-model.md](05-data/data-model.md) | Сущности и модель хранения | 🟢 |

## 06. Конфигурация
| Документ | Назначение | Статус |
|---|---|---|
| [configuration.md](06-config/configuration.md) | Бутстрап `.env` + конфиг в БД, секреты | 🟢 |

## 07. Эксплуатация
| Документ | Назначение | Статус |
|---|---|---|
| [deployment.md](07-operations/deployment.md) | Развёртывание | 🟡 |
| [observability.md](07-operations/observability.md) | Логи, метрики, мониторинг | 🟡 |

## 08. Разработка
| Документ | Назначение | Статус |
|---|---|---|
| [setup.md](08-development/setup.md) | Настройка окружения разработчика | 🟡 |
| [testing.md](08-development/testing.md) | Подход к тестированию | 🟡 |
| [coding-standards.md](08-development/coding-standards.md) | Стандарты кода | 🟡 |

## 09. Планы
| Документ | Назначение | Статус |
|---|---|---|
| [roadmap.md](09-roadmap/roadmap.md) | Дорожная карта | 🟡 |
