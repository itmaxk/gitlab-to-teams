import asyncio
import calendar
import logging
import os
import smtplib
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from db import get_db
from models import (
    NotifyMissingRequest,
    OvertimeDebugRequest,
    ReportRequest,
    ReportSettingsUpdate,
    SendReportRequest,
)
from services import jira_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
_LOOKUP_DIAGNOSTICS_TIMEOUT_SECONDS = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Официальные праздничные дни РФ (фиксированные даты, ст. 112 ТК РФ).
_RU_HOLIDAY_DATES = [
    (1, 1),
    (1, 2),
    (1, 3),
    (1, 4),
    (1, 5),
    (1, 6),
    (1, 7),
    (1, 8),
    (2, 23),
    (3, 8),
    (5, 1),
    (5, 9),
    (6, 12),
    (11, 4),
]


def _load_overrides(year: int) -> dict[str, int]:
    """Загружает переопределения из БД: {dt_str: day_type}.
    day_type: 1=нерабочий (праздник), 0=рабочий (перенос)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT dt, day_type FROM holiday_overrides WHERE dt LIKE ?",
        (f"{year}-%",),
    ).fetchall()
    conn.close()
    return {r["dt"]: r["day_type"] for r in rows}


def is_workday(d: date) -> bool:
    overrides = _load_overrides(d.year)
    dt_str = d.isoformat()
    if dt_str in overrides:
        return overrides[dt_str] == 0  # 0=working, 1=holiday
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in {(m, dy) for m, dy in _RU_HOLIDAY_DATES}:
        return False
    return True


def _get_year_calendar(year: int) -> dict[str, int]:
    """Возвращает {date_str: day_type} для всех дней года.
    day_type: 0=рабочий, 1=нерабочий."""
    overrides = _load_overrides(year)
    holidays_set = {(m, d) for m, d in _RU_HOLIDAY_DATES}
    result = {}
    _, dec_last = calendar.monthrange(year, 12)
    for month in range(1, 13):
        _, last_day = calendar.monthrange(year, month)
        for day in range(1, last_day + 1):
            d = date(year, month, day)
            dt_str = d.isoformat()
            if dt_str in overrides:
                result[dt_str] = overrides[dt_str]
            elif d.weekday() >= 5 or (d.month, d.day) in holidays_set:
                result[dt_str] = 1
            else:
                result[dt_str] = 0
    return result


def get_workdays_in_month(year: int, month: int) -> list[date]:
    cal = _get_year_calendar(year)
    _, last_day = calendar.monthrange(year, month)
    return [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if cal.get(date(year, month, d).isoformat(), 0) == 0
    ]


def _month_range(year: int, month: int) -> tuple[str, str]:
    _, last_day = calendar.monthrange(year, month)
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


def _upsert_users(user_map: dict[str, dict]):
    """Upsert пользователей в jira_users, пометить пропавших."""
    conn = get_db()
    existing = {
        row["account_id"]: dict(row)
        for row in conn.execute("SELECT * FROM jira_users").fetchall()
    }

    seen_ids = set(user_map.keys())
    now = datetime.now().isoformat(timespec="seconds")

    new_users = []
    removed_users = []

    for uid, info in user_map.items():
        if uid in existing:
            conn.execute(
                "UPDATE jira_users SET display_name=?, email_address=?, active=1, last_seen_at=? WHERE account_id=?",
                (info["display_name"], info["email"], now, uid),
            )
            if not existing[uid]["active"]:
                new_users.append(uid)
        else:
            conn.execute(
                "INSERT INTO jira_users (account_id, display_name, email_address, first_seen_at, last_seen_at) VALUES (?,?,?,?,?)",
                (uid, info["display_name"], info["email"], now, now),
            )
            new_users.append(uid)

    for uid, row in existing.items():
        if uid not in seen_ids and row["active"]:
            conn.execute(
                "UPDATE jira_users SET active=0, last_seen_at=? WHERE account_id=?",
                (now, uid),
            )
            removed_users.append(uid)

    conn.commit()
    conn.close()
    return new_users, removed_users


def _format_hours(seconds: int) -> str:
    h = seconds / 3600
    return f"{h:.1f}"


def _resolve_display_name(
    uid: str,
    entries: list[dict],
    db_users: dict[str, dict] | None = None,
    fallback_users: dict[str, dict] | None = None,
) -> str:
    if entries:
        return entries[0].get("display_name") or uid
    if fallback_users and uid in fallback_users:
        return fallback_users[uid].get("display_name") or uid
    if db_users and uid in db_users:
        return db_users[uid].get("display_name") or uid
    return uid


def _evaluate_overtime_day(
    day_str: str,
    day_entries: list[dict],
    project: str,
    year_cal: dict[str, int],
) -> dict:
    d = date.fromisoformat(day_str)
    total_seconds = sum(e["seconds"] for e in day_entries)
    total_hours = total_seconds / 3600

    is_non_working = year_cal.get(day_str, 0) == 1
    is_weekend = d.weekday() >= 5
    is_holiday = is_non_working and not is_weekend
    is_wd = not is_non_working

    decision_reason = "workday_not_over_8h"
    qualifies_overtime = False
    if is_wd and total_hours > 8:
        qualifies_overtime = True
        decision_reason = "workday_over_8h"
    elif is_weekend:
        qualifies_overtime = True
        decision_reason = "weekend"
    elif is_holiday:
        qualifies_overtime = True
        decision_reason = "holiday"

    proj_seconds = sum(e["seconds"] for e in day_entries if e["project"] == project)
    other_seconds = total_seconds - proj_seconds
    day_type = "holiday" if is_holiday else ("weekend" if is_weekend else "workday")
    issues_list = sorted({e["issue_key"] for e in day_entries})
    projects_list = sorted({e["project"] for e in day_entries})

    report_row = None
    if qualifies_overtime:
        report_row = {
            "date": day_str,
            "day_type": day_type,
            "total_hours": f"{total_hours:.1f}",
            "project_hours": _format_hours(proj_seconds),
            "other_hours": _format_hours(other_seconds),
            "over_norm": f"{(total_hours - 8):.1f}" if is_wd else f"{total_hours:.1f}",
            "projects": projects_list,
            "issues": issues_list,
        }

    return {
        "date": day_str,
        "day_type": day_type,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "is_workday": is_wd,
        "entry_count": len(day_entries),
        "total_hours": f"{total_hours:.1f}",
        "project_hours": _format_hours(proj_seconds),
        "other_hours": _format_hours(other_seconds),
        "qualifies_overtime": qualifies_overtime,
        "decision_reason": decision_reason,
        "issues": issues_list,
        "projects": projects_list,
        "report_row": report_row,
    }


def _build_overtime_rows_and_checks(
    uid: str,
    display_name: str,
    entries: list[dict],
    project: str,
    year_cal: dict[str, int],
) -> tuple[list[dict], list[dict]]:
    by_date: dict[str, list[dict]] = {}
    for entry in entries:
        by_date.setdefault(entry["date"], []).append(entry)

    rows: list[dict] = []
    day_checks: list[dict] = []
    for day_str, day_entries in sorted(by_date.items()):
        check = _evaluate_overtime_day(day_str, day_entries, project, year_cal)
        day_checks.append(check)
        if not check["qualifies_overtime"]:
            continue
        row = dict(check["report_row"])
        row["account_id"] = uid
        row["display_name"] = display_name
        rows.append(row)

    return rows, day_checks


def _build_issue_debug_entries(
    issue_key: str,
    raw_worklogs: list[dict],
    date_from: str,
    date_to: str,
) -> list[dict]:
    issue_project = issue_key.split("-", 1)[0] if "-" in issue_key else ""
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    entries = []

    for worklog in raw_worklogs:
        author = worklog.get("author", {})
        author_key = (
            author.get("accountId") or author.get("key") or author.get("name", "")
        )
        started = worklog.get("started", "")
        if not author_key or not started:
            continue

        started_date = started[:10]
        wl_date = date.fromisoformat(started_date)
        if wl_date < d_from or wl_date > d_to:
            continue

        seconds = worklog.get("timeSpentSeconds", 0)
        entries.append(
            {
                "issue_key": issue_key,
                "project": issue_project,
                "author_key": author_key,
                "display_name": author.get("displayName", ""),
                "email": author.get("emailAddress", ""),
                "author_account_id": author.get("accountId", ""),
                "author_key_field": author.get("key", ""),
                "author_name": author.get("name", ""),
                "author_candidates": [
                    value
                    for value in dict.fromkeys(
                        [
                            author.get("accountId", ""),
                            author.get("key", ""),
                            author.get("name", ""),
                        ]
                    )
                    if value
                ],
                "started": started,
                "date": started_date,
                "seconds": seconds,
                "hours": _format_hours(seconds),
            }
        )

    return entries


def _send_email(recipients: list[str], subject: str, html_body: str):
    host = os.getenv("SMTP_HOST", "")
    if not recipients or not host:
        raise ConnectionError("SMTP не настроен или нет получателей")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        if port == 587:
            server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    conn = get_db()
    users = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM jira_users ORDER BY display_name"
        ).fetchall()
    ]
    vacations = {}
    for v in conn.execute("SELECT * FROM user_vacations ORDER BY date_from").fetchall():
        vacations.setdefault(v["account_id"], []).append(dict(v))
    settings_rows = conn.execute("SELECT * FROM report_settings").fetchall()
    conn.close()
    settings = {row["report_type"]: dict(row) for row in settings_rows}
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "jira_url": os.getenv("JIRA_URL", ""),
            "jira_project": os.getenv("JIRA_PROJECT", ""),
            "users": users,
            "vacations": vacations,
            "settings": settings,
        },
    )


# ---------------------------------------------------------------------------
# API: Time Logging report
# ---------------------------------------------------------------------------


@router.post("/api/reports/time-logging")
async def time_logging_report(body: ReportRequest):
    project = os.getenv("JIRA_PROJECT", "")
    date_from, date_to = _month_range(body.year, body.month)

    project_worklogs = await jira_client.get_all_worklogs_for_project(
        project,
        date_from,
        date_to,
    )

    user_info: dict[str, dict] = {}
    for uid, entries in project_worklogs.items():
        if entries:
            user_info[uid] = {
                "display_name": entries[0]["display_name"],
                "email": entries[0]["email"],
            }

    new_users, removed_users = _upsert_users(user_info)

    user_ids = list(project_worklogs.keys())
    other_worklogs: dict[str, list[dict]] = {}
    if user_ids:
        other_worklogs = await jira_client.get_worklogs_for_users_all_projects(
            user_ids,
            date_from,
            date_to,
        )

    workdays = get_workdays_in_month(body.year, body.month)
    workday_strs = {d.isoformat() for d in workdays}
    today = date.today()
    today_str = today.isoformat()

    # Рабочие дни строго до сегодняшнего дня (сегодня не учитывается)
    past_workdays = {d for d in workday_strs if d < today_str}

    # Загрузить отпуска пользователей
    vacation_dates = (
        _get_vacation_dates(user_ids, date_from, date_to) if user_ids else {}
    )

    rows = []
    for uid in sorted(
        user_ids,
        key=lambda u: (
            project_worklogs.get(u, [{}])[0].get("display_name", "")
            if project_worklogs.get(u)
            else ""
        ),
    ):
        proj_entries = project_worklogs.get(uid, [])
        all_entries = other_worklogs.get(uid, [])

        # Объединяем даты из проектных и всех остальных ворклогов
        proj_dates = {e["date"] for e in proj_entries}
        all_dates = {e["date"] for e in proj_entries} | {e["date"] for e in all_entries}

        proj_seconds = sum(e["seconds"] for e in proj_entries)
        other_entries = [e for e in all_entries if e["project"] != project]
        other_seconds = sum(e["seconds"] for e in other_entries)

        user_vacation = vacation_dates.get(uid, set())
        missing_days = sorted(past_workdays - all_dates - user_vacation)

        display_name = proj_entries[0]["display_name"] if proj_entries else uid
        email = proj_entries[0]["email"] if proj_entries else ""

        status = (
            "new"
            if uid in new_users
            else ("removed" if uid in removed_users else "active")
        )

        # Список задач пользователя с часами
        issues_map: dict[str, dict] = {}
        for e in proj_entries:
            ik = e["issue_key"]
            if ik not in issues_map:
                issues_map[ik] = {
                    "issue_key": ik,
                    "project": e["project"],
                    "seconds": 0,
                }
            issues_map[ik]["seconds"] += e["seconds"]
        for e in all_entries:
            ik = e["issue_key"]
            if ik not in issues_map:
                issues_map[ik] = {
                    "issue_key": ik,
                    "project": e["project"],
                    "seconds": 0,
                }
            issues_map[ik]["seconds"] += e["seconds"]
        user_issues = sorted(issues_map.values(), key=lambda x: x["issue_key"])

        rows.append(
            {
                "account_id": uid,
                "display_name": display_name,
                "email": email,
                "status": status,
                "project_hours": _format_hours(proj_seconds),
                "other_hours": _format_hours(other_seconds),
                "days_logged": len(proj_dates),
                "total_workdays": len(past_workdays),
                "missing_days": missing_days,
                "missing_count": len(missing_days),
                "issues": [
                    {
                        "issue_key": i["issue_key"],
                        "project": i["project"],
                        "hours": _format_hours(i["seconds"]),
                    }
                    for i in user_issues
                ],
            }
        )

    return {"rows": rows, "year": body.year, "month": body.month, "project": project}


# ---------------------------------------------------------------------------
# API: Overtime report
# ---------------------------------------------------------------------------


@router.post("/api/reports/overtime")
async def overtime_report(body: ReportRequest):
    project = os.getenv("JIRA_PROJECT", "")
    date_from, date_to = _month_range(body.year, body.month)

    # 1. Собираем пользователей из проекта за период
    project_worklogs = await jira_client.get_all_worklogs_for_project(
        project,
        date_from,
        date_to,
    )
    project_user_ids = set(project_worklogs.keys())

    # 2. Добавляем сохранённых в БД пользователей
    conn = get_db()
    db_users = conn.execute("SELECT * FROM jira_users").fetchall()
    conn.close()
    db_user_ids = {r["account_id"] for r in db_users}

    all_user_ids = sorted(project_user_ids | db_user_ids)
    if not all_user_ids:
        return {"rows": [], "year": body.year, "month": body.month}

    # 3. Для всех пользователей ищем ворклоги во всех проектах
    all_worklogs = await jira_client.get_worklogs_for_users_all_projects(
        all_user_ids,
        date_from,
        date_to,
    )

    year_cal = _get_year_calendar(body.year)
    rows = []

    for uid in all_user_ids:
        entries = all_worklogs.get(uid, [])
        if not entries:
            continue

        display_name = _resolve_display_name(uid, entries)
        user_rows, _ = _build_overtime_rows_and_checks(
            uid, display_name, entries, project, year_cal
        )
        rows.extend(user_rows)

    return {"rows": rows, "year": body.year, "month": body.month, "project": project}


@router.post("/api/reports/overtime/debug-issue")
async def overtime_debug_issue(body: OvertimeDebugRequest):
    project = os.getenv("JIRA_PROJECT", "")
    issue_key = body.issue_key.strip().upper()
    date_from, date_to = _month_range(body.year, body.month)

    raw_issue_worklogs = await jira_client.get_issue_worklogs(issue_key)
    issue_entries = _build_issue_debug_entries(
        issue_key,
        raw_issue_worklogs.get("worklogs", []),
        date_from,
        date_to,
    )

    issue_user_map: dict[str, dict] = {}
    for entry in issue_entries:
        issue_user_map[entry["author_key"]] = {
            "display_name": entry["display_name"],
            "email": entry["email"],
        }
    issue_user_ids = set(issue_user_map.keys())

    conn = get_db()
    db_rows = conn.execute("SELECT * FROM jira_users").fetchall()
    conn.close()
    db_users = {row["account_id"]: dict(row) for row in db_rows}
    db_user_ids = set(db_users.keys())

    project_user_ids = {
        entry["author_key"]
        for entry in issue_entries
        if entry.get("project") == project
    }
    report_scope_user_ids = sorted(project_user_ids | db_user_ids)
    diagnostic_user_ids = sorted(issue_user_ids | db_user_ids)

    year_cal = _get_year_calendar(body.year)
    users = []

    for uid in diagnostic_user_ids:
        issue_specific_entries = [
            entry for entry in issue_entries if entry["author_key"] == uid
        ]
        display_name = _resolve_display_name(
            uid, issue_specific_entries, db_users, issue_user_map
        )
        issue_specific_rows, issue_specific_checks = _build_overtime_rows_and_checks(
            uid, display_name, issue_specific_entries, project, year_cal
        )
        raw_author = issue_specific_entries[0] if issue_specific_entries else {}
        lookup_candidates = list(
            dict.fromkeys(
                [
                    raw_author.get("author_account_id", ""),
                    raw_author.get("author_key_field", ""),
                    raw_author.get("author_name", ""),
                    uid,
                ]
            )
        )
        lookup_candidates = [candidate for candidate in lookup_candidates if candidate]
        lookup_diagnostics = {}
        lookup_diagnostics_error = ""
        if lookup_candidates:
            try:
                lookup_diagnostics = await asyncio.wait_for(
                    jira_client.diagnose_worklog_author_candidates(
                        lookup_candidates,
                        date_from,
                        date_to,
                        issue_key=issue_key,
                    ),
                    timeout=_LOOKUP_DIAGNOSTICS_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                lookup_diagnostics_error = (
                    f"lookup timeout after {_LOOKUP_DIAGNOSTICS_TIMEOUT_SECONDS}s"
                )
            except Exception as exc:
                lookup_diagnostics_error = str(exc)

        if issue_specific_rows:
            exclusion_reason = "included_via_issue"
        elif not issue_specific_entries:
            exclusion_reason = "no_issue_worklogs_in_period"
        elif uid not in report_scope_user_ids:
            exclusion_reason = "user_not_in_report_scope"
        elif not any(
            candidate_info.get("strict_entry_count", 0)
            or candidate_info.get("candidate_match_entry_count", 0)
            for candidate_info in lookup_diagnostics.values()
        ):
            exclusion_reason = "user_lookup_returned_no_entries"
        elif issue_specific_checks:
            exclusion_reason = ",".join(
                sorted({check["decision_reason"] for check in issue_specific_checks})
            )
        else:
            exclusion_reason = "issue_not_present_in_user_period_entries"

        users.append(
            {
                "account_id": uid,
                "display_name": display_name,
                "in_issue_worklogs": bool(issue_specific_entries),
                "in_project_scope": uid in project_user_ids,
                "in_db_scope": uid in db_user_ids,
                "in_report_scope": uid in report_scope_user_ids,
                "issue_entry_count": len(issue_specific_entries),
                "issue_hours": _format_hours(
                    sum(entry["seconds"] for entry in issue_specific_entries)
                ),
                "period_entry_count": len(issue_specific_entries),
                "period_hours": _format_hours(
                    sum(entry["seconds"] for entry in issue_specific_entries)
                ),
                "included_in_monthly_report": bool(issue_specific_rows),
                "included_due_to_issue": bool(issue_specific_rows),
                "exclusion_reason": exclusion_reason,
                "issue_worklogs": issue_specific_entries,
                "issue_day_checks": issue_specific_checks,
                "report_rows": issue_specific_rows,
                "author_identifiers": {
                    "account_id": raw_author.get("author_account_id", ""),
                    "key": raw_author.get("author_key_field", ""),
                    "name": raw_author.get("author_name", ""),
                    "primary": raw_author.get("author_key", uid),
                },
                "lookup_candidates": lookup_candidates,
                "lookup_diagnostics": lookup_diagnostics,
                "lookup_diagnostics_error": lookup_diagnostics_error,
            }
        )

    return {
        "year": body.year,
        "month": body.month,
        "project": project,
        "issue_key": issue_key,
        "issue_worklogs": issue_entries,
        "project_user_ids": sorted(project_user_ids),
        "db_user_ids": sorted(db_user_ids),
        "report_scope_user_ids": report_scope_user_ids,
        "diagnostic_user_ids": diagnostic_user_ids,
        "users": users,
    }


# ---------------------------------------------------------------------------
# API: Users
# ---------------------------------------------------------------------------


@router.get("/api/reports/users")
def list_users():
    conn = get_db()
    users = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM jira_users ORDER BY display_name"
        ).fetchall()
    ]
    conn.close()
    return users


@router.patch("/api/reports/users/{account_id}")
def toggle_user(account_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT active FROM jira_users WHERE account_id = ?", (account_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"error": "not found"}
    new_active = 0 if row["active"] else 1
    conn.execute(
        "UPDATE jira_users SET active = ? WHERE account_id = ?",
        (new_active, account_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "active": new_active}


# ---------------------------------------------------------------------------
# API: Vacations
# ---------------------------------------------------------------------------


@router.get("/api/reports/users/{account_id}/vacations")
def get_vacations(account_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM user_vacations WHERE account_id = ? ORDER BY date_from",
        (account_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/api/reports/users/{account_id}/vacations")
def add_vacation(account_id: str, body: dict):
    date_from = body.get("date_from", "")
    date_to = body.get("date_to", "")
    note = body.get("note", "")
    if not date_from or not date_to:
        return {"error": "date_from and date_to required"}
    conn = get_db()
    conn.execute(
        "INSERT INTO user_vacations (account_id, date_from, date_to, note) VALUES (?,?,?,?)",
        (account_id, date_from, date_to, note),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.delete("/api/reports/vacations/{vacation_id}")
def delete_vacation(vacation_id: int):
    conn = get_db()
    conn.execute("DELETE FROM user_vacations WHERE id = ?", (vacation_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


def _get_vacation_dates(
    user_ids: list[str], date_from: str, date_to: str
) -> dict[str, set[str]]:
    """Возвращает {account_id: set of vacation date strings} для периода."""
    conn = get_db()
    placeholders = ",".join("?" for _ in user_ids)
    rows = conn.execute(
        f"SELECT * FROM user_vacations WHERE account_id IN ({placeholders}) "
        f"AND date_to >= ? AND date_from <= ?",
        [*user_ids, date_from, date_to],
    ).fetchall()
    conn.close()

    result: dict[str, set[str]] = {}
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    for r in rows:
        vac_from = max(date.fromisoformat(r["date_from"]), d_from)
        vac_to = min(date.fromisoformat(r["date_to"]), d_to)
        uid = r["account_id"]
        result.setdefault(uid, set())
        d = vac_from
        while d <= vac_to:
            result[uid].add(d.isoformat())
            d += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# API: Notify missing time
# ---------------------------------------------------------------------------


@router.post("/api/reports/notify-missing")
async def notify_missing(body: NotifyMissingRequest):
    project = os.getenv("JIRA_PROJECT", "")
    date_from, date_to = _month_range(body.year, body.month)
    workdays = get_workdays_in_month(body.year, body.month)
    today = date.today()
    relevant_workdays = {d.isoformat() for d in workdays if d <= today}

    all_worklogs = await jira_client.get_worklogs_for_users_all_projects(
        body.user_ids,
        date_from,
        date_to,
    )

    conn = get_db()
    settings_row = conn.execute(
        "SELECT * FROM report_settings WHERE report_type = 'time_logging'"
    ).fetchone()
    conn.close()
    webhook_url = (
        settings_row["teams_webhook_url"] if settings_row else ""
    ) or os.getenv("TEAMS_WEBHOOK_URL", "")

    results = []
    for uid in body.user_ids:
        entries = all_worklogs.get(uid, [])
        logged_dates = {e["date"] for e in entries}
        missing = sorted(relevant_workdays - logged_dates)

        if not missing:
            results.append(
                {"account_id": uid, "sent": False, "reason": "no missing days"}
            )
            continue

        conn2 = get_db()
        user_row = conn2.execute(
            "SELECT * FROM jira_users WHERE account_id = ?", (uid,)
        ).fetchone()
        conn2.close()
        display_name = user_row["display_name"] if user_row else uid

        month_name = f"{body.year}-{body.month:02d}"
        missing_str = ", ".join(missing)
        message = (
            f"Напоминание: у пользователя {display_name} не залогировано время "
            f"за {month_name} в проекте {project}.\n"
            f"Пропущенные дни: {missing_str}"
        )

        sent_teams = False
        sent_email = False
        error = ""

        if webhook_url:
            try:
                from services.teams_client import send_teams_notification

                await send_teams_notification(
                    webhook_url=webhook_url,
                    mr_title=f"Незалогированное время — {display_name}",
                    mr_url=f"{os.getenv('JIRA_URL', '')}/secure/Dashboard.jspa",
                    file_path=f"Проект: {project}",
                    file_content=message,
                    rule_name="Jira Time Logging",
                )
                sent_teams = True
            except Exception as e:
                error = str(e)
                logger.error("notify_missing teams %s: %s", uid, e)

        email = user_row["email_address"] if user_row else ""
        if email:
            try:
                _send_email(
                    [email],
                    f"Незалогированное время — {month_name}",
                    f"<h3>{message}</h3>",
                )
                sent_email = True
            except Exception as e:
                error += f" email: {e}"
                logger.error("notify_missing email %s: %s", uid, e)

        results.append(
            {"account_id": uid, "sent": sent_teams or sent_email, "error": error}
        )

    return {"results": results}


# ---------------------------------------------------------------------------
# API: Send overtime report
# ---------------------------------------------------------------------------


def _build_overtime_summary(rows: list[dict]) -> dict[str, dict]:
    """Сводка по пользователям: сумма переработок за месяц."""
    summary: dict[str, dict] = {}
    for r in rows:
        name = r.get("display_name", "")
        if name not in summary:
            summary[name] = {"total": 0.0, "project": 0.0, "other": 0.0, "days": 0}
        summary[name]["total"] += float(r.get("over_norm", 0))
        summary[name]["project"] += float(r.get("project_hours", 0))
        summary[name]["other"] += float(r.get("other_hours", 0))
        summary[name]["days"] += 1
    return summary


@router.post("/api/reports/send-overtime")
async def send_overtime_email(body: SendReportRequest):
    rows = body.rows
    project = body.project or os.getenv("JIRA_PROJECT", "")

    if not rows:
        report = await overtime_report(ReportRequest(year=body.year, month=body.month))
        rows = report["rows"]
        project = report["project"]

    if not rows:
        return {"sent": False, "reason": "no overtime data"}

    jira_url = os.getenv("JIRA_URL", "").rstrip("/")

    summary = _build_overtime_summary(rows)
    summary_html = ""
    for name, s in sorted(summary.items()):
        summary_html += (
            f"<tr>"
            f'<td style="padding:4px 8px;font-weight:bold">{name}</td>'
            f'<td style="padding:4px 8px">{s["days"]}</td>'
            f'<td style="padding:4px 8px;color:#06b6d4">{s["project"]:.1f}h</td>'
            f'<td style="padding:4px 8px;color:gray">{s["other"]:.1f}h</td>'
            f'<td style="padding:4px 8px;color:red;font-weight:bold">+{s["total"]:.1f}h</td>'
            f"</tr>"
        )

    html_rows = ""
    for r in rows:
        day_class = "color:red" if r.get("day_type") != "workday" else "color:orange"
        issues_links = ", ".join(
            f'<a href="{jira_url}/browse/{ik}">{ik}</a>' for ik in r.get("issues", [])
        )
        html_rows += (
            f'<tr style="{day_class}">'
            f'<td style="padding:4px 8px">{r.get("display_name", "")}</td>'
            f'<td style="padding:4px 8px">{r.get("date", "")}</td>'
            f'<td style="padding:4px 8px">{r.get("day_type", "")}</td>'
            f'<td style="padding:4px 8px">{r.get("total_hours", "")}h</td>'
            f'<td style="padding:4px 8px">{r.get("project_hours", "")}h</td>'
            f'<td style="padding:4px 8px">{r.get("other_hours", "")}h</td>'
            f'<td style="padding:4px 8px">+{r.get("over_norm", "")}h</td>'
            f'<td style="padding:4px 8px">{issues_links}</td>'
            f"</tr>"
        )

    month_name = f"{body.year}-{body.month:02d}"
    html = f"""\
