import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from services import poller


def test_poll_once_marks_mr_processed_even_when_audit_log_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Path-only opened MR rule",
            "Rule used to exercise poller completion path",
            1,
            "configuration/**",
            "",
            "contains",
            "master",
            "opened",
            0,
        ),
    )
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE name = ?",
        ("Path-only opened MR rule",),
    ).fetchone()["id"]
    conn.commit()
    conn.close()

    poller._project_id = None

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch):
        return [
            {
                "iid": 123,
                "title": "MR title",
                "web_url": "https://example.test/mr/123",
                "state": state,
                "source_branch": "feature/branch",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2026-04-29T00:00:00Z",
            }
        ]

    async def fake_get_mr_changes(project_id, mr_iid):
        return ["configuration/service/configuration.json"]

    async def fake_dispatch_notifications(matches, mr_iid, mr_title, mr_url, force=False):
        return None

    def fake_log_polled_mr(*args, **kwargs):
        raise RuntimeError("polled_mrs missing")

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(poller, "get_mr_changes", fake_get_mr_changes)
    monkeypatch.setattr(poller, "dispatch_notifications", fake_dispatch_notifications)
    monkeypatch.setattr(poller, "_log_polled_mr", fake_log_polled_mr)

    asyncio.run(
        poller.poll_once(
            [
                {
                    "id": rule_id,
                    "target_branch": "master",
                    "mr_state": "opened",
                }
            ]
        )
    )

    conn = db.get_db()
    processed = conn.execute(
        "SELECT 1 FROM processed_mrs WHERE rule_id = ? AND mr_iid = ?",
        (rule_id, 123),
    ).fetchone()
    conn.close()

    assert processed is not None


def test_poll_once_global_title_skip_blocks_all_rule_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    conn.execute(
        "INSERT INTO global_settings (key, value) VALUES (?, ?)",
        ("global_title_excludes", "[skip_changelog]"),
    )
    action_types = [
        "notify",
        "title_check",
        "pipeline_check",
        "xlsx_review",
        "code_review",
    ]
    rule_ids = []
    for action_type in action_types:
        cur = conn.execute(
            """
            INSERT INTO notification_rules
              (name, description, enabled, file_pattern, content_match, match_type,
               target_branch, mr_state, action_type, send_email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{action_type} rule",
                "Rule used to prove global title excludes short-circuit all actions",
                1,
                "*.md",
                "",
                "contains",
                "master",
                "opened",
                action_type,
                0,
            ),
        )
        rule_ids.append(cur.lastrowid)
    conn.commit()
    conn.close()

    poller._project_id = None
    calls = []

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch):
        return [
            {
                "iid": 124,
                "title": "Merge [skip_changelog] into master",
                "web_url": "https://example.test/mr/124",
                "state": state,
                "source_branch": "feature/branch",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2026-04-29T00:00:00Z",
            }
        ]

    async def forbidden_async_call(*args, **kwargs):
        calls.append(args)
        raise AssertionError("global title skip should stop before rule action")

    def forbidden_sync_call(*args, **kwargs):
        calls.append(args)
        raise AssertionError("global title skip should stop before rule action")

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(poller, "get_mr_changes", forbidden_async_call)
    monkeypatch.setattr(poller, "dispatch_notifications", forbidden_async_call)
    monkeypatch.setattr(poller, "post_merge_request_discussion", forbidden_async_call)
    monkeypatch.setattr(poller, "check_pipeline_job_failed", forbidden_async_call)
    monkeypatch.setattr(poller, "review_xlsx_mr", forbidden_async_call)
    monkeypatch.setattr(poller, "review_mr", forbidden_async_call)
    monkeypatch.setattr(poller, "is_title_valid", forbidden_sync_call)

    asyncio.run(
        poller.poll_once(
            [
                {
                    "id": rule_id,
                    "target_branch": "master",
                    "mr_state": "opened",
                    "action_type": action_type,
                }
                for rule_id, action_type in zip(rule_ids, action_types)
            ]
        )
    )

    assert calls == []
