import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fastapi.templating
from fastapi import HTTPException


class _DummyJinja2Templates:
    def __init__(self, *args, **kwargs):
        pass


fastapi.templating.Jinja2Templates = _DummyJinja2Templates

import db
from models import OvertimeDebugRequest, ReportRequest
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


def test_overtime_debug_issue_explains_why_user_is_missing(monkeypatch, tmp_path):
    test_db = tmp_path / "debug-data.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    monkeypatch.setenv("JIRA_PROJECT", "MAIN")

    async def fake_get_issue_worklogs(issue_key, start_at=0, max_results=1000):
        assert issue_key == "MAIN-1"
        return {
            "worklogs": [
                {
                    "author": {
                        "accountId": "user-a",
                        "displayName": "User A",
                        "emailAddress": "a@example.com",
                    },
                    "started": "2026-03-02T09:00:00.000+0000",
                    "timeSpentSeconds": 10 * 3600,
                },
                {
                    "author": {
                        "accountId": "user-b",
                        "displayName": "User B",
                        "emailAddress": "b@example.com",
                    },
                    "started": "2026-03-02T11:00:00.000+0000",
                    "timeSpentSeconds": 6 * 3600,
                },
            ]
        }

    async def fake_get_all_worklogs_for_project(project, date_from, date_to):
        return {
            "user-a": [
                {
                    "issue_key": "MAIN-1",
                    "date": "2026-03-02",
                    "seconds": 10 * 3600,
                    "project": "MAIN",
                    "display_name": "User A",
                    "email": "a@example.com",
                    "author_key": "user-a",
                }
            ],
            "user-b": [
                {
                    "issue_key": "MAIN-1",
                    "date": "2026-03-02",
                    "seconds": 6 * 3600,
                    "project": "MAIN",
                    "display_name": "User B",
                    "email": "b@example.com",
                    "author_key": "user-b",
                }
            ],
        }

    async def fake_get_worklogs_for_users_all_projects(user_ids, date_from, date_to):
        return {
            "user-a": [
                {
                    "issue_key": "MAIN-1",
                    "date": "2026-03-02",
                    "seconds": 10 * 3600,
                    "project": "MAIN",
                    "display_name": "User A",
                    "email": "a@example.com",
                    "author_key": "user-a",
                }
            ],
            "user-b": [
                {
                    "issue_key": "MAIN-1",
                    "date": "2026-03-02",
                    "seconds": 6 * 3600,
                    "project": "MAIN",
                    "display_name": "User B",
                    "email": "b@example.com",
                    "author_key": "user-b",
                }
            ],
        }

    async def fake_diagnose_worklog_author_candidates(
        candidate_ids, date_from, date_to, issue_key=""
    ):
        assert issue_key == "MAIN-1"
        return {
            "user-a": {
                "issue_key_filter": issue_key,
                "issues_found": 1,
                "issue_keys": ["MAIN-1"],
                "strict_entry_count": 1,
                "strict_hours": 10.0,
                "candidate_match_entry_count": 1,
                "candidate_match_hours": 10.0,
                "candidate_match_issue_keys": ["MAIN-1"],
            },
            "user-b": {
                "issue_key_filter": issue_key,
                "issues_found": 1,
                "issue_keys": ["MAIN-1"],
                "strict_entry_count": 1,
                "strict_hours": 6.0,
                "candidate_match_entry_count": 1,
                "candidate_match_hours": 6.0,
                "candidate_match_issue_keys": ["MAIN-1"],
            },
        }

    monkeypatch.setattr(
        reports.jira_client,
        "get_issue_worklogs",
        fake_get_issue_worklogs,
    )
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
    monkeypatch.setattr(
        reports.jira_client,
        "diagnose_worklog_author_candidates",
        fake_diagnose_worklog_author_candidates,
    )

    result = asyncio.run(
        reports.overtime_debug_issue(
            OvertimeDebugRequest(year=2026, month=3, issue_key="main-1")
        )
    )

    assert result["issue_key"] == "MAIN-1"
    assert len(result["issue_worklogs"]) == 2

    users = {user["account_id"]: user for user in result["users"]}
    assert users["user-a"]["included_in_monthly_report"] is True
    assert users["user-a"]["included_due_to_issue"] is True
    assert users["user-a"]["exclusion_reason"] == "included_via_issue"
    assert users["user-a"]["lookup_diagnostics"]["user-a"]["issue_key_filter"] == "MAIN-1"

    assert users["user-b"]["included_in_monthly_report"] is False
    assert users["user-b"]["included_due_to_issue"] is False
    assert users["user-b"]["exclusion_reason"] == "workday_not_over_8h"
    assert users["user-b"]["issue_day_checks"][0]["qualifies_overtime"] is False