<html><body style="font-family:Arial,sans-serif;color:#333">
<h2>Отчёт по переработкам — {month_name}</h2>

<h3>Сводка по пользователям</h3>
<table style="border-collapse:collapse;border:1px solid #ccc;margin-bottom:20px">
<tr style="background:#f0f0f0;font-weight:bold">
<td style="padding:4px 8px">Пользователь</td>
<td style="padding:4px 8px">Дней</td>
<td style="padding:4px 8px">{project}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Итого сверх нормы</td>
</tr>
{summary_html}
</table>

<h3>Детализация</h3>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#f0f0f0;font-weight:bold">
<td style="padding:4px 8px">Пользователь</td>
<td style="padding:4px 8px">Дата</td>
<td style="padding:4px 8px">Тип дня</td>
<td style="padding:4px 8px">Всего</td>
<td style="padding:4px 8px">{project}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Сверх нормы</td>
<td style="padding:4px 8px">Задачи</td>
</tr>
{html_rows}
</table>
</body></html>"""

    _send_email(body.emails, f"Отчёт по переработкам — {month_name}", html)
    return {"sent": True}


# ---------------------------------------------------------------------------
# API: Send time logging report
# ---------------------------------------------------------------------------


@router.post("/api/reports/send-time-logging")
async def send_time_logging_email(body: SendReportRequest):
    rows = body.rows
    project = body.project or os.getenv("JIRA_PROJECT", "")

    if not rows:
        report = await time_logging_report(
            ReportRequest(year=body.year, month=body.month)
        )
        rows = report["rows"]
        project = report["project"]

    if not rows:
        return {"sent": False, "reason": "no time logging data"}

    html_rows = ""
    for r in rows:
        missing_style = "color:red" if r.get("missing_count", 0) > 0 else ""
        html_rows += (
            f"<tr>"
            f'<td style="padding:4px 8px">{r.get("display_name", "")}</td>'
            f'<td style="padding:4px 8px">{r.get("days_logged", "")}/{r.get("total_workdays", "")}</td>'
            f'<td style="padding:4px 8px;color:#06b6d4">{r.get("project_hours", "")}h</td>'
            f'<td style="padding:4px 8px;color:gray">{r.get("other_hours", "")}h</td>'
            f'<td style="padding:4px 8px;{missing_style}">{r.get("missing_count", 0)} дн.</td>'
            f"</tr>"
        )

    month_name = f"{body.year}-{body.month:02d}"
    html = f"""\
