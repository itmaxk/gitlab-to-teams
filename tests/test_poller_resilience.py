import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from services import poller


def test_poll_once_marks_mr_processed_even_when_audit_log_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

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


def test_poll_once_initializes_merged_cursor_without_processing_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, action_type, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Merged changelog rule",
            "Merged rules should run once per MR and then be remembered",
            1,
            "changelogs/unreleased/*.md",
            "type: breaking",
            "contains",
            "master",
            "merged",
            "notify",
            0,
        ),
    )
    rule_id = cur.lastrowid
    conn.commit()
    conn.close()

    poller._project_id = None
    change_calls = []

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(
        project_id,
        state,
        target_branch,
        updated_after="",
        order_by="updated_at",
    ):
        assert updated_after == ""
        assert order_by == "merged_at"
        return [
            {
                "iid": 5636,
                "title": "ADIRGSLSUPP-968: test-idsrv-config",
                "web_url": "https://example.test/mr/5636",
                "state": state,
                "source_branch": "feature/ADIRGSLSUPP-968-test-idsrv-config",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2023-12-04T00:00:00Z",
                "merged_at": "2023-12-04T13:55:27Z",
            }
        ]

    async def fake_get_mr_changes(project_id, mr_iid):
        change_calls.append((project_id, mr_iid))
        return ["README.md", "configuration/service.json"]

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(poller, "get_mr_changes", fake_get_mr_changes)

    rule = {
        "id": rule_id,
        "target_branch": "master",
        "mr_state": "merged",
        "action_type": "notify",
    }
    asyncio.run(poller.poll_once([rule]))

    assert change_calls == []

    conn = db.get_db()
    cursor_row = conn.execute(
        "SELECT value FROM global_settings WHERE key = ?",
        (poller.MERGED_MR_POLL_CURSORS_KEY,),
    ).fetchone()
    processed = conn.execute(
        "SELECT 1 FROM processed_mrs WHERE rule_id = ? AND mr_iid = ?",
        (rule_id, 5636),
    ).fetchone()
    polled_rows = conn.execute(
        "SELECT 1 FROM polled_mrs WHERE mr_iid = ?",
        (5636,),
    ).fetchall()
    conn.close()

    assert cursor_row is not None
    assert '"master": "2023-12-04T13:55:27Z"' in cursor_row["value"]
    assert processed is None
    assert polled_rows == []


def test_poll_once_marks_new_merged_notify_mr_processed_after_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    poller._set_merged_mr_poll_cursor("master", "2023-12-04T13:55:27Z")

    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, action_type, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Merged changelog rule",
            "Merged rules should run once per MR and then be remembered",
            1,
            "changelogs/unreleased/*.md",
            "type: breaking",
            "contains",
            "master",
            "merged",
            "notify",
            0,
        ),
    )
    rule_id = cur.lastrowid
    conn.commit()
    conn.close()

    poller._project_id = None
    change_calls = []
    notification_calls = []

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(
        project_id,
        state,
        target_branch,
        updated_after="",
        order_by="updated_at",
    ):
        assert updated_after == "2023-12-04T13:55:27Z"
        assert order_by == "merged_at"
        return [
            {
                "iid": 5636,
                "title": "ADIRGSLSUPP-968: test-idsrv-config",
                "web_url": "https://example.test/mr/5636",
                "state": state,
                "source_branch": "feature/ADIRGSLSUPP-968-test-idsrv-config",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2023-12-04T00:00:00Z",
                "merged_at": "2023-12-04T13:55:27Z",
            },
            {
                "iid": 5637,
                "title": "ADIRGSLSUPP-969: next merged MR",
                "web_url": "https://example.test/mr/5637",
                "state": state,
                "source_branch": "feature/ADIRGSLSUPP-969-next",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2026-05-08T00:00:00Z",
                "merged_at": "2026-05-08T13:55:27Z",
            },
        ]

    async def fake_get_mr_changes(project_id, mr_iid):
        change_calls.append((project_id, mr_iid))
        return ["README.md", "configuration/service.json"]

    async def fake_dispatch_notifications(matches, mr_iid, mr_title, mr_url, force=False):
        notification_calls.append((matches, mr_iid))

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(poller, "get_mr_changes", fake_get_mr_changes)
    monkeypatch.setattr(poller, "dispatch_notifications", fake_dispatch_notifications)

    rule = {
        "id": rule_id,
        "target_branch": "master",
        "mr_state": "merged",
        "action_type": "notify",
    }
    asyncio.run(poller.poll_once([rule]))

    assert change_calls == [(26, 5637)]
    assert notification_calls == []

    conn = db.get_db()
    processed = conn.execute(
        "SELECT 1 FROM processed_mrs WHERE rule_id = ? AND mr_iid = ?",
        (rule_id, 5637),
    ).fetchone()
    polled_rows = conn.execute(
        "SELECT mr_iid, rules_checked, rules_matched, mr_state FROM polled_mrs ORDER BY mr_iid",
    ).fetchall()
    cursor_row = conn.execute(
        "SELECT value FROM global_settings WHERE key = ?",
        (poller.MERGED_MR_POLL_CURSORS_KEY,),
    ).fetchone()
    conn.close()

    assert processed is not None
    assert [dict(row) for row in polled_rows] == [
        {"mr_iid": 5637, "rules_checked": 1, "rules_matched": 0, "mr_state": "merged"}
    ]
    assert '"master": "2026-05-08T13:55:27Z"' in cursor_row["value"]


