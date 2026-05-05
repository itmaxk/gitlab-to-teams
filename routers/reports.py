import asyncio
import calendar
import html as html_mod
import logging
import os
import smtplib
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from db import get_db
from models import (
    NotifyMissingRequest,
    OvertimeDebugRequest,
    ReportRequest,
    ReportSettingsUpdate,
    SendReportRequest,
    VacationCreateRequest,
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
                "INSERT OR REPLACE INTO jira_users (account_id, display_name, email_address, first_seen_at, last_seen_at, active) VALUES (?,?,?,?,?,1)",
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


def _collect_user_lookup_candidates(
    uid: str,
    project_worklogs: dict[str, list[dict]],
    db_users: list[dict] | None = None,
) -> list[str]:
    candidates = [uid]
    for entry in project_worklogs.get(uid, []):
        for candidate in entry.get("author_candidates", []):
            if candidate not in candidates:
                candidates.append(candidate)
        for field in (
            entry.get("author_account_id", ""),
            entry.get("author_key_field", ""),
            entry.get("author_name", ""),
        ):
            if field and field not in candidates:
                candidates.append(field)

    for user in db_users or []:
        account_id = user["account_id"] if hasattr(user, "__getitem__") else user.get("account_id")
        if account_id not in candidates:
            candidates.append(account_id)

    return candidates


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
    if qualifies_overtime and proj_seconds > 0:
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
        if not check["qualifies_overtime"] or not check["report_row"]:
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
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as server:
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

    try:
        project_worklogs = await jira_client.get_all_worklogs_for_project(
            project,
            date_from,
            date_to,
        )
    except Exception as exc:
        logger.error("time_logging_report project worklogs failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to load Jira worklogs for time logging report",
        ) from exc

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
        try:
            other_worklogs = await jira_client.get_worklogs_for_users_all_projects(
                user_ids,
                date_from,
                date_to,
            )
        except Exception as exc:
            logger.error("time_logging_report other worklogs failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="Failed to load Jira user worklogs for time logging report",
            ) from exc

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
                "available_workdays": len(past_workdays) - len(missing_days),
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
    try:
        project_worklogs = await jira_client.get_all_worklogs_for_project(
            project,
            date_from,
            date_to,
        )
    except Exception as exc:
        logger.error("overtime_report project worklogs failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to load Jira worklogs for overtime report",
        ) from exc
    project_user_ids = set(project_worklogs.keys())

    # 2. Добавляем сохранённых в БД пользователей
    conn = get_db()
    db_users = conn.execute("SELECT * FROM jira_users").fetchall()
    conn.close()
    db_user_ids = {r["account_id"] for r in db_users}

    user_ids = sorted(project_user_ids | db_user_ids)
    if not user_ids:
        return {"rows": [], "year": body.year, "month": body.month, "project": project}

    other_worklogs: dict[str, list[dict]] = {}
    user_candidates = {
        uid: _collect_user_lookup_candidates(uid, project_worklogs, db_users)
        for uid in user_ids
    }
    try:
        other_worklogs = await jira_client.get_worklogs_for_users_all_projects_by_candidates(
            user_candidates,
            date_from,
            date_to,
        )
    except Exception as exc:
        logger.error("overtime_report other worklogs failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to load Jira user worklogs for overtime report",
        ) from exc

    year_cal = _get_year_calendar(body.year)
    rows = []
    db_users_by_id = {r["account_id"]: dict(r) for r in db_users}

    for uid in user_ids:
        proj_entries = project_worklogs.get(uid, [])
        other_entries = other_worklogs.get(uid, [])
        has_project_entries = proj_entries or any(
            entry.get("project") == project for entry in other_entries
        )
        if not has_project_entries:
            continue

        other_entry_scope = {
            (
                entry.get("issue_key", ""),
                entry.get("date", ""),
                entry.get("project", ""),
            )
            for entry in other_entries
        }
        fallback_project_entries = [
            entry
            for entry in proj_entries
            if (
                entry.get("issue_key", ""),
                entry.get("date", ""),
                entry.get("project", ""),
            )
            not in other_entry_scope
        ]
        entries = jira_client.dedupe_worklog_entries(
            fallback_project_entries + other_entries
        )
        if not entries:
            continue

        display_name = _resolve_display_name(uid, entries, db_users_by_id)
        user_rows, _ = _build_overtime_rows_and_checks(
            uid, display_name, entries, project, year_cal
        )
        rows.extend(user_rows)

    return {"rows": rows, "year": body.year, "month": body.month, "project": project}


@router.post("/api/reports/overtime/debug-issue")
async def overtime_debug_issue(body: OvertimeDebugRequest):
    """Диагностика: почему ворклоги задачи попали / не попали в отчёт.

    Воспроизводит логику overtime_report: project_worklogs (двойной JQL)
    как основной источник, без worklogAuthor JQL.
    """
    project = os.getenv("JIRA_PROJECT", "")
    issue_key = body.issue_key.strip().upper()
    date_from, date_to = _month_range(body.year, body.month)

    # 1. Прямой запрос ворклогов задачи (эталон)
    try:
        raw_issue_worklogs = await jira_client.get_issue_worklogs(issue_key)
    except Exception as exc:
        logger.error("overtime_debug_issue get_issue_worklogs failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to load worklogs for issue {issue_key}",
        ) from exc
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

    # 2. Те же project_worklogs, что использует overtime_report
    try:
        project_worklogs = await jira_client.get_all_worklogs_for_project(
            project, date_from, date_to,
        )
    except Exception:
        project_worklogs = {}

    conn = get_db()
    db_rows = conn.execute("SELECT * FROM jira_users").fetchall()
    conn.close()
    db_users = {row["account_id"]: dict(row) for row in db_rows}
    db_user_ids = set(db_users.keys())

    project_user_ids = set(project_worklogs.keys())
    report_scope_user_ids = sorted(project_user_ids | db_user_ids)
    diagnostic_user_ids = sorted(issue_user_ids | db_user_ids)

    lookup_diagnostics: dict[str, dict] = {}
    lookup_diagnostics_error = ""
    if issue_user_ids:
        try:
            lookup_diagnostics = await asyncio.wait_for(
                jira_client.diagnose_worklog_author_candidates(
                    sorted(issue_user_ids),
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

    year_cal = _get_year_calendar(body.year)
    users = []

    for uid in diagnostic_user_ids:
        # Ворклоги конкретной задачи для этого пользователя
        issue_specific_entries = [
            entry for entry in issue_entries if entry["author_key"] == uid
        ]

        # ВСЕ ворклоги пользователя за период (как в overtime_report)
        proj_entries = project_worklogs.get(uid, [])
        all_period_entries = jira_client.dedupe_worklog_entries(proj_entries)

        # Есть ли задача в project_worklogs (найдена ли JQL-запросами)
        issue_in_project_worklogs = any(
            e["issue_key"] == issue_key for e in proj_entries
        )

        display_name = _resolve_display_name(
            uid, all_period_entries or issue_specific_entries,
            db_users, issue_user_map,
        )

        # Даты, когда есть ворклоги по этой задаче
        issue_dates = {e["date"] for e in issue_specific_entries}

        # Полные day-checks по ВСЕМ ворклогам пользователя (как в отчёте)
        full_rows, full_checks = _build_overtime_rows_and_checks(
            uid, display_name, all_period_entries, project, year_cal,
        )
        # Только дни, связанные с задачей
        issue_day_checks = [c for c in full_checks if c["date"] in issue_dates]
        issue_day_rows = [r for r in full_rows if r["date"] in issue_dates]

        # Day-checks только по ворклогам задачи (для сравнения)
        issue_only_rows, issue_only_checks = _build_overtime_rows_and_checks(
            uid, display_name, issue_specific_entries, project, year_cal,
        )

        # Причина исключения
        if issue_day_rows:
            exclusion_reason = "included_via_issue"
        elif not issue_specific_entries:
            exclusion_reason = "no_issue_worklogs_in_period"
        elif uid not in report_scope_user_ids:
            exclusion_reason = "user_not_in_report_scope"
        elif not all_period_entries:
            exclusion_reason = "user_has_no_period_entries"
        elif not issue_in_project_worklogs:
            exclusion_reason = "issue_not_found_by_project_jql"
        elif issue_day_checks:
            reasons = {
                c["decision_reason"]
                for c in issue_day_checks
                if not c["qualifies_overtime"]
            }
            exclusion_reason = ",".join(sorted(reasons)) if reasons else "included"
        else:
            exclusion_reason = "unknown"

        raw_author = issue_specific_entries[0] if issue_specific_entries else {}

        users.append(
            {
                "account_id": uid,
                "display_name": display_name,
                "in_issue_worklogs": bool(issue_specific_entries),
                "in_project_scope": uid in project_user_ids,
                "in_db_scope": uid in db_user_ids,
                "in_report_scope": uid in report_scope_user_ids,
                "issue_in_project_worklogs": issue_in_project_worklogs,
                "issue_entry_count": len(issue_specific_entries),
                "issue_hours": _format_hours(
                    sum(e["seconds"] for e in issue_specific_entries)
                ),
                "period_entry_count": len(all_period_entries),
                "period_hours": _format_hours(
                    sum(e["seconds"] for e in all_period_entries)
                ),
                "included_in_monthly_report": bool(issue_day_rows),
                "included_due_to_issue": bool(issue_day_rows or issue_only_rows),
                "exclusion_reason": exclusion_reason,
                "lookup_diagnostics": lookup_diagnostics,
                "lookup_diagnostics_error": lookup_diagnostics_error,
                "issue_worklogs": issue_specific_entries,
                "issue_day_checks": issue_day_checks,
                "issue_day_checks_full": issue_day_checks,
                "issue_day_checks_issue_only": issue_only_checks,
                "report_rows": issue_day_rows,
                "author_identifiers": {
                    "account_id": raw_author.get("author_account_id", ""),
                    "key": raw_author.get("author_key_field", ""),
                    "name": raw_author.get("author_name", ""),
                    "primary": raw_author.get("author_key", uid),
                },
            }
        )

    return {
        "year": body.year,
        "month": body.month,
        "project": project,
        "issue_key": issue_key,
        "issue_worklogs": issue_entries,
        "project_worklogs_user_count": len(project_worklogs),
        "project_worklogs_issue_keys": sorted(
            {e["issue_key"] for entries in project_worklogs.values() for e in entries}
        ),
        "issue_found_by_project_jql": any(
            e["issue_key"] == issue_key
            for entries in project_worklogs.values()
            for e in entries
        ),
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
        raise HTTPException(status_code=404, detail="User not found")
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
def add_vacation(account_id: str, body: VacationCreateRequest):
    date_from = body.date_from
    date_to = body.date_to
    note = body.note
    if not date_from or not date_to:
        raise HTTPException(status_code=422, detail="date_from and date_to required")
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
        if d_from > d_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")
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
    cursor = conn.execute("DELETE FROM user_vacations WHERE id = ?", (vacation_id,))
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Vacation not found")
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
        display_name = (user_row["display_name"] or uid) if user_row else uid

        month_name = f"{body.year}-{body.month:02d}"
        missing_str = ", ".join(missing)
        display_name_esc = html_mod.escape(display_name)
        project_esc = html_mod.escape(project)
        message = (
            f"Напоминание: у пользователя {display_name_esc} не залогировано время "
            f"за {month_name} в проекте {project_esc}.\n"
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

        email = (user_row["email_address"] or "") if user_row else ""
        if email:
            try:
                now_str = datetime.now().strftime("%H:%M")
                _send_email(
                    [email],
                    f"\u26a0\ufe0f Незалогированное время — {month_name} ({now_str})",
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
            summary[name] = {
                "total": 0.0,
                "workday_hours": 0.0,
                "weekend_hours": 0.0,
                "project": 0.0,
                "other": 0.0,
                "days": 0,
            }
        over_norm = float(r.get("over_norm", 0))
        summary[name]["total"] += over_norm
        if r.get("day_type") == "workday":
            summary[name]["workday_hours"] += over_norm
        else:
            summary[name]["weekend_hours"] += over_norm
        summary[name]["project"] += float(r.get("project_hours", 0))
        summary[name]["other"] += float(r.get("other_hours", 0))
        summary[name]["days"] += 1
    return summary


@router.post("/api/reports/send-overtime")
async def send_overtime_email(body: SendReportRequest):
    project = body.project or os.getenv("JIRA_PROJECT", "")

    report = await overtime_report(ReportRequest(year=body.year, month=body.month))
    rows = report["rows"]
    project = report["project"]

    if not rows:
        return {"sent": False, "reason": "no overtime data"}

    jira_url = os.getenv("JIRA_URL", "").rstrip("/")

    summary = _build_overtime_summary(rows)
    totals = {"days": 0, "project": 0.0, "other": 0.0, "workday_hours": 0.0, "weekend_hours": 0.0, "total": 0.0}
    summary_html = ""
    for name, s in sorted(summary.items()):
        for k in totals:
            totals[k] += s[k]
        name_esc = html_mod.escape(name)
        summary_html += (
            f"<tr>"
            f'<td style="padding:4px 8px;font-weight:bold">{name_esc}</td>'
            f'<td style="padding:4px 8px">{s["days"]}</td>'
            f'<td style="padding:4px 8px;color:#06b6d4">{s["project"]:.1f}h</td>'
            f'<td style="padding:4px 8px;color:gray">{s["other"]:.1f}h</td>'
            f'<td style="padding:4px 8px;color:orange">{s["workday_hours"]:.1f}h</td>'
            f'<td style="padding:4px 8px;color:red">{s["weekend_hours"]:.1f}h</td>'
            f'<td style="padding:4px 8px;color:red;font-weight:bold">+{s["total"]:.1f}h</td>'
            f"</tr>"
        )
    summary_html += (
        f'<tr style="border-top:2px solid #999;font-weight:bold">'
        f'<td style="padding:4px 8px">Итого</td>'
        f'<td style="padding:4px 8px">{totals["days"]}</td>'
        f'<td style="padding:4px 8px;color:#06b6d4">{totals["project"]:.1f}h</td>'
        f'<td style="padding:4px 8px;color:gray">{totals["other"]:.1f}h</td>'
        f'<td style="padding:4px 8px;color:orange">{totals["workday_hours"]:.1f}h</td>'
        f'<td style="padding:4px 8px;color:red">{totals["weekend_hours"]:.1f}h</td>'
        f'<td style="padding:4px 8px;color:red;font-weight:bold">+{totals["total"]:.1f}h</td>'
        f'</tr>'
    )

    html_rows = ""
    for r in rows:
        day_class = "color:red" if r.get("day_type") != "workday" else "color:orange"
        issues_links = ", ".join(
            f'<a href="{jira_url}/browse/{html_mod.escape(ik)}">{html_mod.escape(ik)}</a>' for ik in r.get("issues", [])
        )
        html_rows += (
            f'<tr style="{day_class}">'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("display_name", "")))}</td>'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("date", "")))}</td>'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("day_type", "")))}</td>'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("total_hours", "")))}h</td>'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("project_hours", "")))}h</td>'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("other_hours", "")))}h</td>'
            f'<td style="padding:4px 8px">+{html_mod.escape(str(r.get("over_norm", "")))}h</td>'
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
<td style="padding:4px 8px">{html_mod.escape(project)}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Итого рабочих дней (часы)</td>
<td style="padding:4px 8px">Итого выходных (часы)</td>
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
<td style="padding:4px 8px">{html_mod.escape(project)}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Сверх нормы</td>
<td style="padding:4px 8px">Задачи</td>
</tr>
{html_rows}
</table>
</body></html>"""

    now_str = datetime.now().strftime("%H:%M")
    _send_email(body.emails, f"\U0001f525 Отчёт по переработкам — {month_name} ({now_str})", html)
    return {"sent": True}


# ---------------------------------------------------------------------------
# API: Send time logging report
# ---------------------------------------------------------------------------


@router.post("/api/reports/send-time-logging")
async def send_time_logging_email(body: SendReportRequest):
    project = body.project or os.getenv("JIRA_PROJECT", "")

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
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("display_name", "")))}</td>'
            f'<td style="padding:4px 8px">{html_mod.escape(str(r.get("days_logged", "")))}/{html_mod.escape(str(r.get("total_workdays", "")))}</td>'
            f'<td style="padding:4px 8px;color:#06b6d4">{html_mod.escape(str(r.get("project_hours", "")))}h</td>'
            f'<td style="padding:4px 8px;color:gray">{html_mod.escape(str(r.get("other_hours", "")))}h</td>'
            f'<td style="padding:4px 8px;{missing_style}">{r.get("missing_count", 0)} дн.</td>'
            f"</tr>"
        )

    month_name = f"{body.year}-{body.month:02d}"
    html = f"""\