<html><body style="font-family:Arial,sans-serif;color:#333">
<h2>Отчёт учёта времени — {month_name}</h2>
<p>Проект: {project}</p>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#f0f0f0;font-weight:bold">
<td style="padding:4px 8px">Пользователь</td>
<td style="padding:4px 8px">Дни</td>
<td style="padding:4px 8px">{project}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Пропущено</td>
</tr>
{html_rows}
</table>
</body></html>"""

    _send_email(body.emails, f"Отчёт учёта времени — {month_name}", html)
    return {"sent": True}


# ---------------------------------------------------------------------------
# API: Settings
# ---------------------------------------------------------------------------


@router.get("/api/reports/settings")
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT * FROM report_settings").fetchall()
    conn.close()
    return {row["report_type"]: dict(row) for row in rows}


@router.put("/api/reports/settings/{report_type}")
def update_settings(report_type: str, body: ReportSettingsUpdate):
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """UPDATE report_settings SET
            auto_send_enabled=?, auto_send_day=?, auto_send_time=?,
            send_email=?, email_recipients=?, teams_webhook_url=?,
            missing_time_auto_notify=?, missing_time_interval_days=?,
            updated_at=?
           WHERE report_type=?""",
        (
            int(body.auto_send_enabled),
            body.auto_send_day,
            body.auto_send_time,
            int(body.send_email),
            body.email_recipients,
            body.teams_webhook_url,
            int(body.missing_time_auto_notify),
            body.missing_time_interval_days,
            now,
            report_type,
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Holiday Calendar
# ---------------------------------------------------------------------------


@router.get("/api/reports/calendar/{year}")
def get_calendar(year: int):
    """Возвращает календарь на год: {date_str: day_type}."""
    return _get_year_calendar(year)


@router.put("/api/reports/calendar/{year}")
def save_calendar(year: int, body: dict):
    """Сохраняет переопределения календаря.
    Body: {overrides: {date_str: day_type}} — только дни, отличающиеся от дефолта."""
    overrides = body.get("overrides", {})
    holidays_set = {(m, d) for m, d in _RU_HOLIDAY_DATES}

    conn = get_db()
    conn.execute("DELETE FROM holiday_overrides WHERE dt LIKE ?", (f"{year}-%",))

    for dt_str, day_type in overrides.items():
        d = date.fromisoformat(dt_str)
        default_non_working = d.weekday() >= 5 or (d.month, d.day) in holidays_set
        default_type = 1 if default_non_working else 0
        if day_type != default_type:
            conn.execute(
                "INSERT OR REPLACE INTO holiday_overrides (dt, day_type) VALUES (?, ?)",
                (dt_str, day_type),
            )

    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/api/reports/calendar/{year}/fetch")
async def fetch_calendar_from_isdayoff(year: int):
    """Загружает производственный календарь из isdayoff.ru."""
    import httpx

    url = f"https://isdayoff.ru/api/getdata?year={year}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"ok": False, "error": f"isdayoff.ru вернул {resp.status_code}"}
            data = resp.text.strip()
    except Exception as e:
        return {"ok": False, "error": f"Не удалось подключиться к isdayoff.ru: {e}"}

    if len(data) < 365:
        return {"ok": False, "error": f"Некорректный ответ ({len(data)} символов)"}

    holidays_set = {(m, d) for m, d in _RU_HOLIDAY_DATES}
    conn = get_db()
    conn.execute("DELETE FROM holiday_overrides WHERE dt LIKE ?", (f"{year}-%",))

    idx = 0
    for month in range(1, 13):
        _, last_day = calendar.monthrange(year, month)
        for day in range(1, last_day + 1):
            if idx >= len(data):
                break
            ch = data[idx]
            idx += 1

            d = date(year, month, day)
            is_non_working = ch in ("1", "2")
            default_non_working = d.weekday() >= 5 or (d.month, d.day) in holidays_set
            day_type = 1 if is_non_working else 0
            default_type = 1 if default_non_working else 0

            if day_type != default_type:
                conn.execute(
                    "INSERT OR REPLACE INTO holiday_overrides (dt, day_type) VALUES (?, ?)",
                    (d.isoformat(), day_type),
                )

    conn.commit()
    conn.close()
    return {"ok": True, "days_total": idx}