def test_overtime_debug_issue_survives_lookup_diagnostics_failure(
    monkeypatch, tmp_path
):
    test_db = tmp_path / "debug-failure-data.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    monkeypatch.setenv("JIRA_PROJECT", "MAIN")

    async def fake_get_issue_worklogs(issue_key, start_at=0, max_results=1000):
        return {
            "worklogs": [
                {
                    "author": {
                        "accountId": "user-a",
                        "displayName": "User A",
                        "emailAddress": "a@example.com",
                    },
                    "started": "2026-03-02T09:00:00.000+0000",
                    "timeSpentSeconds": 10 * 3600,
                }
            ]
        }

    async def fake_diagnose_worklog_author_candidates(
        candidate_ids, date_from, date_to, issue_key=""
    ):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(
        reports.jira_client,
        "get_issue_worklogs",
        fake_get_issue_worklogs,
    )
    monkeypatch.setattr(
        reports.jira_client,
        "diagnose_worklog_author_candidates",
        fake_diagnose_worklog_author_candidates,
    )

    result = asyncio.run(
        reports.overtime_debug_issue(
            OvertimeDebugRequest(year=2026, month=3, issue_key="main-1")
        )
    )

    users = {user["account_id"]: user for user in result["users"]}
    assert users["user-a"]["included_due_to_issue"] is True
    assert users["user-a"]["lookup_diagnostics"] == {}
    assert users["user-a"]["lookup_diagnostics_error"] == "connect failed"


def test_overtime_debug_issue_times_out_slow_lookup_diagnostics(
    monkeypatch, tmp_path
):
    test_db = tmp_path / "debug-timeout-data.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    monkeypatch.setenv("JIRA_PROJECT", "MAIN")
    monkeypatch.setattr(reports, "_LOOKUP_DIAGNOSTICS_TIMEOUT_SECONDS", 0.01)

    async def fake_get_issue_worklogs(issue_key, start_at=0, max_results=1000):
        return {
            "worklogs": [
                {
                    "author": {
                        "accountId": "user-a",
                        "displayName": "User A",
                        "emailAddress": "a@example.com",
                    },
                    "started": "2026-03-02T09:00:00.000+0000",
                    "timeSpentSeconds": 10 * 3600,
                }
            ]
        }

    async def fake_diagnose_worklog_author_candidates(
        candidate_ids, date_from, date_to, issue_key=""
    ):
        await asyncio.sleep(0.05)
        return {"user-a": {"issues_found": 1}}

    monkeypatch.setattr(
        reports.jira_client,
        "get_issue_worklogs",
        fake_get_issue_worklogs,
    )
    monkeypatch.setattr(
        reports.jira_client,
        "diagnose_worklog_author_candidates",
        fake_diagnose_worklog_author_candidates,
    )

    result = asyncio.run(
        reports.overtime_debug_issue(
            OvertimeDebugRequest(year=2026, month=3, issue_key="main-1")
        )
    )

    users = {user["account_id"]: user for user in result["users"]}
    assert users["user-a"]["lookup_diagnostics"] == {}
    assert users["user-a"]["lookup_diagnostics_error"] == "lookup timeout after 0.01s"


def test_overtime_report_returns_http_502_when_jira_project_lookup_fails(
    monkeypatch, tmp_path
):
    test_db = tmp_path / "overtime-http-error.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    monkeypatch.setenv("JIRA_PROJECT", "MAIN")

    async def fake_get_all_worklogs_for_project(project, date_from, date_to):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(
        reports.jira_client,
        "get_all_worklogs_for_project",
        fake_get_all_worklogs_for_project,
    )

    try:
        asyncio.run(reports.overtime_report(ReportRequest(year=2026, month=3)))
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 502
        assert exc.detail == "Failed to load Jira worklogs for overtime report"
