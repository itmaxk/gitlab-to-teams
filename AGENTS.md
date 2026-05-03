# Repository Guidelines

## Project Structure & Module Organization
The app is a FastAPI service rooted in `main.py`. Core database setup lives in `db.py`, shared models in `models.py`, HTTP routes in `routers/`, and business logic in `services/`. Server-rendered UI templates are in `templates/`, with static assets in `static/`. Tests live in `tests/` and cover review, XLSX comparison, GitLab client fallbacks, and settings persistence.

## Build, Test, and Development Commands
- `python -m venv .venv` then `.venv\Scripts\python.exe -m pip install -r requirements.txt`
  Creates a local virtual environment and installs runtime dependencies.
- `.venv\Scripts\python.exe main.py`
  Starts the FastAPI app locally on the host/port from `.env`.
- `pytest`
  Runs the full test suite.
- `pytest tests/test_xlsx_review_service.py`
  Runs a focused test file while iterating on XLSX review behavior.

## Coding Style & Naming Conventions
Use 4-space indentation and follow existing Python style: `snake_case` for functions/variables, `PascalCase` for Pydantic models, and uppercase for module constants such as `DEFAULT_XLSX_BASE_REF`. Prefer small helper functions in `services/` over large route handlers. Keep comments brief and only when logic is not obvious. No formatter is enforced in the repo; match surrounding code style carefully.

## Text Encoding
All text files in this repository use UTF-8 without BOM. Preserve readable Cyrillic text directly in UTF-8; do not introduce CP1251 text, mojibake fragments, or Unicode replacement characters.

## Testing Guidelines
Pytest is the test runner. Add tests alongside the affected behavior in `tests/` using `test_*.py` names and descriptive test functions such as `test_review_xlsx_mr_prefers_head_sha_over_source_branch_for_source_content`. For bug fixes, add a regression test first when practical. Favor small, deterministic fixtures over network access.

## Commit & Pull Request Guidelines
Recent history uses short imperative subjects, for example `Read XLSX review files from MR head refs`. Keep commits focused and explain why the change was needed. Pull requests should include:
- a concise summary of behavior changes,
- affected routes/services (for example `services/xlsx_review_service.py`),
- test evidence (`pytest ...`),
- screenshots when UI templates in `templates/` change.

## Security & Configuration Tips
Do not commit real secrets. Copy `.env.example` to `.env` for local setup. GitLab, Teams, SMTP, Jira, and review API credentials are all environment-driven. Treat `data.db` as local state unless a task explicitly requires migrating or reseeding it.
