from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Optional

from db import get_db

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_db()
    logs = conn.execute(
        """SELECT l.*, r.name as rule_name
           FROM notification_log l
           LEFT JOIN notification_rules r ON r.id = l.rule_id
           ORDER BY l.created_at DESC LIMIT 50"""
    ).fetchall()
    stats = {
        "rules_count": conn.execute("SELECT COUNT(*) FROM notification_rules").fetchone()[0],
        "notifications_count": conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0],
    }
    conn.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "logs": [dict(r) for r in logs], "stats": stats
    })


@router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request):
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
        d["send_email"] = bool(d["send_email"])
        rules.append(d)
    conn.close()
    return templates.TemplateResponse(request, "rules/list.html", {
        "rules": rules
    })


@router.get("/rules/new", response_class=HTMLResponse)
def new_rule_form(request: Request):
    return templates.TemplateResponse(request, "rules/form.html", {
        "rule": None
    })


@router.post("/rules/new/save")
async def save_new_rule(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    file_pattern: str = Form("changelogs/unreleased/*.md"),
    content_match: str = Form("type: breaking"),
    match_type: str = Form("contains"),
    teams_webhook_url: str = Form(""),
    send_email: Optional[str] = Form(None),
):
    form = await request.form()
    emails = form.getlist("emails")

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO notification_rules
           (name, description, file_pattern, content_match, match_type, teams_webhook_url, send_email)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, description, file_pattern, content_match, match_type, teams_webhook_url, 1 if send_email else 0),
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
    rule["send_email"] = bool(rule["send_email"])
    conn.close()
    return templates.TemplateResponse(request, "rules/form.html", {
        "rule": rule
    })


@router.post("/rules/{rule_id}/save")
async def save_edit_rule(
    request: Request,
    rule_id: int,
    name: str = Form(...),
    description: str = Form(""),
    file_pattern: str = Form("changelogs/unreleased/*.md"),
    content_match: str = Form("type: breaking"),
    match_type: str = Form("contains"),
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
           match_type=?, teams_webhook_url=?, send_email=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (name, description, 1 if enabled else 0, file_pattern, content_match,
         match_type, teams_webhook_url, 1 if send_email else 0, rule_id),
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
