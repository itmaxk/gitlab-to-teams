import os

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Optional

from db import get_db

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    rule_id: int = 0,
    teams_sent: int = -1,
    email_sent: int = -1,
    has_error: int = -1,
):
    conn = get_db()

    query = """SELECT l.*, r.name as rule_name
               FROM notification_log l
               LEFT JOIN notification_rules r ON r.id = l.rule_id
               WHERE 1=1"""
    params: list = []
    if rule_id > 0:
        query += " AND l.rule_id = ?"
        params.append(rule_id)
    if teams_sent >= 0:
        query += " AND l.teams_sent = ?"
        params.append(teams_sent)
    if email_sent >= 0:
        query += " AND l.email_sent = ?"
        params.append(email_sent)
    if has_error == 1:
        query += " AND l.error != ''"
    elif has_error == 0:
        query += " AND (l.error = '' OR l.error IS NULL)"
    query += " ORDER BY l.created_at DESC LIMIT 200"

    logs = conn.execute(query, params).fetchall()
    rules_list = conn.execute(
        "SELECT id, name FROM notification_rules ORDER BY name"
    ).fetchall()
    stats = {
        "rules_count": conn.execute("SELECT COUNT(*) FROM notification_rules").fetchone()[0],
        "notifications_count": conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0],
    }
    conn.close()

    return templates.TemplateResponse(request, "dashboard.html", {
        "logs": [dict(r) for r in logs],
        "stats": stats,
        "rules_list": [dict(r) for r in rules_list],
        "filters": {
            "rule_id": rule_id,
            "teams_sent": teams_sent,
            "email_sent": email_sent,
            "has_error": has_error,
        },
    })


@router.get("/polled", response_class=HTMLResponse)
def polled_mrs(
    request: Request,
    mr_state: str = "",
    success: int = -1,
    has_matches: int = -1,
    target_branch: str = "",
):
    conn = get_db()

    query = "SELECT * FROM polled_mrs WHERE 1=1"
    params: list = []
    if mr_state:
        query += " AND mr_state = ?"
        params.append(mr_state)
    if success >= 0:
        query += " AND success = ?"
        params.append(success)
    if has_matches == 1:
        query += " AND rules_matched > 0"
    elif has_matches == 0:
        query += " AND rules_matched = 0"
    if target_branch:
        query += " AND target_branch = ?"
        params.append(target_branch)
    query += " ORDER BY polled_at DESC LIMIT 500"

    rows = conn.execute(query, params).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM polled_mrs").fetchone()[0]
    success_count = conn.execute("SELECT COUNT(*) FROM polled_mrs WHERE success = 1").fetchone()[0]
    conn.close()

    return templates.TemplateResponse(request, "polled.html", {
        "rows": [dict(r) for r in rows],
        "stats": {
            "total": total,
            "success": success_count,
            "errors": total - success_count,
        },
        "filters": {
            "mr_state": mr_state,
            "success": success,
            "has_matches": has_matches,
            "target_branch": target_branch,
        },
    })


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    return templates.TemplateResponse(request, "queue.html", {})


@router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request):
    default_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notification_rules ORDER BY created_at DESC"
    ).fetchall()
    rules = []
    for r in rows:
        d = dict(r)
        emails = conn.execute(
            "SELECT email FROM email_recipients WHERE rule_id = ?", (d["id"],)
        ).fetchall()
        d["emails"] = [e["email"] for e in emails]
        d["enabled"] = bool(d["enabled"])
        d["send_teams"] = bool(d.get("send_teams", 1))
        d["send_email"] = bool(d["send_email"])
        d["file_check_enabled"] = bool(d.get("file_check_enabled", 0))
        d["effective_interval"] = d["poll_interval_seconds"] or default_interval
        rules.append(d)
    conn.close()
    return templates.TemplateResponse(request, "rules/list.html", {
        "rules": rules
    })


@router.get("/rules/new", response_class=HTMLResponse)
def new_rule_form(request: Request):
    return templates.TemplateResponse(request, "rules/form.html", {
        "rule": None,
        "mr_states": ["merged", "opened", "closed", "all"],
    })


