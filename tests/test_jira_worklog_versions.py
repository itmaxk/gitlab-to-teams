import asyncio
import sys
from datetime import date
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
from services import jira_client


def _entry(worklog_id: str, day: str, updated: str, seconds: int = 10 * 3600):
    return {
        "issue_key": "MAIN-1",
        "worklog_id": worklog_id,
        "date": day,
        "seconds": seconds,
        "project": "MAIN",
        "display_name": "User A",
        "email": "a@example.com",
        "author_key": "user-a",
        "updated": updated,
    }


def test_extract_worklogs_keeps_jira_worklog_identity_and_updated_timestamp():
    entries = jira_client._extract_worklogs(
        [
            {
                "id": "12345",
                "started": "2026-03-02T09:00:00.000+0000",
                "updated": "2026-03-04T12:00:00.000+0000",
                "timeSpentSeconds": 3600,
                "author": {
                    "accountId": "user-a",
                    "displayName": "User A",
                    "emailAddress": "a@example.com",
                },
            }
        ],
        "MAIN-1",
        "MAIN",
        date(2026, 3, 1),
        date(2026, 3, 31),
    )

    assert entries[0]["worklog_id"] == "12345"
    assert entries[0]["started"] == "2026-03-02T09:00:00.000+0000"
    assert entries[0]["updated"] == "2026-03-04T12:00:00.000+0000"


def test_dedupe_worklog_entries_prefers_latest_updated_version_by_worklog_id():
    result = jira_client.dedupe_worklog_entries(
        [
            _entry("wl-1", "2026-03-07", "2026-03-07T09:00:00.000+0000"),
            _entry("wl-1", "2026-03-02", "2026-03-08T10:00:00.000+0000"),
        ]
    )

    assert len(result) == 1
    assert result[0]["date"] == "2026-03-02"


def test_overtime_report_uses_edited_worklog_date_when_sources_disagree(
    monkeypatch, tmp_path
):
    test_db = tmp_path / "edited-worklog-overtime.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    monkeypatch.setenv("JIRA_PROJECT", "MAIN")

    async def fake_get_all_worklogs_for_project(project, date_from, date_to):
        return {
            "user-a": [
                _entry("wl-1", "2026-03-07", "2026-03-07T09:00:00.000+0000")
            ]
        }

    async def fake_get_worklogs_for_users_all_projects_by_candidates(
        user_candidates, date_from, date_to
    ):
        return {
            "user-a": [
                _entry("wl-1", "2026-03-02", "2026-03-08T10:00:00.000+0000")
            ]
        }

    monkeypatch.setattr(
        reports.jira_client,
        "get_all_worklogs_for_project",
        fake_get_all_worklogs_for_project,
    )
    monkeypatch.setattr(
        reports.jira_client,
        "get_worklogs_for_users_all_projects_by_candidates",
        fake_get_worklogs_for_users_all_projects_by_candidates,
    )

    result = asyncio.run(reports.overtime_report(ReportRequest(year=2026, month=3)))

    assert len(result["rows"]) == 1
    assert result["rows"][0]["date"] == "2026-03-02"
    assert result["rows"][0]["day_type"] == "workday"


def test_time_logging_report_uses_edited_worklog_date_when_sources_disagree(
    monkeypatch, tmp_path
):
    test_db = tmp_path / "edited-worklog-time-logging.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    monkeypatch.setenv("JIRA_PROJECT", "MAIN")

    async def fake_get_all_worklogs_for_project(project, date_from, date_to):
        return {
            "user-a": [
                _entry("wl-1", "2026-03-02", "2026-03-02T09:00:00.000+0000")
            ]
        }

    async def fake_get_worklogs_for_users_all_projects(user_ids, date_from, date_to):
        return {
            "user-a": [
                _entry("wl-1", "2026-03-03", "2026-03-04T10:00:00.000+0000")
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

    result = asyncio.run(reports.time_logging_report(ReportRequest(year=2026, month=3)))

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["days_logged"] == 1
    assert "2026-03-02" in row["missing_days"]
    assert "2026-03-03" not in row["missing_days"]
