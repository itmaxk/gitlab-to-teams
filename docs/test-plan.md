# Test Plan

## Scope
- Проверка отчёта `POST /api/reports/overtime`

## Critical cases
- Пользователь из Jira-проекта за период попадает в отчёт
- Пользователь из `jira_users` с `active = 0`, но с переработкой в Jira за период, тоже попадает в отчёт
- Пользователь без переработки не попадает в итоговые строки

## Validation
- Автотест на `overtime_report` с моками Jira-клиента и временной SQLite БД
- `pytest`

## Out of scope
- SMTP/Teams отправка
- UI-рендеринг шаблона
