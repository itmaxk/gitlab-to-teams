import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from models import ReviewPublishFindingRequest
from routers import review as review_router


def test_publish_review_finding_comment_posts_single_discussion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-finding.db")
    db.init_db()
    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO code_reviews (mr_iid, mr_title, mr_url, model_used, findings_json, summary_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            42,
            "MR title",
            "https://gitlab.example/mr/42",
            "local-model",
            json.dumps(
                [
                    {
                        "severity": "error",
                        "category": "logic",
                        "file_path": "config/attribute.json",
                        "line": 11,
                        "message": "Bad attribute",
                        "suggestion": "Fix attribute",
                    }
                ]
            ),
            "{}",
        ),
    )
    conn.commit()
    review_id = cur.lastrowid
    conn.close()

    posted = {}

    async def fake_post_merge_request_discussion(mr_iid, body):
        posted["mr_iid"] = mr_iid
        posted["body"] = body
        return {"id": "discussion-1"}

    monkeypatch.setattr(
        review_router,
        "post_merge_request_discussion",
        fake_post_merge_request_discussion,
    )

    result = asyncio.run(
        review_router.publish_review_finding_comment(
            ReviewPublishFindingRequest(review_id=review_id, finding_index=0)
        )
    )

    assert result["ok"] is True
    assert result["discussion_id"] == "discussion-1"
    assert posted["mr_iid"] == 42
    assert "`config/attribute.json:11`" in posted["body"]
    assert "Bad attribute" in posted["body"]
    assert "Resolve" in posted["body"]
