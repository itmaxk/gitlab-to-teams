import asyncio
import logging
import os
from datetime import date, datetime

from db import get_db

logger = logging.getLogger(__name__)


async def start_reports_scheduler():
    """Background loop: checks report_settings every 60s, triggers auto-send."""
    logger.info("Reports scheduler started")
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error("Reports scheduler error: %s", e)
        await asyncio.sleep(60)


async def _tick():
    now = datetime.now()
    conn = get_db()
    settings = conn.execute(
        "SELECT * FROM report_settings WHERE auto_send_enabled = 1"
    ).fetchall()
    conn.close()

    for s in settings:
        if now.day != s["auto_send_day"]:
            continue
        if now.strftime("%H:%M") != s["auto_send_time"]:
            continue

        today_str = now.date().isoformat()
        last_sent = s["last_auto_sent_at"] or ""
        if last_sent.startswith(today_str):
            continue

        report_type = s["report_type"]
        logger.info("Auto-generating report: %s", report_type)

        try:
            await _auto_generate_and_send(report_type, now.year, now.month, s)
            conn2 = get_db()
            conn2.execute(
                "UPDATE report_settings SET last_auto_sent_at = ? WHERE report_type = ?",
                (now.isoformat(timespec="seconds"), report_type),
            )
            conn2.commit()
            conn2.close()
        except Exception as e:
            logger.error("Auto-send %s failed: %s", report_type, e)

    await _check_missing_time_notify()


async def _auto_generate_and_send(report_type: str, year: int, month: int, settings: dict):
    from models import ReportRequest, SendReportRequest
    from routers.reports import time_logging_report, overtime_report, _send_email

    recipients_str = settings["email_recipients"] or ""
    recipients = [e.strip() for e in recipients_str.split(",") if e.strip()]

    if report_type == "time_logging":
        data = await time_logging_report(ReportRequest(year=year, month=month))
        if not recipients or not settings["send_email"]:
            return

        rows_html = ""
        for r in data["rows"]:
            missing_cls = 'color:red' if r["missing_count"] > 0 else ''
            rows_html += (
                f'<tr>'
                f'<td style="padding:4px 8px">{r["display_name"]}</td>'
                f'<td style="padding:4px 8px">{r["days_logged"]}/{r["total_workdays"]}</td>'
                f'<td style="padding:4px 8px;color:cyan">{r["project_hours"]}h</td>'
                f'<td style="padding:4px 8px;color:gray">{r["other_hours"]}h</td>'
                f'<td style="padding:4px 8px;{missing_cls}">{r["missing_count"]} дн.</td>'
                f'</tr>'
            )

        month_name = f"{year}-{month:02d}"
        html = f"""\
<html><body style="font-family:Arial,sans-serif;color:#333">
<h2>Отчёт учёта времени — {month_name}</h2>
<p>Проект: {data['project']}</p>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#f0f0f0;font-weight:bold">
<td style="padding:4px 8px">Пользователь</td>
<td style="padding:4px 8px">Дни</td>
<td style="padding:4px 8px">{data['project']}</td>
<td style="padding:4px 8px">Другие</td>
<td style="padding:4px 8px">Пропущено</td>
</tr>
{rows_html}
</table>
</body></html>"""
        _send_email(recipients, f"Отчёт учёта времени — {month_name}", html)

    elif report_type == "overtime":
        if not recipients or not settings["send_email"]:
            return
        from routers.reports import send_overtime_email
        await send_overtime_email(SendReportRequest(year=year, month=month, emails=recipients))


async def _check_missing_time_notify():
    conn = get_db()
    s = conn.execute(
        "SELECT * FROM report_settings WHERE report_type = 'time_logging'"
    ).fetchone()
    conn.close()

    if not s or not s["missing_time_auto_notify"] or s["missing_time_interval_days"] <= 0:
        return

    interval = s["missing_time_interval_days"]
    today = date.today()

    last_sent = s["last_auto_sent_at"] or ""
    if last_sent:
        try:
            last_date = date.fromisoformat(last_sent[:10])
            if (today - last_date).days < interval:
                return
        except ValueError:
            pass

    project = os.getenv("JIRA_PROJECT", "")
    if not project:
        return

    conn2 = get_db()
    users = conn2.execute("SELECT account_id FROM jira_users WHERE active = 1").fetchall()
    conn2.close()
    if not users:
        return

    user_ids = [r["account_id"] for r in users]

    from models import NotifyMissingRequest
    from routers.reports import notify_missing

    logger.info("Auto-notifying %d users about missing time", len(user_ids))
    try:
        await notify_missing(NotifyMissingRequest(
            user_ids=user_ids,
            year=today.year,
            month=today.month,
        ))
    except Exception as e:
        logger.error("Auto-notify missing time failed: %s", e)
