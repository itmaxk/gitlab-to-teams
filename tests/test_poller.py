import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import poller


def test_get_mr_file_content_prefers_source_branch(monkeypatch):
    calls = []

    async def fake_get_file_content(project_id, file_path, ref):
        calls.append((project_id, file_path, ref))
        return "source content"

    monkeypatch.setattr(poller, "get_file_content", fake_get_file_content)

    result = asyncio.run(
        poller._get_mr_file_content(
            26,
            101,
            "configuration/app/errorMapping.js",
            "feature/send-request-info",
            "master",
        )
    )

    assert result == "source content"
    assert calls == [
        (26, "configuration/app/errorMapping.js", "feature/send-request-info")
    ]


def test_get_mr_file_content_falls_back_to_target_branch(monkeypatch):
    calls = []

    async def fake_get_file_content(project_id, file_path, ref):
        calls.append((project_id, file_path, ref))
        if ref == "feature/send-request-info":
            raise RuntimeError("404")
        return "target content"

    monkeypatch.setattr(poller, "get_file_content", fake_get_file_content)

    result = asyncio.run(
        poller._get_mr_file_content(
            26,
            101,
            "configuration/app/errorMapping.js",
            "feature/send-request-info",
            "master",
        )
    )

    assert result == "target content"
    assert calls == [
        (26, "configuration/app/errorMapping.js", "feature/send-request-info"),
        (26, "configuration/app/errorMapping.js", "master"),
    ]


def test_pipeline_check_can_publish_custom_regular_gitlab_comment(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, action_type, send_teams, send_email, send_gitlab)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Проверка changelog:validate",
            "Checks changelog validation",
            1,
            "*",
            "changelog:validate",
            "contains",
            "master",
            "opened",
            "pipeline_check",
            0,
            0,
            1,
        ),
    )
    rule_id = cur.lastrowid
    conn.commit()
    conn.close()
    poller._project_id = None

    async def fake_get_project_id():
        return 26

    async def fake_get_merge_requests(project_id, state, target_branch):
        return [
            {
                "iid": 777,
                "title": "ADIRGSLSUPP-777: Validate changelog",
                "web_url": "https://example.test/mr/777",
                "state": state,
                "source_branch": "feature/changelog",
                "target_branch": target_branch,
                "assignees": [{"username": "dev"}],
                "author": {"name": "Dev"},
                "created_at": "2026-05-12T00:00:00Z",
            }
        ]

    async def fake_check_pipeline_job_failed(project_id, mr_iid, job_name):
        return poller.PipelineCheckResult(
            failed=True,
            completed=False,
            job_web_url="https://example.test/job/777",
        )

    notes = []
    discussions = []

    async def fake_post_merge_request_note(mr_iid, body):
        notes.append((mr_iid, body))
        return {"id": 10}

    async def fake_post_merge_request_discussion(mr_iid, body):
        discussions.append((mr_iid, body))
        return {"id": "discussion-10"}

    monkeypatch.setattr(poller, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(poller, "get_merge_requests", fake_get_merge_requests)
    monkeypatch.setattr(poller, "check_pipeline_job_failed", fake_check_pipeline_job_failed)
    monkeypatch.setattr(poller, "post_merge_request_note", fake_post_merge_request_note)
    monkeypatch.setattr(poller, "post_merge_request_discussion", fake_post_merge_request_discussion)

    asyncio.run(
        poller.poll_once(
            [
                {
                    "id": rule_id,
                    "name": "Проверка changelog:validate",
                    "target_branch": "master",
                    "mr_state": "opened",
                    "action_type": "pipeline_check",
                    "content_match": "changelog:validate",
                    "send_gitlab": 1,
                    "gitlab_comment_mode": "note",
                    "gitlab_comment_template": "{mentions} please fix {job_name}: {job_web_url}",
                }
            ]
        )
    )

    assert notes == [
        (
            777,
            "@dev please fix changelog:validate: https://example.test/job/777",
        )
    ]
    assert discussions == []
