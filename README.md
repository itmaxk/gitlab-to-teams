# GitLab → Teams

Сервис уведомлений: периодически опрашивает GitLab API на предмет новых MR
и отправляет уведомления в Microsoft Teams и/или на email,
если изменённые файлы соответствуют настроенным правилам.

Не требует прямого сетевого доступа от GitLab к сервису (нет webhook) —
работает через polling GitLab API.

## Возможности

- Периодический опрос GitLab API (polling) — не нужен webhook
- Проверка изменённых файлов по glob-паттерну (например `changelogs/unreleased/*.md`)
- Проверка содержимого файла (contains / regex / exact)
- Проверка ссылок на файлы: если в changelog указан `fileName.sql`,
  можно проверить наличие `database/postgres/migration/fileName.sql` в MR
- Настройка целевой ветки, статуса MR, интервала опроса для каждого правила
- Отправка в Microsoft Teams через Incoming Webhook (Adaptive Card)
- Дублирование на email (SMTP)
- Web-интерфейс для управления правилами (создание, редактирование, удаление, вкл/выкл)
- История уведомлений с фильтрацией (по правилу, статусу отправки, ошибкам)
- API с автодокументацией (Swagger)

## Быстрый старт

### 1. Установка зависимостей

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt    # Windows
# .venv/bin/pip install -r requirements.txt                    # Linux/Mac
```

### 2. Настройка

Скопировать `.env.example` в `.env` и заполнить:

```bash
copy .env.example .env
```

```env
# GitLab
GITLAB_TOKEN="your-gitlab-personal-access-token"
GITLAB_URL="https://gitlabru.domain.com"
GITLAB_PROJECT="group/project-name"

# Microsoft Teams
TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/..."

# Интервал опроса по умолчанию (секунды), переопределяется в правиле
POLL_INTERVAL_SECONDS=300

# SMTP (опционально, для email-уведомлений)
SMTP_HOST=""
SMTP_PORT=587
SMTP_USER=""
SMTP_PASSWORD=""
SMTP_FROM=""

# Сервер
HOST=0.0.0.0
PORT=8055
```

### 3. Запуск

```bash
.venv\Scripts\python.exe main.py    # Windows
# .venv/bin/python main.py          # Linux/Mac
```

Сервер запустится и выведет ссылки в консоль:
```
  Web-интерфейс: http://localhost:8055/rules
  Дашборд:       http://localhost:8055/
  Swagger API:   http://localhost:8055/docs
```

Polling GitLab API стартует автоматически в фоне.

## Интерфейсы

| URL | Описание |
|-----|----------|
| `http://localhost:8055/` | Дашборд — история уведомлений с фильтрами |
| `http://localhost:8055/rules` | Управление правилами |
| `http://localhost:8055/rules/new` | Создание нового правила |
| `http://localhost:8055/docs` | Swagger — документация API |

## Как работает polling

1. При запуске сервис определяет ID проекта GitLab по `GITLAB_PROJECT`
2. Для каждого включённого правила запускается фоновый цикл опроса
3. На каждой итерации:
   - Запрашиваются последние MR с указанными `target_branch` и `mr_state`
   - Пропускаются уже обработанные MR (хранятся в таблице `processed_mrs`)
   - Для новых MR загружаются изменённые файлы
   - Файлы проверяются по правилам
   - При совпадении — отправляется уведомление
4. Интервал опроса берётся из правила, или из `POLL_INTERVAL_SECONDS` если не задан

## Настройки правила

| Поле | Описание | По умолчанию |
|------|----------|------------|
| Целевая ветка | Ветка MR (target_branch) | `master` |
| Статус MR | `merged` / `opened` / `closed` / `all` | `merged` |
| Интервал опроса | Секунды (0 = из .env) | 0 → 300 |
| Паттерн файла | Glob-паттерн пути файла | `changelogs/unreleased/*.md` |
| Условие | Что искать в содержимом файла | `type: breaking` |
| Тип совпадения | `contains` / `regex` / `exact` | `contains` |
| Проверка файлов | Проверить наличие файлов из changelog в MR | Выкл |
| Префикс пути | Путь для поиска ссылочных файлов | — |
| Teams Webhook URL | Override (пусто = из .env) | — |
| Email | Дублирование на email | Выкл |

### Типы совпадения (`match_type`)

| Тип | Описание | Пример |
|-----|----------|--------|
| `contains` | Подстрока содержится в файле | `type: breaking` |
| `regex` | Регулярное выражение | `type:\s*(breaking\|security)` |
| `exact` | Точное совпадение (с trim) | Полный текст файла |

### Проверка ссылок на файлы

Если включена опция "Проверять наличие файлов из changelog в MR"
и указан префикс пути (например `database/postgres/migration`):

1. Из содержимого changelog извлекаются имена файлов (например `migrate_001.sql`)
2. Проверяется наличие `database/postgres/migration/migrate_001.sql` в изменениях MR
3. Если файл не найден — правило не срабатывает

### Пример правила

- **Название:** Breaking Changes
- **Целевая ветка:** master
- **Статус MR:** merged
- **Паттерн файла:** `changelogs/unreleased/*.md`
- **Условие:** `type: breaking`
- **Тип:** contains

## API

Все эндпоинты доступны в Swagger: `http://localhost:8055/docs`

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/rules` | Список правил |
| POST | `/api/rules` | Создание правила |
| GET | `/api/rules/{id}` | Получение правила |
| PUT | `/api/rules/{id}` | Обновление правила |
| DELETE | `/api/rules/{id}` | Удаление правила |
| PATCH | `/api/rules/{id}/toggle` | Вкл/выкл правила |
| POST | `/api/rules/{id}/test` | Тестовая отправка в Teams |
| GET | `/api/rules/logs/recent` | Лог уведомлений (с фильтрами) |

### Фильтры для логов

`GET /api/rules/logs/recent?rule_id=1&teams_sent=1&email_sent=0&limit=50`

| Параметр | Описание |
|----------|----------|
| `rule_id` | Фильтр по ID правила |
| `teams_sent` | 1 = отправлено, 0 = нет |
| `email_sent` | 1 = отправлено, 0 = нет |
| `limit` | Количество записей (по умолчанию 100) |

## Хранение данных

SQLite база `data.db` создаётся автоматически при первом запуске.
При первом запуске также создаётся дефолтное правило "Breaking Changes".

Таблицы:
- `notification_rules` — правила уведомлений
- `email_recipients` — email-получатели (привязаны к правилам)
- `notification_log` — история отправленных уведомлений
- `processed_mrs` — обработанные MR (для предотвращения повторной обработки)

## Требования

- Python 3.11+
- Доступ к GitLab API (Personal Access Token с правами `read_api`)
- Microsoft Teams Incoming Webhook URL
- SMTP-сервер (опционально, для email)
