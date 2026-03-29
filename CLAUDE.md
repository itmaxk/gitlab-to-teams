# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GitLab Manager service. Polls GitLab API for merge requests and sends notifications to Microsoft Teams/email when changed files match configured rules. No webhook required — works via periodic API polling.

## Commands

```bash
# Install dependencies
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# Run the server (starts polling automatically)
.venv\Scripts\python.exe main.py

# Verify syntax (no test suite exists)
python -c "import py_compile; py_compile.compile('main.py', doraise=True)"
```

Server runs on port 8055 by default. UI at `/rules`, dashboard at `/`, cherry-pick queue at `/queue`, compare at `/compare`, API docs at `/docs`.

## Architecture

**Entry point:** `main.py` — FastAPI app with lifespan that initializes DB and starts background polling.

**Router layers:**
- `routers/pages.py` — HTML pages (dashboard `/`, polled MRs `/polled`, rule CRUD `/rules/*`, queue `/queue`, compare `/compare`)
- `routers/rules.py` — JSON API under `/api/rules` (CRUD, toggle, copy, test, resend)
- `routers/queue.py` — Cherry-pick queue API under `/api/queue` (search by Jira ID, load/filter MRs, cherry-pick, session history)
- `routers/compare.py` — Cross-branch comparison API under `/api/compare` (find MRs by date range, group by JIRA ID, compare across branches)

**Polling flow** (`services/poller.py`):
1. Rules grouped by `poll_interval_seconds` → each group gets its own `asyncio` loop
2. Within a loop, rules further grouped by `(target_branch, mr_state)` to deduplicate API calls
3. For each new MR: fetch changed files → evaluate rules → dispatch notifications → mark processed
4. Every polled MR is logged to `polled_mrs` table

**Rule evaluation** (`services/rules_engine.py`):
- Matches changed file paths against glob pattern (`file_pattern`)
- Fetches file content and checks against `content_match` (contains/regex/exact)
- Optional: extracts file references from content and verifies they exist in MR changes

**Notification dispatch** (`services/notification_dispatcher.py`):
- Deduplication check against `notification_log` before sending (rule_id + mr_iid + file_path)
- Sends to Teams webhook, then optionally email
- Email recipients: rule-level → `DEFAULT_EMAIL` env fallback → skip
- `force=True` bypasses dedup (used by manual resend)

**Cherry-pick queue** (`routers/queue.py`):
- Searches MRs by JIRA ID via GitLab's MR title search
- Detects existing cherry-picks by checking for `cherry-pick-{sha[:8]}` branch merged into target
- Supports manual and auto cherry-pick (branch + cherry-pick + create MR + approve + merge)
- Sessions saved to `cherry_pick_sessions` / `cherry_pick_items` tables

**Compare** (`routers/compare.py`):
- Fetches all merged MRs per branch in a date range (paginated via `get_all_merged_mrs`)
- Groups by JIRA ID extracted from MR title (regex `[A-Z][A-Z0-9]+-\d+`)
- Classifies each branch entry: direct / cherry-pick (source starts with `cherry-pick-`) / manual / missing

**Database** (`db.py`): SQLite `data.db`, auto-created with migrations. Key tables:
- `notification_rules` + `email_recipients` — rule config
- `notification_log` — sent notifications (also used for dedup)
- `polled_mrs` — polling audit log
- `processed_mrs` — prevents re-processing same MR per rule
- `cherry_pick_sessions` + `cherry_pick_items` — cherry-pick session history

**External clients** (`services/`): `gitlab_client.py` (httpx, async), `teams_client.py` (Adaptive Card), `email_client.py` (SMTP).

## Key Patterns

- All GitLab API calls use `httpx.AsyncClient(verify=False)` — internal GitLab with self-signed certs
- DB access is synchronous `sqlite3` (no async ORM) — `get_db()` returns a new connection each call
- Templates use Jinja2 with Tailwind CSS (CDN). Rule/notification JS in `static/app.js`; queue and compare pages use inline `<script>` in their templates
- Config from `.env` via `python-dotenv` (optional import). Rule-level settings override env defaults
- Pydantic models in `models.py` are for API validation only; pages use `Form()` parameters directly