<html><body style="font-family:Arial,sans-serif;color:#333">
<h2>Отчёт учёта времени — {month_name}</h2>
<p>Проект: {html_mod.escape(project)}</p>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#f0f0f0;font-weight:bold">
<td style="padding:4px 8px">Пользователь</td>
<td style="padding:4px 8px">Дни</td>
<td style="padding:4px 8px">{html_mod.escape(project)}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Пропущено</td>
</tr>
{html_rows}
</table>
</body></html>"""

    now_str = datetime.now().strftime("%H:%M")
    _send_email(body.emails, f"\U0001f552 Отчёт учёта времени — {month_name} ({now_str})", html)
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
            auto_send_schedules=?,
            send_email=?, email_recipients=?, teams_webhook_url=?,
            missing_time_auto_notify=?, missing_time_interval_days=?,
            updated_at=?
           WHERE report_type=?""",
        (
            int(body.auto_send_enabled),
            body.auto_send_day,
            body.auto_send_time,
            body.auto_send_schedules,
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
async def save_calendar(year: int, body: dict):
    """Сохраняет переопределения календаря.
    Body: {overrides: {date_str: day_type}} — только дни, отличающиеся от дефолта."""
    overrides = body.get("overrides", {})
    holidays_set = {(m, d) for m, d in _RU_HOLIDAY_DATES}

    conn = get_db()
    conn.execute("DELETE FROM holiday_overrides WHERE dt LIKE ?", (f"{year}-%",))

    for dt_str, day_type in overrides.items():
        try:
            d = date.fromisoformat(dt_str)
        except (ValueError, TypeError):
            continue
        if d.year != year:
            continue
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

    if len(data) != 365 + int(calendar.isleap(year)):
        return {"ok": False, "error": f"Некорректный ответ ({len(data)} символов, ожидалось {365 + int(calendar.isleap(year))})"}

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
