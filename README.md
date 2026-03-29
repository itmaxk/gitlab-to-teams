# GitLab Manager

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
- Дублирование на email (SMTP) с fallback на `DEFAULT_EMAIL` из `.env`
- Копирование правил (создаётся выключенная копия со всеми настройками)
- Защита от повторной отправки (дедупликация по rule + MR + файл)
- Повторная ручная отправка уведомлений с дашборда
- Web-интерфейс для управления правилами (создание, редактирование, копирование, удаление, вкл/выкл)
- Журнал опрошенных MR с фильтрацией (статус, результат, совпадения, ветка)
- История уведомлений с фильтрацией (по правилу, статусу отправки, ошибкам)
- Cherry-Pick очередь: поиск MR по Jira ID, автоматический и ручной cherry-pick в релизные ветки, сохранение сессий
- Compare: сравнение наличия задач JIRA по веткам за период (direct / cherry-pick / ручная пересборка / отсутствует)
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

# Email-получатели по умолчанию (если не указаны в правиле), через запятую
DEFAULT_EMAIL=""

# SMTP (опционально, для email-уведомлений)
SMTP_HOST=""
SMTP_PORT=587
SMTP_USER=""
SMTP_PASSWORD=""
SMTP_FROM=""

# Jira (для Cherry-Pick очереди и Compare — ссылки на задачи)
JIRA_URL="https://jira.domain.com"
JIRA_PROJECT="PROJ"

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
| `http://localhost:8055/` | Дашборд — история уведомлений с фильтрами и повторной отправкой |
| `http://localhost:8055/polled` | Журнал опрошенных MR (автор, ветки, статус, ошибки) |
| `http://localhost:8055/rules` | Управление правилами (создание, копирование, вкл/выкл) |
| `http://localhost:8055/rules/new` | Создание нового правила |
| `http://localhost:8055/queue` | Cherry-Pick очередь — поиск MR по Jira, cherry-pick в релизные ветки |
| `http://localhost:8055/compare` | Compare — сравнение наличия задач по веткам за период |
| `http://localhost:8055/docs` | Swagger — документация API |

## Как работает polling

1. При запуске сервис определяет ID проекта GitLab по `GITLAB_PROJECT`
2. Для каждого включённого правила запускается фоновый цикл опроса
3. На каждой итерации:
   - Запрашиваются последние MR с указанными `target_branch` и `mr_state`
   - Пропускаются уже обработанные MR (хранятся в таблице `processed_mrs`)
   - Для новых MR загружаются изменённые файлы
   - Файлы проверяются по правилам
   - При совпадении — проверяется, не было ли уже отправки (по `notification_log`)
   - Если дубля нет — отправляется уведомление
   - Каждый опрошенный MR логируется в `polled_mrs`
4. Интервал опроса берётся из правила, или из `POLL_INTERVAL_SECONDS` если не задан

## Cherry-Pick очередь

Инструмент для массового cherry-pick задач из master в релизные ветки.

1. Ввести номера задач Jira (через запятую, URL или номер) — система найдёт связанные MR через поиск GitLab
2. Загрузить информацию о MR и отфильтровать уже cherry-picked (по наличию ветки `cherry-pick-{sha[:8]}` в целевой ветке)
3. Для каждого MR:
   - **Ручной CP:** создаётся ветка `cherry-pick-{sha[:8]}` от target, cherry-pick коммита, возвращается URL для создания MR
   - **Авто CP:** ветка + cherry-pick + создание MR + approve + merge — всё автоматически
4. Сессия сохраняется в историю для аудита

## Compare: сравнение веток

Сводная таблица показывающая, в каких ветках присутствуют задачи JIRA.

1. Указать период (дата с — дата по) и список веток (например `master, release/97, release/98`)
2. Для каждой ветки загружаются все merged MR за период (с пагинацией)
3. Из title каждого MR извлекается JIRA ID (формат `PROJ-123: описание`)
4. Строится матрица задача × ветка, для каждой ячейки определяется статус:
   - **Direct** — MR влит напрямую в ветку (обычная feature-ветка)
   - **Cherry-pick** — MR создан из ветки `cherry-pick-*`
   - **Ручной** — MR с тем же JIRA ID, но не через cherry-pick (ручная пересборка из-за конфликтов)
   - **Нет** — MR для этой задачи в ветке отсутствует
5. MR без JIRA ID в title показаны отдельной секцией внизу

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

### Email-получатели

Приоритет:
1. Email-адреса из правила (если указаны)
2. `DEFAULT_EMAIL` из `.env` (если в правиле не указаны)
3. Отправка пропускается (если нигде не указаны)

### Дедупликация уведомлений

Перед отправкой проверяется таблица `notification_log`. Если для комбинации
(правило + MR + файл) уже есть успешная отправка — повторная автоматическая
отправка пропускается. Ручная повторная отправка с дашборда игнорирует эту проверку.

### Копирование правил

Кнопка "Копия" на странице правил создаёт дубликат со всеми настройками
и email-получателями. Копия создаётся выключенной, чтобы избежать
дублирующего опроса.

### Пример правила

- **Название:** Breaking Changes
- **Целевая ветка:** master
- **Статус MR:** merged
- **Паттерн файла:** `changelogs/unreleased/*.md`
- **Условие:** `type: breaking`
- **Тип:** contains

## API

Все эндпоинты доступны в Swagger: `http://localhost:8055/docs`

### Правила

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/rules` | Список правил |
| POST | `/api/rules` | Создание правила |
| GET | `/api/rules/{id}` | Получение правила |
| PUT | `/api/rules/{id}` | Обновление правила |
| DELETE | `/api/rules/{id}` | Удаление правила |
| PATCH | `/api/rules/{id}/toggle` | Вкл/выкл правила |
| POST | `/api/rules/{id}/copy` | Копирование правила |
| POST | `/api/rules/{id}/test` | Тестовая отправка в Teams |
| POST | `/api/rules/logs/{id}/resend` | Повторная отправка уведомления |
| GET | `/api/rules/logs/recent` | Лог уведомлений (с фильтрами) |

### Cherry-Pick очередь

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/api/queue/search-jira` | Поиск MR по Jira ID в title |
| POST | `/api/queue/load` | Загрузка информации о MR по списку ID |
| POST | `/api/queue/load-filtered` | Загрузка MR с фильтрацией уже cherry-picked |
| POST | `/api/queue/cherry-pick` | Ручной cherry-pick (ветка + cherry-pick + URL создания MR) |
| POST | `/api/queue/auto-cherry-pick` | Автоматический cherry-pick (ветка + CP + MR + approve + merge) |
| POST | `/api/queue/check` | Проверка статуса cherry-pick MR по source branch |
| POST | `/api/queue/save` | Сохранение сессии cherry-pick |
| GET | `/api/queue/history` | Список сессий cherry-pick |
| GET | `/api/queue/history/{id}` | Детали сессии |
| DELETE | `/api/queue/history/{id}` | Удаление сессии |

### Compare

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/api/compare/run` | Сравнение MR по веткам за период |

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
- `polled_mrs` — журнал всех опрошенных MR (автор, ветки, статус, ошибки)
- `processed_mrs` — обработанные MR (для предотвращения повторной обработки)
- `cherry_pick_sessions` — сессии cherry-pick (имя, целевая ветка, кол-во MR)
- `cherry_pick_items` — элементы сессии (MR, SHA, ветка CP, URL созданного MR, дата мержа)

## Требования

- Python 3.11+
- Доступ к GitLab API (Personal Access Token с правами `read_api`)
- Microsoft Teams Incoming Webhook URL
- SMTP-сервер (опционально, для email)
