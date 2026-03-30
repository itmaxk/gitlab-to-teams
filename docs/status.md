# Status

## Current phase
- `Milestone 1` completed

## Done
- Найден маршрут `POST /api/reports/overtime`
- Проверен источник списка пользователей: `project_worklogs` + `jira_users WHERE active = 1`
- Сформулирована гипотеза: пользователи выпадают после деактивации в `jira_users`, хотя Jira ещё возвращает их ворклоги
- В `overtime_report` убран фильтр `active = 1` для источника пользователей из БД
- Добавлен регрессионный тест на пользователя с `active = 0`
- `pytest` пройден успешно

## In progress
- Нет

## Next
- Если нужно, отдельно обсудить, должны ли вручную деактивированные пользователи участвовать в overtime report

## Decisions
- Не трогать существующие незакоммиченные форматирующие изменения
- Ограничить изменение только логикой overtime report
- Для overtime report считать источником всех сохранённых пользователей `jira_users`, а не только `active = 1`

## Assumptions
- Для отчёта переработок флаг `active` не должен скрывать пользователя, если по нему находятся ворклоги за период

## Commands
- `rg -n "Переработ|overtime|reports" .`
- `git diff -- routers/reports.py`
- `git diff -- services/jira_client.py`

## Blockers
- Нет

## Audit log
- 2026-03-30: найден вероятный root cause в выборке `db_users` для overtime report
- 2026-03-30: внесена правка и добавлен автотест, `pytest` зелёный