def test_poll_once_global_title_skip_blocks_all_rule_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    conn.execute(
        "INSERT INTO global_settings (key, value) VALUES (?, ?)",
        ("global_title_excludes", "[skip_changelog]"),
    )
    action_types = [
        "notify",
        "title_check",
        "pipeline_check",
        "pipeline_job_retry",
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
    monkeypatch.setattr(poller, "retry_failed_config_jobs", forbidden_async_call)
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


def test_poll_once_title_check_reports_changed_invalid_open_title(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, action_type, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "MR title check",
            "Reports bad MR title format",
            1,
            "*",
            "",
            "contains",
            "master",
            "opened",
            "title_check",
            0,
        ),
    )
    rule_id = cur.lastrowid
    conn.execute(
        "INSERT INTO processed_mrs (rule_id, mr_iid) VALUES (?, ?)",
        (rule_id, 125),
    )
    conn.execute(
        """INSERT INTO notification_log
           (rule_id, mr_iid, mr_title, mr_url, file_path, file_content,
            teams_sent, email_sent, gitlab_sent, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_id,
            125,
            "ADIRGSLSUPP-6752 Economic parameters",
            "https://example.test/mr/125",
            "title_check",
            "old title error",
            0,
            0,
            1,
            "",
        ),
    )
    conn.commit()
    conn.close()

    poller._project_id = None
    discussions = []

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch):
        return [
            {
                "iid": 125,
                "title": "ADIRGSLSUPP-6752 : Economic parameters",
                "web_url": "https://example.test/mr/125",
                "state": state,
                "source_branch": "feature/branch",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2026-04-29T00:00:00Z",
            }
        ]

    async def fake_post_merge_request_discussion(mr_iid, comment_body):
        discussions.append((mr_iid, comment_body))

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(
        poller, "post_merge_request_discussion", fake_post_merge_request_discussion
    )

    asyncio.run(
        poller.poll_once(
            [
                {
                    "id": rule_id,
                    "target_branch": "master",
                    "mr_state": "opened",
                    "action_type": "title_check",
                }
            ]
        )
    )

    assert len(discussions) == 1
    assert discussions[0][0] == 125
    assert "JIRA-TASK: Short description" in discussions[0][1]


def test_poll_once_title_check_resolves_thread_when_title_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, action_type, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "MR title check",
            "Reports bad MR title format",
            1,
            "*",
            "",
            "contains",
            "master",
            "opened",
            "title_check",
            0,
        ),
    )
    rule_id = cur.lastrowid
    conn.execute(
        """INSERT INTO notification_log
           (rule_id, mr_iid, mr_title, mr_url, file_path, file_content,
            teams_sent, email_sent, gitlab_sent, gitlab_discussion_id, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_id,
            126,
            "ADIRGSLSUPP-6752 : Economic parameters",
            "https://example.test/mr/126",
            "title_check",
            "old title error",
            0,
            0,
            1,
            "discussion-1",
            "",
        ),
    )
    conn.commit()
    conn.close()

    poller._project_id = None
    resolved = []

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch):
        return [
            {
                "iid": 126,
                "title": "ADIRGSLSUPP-6752: Economic parameters",
                "web_url": "https://example.test/mr/126",
                "state": state,
                "source_branch": "feature/branch",
                "target_branch": target_branch,
                "author": {"name": "Dev"},
                "created_at": "2026-04-29T00:00:00Z",
            }
        ]

    async def fake_resolve_merge_request_discussion(mr_iid, discussion_id):
        resolved.append((mr_iid, discussion_id))
        return {"id": discussion_id, "resolved": True}

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(
        poller,
        "resolve_merge_request_discussion",
        fake_resolve_merge_request_discussion,
    )

    asyncio.run(
        poller.poll_once(
            [
                {
                    "id": rule_id,
                    "target_branch": "master",
                    "mr_state": "opened",
                    "action_type": "title_check",
                }
            ]
        )
    )

    assert resolved == [(126, "discussion-1")]
    conn = db.get_db()
    row = conn.execute(
        "SELECT 1 FROM notification_log WHERE rule_id = ? AND mr_iid = ?",
        (rule_id, 126),
    ).fetchone()
    conn.close()
    assert row is None


def test_poll_once_pipeline_job_retry_is_not_mr_processed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_retry_fresh_packages",),
    ).fetchone()["id"]
    conn.close()

    poller._project_id = None
    retry_calls = []

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch=None):
        return [
            {
                "iid": 127,
                "title": "ADIRGSLSUPP-6752: Economic parameters",
                "web_url": "https://example.test/mr/127",
                "state": state,
                "source_branch": "feature/branch",
                "target_branch": "master",
                "author": {"name": "Dev"},
                "created_at": "2026-04-29T00:00:00Z",
            }
        ]

    async def fake_retry_failed_config_jobs(project_id, mr_iid, job_names, rule_id):
        retry_calls.append((project_id, mr_iid, job_names, rule_id))

        class Result:
            retried = [{"job_id": 101}]

        return Result()

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(
        poller, "retry_failed_config_jobs", fake_retry_failed_config_jobs
    )

    asyncio.run(
        poller.poll_once(
            [
                {
                    "id": rule_id,
                    "target_branch": "*",
                    "mr_state": "opened",
                    "action_type": "pipeline_job_retry",
                    "content_match": "config:check-uncommitted,config:validate",
                }
            ]
        )
    )

    assert retry_calls == [
        (
            26,
            127,
            ["config:check-uncommitted", "config:validate"],
            rule_id,
        )
    ]
    conn = db.get_db()
    processed = conn.execute(
        "SELECT 1 FROM processed_mrs WHERE rule_id = ? AND mr_iid = ?",
        (rule_id, 127),
    ).fetchone()
    conn.close()
    assert processed is None
