# Test Plan

## XLSX Review In /review

## Scope
- Workbook row extraction, row-level xlsx diff generation, and MR-level xlsx compare flow

## Critical cases
- A changed xlsx row is detected with old/new values
- The compare flow loads base and source-branch versions and emits findings for changed rows
- The selected base ref is preserved in the review result metadata

## Validation
- `pytest tests/test_xlsx_review_service.py`

## Out of scope
- Rich Excel formatting diffs, formulas with formatting semantics, and binary workbook features outside cell value comparison

## Review batching stability

## Scope
- Review MR batching defaults and env reload for review tuning

## Critical cases
- Default batch size falls back to a safer limit when no explicit batch env is set
- Explicit batch size is capped by the max diff size
- `.env` reload updates the review timeout knob together with other review settings

## Validation
- `pytest tests/test_review_batching.py tests/test_env_reload.py`

## Out of scope
- Live end-to-end latency against a real LLM backend

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
