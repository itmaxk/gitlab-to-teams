import os
import asyncio
import re

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from db import get_db
from services.gitlab_client import get_branches, get_project_id
from services.poller import _get_merged_mr_poll_cursors
from services.review_config import is_review_llm_configured
from services.rule_store import get_rule_aggregate, list_rule_aggregates, upsert_rule_aggregate

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _branch_created_at(branch: dict) -> str:
    commit = branch.get("commit") or {}
    return (
        branch.get("created_at")
        or commit.get("created_at")
        or commit.get("committed_date")
        or commit.get("authored_date")
        or ""
    )


async def _load_latest_release_branches() -> list[dict]:
    project_id = await get_project_id()
    release_re = re.compile(r"^release/(\d+)$")
    releases = []
    page = 1
    while True:
        batch = await get_branches(project_id, search="release/", per_page=100, page=page)
        if not batch:
            break
        for branch in batch:
            match = release_re.match(branch.get("name", ""))
            if match:
                releases.append({
                    "version": int(match.group(1)),
                    "name": branch.get("name", ""),
                    "created_at": _branch_created_at(branch),
                })
        if len(batch) < 100:
            break
        page += 1

    releases.sort(key=lambda item: item["version"], reverse=True)
    return releases[:2]


def _latest_release_branch_dates() -> list[dict]:
    try:
        return asyncio.run(_load_latest_release_branches())
    except Exception:
        return []


def _merged_cursor_rows(conn, merged_cursors: dict[str, str]) -> list[dict]:
    rows = []
    for branch, merged_at in sorted(merged_cursors.items()):
        row = conn.execute(
            """SELECT mr_iid, mr_url
               FROM polled_mrs
               WHERE target_branch = ?
                 AND LOWER(mr_state) = 'merged'
                 AND mr_merged_at = ?
               ORDER BY polled_at DESC
               LIMIT 1""",
            (branch, merged_at),
        ).fetchone()
        rows.append({
            "branch": branch,
            "merged_at": merged_at,
            "mr_iid": row["mr_iid"] if row else "",
            "mr_url": row["mr_url"] if row else "",
        })
    return rows


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
        "rules_count": conn.execute(
            "SELECT COUNT(*) FROM notification_rules"
        ).fetchone()[0],
        "notifications_count": conn.execute(
            "SELECT COUNT(*) FROM notification_log"
        ).fetchone()[0],
    }
    conn.close()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "logs": [dict(r) for r in logs],
            "stats": stats,
            "rules_list": [dict(r) for r in rules_list],
            "filters": {
                "rule_id": rule_id,
                "teams_sent": teams_sent,
                "email_sent": email_sent,
                "has_error": has_error,
            },
        },
    )


@router.get("/polled", response_class=HTMLResponse)
def polled_mrs(
    request: Request,
    mr_state: str = "",
    success: int = -1,
    has_matches: int = -1,
    target_branch: str = "",
    show_all: int = 0,
):
    conn = get_db()

    where_clauses = ["1=1"]
    params: list = []
    recent_merged_from = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    show_all_enabled = str(show_all).lower() in {"1", "true", "yes", "on"}
    if not show_all_enabled:
        where_clauses.append("""(
            LOWER(mr_state) IN ('open', 'opened')
            OR (
                LOWER(mr_state) = 'merged'
                AND COALESCE(NULLIF(mr_merged_at, ''), NULLIF(mr_created_at, ''), '') >= ?
            )
        )""")
        params.append(recent_merged_from)
    if mr_state:
        where_clauses.append("LOWER(mr_state) = LOWER(?)")
        params.append(mr_state)
    if success >= 0:
        where_clauses.append("success = ?")
        params.append(success)
    if has_matches == 1:
        where_clauses.append("rules_matched > 0")
    elif has_matches == 0:
        where_clauses.append("rules_matched = 0")
    if target_branch:
        where_clauses.append("target_branch = ?")
        params.append(target_branch)
    where_sql = " AND ".join(where_clauses)
    latest_polled_cte = """
        WITH latest_polled AS (
            SELECT p.*
            FROM polled_mrs p
            JOIN (
                SELECT mr_iid, MAX(id) AS latest_id
                FROM polled_mrs
                GROUP BY mr_iid
            ) latest ON latest.latest_id = p.id
        )
    """
    query = (
        f"{latest_polled_cte} "
        f"""SELECT * FROM latest_polled WHERE {where_sql}
            ORDER BY
                CASE WHEN LOWER(mr_state) IN ('open', 'opened') THEN 0 ELSE 1 END,
                polled_at DESC,
                id DESC"""
    )

    rows = conn.execute(query, params).fetchall()
    total = conn.execute(
        f"{latest_polled_cte} SELECT COUNT(*) FROM latest_polled WHERE {where_sql}",
        params,
    ).fetchone()[0]
    success_count = conn.execute(
        f"{latest_polled_cte} SELECT COUNT(*) FROM latest_polled WHERE {where_sql} AND success = 1",
        params,
    ).fetchone()[0]
    merged_cursors = _get_merged_mr_poll_cursors()
    merged_cursor_rows = _merged_cursor_rows(conn, merged_cursors)
    conn.close()
    release_branches = _latest_release_branch_dates()

    return templates.TemplateResponse(
        request,
        "polled.html",
        {
            "rows": [dict(r) for r in rows],
            "stats": {
                "total": total,
                "success": success_count,
                "errors": total - success_count,
                "merged_cursors": merged_cursor_rows,
                "release_branches": release_branches,
            },
            "filters": {
                "mr_state": mr_state,
                "success": success,
                "has_matches": has_matches,
                "target_branch": target_branch,
                "show_all": 1 if show_all_enabled else 0,
                "recent_merged_from": recent_merged_from,
            },
        },
    )


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "jira_url": os.getenv("JIRA_URL", ""),
            "jira_project": os.getenv("JIRA_PROJECT", ""),
        },
    )


