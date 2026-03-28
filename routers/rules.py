import os

from fastapi import APIRouter, HTTPException

from db import get_db
from models import RuleCreate, RuleUpdate
from services.teams_client import send_teams_notification
from services.notification_dispatcher import dispatch_notifications

router = APIRouter(prefix="/api/rules", tags=["rules"])

MR_STATES = ["merged", "opened", "closed", "all"]


@router.get("/logs/recent")
def recent_logs(rule_id: int = 0, teams_sent: int = -1, email_sent: int = -1, limit: int = 100) -> list[dict]:
    conn = get_db()
    query = """SELECT l.*, r.name as rule_name
               FROM notification_log l
               LEFT JOIN notification_rules r ON r.id = l.rule_id
               WHERE 1=1"""
    params = []
    if rule_id > 0:
        query += " AND l.rule_id = ?"
        params.append(rule_id)
    if teams_sent >= 0:
        query += " AND l.teams_sent = ?"
        params.append(teams_sent)
    if email_sent >= 0:
        query += " AND l.email_sent = ?"
        params.append(email_sent)
    query += " ORDER BY l.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
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
    d["send_teams"] = bool(d.get("send_teams", 1))
    d["send_email"] = bool(d["send_email"])
    d["file_check_enabled"] = bool(d.get("file_check_enabled", 0))
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
           (name, description, file_pattern, content_match, content_exclude, match_type,
            target_branch, mr_state, poll_interval_seconds,
            file_check_enabled, file_check_path_prefix, file_check_mode,
            send_teams, teams_webhook_url, send_email)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.name, data.description, data.file_pattern,
            data.content_match, data.content_exclude, data.match_type,
            data.target_branch, data.mr_state, data.poll_interval_seconds,
            int(data.file_check_enabled), data.file_check_path_prefix,
            data.file_check_mode,
            int(data.send_teams), data.teams_webhook_url, int(data.send_email),
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
           content_exclude=?, match_type=?, target_branch=?, mr_state=?, poll_interval_seconds=?,
           file_check_enabled=?, file_check_path_prefix=?, file_check_mode=?,
           send_teams=?, teams_webhook_url=?, send_email=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            data.name, data.description, int(data.enabled),
            data.file_pattern, data.content_match, data.content_exclude, data.match_type,
            data.target_branch, data.mr_state, data.poll_interval_seconds,
            int(data.file_check_enabled), data.file_check_path_prefix,
            data.file_check_mode,
            int(data.send_teams), data.teams_webhook_url, int(data.send_email), rule_id,
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


@router.post("/{rule_id}/copy")
def copy_rule(rule_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Rule not found")

    src = dict(row)
    cur = conn.execute(
        """INSERT INTO notification_rules
           (name, description, file_pattern, content_match, match_type,
            target_branch, mr_state, poll_interval_seconds,
            file_check_enabled, file_check_path_prefix,
            teams_webhook_url, send_email, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            src["name"] + " (копия)", src["description"], src["file_pattern"],
            src["content_match"], src["match_type"],
            src["target_branch"], src["mr_state"], src["poll_interval_seconds"],
            src["file_check_enabled"], src["file_check_path_prefix"],
            src["teams_webhook_url"], src["send_email"],
        ),
    )
    new_id = cur.lastrowid
    emails = conn.execute(
        "SELECT email FROM email_recipients WHERE rule_id = ?", (rule_id,)
    ).fetchall()
    for e in emails:
        conn.execute(
            "INSERT OR IGNORE INTO email_recipients (email, rule_id) VALUES (?, ?)",
            (e["email"], new_id),
        )
    conn.commit()
    new_row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (new_id,)
    ).fetchone()
    conn.close()
    return _rule_to_out(new_row)


@router.post("/{rule_id}/test")
async def test_rule(rule_id: int):
    from services.email_client import send_changelog_email as send_email_fn

    conn = get_db()
    raw = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not raw:
        conn.close()
        raise HTTPException(status_code=404, detail="Rule not found")

    row = dict(raw)
    emails_rows = conn.execute(
        "SELECT email FROM email_recipients WHERE rule_id = ?", (rule_id,)
    ).fetchall()
    conn.close()

    send_teams = bool(row.get("send_teams", 1))
    send_email = bool(row.get("send_email", 0))

    if not send_teams and not send_email:
        raise HTTPException(status_code=400, detail="Ни Teams, ни Email не включены в правиле")

    test_data = {
        "mr_title": "Test MR #0 — Тестовое уведомление",
        "mr_url": "https://gitlab.example.com/test/mr/0",
        "file_path": "changelogs/unreleased/test-001.md",
        "file_content": "Это тестовое сообщение.\n\ntype: breaking\n",
        "rule_name": row["name"],
    }
    results = []

    if send_teams:
        webhook_url = row["teams_webhook_url"] or os.getenv("TEAMS_WEBHOOK_URL", "")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="Teams включён, но webhook URL не настроен")
        await send_teams_notification(webhook_url=webhook_url, **test_data)
        results.append("teams")

    if send_email:
        emails = [e["email"] for e in emails_rows]
        if not emails:
            default_email = os.getenv("DEFAULT_EMAIL", "")
            if default_email:
                emails = [e.strip() for e in default_email.split(",") if e.strip()]
        if not emails:
            raise HTTPException(status_code=400, detail="Email включён, но получатели не настроены")
        send_email_fn(recipients=emails, **test_data)
        results.append("email")

    return {"status": "sent", "channels": results}


@router.post("/logs/{log_id}/resend")
async def resend_notification(log_id: int):
    conn = get_db()
    row = conn.execute(
        """SELECT l.*, r.name as rule_name, r.send_teams, r.teams_webhook_url, r.send_email
           FROM notification_log l
           LEFT JOIN notification_rules r ON r.id = l.rule_id
           WHERE l.id = ?""",
        (log_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Log entry not found")

    log = dict(row)
    emails_rows = conn.execute(
        "SELECT email FROM email_recipients WHERE rule_id = ?", (log["rule_id"],)
    ).fetchall()
    conn.close()

    emails = [e["email"] for e in emails_rows]
    if not emails:
        default_email = os.getenv("DEFAULT_EMAIL", "")
        if default_email:
            emails = [e.strip() for e in default_email.split(",") if e.strip()]

    match = {
        "rule": {
            "id": log["rule_id"],
            "name": log.get("rule_name", ""),
            "send_teams": log.get("send_teams", 1),
            "teams_webhook_url": log.get("teams_webhook_url", ""),
            "send_email": log.get("send_email", 0),
        },
        "file_path": log["file_path"],
        "file_content": log["file_content"],
        "emails": emails,
    }
    await dispatch_notifications(
        [match], log["mr_iid"], log["mr_title"], log["mr_url"], force=True,
    )
    return {"status": "resent"}
