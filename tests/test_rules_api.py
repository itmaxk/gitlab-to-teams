import sys
from pathlib import Path
import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from routers.rules import copy_rule, create_rule, test_rule as run_rule_test


def test_copy_rule_preserves_content_exclude_and_teams_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, content_exclude,
           match_type, target_branch, mr_state, action_type, send_teams, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Rule with local exclusions",
            "Copied rule should keep behavior-changing fields",
            1,
            "*.md",
            "type: breaking",
            "type:\\s*internal",
            "regex",
            "master",
            "opened",
            "notify",
            0,
            1,
        ),
    )
    rule_id = cur.lastrowid
    conn.commit()
    conn.close()

    copied = copy_rule(rule_id)

    assert copied["enabled"] is False
    assert copied["content_exclude"] == "type:\\s*internal"
    assert copied["send_teams"] is False
    assert copied["send_email"] is True


def test_pipeline_job_retry_test_runs_rule_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_retry_fresh_packages",),
    ).fetchone()["id"]
    conn.close()

    from services import poller

    poller._project_id = None

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch=None):
        return [
            {
                "iid": 501,
                "title": "ADIRGSLSUPP-501: Config validate retry",
                "web_url": "https://example.test/mr/501",
                "state": state,
                "source_branch": "feature/config-retry",
                "target_branch": "master",
                "author": {"name": "Dev"},
                "created_at": "2026-05-08T00:00:00Z",
            }
        ]

    async def fake_retry_failed_config_jobs(project_id, mr_iid, job_names, rule_id):
        class Result:
            retried = [{"job_id": 9001}]

        return Result()

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(poller, "retry_failed_config_jobs", fake_retry_failed_config_jobs)

    result = asyncio.run(run_rule_test(rule_id))

    assert result == {"status": "checked", "action": "pipeline_job_retry"}

    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM polled_mrs WHERE mr_iid = ?",
        (501,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["mr_state"] == "opened"
    assert row["rules_checked"] == 1
    assert row["rules_matched"] == 1
    assert row["success"] == 1


def test_create_rule_accepts_aggregate_payload_and_writes_child_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    created = create_rule({
        "name": "Aggregate retry rule",
        "description": "Built by constructor DTO",
        "enabled": True,
        "scope": {
            "target_branch": "*",
            "mr_state": "opened",
            "poll_interval_seconds": 600,
        },
        "conditions": [
            {"type": "changed_file_glob", "operator": "glob", "value": "*"},
            {"type": "title_exclude_regex", "operator": "regex", "value": "Draft"},
        ],
        "actions": [{"type": "pipeline_job_retry", "enabled": True}],
        "channels": {
            "teams": {"enabled": False, "settings": {}},
            "email": {"enabled": True, "settings": {}},
            "gitlab": {"enabled": False, "settings": {}},
        },
        "recipients": ["dev@example.test"],
        "configs": {
            "pipeline_job_retry": {
                "jobs": ["config:validate", "config:check-uncommitted"],
                "trace_marker": "[5/5] Building fresh packages...",
                "trace_matcher_regex": "TLS socket disconnected",
            }
        },
    })

    assert created["action_type"] == "pipeline_job_retry"
    assert created["content_match"] == "config:validate,config:check-uncommitted"
    assert created["send_email"] is True
    assert created["emails"] == ["dev@example.test"]

    conn = db.get_db()
    condition_count = conn.execute(
        "SELECT COUNT(*) FROM rule_conditions WHERE rule_id = ?", (created["id"],)
    ).fetchone()[0]
    job_rows = conn.execute(
        "SELECT job_name FROM rule_pipeline_retry_jobs WHERE rule_id = ? ORDER BY sort_order",
        (created["id"],),
    ).fetchall()
    conn.close()

    assert condition_count == 2
    assert [row["job_name"] for row in job_rows] == [
        "config:validate",
        "config:check-uncommitted",
    ]
