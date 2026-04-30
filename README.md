# GitLab Manager

Сервис уведомлений: периодически опрашивает GitLab API на предмет новых MR
и отправляет уведомления в Microsoft Teams и/или на email,
если изменённые файлы соответствуют настроенным правилам.

Не требует прямого сетевого доступа от GitLab к сервису (нет webhook) —
работает через polling GitLab API.

## Возможности

**Уведомления о MR:**
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

**Cherry-Pick и Compare:**
- Cherry-Pick очередь: поиск MR по Jira ID, автоматический и ручной cherry-pick в релизные ветки, сохранение сессий
- Compare: сравнение наличия задач JIRA по веткам за период (direct / cherry-pick / ручная пересборка / отсутствует)

**Jira Reports:**
- Отчёт по переработкам (overtime) — анализ worklogs сотрудников за период с учётом праздников и отпусков
- Отчёт по логированию времени (time logging) — контроль кто не залогировал часы за период
- Уведомления в Teams о незалогированном времени
- Автоматическая отправка отчётов по расписанию (настраиваемый день недели и время)
- Управление пользователями Jira: скрытие из отчётов, учёт отпусков
- Производственный календарь с учётом праздников РФ

**Code Review (LLM):**
- Автоматическая проверка кода MR с помощью LLM (OpenAI-совместимый API)
- Настраиваемый системный промпт для ревью
- История ревью с детальными находками по категориям
- Поддержка локальных и облачных моделей

**Общее:**
- Web-интерфейс для управления правилами (создание, редактирование, копирование, удаление, вкл/выкл)
- Журнал опрошенных MR с фильтрацией (статус, результат, совпадения, ветка)
- История уведомлений с фильтрацией (по правилу, статусу отправки, ошибкам)
- Страница настроек с просмотром текущих переменных окружения
- Горячая перезагрузка `.env` без перезапуска сервера (автоматически при каждом цикле опроса + кнопка в настройках)
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

# Jira (для Cherry-Pick очереди, Compare и Reports)
JIRA_URL="https://jira.domain.com"
JIRA_PROJECT="PROJ"
JIRA_TOKEN=""

# Code Review (LLM) — OpenAI-compatible API
REVIEW_API_URL=https://api.openai.com/v1/chat/completions
REVIEW_API_KEY=
REVIEW_MODEL=gpt-4o
REVIEW_MAX_DIFF_CHARS=60000
REVIEW_BATCH_MAX_CHARS=20000
REVIEW_LLM_READ_TIMEOUT=120

# Сервер
HOST=0.0.0.0
PORT=8055
```

> Все параметры `.env` можно менять без перезапуска сервера — они подхватываются
> автоматически при следующем цикле опроса или через кнопку «Перечитать .env»
> на странице настроек (`/settings`). Исключение: `HOST` и `PORT` применяются только при старте.

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
| `http://localhost:8055/reports` | Jira Reports — отчёты по переработкам и логированию времени |
| `http://localhost:8055/review` | Code Review — LLM-ревью кода MR |
| `http://localhost:8055/settings` | Настройки — просмотр переменных окружения, перезагрузка .env |
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

## Jira Reports

Модуль отчётов по данным из Jira (вкладка «Jira Reports»).

### Отчёт по переработкам (Overtime)

Анализирует worklogs сотрудников за указанный период и считает переработки:

1. Загружаются все задачи проекта с worklogs за период
2. Для каждого рабочего дня определяется норма (8ч), с учётом:
   - Праздники РФ (ст. 112 ТК) — исключаются из расчёта
   - Производственный календарь с возможностью ручной корректировки
   - Отпуска сотрудников (управляются через UI)
3. Часы сверх нормы считаются переработкой

### Отчёт по логированию времени (Time Logging)

Показывает, кто из команды не залогировал время за период:

1. Для каждого рабочего дня проверяется наличие worklogs
2. Дни без логов выводятся в отчёт
3. Можно отправить уведомление в Teams конкретным сотрудникам

### Автоматическая отправка

