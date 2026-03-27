# GitLab → Teams

Сервис уведомлений: при merge MR в GitLab отправляет сообщение в Microsoft Teams
и/или на email, если изменённые файлы соответствуют настроенным правилам.

## Возможности

- Приём webhook от GitLab при merge MR
- Проверка изменённых файлов по glob-паттерну (например `changelogs/unreleased/*.md`)
- Проверка содержимого файла (contains / regex / exact)
- Отправка в Microsoft Teams через Incoming Webhook (Adaptive Card)
- Дублирование на email (SMTP)
- Web-интерфейс для управления правилами (создание, редактирование, удаление, вкл/выкл)
- Лог отправленных уведомлений
- API с автодокументацией (Swagger)

## Быстрый старт

### 1. Установка зависимостей

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
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
GITLAB_PROJECT="gitlab-project"
GITLAB_WEBHOOK_SECRET=""

# Microsoft Teams
TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/..."

# SMTP (опционально, для email-уведомлений)
SMTP_HOST=""
SMTP_PORT=587
SMTP_USER=""
SMTP_PASSWORD=""
SMTP_FROM=""

# Сервер
HOST=0.0.0.0
PORT=8000
```

### 3. Запуск

```bash
python main.py
```

Сервер запустится на `http://localhost:8000`.

## Интерфейсы

| URL | Описание |
|-----|----------|
| `http://localhost:8000/` | Дашборд — лог уведомлений и статистика |
| `http://localhost:8000/rules` | Управление правилами |
| `http://localhost:8000/rules/new` | Создание нового правила |
| `http://localhost:8000/docs` | Swagger — документация API |

## Настройка GitLab Webhook

В проекте GitLab: **Settings → Webhooks**

| Параметр | Значение |
|----------|----------|
| URL | `https://your-host:8000/api/webhook/gitlab` |
| Secret token | Значение `GITLAB_WEBHOOK_SECRET` из `.env` |
| Trigger | **Merge request events** |
| SSL verification | Включено (если используется HTTPS) |

## Как работают правила

1. GitLab отправляет webhook при merge MR
2. Сервис получает список изменённых файлов через GitLab API
3. Для каждого включённого правила:
   - Путь файла проверяется по glob-паттерну (`file_pattern`)
   - Если совпал — загружается содержимое файла
   - Содержимое проверяется по условию (`content_match` + `match_type`)
4. При совпадении:
   - Полный текст файла отправляется в Teams (Adaptive Card)
   - Если включена email-рассылка — дублируется на указанные адреса
5. Результат записывается в лог

### Типы совпадения (`match_type`)

| Тип | Описание | Пример |
|-----|----------|--------|
| `contains` | Подстрока содержится в файле | `type: breaking` |
| `regex` | Регулярное выражение | `type:\s*(breaking\|security)` |
| `exact` | Точное совпадение (с trim) | Полный текст файла |

### Пример правила

- **Название:** Breaking Changes
- **Паттерн файла:** `changelogs/unreleased/*.md`
- **Условие:** `type: breaking`
- **Тип:** contains

При merge MR, если в изменениях есть файл `changelogs/unreleased/TASK-123.md`
с текстом `type: breaking`, полный текст файла будет отправлен в Teams.

## API

Все эндпоинты доступны в Swagger: `http://localhost:8000/docs`

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/api/webhook/gitlab` | Приём webhook от GitLab |
| GET | `/api/rules` | Список правил |
| POST | `/api/rules` | Создание правила |
| GET | `/api/rules/{id}` | Получение правила |
| PUT | `/api/rules/{id}` | Обновление правила |
| DELETE | `/api/rules/{id}` | Удаление правила |
| PATCH | `/api/rules/{id}/toggle` | Вкл/выкл правила |
| POST | `/api/rules/{id}/test` | Тестовая отправка в Teams |
| GET | `/api/rules/logs/recent` | Последние 50 записей лога |

## Хранение данных

SQLite база `data.db` создаётся автоматически при первом запуске.
При первом запуске также создаётся дефолтное правило "Breaking Changes".

Таблицы:
- `notification_rules` — правила уведомлений
- `email_recipients` — email-получатели (привязаны к правилам)
- `notification_log` — лог отправленных уведомлений

## Требования

- Python 3.11+
- Доступ к GitLab API (Personal Access Token с правами `read_api`)
- Microsoft Teams Incoming Webhook URL
- SMTP-сервер (опционально, для email)
