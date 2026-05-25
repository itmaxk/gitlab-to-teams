# Status

## Review latest MR version

## Current phase
- `Milestone 1` completed

## Done
- Found that `/review` already calls `review_mr(..., force_refresh_diff=True)` through both direct and queued run paths
- Changed the forced GitLab diff refresh path to load the newest MR diff version from `GET /versions` and `GET /versions/:id`
- Preserved the latest version `head_commit_sha` as `source_ref` so full-file context also comes from the newest MR commit
- Added a regression test where stale `/changes` content is replaced by the latest diff version payload
- Confirmed a second source of false positives: latest full-file context was allowed to originate findings even when a variable was not in the MR diff
- Made full-file context reference-only and filtered `source=full_file_context` findings before saving review results
- Added a regression test that keeps a diff finding and drops a context-only variable finding

## In progress
- None

## Next
- None

## Decisions
- Scope the latest-version lookup to `force_refresh=True`, which is the `/review` launch path, while keeping normal cached lookups lightweight
- Cache the fresh latest-version result under the same MR key after a forced refresh

## Assumptions
- GitLab returns MR diff versions newest-first or with sortable `created_at`/`id`; the implementation chooses the max by those fields

## Commands
- `pytest tests/test_gitlab_client_diff_fallback.py`
- `pytest tests/test_review_batching.py tests/test_xlsx_review_service.py`
- `pytest tests/test_review_batching.py tests/test_gitlab_client_diff_fallback.py tests/test_xlsx_review_service.py`

## Blockers
- None

## Audit log
- 2026-05-13: implemented latest MR diff version loading for forced `/review` refresh and passed focused tests
- 2026-05-25: prevented latest full-file context from producing standalone findings for variables outside the MR diff

## Pipeline Config Retry Trace Matching

## Current phase
- `Milestone 1` completed

## Done
- Found the strict suffix check in `services/pipeline_check.py`
- Added a regression trace with GitLab section/artifact/cleanup boilerplate after `[5/5] Building fresh packages...`
- Relaxed the matcher to ignore only known failure boilerplate after the marker
- Passed focused pipeline retry tests
- Passed poller resilience tests

## In progress
- None

## Next
- None

## Decisions
- Keep arbitrary command output after the marker as a non-match
- Treat GitLab section markers, artifact upload warnings, cleanup lines, and final `ERROR: Job failed` as boilerplate

## Assumptions
- The missed matches are caused by GitLab adding job-failure boilerplate after the Yarn fresh-packages stall marker

## Commands
- `pytest tests/test_pipeline_check.py`
- `pytest tests/test_poller_resilience.py`

## Blockers
- None

## Audit log
- 2026-05-09: started pipeline retry trace matching fix
- 2026-05-09: completed matcher fix and focused validation

## XLSX Review In /review

## Current phase
- `Milestone 2` completed

## Done
- Confirmed there is no existing xlsx parser in the repo and no spreadsheet dependency in `requirements.txt`
- Chose a no-new-dependency approach based on parsing `.xlsx` as zip+xml
- Added structured `xlsx_rows` details to added/deleted row findings
- Added expandable XLSX row table rendering in `/review`
- Added XLSX rows markdown table rendering for GitLab comments

## In progress
- None

## Next
- Manual smoke-check in `/review` against a real MR with grouped added `.xlsx` rows

## Decisions
- Reuse the existing `/review` history and publish pipeline instead of introducing a separate xlsx storage model
- Store xlsx comparison output as regular review findings so UI and GitLab comment formatting stay aligned
- Keep the old text `suggestion` for compatibility and add structured rows as an optional field

## Assumptions
- Row-level comparison based on workbook cell values is sufficient for the requested review use case
- GitLab accepts markdown tables inside a `<details>` block in MR notes

## Commands
- `pytest tests/test_xlsx_review_service.py`
- `pytest tests/test_xlsx_review_service.py tests/test_review_comment_formatter.py`

## Blockers
- None

## Audit log
- 2026-04-30: started xlsx review implementation using GitLab raw file bytes plus zip/xml workbook parsing
- 2026-04-30: finished `/review` xlsx mode, added row-level compare output, and passed focused tests
- 2026-05-08: started readable grouped XLSX row tables for UI and GitLab publish
- 2026-05-08: completed structured XLSX row tables and passed focused regression tests

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