@router.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request):
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "jira_url": os.getenv("JIRA_URL", ""),
            "jira_project": os.getenv("JIRA_PROJECT", ""),
        },
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    llm_configured = is_review_llm_configured()
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "llm_configured": llm_configured,
        },
    )


ENV_KEYS = [
    "GITLAB_URL",
    "GITLAB_PROJECT",
    "GITLAB_TOKEN",
    "TEAMS_WEBHOOK_URL",
    "POLL_INTERVAL_SECONDS",
    "DEFAULT_EMAIL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "JIRA_URL",
    "JIRA_PROJECT",
    "JIRA_TOKEN",
    "REVIEW_API_URL",
    "REVIEW_API_KEY",
    "REVIEW_MODEL",
    "REVIEW_MAX_DIFF_CHARS",
    "REVIEW_BATCH_MAX_CHARS",
    "REVIEW_LLM_READ_TIMEOUT",
    "SONAR_URL",
    "SONAR_PROJECT",
    "SONAR_TOKEN",
    "HOST",
    "PORT",
]


@router.get("/schema", response_class=HTMLResponse)
def schema_page(request: Request):
    return templates.TemplateResponse(request, "schema.html", {})


@router.get("/database", response_class=HTMLResponse)
def database_page(request: Request):
    return templates.TemplateResponse(request, "database.html", {})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    env = {key: os.getenv(key, "") for key in ENV_KEYS}
    return templates.TemplateResponse(request, "settings.html", {"env": env})


@router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request):
    default_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    conn = get_db()
    rows = list_rule_aggregates(conn)
    rules = []
    for r in rows:
        d = dict(r)
        d["emails"] = d.get("recipients", [])
        d["enabled"] = bool(d["enabled"])
        d["send_teams"] = bool(d.get("send_teams", 0))
        d["send_email"] = bool(d.get("send_email", 0))
        d["send_gitlab"] = bool(d.get("send_gitlab", 0))
        d["file_check_enabled"] = bool(d.get("file_check_enabled", 0))
        d["action_type"] = d.get("action_type", "notify") or "notify"
        d["effective_interval"] = d["poll_interval_seconds"] or default_interval
        rules.append(d)
    conn.close()
    return templates.TemplateResponse(request, "rules/list.html", {"rules": rules})


