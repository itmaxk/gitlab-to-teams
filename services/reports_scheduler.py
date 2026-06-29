import asyncio
import json
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
        # Build list of schedules: new multi-schedule or legacy single day/time
        schedules = []
        raw_schedules = s["auto_send_schedules"] if "auto_send_schedules" in s.keys() else ""
        if raw_schedules:
            try:
                schedules = json.loads(raw_schedules)
            except (json.JSONDecodeError, TypeError):
                pass
        if not schedules:
            schedules = [{"day": s["auto_send_day"], "time": s["auto_send_time"]}]

        # Check if any schedule matches now
        matched = False
        for sched in schedules:
            if now.day == sched.get("day") and now.strftime("%H:%M") == sched.get("time"):
                matched = True
                break
        if not matched:
            continue

        today_str = now.date().isoformat()
        current_time = now.strftime("%H:%M")
        last_sent = s["last_auto_sent_at"] or ""
        # Prevent duplicate: check if already sent today at this exact time
        if last_sent.startswith(today_str + "T" + current_time):
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
    from models import SendReportRequest

    recipients_str = settings["email_recipients"] or ""
    recipients = [e.strip() for e in recipients_str.split(",") if e.strip()]

    if not recipients or not settings["send_email"]:
        return

    # Используем те же отрисовщики, что и ручная отправка, чтобы дизайн письма
    # (Outlook-вёрстка, KPI-карточки) был единым для авто- и ручной рассылки.
    if report_type == "time_logging":
        from routers.reports import send_time_logging_email

        await send_time_logging_email(
            SendReportRequest(year=year, month=month, emails=recipients)
        )

    elif report_type == "overtime":
        from routers.reports import send_overtime_email

        await send_overtime_email(
            SendReportRequest(year=year, month=month, emails=recipients)
        )


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

    last_missing = s["last_missing_notify_at"] or ""
    if last_missing:
        try:
            last_missing_date = date.fromisoformat(last_missing[:10])
            if (today - last_missing_date).days < interval:
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
        conn3 = get_db()
        conn3.execute(
            "UPDATE report_settings SET last_missing_notify_at = ? WHERE report_type = 'time_logging'",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn3.commit()
        conn3.close()
    except Exception as e:
        logger.error("Auto-notify missing time failed: %s", e)
