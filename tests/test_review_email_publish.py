import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from models import ReviewEmailRequest, ReviewRunEmailRequest
from routers import review as review_router


def test_send_review_email_uses_review_table_html(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-email.db")
    db.init_db()
    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO code_reviews (mr_iid, mr_title, mr_url, model_used, findings_json, summary_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            77,
            "Email review",
            "https://gitlab.example/mr/77",
            "local-model",
            json.dumps(
                [
                    {
                        "severity": "warning",
                        "category": "bug",
                        "file_path": "service.py",
                        "line": 12,
                        "message": "Check null",
                        "suggestion": "Add guard",
                    }
                ]
            ),
            json.dumps({"errors": 0, "warnings": 1, "info": 0, "total": 1}),
        ),
    )
    conn.commit()
    review_id = cur.lastrowid
    conn.close()

    sent = {}

    def fake_send_html_email(recipients, subject, html_body):
        sent["recipients"] = recipients
        sent["subject"] = subject
        sent["html_body"] = html_body

    monkeypatch.setattr(review_router, "send_html_email", fake_send_html_email)

    result = review_router.send_review_email(
        ReviewEmailRequest(
            review_id=review_id,
            recipients=["lead@example.com", " ", "dev@example.com"],
        )
    )

    assert result["ok"] is True
    assert sent["recipients"] == ["lead@example.com", "dev@example.com"]
    assert "AI review MR !77" in sent["subject"]
    assert "<table" in sent["html_body"]
    assert "service.py:12" in sent["html_body"]
    assert "Check null" in sent["html_body"]


def test_run_review_and_send_email_waits_for_review_then_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-run-email.db")
    db.init_db()

    async def fake_review_mr(mr_iid, custom_prompt):
        conn = db.get_db()
        cur = conn.execute(
            """
            INSERT INTO code_reviews (mr_iid, mr_title, mr_url, model_used, custom_prompt, findings_json, summary_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mr_iid,
                "Combined review",
                "https://gitlab.example/mr/88",
                "local-model",
                custom_prompt,
                json.dumps(
                    [
                        {
                            "severity": "error",
                            "category": "logic",
                            "file_path": "combined.py",
                            "line": 7,
                            "message": "Broken branch",
                            "suggestion": "Fix branch",
                        }
                    ]
                ),
                json.dumps({"errors": 1, "warnings": 0, "info": 0, "total": 1}),
            ),
        )
        conn.commit()
        review_id = cur.lastrowid
        conn.close()
        return {
            "id": review_id,
            "mr": {"iid": mr_iid, "title": "Combined review"},
            "findings": [],
            "summary": {},
            "model_used": "local-model",
        }

    sent = {}

    def fake_send_html_email(recipients, subject, html_body):
        sent["recipients"] = recipients
        sent["subject"] = subject
        sent["html_body"] = html_body

    monkeypatch.setattr(review_router, "review_mr", fake_review_mr)
    monkeypatch.setattr(review_router, "send_html_email", fake_send_html_email)

    result = asyncio.run(
        review_router.run_review_and_send_email(
            ReviewRunEmailRequest(
                mr_input="88",
                custom_prompt="focus logic",
                recipients=["lead@example.com"],
            )
        )
    )

    assert result["ok"] is True
    assert result["review_id"]
    assert result["email"]["recipients"] == ["lead@example.com"]
    assert sent["recipients"] == ["lead@example.com"]
    assert "AI review MR !88" in sent["subject"]
    assert "combined.py:7" in sent["html_body"]
    assert "Broken branch" in sent["html_body"]