@router.post("/rules/new/save")
async def save_new_rule(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    file_pattern: str = Form("changelogs/unreleased/*.md"),
    content_match: str = Form("type: breaking"),
    content_exclude: str = Form(""),
    match_type: str = Form("contains"),
    target_branch: str = Form("master"),
    mr_state: str = Form("merged"),
    poll_interval_seconds: int = Form(0),
    file_check_enabled: Optional[str] = Form(None),
    file_check_path_prefix: str = Form(""),
    file_check_mode: str = Form("present"),
    send_teams: Optional[str] = Form(None),
    teams_webhook_url: str = Form(""),
    send_email: Optional[str] = Form(None),
):
    form = await request.form()
    emails = form.getlist("emails")

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO notification_rules
           (name, description, file_pattern, content_match, content_exclude, match_type,
            target_branch, mr_state, poll_interval_seconds,
            file_check_enabled, file_check_path_prefix, file_check_mode,
            send_teams, teams_webhook_url, send_email)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, description, file_pattern, content_match, content_exclude, match_type,
            target_branch, mr_state, poll_interval_seconds,
            1 if file_check_enabled else 0, file_check_path_prefix, file_check_mode,
            1 if send_teams else 0, teams_webhook_url, 1 if send_email else 0,
        ),
    )
    rule_id = cur.lastrowid
    for email in emails:
        email = email.strip()
        if email:
            conn.execute(
                "INSERT OR IGNORE INTO email_recipients (email, rule_id) VALUES (?, ?)",
                (email, rule_id),
            )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/rules", status_code=303)


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_rule_form(request: Request, rule_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if not row:
        conn.close()
        return RedirectResponse(url="/rules", status_code=303)
    rule = dict(row)
    emails = conn.execute(
        "SELECT email FROM email_recipients WHERE rule_id = ?", (rule_id,)
    ).fetchall()
    rule["emails"] = [e["email"] for e in emails]
    rule["enabled"] = bool(rule["enabled"])
    rule["send_teams"] = bool(rule.get("send_teams", 1))
    rule["send_email"] = bool(rule["send_email"])
    rule["file_check_enabled"] = bool(rule.get("file_check_enabled", 0))
    conn.close()
    return templates.TemplateResponse(request, "rules/form.html", {
        "rule": rule,
        "mr_states": ["merged", "opened", "closed", "all"],
    })


@router.post("/rules/{rule_id}/save")
async def save_edit_rule(
    request: Request,
    rule_id: int,
    name: str = Form(...),
    description: str = Form(""),
    file_pattern: str = Form("changelogs/unreleased/*.md"),
    content_match: str = Form("type: breaking"),
    content_exclude: str = Form(""),
    match_type: str = Form("contains"),
    target_branch: str = Form("master"),
    mr_state: str = Form("merged"),
    poll_interval_seconds: int = Form(0),
    file_check_enabled: Optional[str] = Form(None),
    file_check_path_prefix: str = Form(""),
    file_check_mode: str = Form("present"),
    send_teams: Optional[str] = Form(None),
    teams_webhook_url: str = Form(""),
    send_email: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),
):
    form = await request.form()
    emails = form.getlist("emails")

    conn = get_db()
    conn.execute(
        """UPDATE notification_rules SET
           name=?, description=?, enabled=?, file_pattern=?, content_match=?,
           content_exclude=?, match_type=?, target_branch=?, mr_state=?, poll_interval_seconds=?,
           file_check_enabled=?, file_check_path_prefix=?, file_check_mode=?,
           send_teams=?, teams_webhook_url=?, send_email=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            name, description, 1 if enabled else 0,
            file_pattern, content_match, content_exclude, match_type,
            target_branch, mr_state, poll_interval_seconds,
            1 if file_check_enabled else 0, file_check_path_prefix, file_check_mode,
            1 if send_teams else 0, teams_webhook_url, 1 if send_email else 0, rule_id,
        ),
    )
    conn.execute("DELETE FROM email_recipients WHERE rule_id = ?", (rule_id,))
    for email in emails:
        email = email.strip()
        if email:
            conn.execute(
                "INSERT OR IGNORE INTO email_recipients (email, rule_id) VALUES (?, ?)",
                (email, rule_id),
            )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/rules", status_code=303)
