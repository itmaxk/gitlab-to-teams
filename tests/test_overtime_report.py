import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fastapi.templating


class _DummyJinja2Templates:
    def __init__(self, *args, **kwargs):
        pass


fastapi.templating.Jinja2Templates = _DummyJinja2Templates

import db
from models import ReportRequest
from routers import reports


def test_overtime_report_includes_inactive_user_with_overtime(monkeypatch, tmp_path):
    test_db = tmp_path / "test-data.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()

    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO jira_users (account_id, display_name, email_address, active)
        VALUES (?, ?, ?, ?)
        """,
        ("inactive-user", "Inactive User", "inactive@example.com", 0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("JIRA_PROJECT", "MAIN")

    async def fake_get_all_worklogs_for_project(project, date_from, date_to):
        return {}

    async def fake_get_worklogs_for_users_all_projects(user_ids, date_from, date_to):
        assert "inactive-user" in user_ids
        return {
            "inactive-user": [
                {
                    "issue_key": "OPS-1",
                    "date": "2026-03-02",
                    "seconds": 10 * 3600,
                    "project": "OPS",
                    "display_name": "Inactive User",
                    "email": "inactive@example.com",
                    "author_key": "inactive-user",
                }
            ]
        }

    monkeypatch.setattr(
        reports.jira_client,
        "get_all_worklogs_for_project",
        fake_get_all_worklogs_for_project,
    )
    monkeypatch.setattr(
        reports.jira_client,
        "get_worklogs_for_users_all_projects",
        fake_get_worklogs_for_users_all_projects,
    )

    result = asyncio.run(reports.overtime_report(ReportRequest(year=2026, month=3)))

    assert len(result["rows"]) == 1
    assert result["rows"][0]["account_id"] == "inactive-user"
    assert result["rows"][0]["display_name"] == "Inactive User"
    assert result["rows"][0]["day_type"] == "workday"
    assert result["rows"][0]["over_norm"] == "2.0"
