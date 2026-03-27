import os
from typing import Any

from db import get_db
from services.teams_client import send_teams_notification
from services.email_client import send_changelog_email


async def dispatch_notifications(
    matches: list[dict[str, Any]],
    mr_iid: int,
    mr_title: str,
    mr_url: str,
) -> None:
    for match in matches:
        rule = match["rule"]
        file_path = match["file_path"]
        file_content = match["file_content"]
        emails = match["emails"]

        teams_sent = False
        email_sent = False
        error = ""

        webhook_url = rule["teams_webhook_url"] or os.getenv("TEAMS_WEBHOOK_URL", "")
        if webhook_url:
            try:
                await send_teams_notification(
                    webhook_url=webhook_url,
                    mr_title=mr_title,
                    mr_url=mr_url,
                    file_path=file_path,
                    file_content=file_content,
                    rule_name=rule["name"],
                )
                teams_sent = True
            except Exception as e:
                error += f"Teams: {e}\n"

        if rule["send_email"] and emails:
            try:
                send_changelog_email(
                    recipients=emails,
                    mr_title=mr_title,
                    mr_url=mr_url,
                    file_path=file_path,
                    file_content=file_content,
                    rule_name=rule["name"],
                )
                email_sent = True
            except Exception as e:
                error += f"Email: {e}\n"

        conn = get_db()
        conn.execute(
            """INSERT INTO notification_log
               (rule_id, mr_iid, mr_title, mr_url, file_path, file_content, teams_sent, email_sent, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule["id"],
                mr_iid,
                mr_title,
                mr_url,
                file_path,
                file_content,
                int(teams_sent),
                int(email_sent),
                error.strip(),
            ),
        )
        conn.commit()
        conn.close()