@router.get("/rules/new", response_class=HTMLResponse)
def new_rule_form(request: Request):
    return templates.TemplateResponse(
        request,
        "rules/form.html",
        {
            "rule": None,
            "mr_states": ["merged", "opened", "closed", "all"],
        },
    )


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
    project_keys: str = Form("*"),
    file_check_enabled: Optional[str] = Form(None),
    file_check_path_prefix: str = Form(""),
    file_check_mode: str = Form("present"),
    title_exclude: str = Form(""),
    action_type: str = Form("notify"),
    send_teams: Optional[str] = Form(None),
    teams_webhook_url: str = Form(""),
    send_email: Optional[str] = Form(None),
    send_gitlab: Optional[str] = Form(None),
    gitlab_comment_mode: str = Form("note"),
    gitlab_comment_template: str = Form(""),
    pipeline_job_name: str = Form("changelog:validate"),
    pipeline_retry_jobs: str = Form(""),
    retry_trace_marker: str = Form("[5/5] Building fresh packages..."),
    retry_trace_matcher_regex: str = Form("TLS socket disconnected|ECONNRESET|connection reset"),
    review_base_ref: str = Form(""),
    review_custom_prompt: str = Form(""),
):
    form = await request.form()
    emails = form.getlist("emails")

    conn = get_db()
    upsert_rule_aggregate(conn, {
        "name": name,
        "description": description,
        "file_pattern": file_pattern,
        "content_match": content_match,
        "content_exclude": content_exclude,
        "match_type": match_type,
        "target_branch": target_branch,
        "mr_state": mr_state,
        "poll_interval_seconds": poll_interval_seconds,
        "project_keys": project_keys,
        "file_check_enabled": bool(file_check_enabled),
        "file_check_path_prefix": file_check_path_prefix,
        "file_check_mode": file_check_mode,
        "title_exclude": title_exclude,
        "action_type": action_type,
        "send_teams": bool(send_teams),
        "teams_webhook_url": teams_webhook_url,
        "send_email": bool(send_email),
        "send_gitlab": bool(send_gitlab),
        "gitlab_comment_mode": gitlab_comment_mode,
        "gitlab_comment_template": gitlab_comment_template,
        "pipeline_job_name": pipeline_job_name,
        "pipeline_retry_jobs": pipeline_retry_jobs,
        "retry_trace_marker": retry_trace_marker,
        "retry_trace_matcher_regex": retry_trace_matcher_regex,
        "review_base_ref": review_base_ref,
        "review_custom_prompt": review_custom_prompt,
        "emails": emails,
    })
    conn.commit()
    conn.close()
    return RedirectResponse(url="/rules", status_code=303)


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_rule_form(request: Request, rule_id: int):
    conn = get_db()
    rule = get_rule_aggregate(conn, rule_id)
    if not rule:
        conn.close()
        return RedirectResponse(url="/rules", status_code=303)
    rule["emails"] = rule.get("recipients", [])
    rule["enabled"] = bool(rule["enabled"])
    rule["send_teams"] = bool(rule.get("send_teams", 1))
    rule["send_email"] = bool(rule["send_email"])
    rule["send_gitlab"] = bool(rule.get("send_gitlab", 0))
    rule["file_check_enabled"] = bool(rule.get("file_check_enabled", 0))
    rule["action_type"] = rule.get("action_type", "notify") or "notify"
    conn.close()
    return templates.TemplateResponse(
        request,
        "rules/form.html",
        {
            "rule": rule,
            "mr_states": ["merged", "opened", "closed", "all"],
        },
    )


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
    project_keys: str = Form("*"),
    file_check_enabled: Optional[str] = Form(None),
    file_check_path_prefix: str = Form(""),
    file_check_mode: str = Form("present"),
    title_exclude: str = Form(""),
    action_type: str = Form("notify"),
    send_teams: Optional[str] = Form(None),
    teams_webhook_url: str = Form(""),
    send_email: Optional[str] = Form(None),
    send_gitlab: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),
    gitlab_comment_mode: str = Form("note"),
    gitlab_comment_template: str = Form(""),
    pipeline_job_name: str = Form("changelog:validate"),
    pipeline_retry_jobs: str = Form(""),
    retry_trace_marker: str = Form("[5/5] Building fresh packages..."),
    retry_trace_matcher_regex: str = Form("TLS socket disconnected|ECONNRESET|connection reset"),
    review_base_ref: str = Form(""),
    review_custom_prompt: str = Form(""),
):
    form = await request.form()
    emails = form.getlist("emails")

    conn = get_db()
    upsert_rule_aggregate(conn, {
        "name": name,
        "description": description,
        "enabled": bool(enabled),
        "file_pattern": file_pattern,
        "content_match": content_match,
        "content_exclude": content_exclude,
        "match_type": match_type,
        "target_branch": target_branch,
        "mr_state": mr_state,
        "poll_interval_seconds": poll_interval_seconds,
        "project_keys": project_keys,
        "file_check_enabled": bool(file_check_enabled),
        "file_check_path_prefix": file_check_path_prefix,
        "file_check_mode": file_check_mode,
        "title_exclude": title_exclude,
        "action_type": action_type,
        "send_teams": bool(send_teams),
        "teams_webhook_url": teams_webhook_url,
        "send_email": bool(send_email),
        "send_gitlab": bool(send_gitlab),
        "gitlab_comment_mode": gitlab_comment_mode,
        "gitlab_comment_template": gitlab_comment_template,
        "pipeline_job_name": pipeline_job_name,
        "pipeline_retry_jobs": pipeline_retry_jobs,
        "retry_trace_marker": retry_trace_marker,
        "retry_trace_matcher_regex": retry_trace_matcher_regex,
        "review_base_ref": review_base_ref,
        "review_custom_prompt": review_custom_prompt,
        "emails": emails,
    }, rule_id)
    conn.commit()
    conn.close()
    return RedirectResponse(url="/rules", status_code=303)
