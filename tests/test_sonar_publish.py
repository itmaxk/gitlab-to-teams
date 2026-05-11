from unittest.mock import AsyncMock, patch
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from services.sonar_publish import (
    parse_sonar_job_name,
    publish_sonar_issues_after_job,
)


def test_parse_sonar_job_name_defaults_to_config_sonar():
    assert parse_sonar_job_name("") == "config:sonar"


def test_seeded_sonar_rule_runs_after_config_sonar(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_sonar_publish_issues",),
    ).fetchone()
    conn.close()

    assert row["action_type"] == "sonar_issues"
    assert row["mr_state"] == "opened"
    assert row["target_branch"] == "*"
    assert row["poll_interval_seconds"] == 600
    assert row["content_match"] == "config:sonar"
    assert row["send_gitlab"] == 1


@pytest.mark.anyio
async def test_publish_sonar_after_completed_job_deletes_previous_note_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_sonar_publish_issues",),
    ).fetchone()["id"]
    conn.close()

    pipelines = [{"id": 42, "status": "success"}]
    jobs = [
        {"id": 101, "name": "config:sonar", "status": "success", "web_url": "https://gitlab.test/job/101"},
    ]
    notes = [
        {"id": 7, "body": "## SonarQube Analysis Results\nold"},
        {"id": 8, "body": "human note"},
    ]
    deleted = []

    async def fake_delete_note(mr_iid, note_id):
        deleted.append((mr_iid, note_id))

    with (
        patch("services.sonar_publish.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.sonar_publish.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
        patch("services.sonar_publish.get_mr_by_iid", new_callable=AsyncMock, return_value={"description": ""}),
        patch(
            "services.sonar_publish.fetch_sonar_issues",
            new_callable=AsyncMock,
            return_value={"issues": [], "total": 0, "formatted": "No issues found."},
        ),
        patch("services.sonar_publish.list_merge_request_notes", new_callable=AsyncMock, return_value=notes),
        patch("services.sonar_publish.delete_merge_request_note", new=fake_delete_note),
        patch("services.sonar_publish.post_merge_request_note", new_callable=AsyncMock, return_value={"id": 9}) as post_mock,
    ):
        result = await publish_sonar_issues_after_job(
            1,
            10,
            "config:sonar",
            rule_id,
            "MR title",
            "https://gitlab.test/mr/10",
        )

    assert result.checked == 1
    assert result.published == [
        {
            "job_id": 101,
            "job_name": "config:sonar",
            "note_id": "9",
            "issues_count": 0,
        }
    ]
    assert deleted == [(10, 7)]
    post_mock.assert_awaited_once()

    conn = db.get_db()
    row = conn.execute(
        "SELECT mr_iid, file_path, gitlab_discussion_id FROM notification_log WHERE rule_id = ?",
        (rule_id,),
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "mr_iid": 10,
        "file_path": "sonar-job:101",
        "gitlab_discussion_id": "9",
    }


@pytest.mark.anyio
async def test_publish_sonar_after_same_job_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_sonar_publish_issues",),
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO notification_log
           (rule_id, mr_iid, file_path, gitlab_sent)
           VALUES (?, ?, ?, ?)""",
        (rule_id, 10, "sonar-job:101", 1),
    )
    conn.commit()
    conn.close()

    with (
        patch("services.sonar_publish.get_mr_pipelines", new_callable=AsyncMock, return_value=[{"id": 42}]),
        patch(
            "services.sonar_publish.get_pipeline_jobs",
            new_callable=AsyncMock,
            return_value=[{"id": 101, "name": "config:sonar", "status": "success"}],
        ),
        patch("services.sonar_publish.fetch_and_publish_sonar_issues", new_callable=AsyncMock) as publish_mock,
    ):
        result = await publish_sonar_issues_after_job(1, 10, "config:sonar", rule_id)

    assert result.published == []
    assert result.skipped == [
        {"job_id": 101, "job_name": "config:sonar", "reason": "already_published"}
    ]
    publish_mock.assert_not_awaited()
