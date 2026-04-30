# Status

## Review batching stability

## Current phase
- `Milestone 1` completed

## Done
- Reproduced the logic path for MR review batching and confirmed the batch loop exists
- Identified two likely causes of the stall: very large default batch size and `600s` LLM read timeout per request

## In progress
- None

## Next
- Optionally tune `.env` for the deployed LLM backend if batches still feel slow

## Decisions
- Keep the batch loop sequential, but fail faster on stuck LLM calls instead of waiting about 10 minutes per batch
- Make safer defaults apply automatically even when `REVIEW_BATCH_MAX_CHARS` is not set

## Assumptions
- The reported `1/3` stall is caused by the first LLM request waiting too long, not by the SSE progress stream dropping updates

## Commands
- `pytest tests/test_review_batching.py tests/test_env_reload.py`

## Blockers
- None

## Audit log
- 2026-04-30: traced the MR review flow and found `REVIEW_BATCH_MAX_CHARS` defaulting to the full diff cap plus a `600s` read timeout
- 2026-04-30: implemented safer review batching defaults, added `REVIEW_LLM_READ_TIMEOUT`, and passed focused pytest checks

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