Отчёты можно настроить на автоматическую отправку:
- Настраиваемый день недели и время отправки
- Отправка в Teams webhook и/или на email
- Фоновый планировщик проверяет расписание каждые 60 секунд

### Управление пользователями

- Список пользователей Jira с возможностью скрытия из отчётов
- Учёт отпусков: добавление периодов отпуска для каждого сотрудника
- Скрытые пользователи и дни отпуска исключаются из расчётов

## Code Review (LLM)

Автоматическая проверка кода MR с помощью LLM (вкладка «Review»).

1. Указать номер MR или URL — система загрузит diff из GitLab
2. Diff отправляется в LLM (OpenAI-совместимый API) с системным промптом
3. Результат: структурированный отчёт с находками по категориям (баги, безопасность, стиль и т.д.)
4. История всех ревью сохраняется в БД

Настройки:
- `REVIEW_API_URL` — URL эндпоинта (поддерживаются OpenAI, Anthropic-compatible, локальные модели)
- `REVIEW_API_KEY` — API ключ
- `REVIEW_MODEL` — модель (по умолчанию `gpt-4o`)
- `REVIEW_MAX_DIFF_CHARS` — максимальный размер diff (по умолчанию 60000 символов)
- `REVIEW_BATCH_MAX_CHARS` — размер одного review-батча; по умолчанию ограничен `20000`
- `REVIEW_LLM_READ_TIMEOUT` — таймаут ожидания ответа LLM на один батч в секундах (по умолчанию `120`)
- Системный промпт настраивается через UI

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

### Reports

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/api/reports/time-logging` | Отчёт по логированию времени |
| POST | `/api/reports/overtime` | Отчёт по переработкам |
| POST | `/api/reports/overtime/debug-issue` | Диагностика расчёта по задаче |
| POST | `/api/reports/notify-missing` | Отправка уведомлений о незалогированном времени |
| POST | `/api/reports/send-overtime` | Отправка отчёта по переработкам (Teams/email) |
| POST | `/api/reports/send-time-logging` | Отправка отчёта по логированию (Teams/email) |
| GET | `/api/reports/users` | Список пользователей Jira |
| PATCH | `/api/reports/users/{account_id}` | Обновление настроек пользователя (скрытие) |
| GET | `/api/reports/users/{account_id}/vacations` | Отпуска пользователя |
| POST | `/api/reports/users/{account_id}/vacations` | Добавление отпуска |
| DELETE | `/api/reports/vacations/{vacation_id}` | Удаление отпуска |
| GET | `/api/reports/settings` | Настройки отчётов |
| PUT | `/api/reports/settings/{report_type}` | Обновление настроек отчёта |
| GET | `/api/reports/calendar/{year}` | Производственный календарь за год |
| PUT | `/api/reports/calendar/{year}` | Обновление календаря |
| POST | `/api/reports/calendar/{year}/fetch` | Загрузка календаря из внешнего источника |

### Code Review

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/api/review/run` | Запустить LLM-ревью MR |
| GET | `/api/review/history` | История ревью |
| GET | `/api/review/settings` | Настройки ревью (системный промпт) |
| PUT | `/api/review/settings` | Обновление настроек ревью |
| GET | `/api/review/{id}` | Детали ревью |
| DELETE | `/api/review/{id}` | Удаление ревью |

### Настройки

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/api/reload-env` | Перечитать .env файл |

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
- `jira_users` — пользователи Jira (display name, скрытие из отчётов)
- `report_settings` — настройки автоматической отправки отчётов (тип, расписание, webhook, email)
- `user_vacations` — отпуска сотрудников
- `holiday_overrides` — корректировки производственного календаря
- `review_settings` — настройки LLM-ревью (системный промпт)
- `code_reviews` — история LLM-ревью MR (diff, findings, summary)

## Требования

- Python 3.11+
- Доступ к GitLab API (Personal Access Token с правами `read_api`)
- Microsoft Teams Incoming Webhook URL
- SMTP-сервер (опционально, для email)
- Jira API Token (опционально, для Reports / Cherry-Pick / Compare)
- OpenAI-совместимый LLM API (опционально, для Code Review)
