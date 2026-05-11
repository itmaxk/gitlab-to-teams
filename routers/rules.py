import os

from fastapi import APIRouter, HTTPException

from db import get_db, get_global_setting, set_global_setting
from models import RuleCreate, RuleUpdate
from services.teams_client import send_teams_notification
from services.notification_dispatcher import dispatch_notifications
from services.rule_store import (
    get_rule_aggregate,
    list_rule_aggregates,
    load_runtime_rule,
    upsert_rule_aggregate,
)

router = APIRouter(prefix="/api/rules", tags=["rules"])

MR_STATES = ["merged", "opened", "closed", "all"]


@router.get("/logs/recent")
def recent_logs(
    rule_id: int = 0,
    teams_sent: int = -1,
    email_sent: int = -1,
    gitlab_sent: int = -1,
    limit: int = 100,
) -> list[dict]:
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
    if gitlab_sent >= 0:
        query += " AND l.gitlab_sent = ?"
        params.append(gitlab_sent)
    query += " ORDER BY l.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _rule_to_out(row) -> dict:
    raw = dict(row)
    conn = get_db()
    d = get_rule_aggregate(conn, raw["id"])
    conn.close()
    if d is None:
        return {}
    d["emails"] = d.get("recipients", [])
    d["enabled"] = bool(d["enabled"])
    d["send_teams"] = bool(d.get("send_teams", 0))
    d["send_email"] = bool(d.get("send_email", 0))
    d["send_gitlab"] = bool(d.get("send_gitlab", 0))
    d["file_check_enabled"] = bool(d.get("file_check_enabled", 0))
    d["action_type"] = d.get("action_type", "notify") or "notify"
    return d


@router.get("")
def list_rules() -> list[dict]:
    conn = get_db()
    rows = list_rule_aggregates(conn)
    conn.close()
    return [_rule_to_out(r) for r in rows]


@router.get("/global-title-excludes")
def get_global_title_excludes():
    value = get_global_setting("global_title_excludes")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return {"patterns": lines}


@router.put("/global-title-excludes")
def update_global_title_excludes(data: dict):
    patterns = data.get("patterns", [])
    if isinstance(patterns, list):
        value = "\n".join(str(p).strip() for p in patterns if str(p).strip())
    else:
        value = str(patterns)
    set_global_setting("global_title_excludes", value)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return {"patterns": lines}


@router.get("/{rule_id}")
def get_rule(rule_id: int) -> dict:
    conn = get_db()
    row = get_rule_aggregate(conn, rule_id)
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _rule_to_out(row)


@router.post("", status_code=201)
def create_rule(data: dict) -> dict:
    conn = get_db()
    rule_id = upsert_rule_aggregate(conn, data)
    conn.commit()
    row = get_rule_aggregate(conn, rule_id)
    conn.close()
    return _rule_to_out(row)


@router.put("/{rule_id}")
def update_rule(rule_id: int, data: dict) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Rule not found")

    upsert_rule_aggregate(conn, data, rule_id)
    conn.commit()
    row = get_rule_aggregate(conn, rule_id)
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

    src = get_rule_aggregate(conn, rule_id)
    src["name"] = src["name"] + " (копия)"
    src["enabled"] = False
    new_id = upsert_rule_aggregate(conn, src, force_disabled=True)
    conn.commit()
    new_row = get_rule_aggregate(conn, new_id)
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

    row = load_runtime_rule(conn, rule_id)
    conn.close()

    if row.get("action_type") in {"pipeline_job_retry", "sonar_issues"}:
        from services.poller import poll_once

        await poll_once([row])
        return {
            "status": "checked",
            "action": row.get("action_type"),
        }

    send_teams = bool(row.get("send_teams", 1))
    send_email = bool(row.get("send_email", 0))
    send_gitlab = bool(row.get("send_gitlab", 0))

    if not send_teams and not send_email and not send_gitlab:
        raise HTTPException(
            status_code=400,
            detail="Ни Teams, ни Email, ни GitLab не включены в правиле",
        )

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
            raise HTTPException(
                status_code=400, detail="Teams включён, но webhook URL не настроен"
            )
        await send_teams_notification(webhook_url=webhook_url, **test_data)
        results.append("teams")

    if send_email:
        emails = row.get("emails", [])
        if not emails:
            default_email = os.getenv("DEFAULT_EMAIL", "")
            if default_email:
                emails = [e.strip() for e in default_email.split(",") if e.strip()]
        if not emails:
            raise HTTPException(
                status_code=400, detail="Email включён, но получатели не настроены"
            )
        send_email_fn(recipients=emails, match_type=row["match_type"], **test_data)
        results.append("email")

    if send_gitlab:
        from services.gitlab_notes import post_merge_request_note
        from services.review_comment_formatter import format_gitlab_review_comment

        comment = format_gitlab_review_comment(
            mr_iid=0,
            mr_title=test_data["mr_title"],
            findings=[],
            summary={"errors": 0, "warnings": 0, "info": 0, "total": 0},
            model_used="test",
        )
        results.append("gitlab")

    return {"status": "sent", "channels": results}


@router.post("/logs/{log_id}/resend")
async def resend_notification(log_id: int):
    conn = get_db()
    row = conn.execute(
        """SELECT l.*, r.name as rule_name, r.send_teams, r.teams_webhook_url, r.send_email, r.send_gitlab, r.match_type
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
            "send_gitlab": log.get("send_gitlab", 0),
            "match_type": log.get("match_type", ""),
        },
        "file_path": log["file_path"],
        "file_content": log["file_content"],
        "emails": emails,
    }
    await dispatch_notifications(
        [match],
        log["mr_iid"],
        log["mr_title"],
        log["mr_url"],
        force=True,
    )
    return {"status": "resent"}
