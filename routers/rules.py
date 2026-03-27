import os

from fastapi import APIRouter, HTTPException

from db import get_db
from models import RuleCreate, RuleUpdate, RuleOut, LogOut
from services.teams_client import send_teams_notification

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("/logs/recent")
def recent_logs() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT l.*, r.name as rule_name
           FROM notification_log l
           LEFT JOIN notification_rules r ON r.id = l.rule_id
           ORDER BY l.created_at DESC
           LIMIT 50"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _rule_to_out(row) -> dict:
    d = dict(row)
    conn = get_db()
    emails = conn.execute(
        "SELECT email FROM email_recipients WHERE rule_id = ?", (d["id"],)
    ).fetchall()
    conn.close()
    d["emails"] = [e["email"] for e in emails]
    d["enabled"] = bool(d["enabled"])
    d["send_email"] = bool(d["send_email"])
    return d


@router.get("")
def list_rules() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notification_rules ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [_rule_to_out(r) for r in rows]


@router.get("/{rule_id}")
def get_rule(rule_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _rule_to_out(row)


@router.post("", status_code=201)
def create_rule(data: RuleCreate) -> dict:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO notification_rules
           (name, description, file_pattern, content_match, match_type, teams_webhook_url, send_email)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            data.name,
            data.description,
            data.file_pattern,
            data.content_match,
            data.match_type,
            data.teams_webhook_url,
            int(data.send_email),
        ),
    )
    rule_id = cur.lastrowid
    for email in data.emails:
        email = email.strip()
        if email:
            conn.execute(
                "INSERT OR IGNORE INTO email_recipients (email, rule_id) VALUES (?, ?)",
                (email, rule_id),
            )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    conn.close()
    return _rule_to_out(row)


@router.put("/{rule_id}")
def update_rule(rule_id: int, data: RuleUpdate) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Rule not found")

    conn.execute(
        """UPDATE notification_rules SET
           name=?, description=?, enabled=?, file_pattern=?, content_match=?,
           match_type=?, teams_webhook_url=?, send_email=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            data.name,
            data.description,
            int(data.enabled),
            data.file_pattern,
            data.content_match,
            data.match_type,
            data.teams_webhook_url,
            int(data.send_email),
            rule_id,
        ),
    )
    conn.execute("DELETE FROM email_recipients WHERE rule_id = ?", (rule_id,))
    for email in data.emails:
        email = email.strip()
        if email:
            conn.execute(
                "INSERT OR IGNORE INTO email_recipients (email, rule_id) VALUES (?, ?)",
                (email, rule_id),
            )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    conn.close()
    return _rule_to_out(row)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int):
    conn = get_db()
    conn.execute("DELETE FROM notification_rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@router.patch("/{rule_id}/toggle")
def toggle_rule(rule_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT enabled FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Rule not found")
    new_val = 0 if row["enabled"] else 1
    conn.execute(
        "UPDATE notification_rules SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_val, rule_id),
    )
    conn.commit()
    conn.close()
    return {"enabled": bool(new_val)}


@router.post("/{rule_id}/test")
async def test_rule(rule_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")

    webhook_url = row["teams_webhook_url"] or os.getenv("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        raise HTTPException(status_code=400, detail="No Teams webhook URL configured")

    await send_teams_notification(
        webhook_url=webhook_url,
        mr_title="Test MR #0 — Тестовое уведомление",
        mr_url="https://gitlab.example.com/test/mr/0",
        file_path="changelogs/unreleased/test-001.md",
        file_content="Это тестовое сообщение.\n\ntype: breaking\n",
        rule_name=row["name"],
    )
    return {"status": "sent"}


