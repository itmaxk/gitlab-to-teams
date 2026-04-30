# Plan

## Review batching stability

### Goal
- Reduce long waits on a single MR review batch and make multi-batch progress move forward predictably.

### Milestone 1
- Status: `[x]`
- Goal: tighten review batch defaults and LLM wait limits.
- Tasks:
- lower the default batch size so batching actually reduces per-request payload
- make the LLM read timeout configurable and shorter by default
- cover the new defaults with targeted tests
- Definition of done:
- batch sizing defaults are safer without extra env tuning
- timeout behavior is configurable via `.env`
- targeted review batching tests pass
- Validation commands:
- `pytest tests/test_review_batching.py tests/test_env_reload.py`
- Known risks:
- smaller batches increase request count for very large MRs
- Stop-and-fix rule:
- if findings coverage regresses, keep full-file coverage and adjust only limits/timeouts

## Jira Reports: Переработки

### Цель
- Исправить сценарий, в котором часть пользователей с переработками не попадает в отчёт `Jira Reports -> Переработки`.

### Предположение
- Пользователь должен попадать в отчёт по переработкам, даже если он неактивен в `jira_users`, но его ворклоги за период всё ещё находятся через Jira API.

### Milestone 1
- Статус: `[x]`
- Goal: найти и исправить фильтр пользователей для overtime report.
- Tasks:
- проверить построение списка `all_user_ids` в `routers/reports.py`
- убрать зависимость от `active = 1`, если она скрывает валидных пользователей с переработками
- добавить регрессионную проверку
- Definition of done:
- пользователь с ворклогами и переработками за период не теряется только из-за флага `active`
- есть локальная автоматическая проверка сценария
- Validation commands:
- `pytest`
- Known risks:
- если бизнес-логика ожидала исключать вручную деактивированных пользователей, поведение отчёта расширится
- Stop-and-fix rule:
- если тест покажет конфликт требований по `active`, остановиться и зафиксировать различие в статусе
