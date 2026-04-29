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
