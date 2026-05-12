import os
from typing import Any

from db import get_db
from services.teams_client import send_teams_notification
from services.email_client import send_changelog_email
from services.gitlab_delivery import publish_gitlab_message, render_gitlab_message
from services.review_comment_formatter import format_gitlab_review_comment


def _is_already_sent(rule_id: int, mr_iid: int, file_path: str) -> bool:
    conn = get_db()
    row = conn.execute(
        """SELECT 1 FROM notification_log
           WHERE rule_id = ? AND mr_iid = ? AND file_path = ?
             AND (teams_sent = 1 OR email_sent = 1 OR gitlab_sent = 1)""",
        (rule_id, mr_iid, file_path),
    ).fetchone()
    conn.close()
    return row is not None


async def dispatch_notifications(
    matches: list[dict[str, Any]],
    mr_iid: int,
    mr_title: str,
    mr_url: str,
    force: bool = False,
) -> None:
    for match in matches:
        rule = match["rule"]
        file_path = match["file_path"]
        file_content = match["file_content"]
        emails = match["emails"]

        if not force and _is_already_sent(rule["id"], mr_iid, file_path):
            continue

        teams_sent = False
        email_sent = False
        gitlab_sent = False
        gitlab_discussion_id = ""
        error = ""

        webhook_url = rule["teams_webhook_url"] or os.getenv("TEAMS_WEBHOOK_URL", "")
        if rule.get("send_teams", 1) and webhook_url:
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

        if not emails:
            default_email = os.getenv("DEFAULT_EMAIL", "")
            if default_email:
                emails = [e.strip() for e in default_email.split(",") if e.strip()]

        if rule["send_email"] and emails:
            try:
                send_changelog_email(
                    recipients=emails,
                    mr_title=mr_title,
                    mr_url=mr_url,
                    file_path=file_path,
                    file_content=file_content,
                    rule_name=rule["name"],
                    match_type=rule.get("match_type", ""),
                )
                email_sent = True
            except Exception as e:
                error += f"Email: {e}\n"

        if rule.get("send_gitlab", 0):
            try:
                template = rule.get("gitlab_comment_template", "")
                if template:
                    comment = render_gitlab_message(
                        template,
                        {
                            "mr_iid": mr_iid,
                            "mr_title": mr_title,
                            "mr_url": mr_url,
                            "rule_name": rule["name"],
                            "file_path": file_path,
                            "file_content": file_content,
                        },
                    )
                else:
                    comment = format_gitlab_review_comment(
                        mr_iid=mr_iid,
                        mr_title=mr_title,
                        findings=match.get("findings", []),
                        summary=match.get("summary", {}),
                        model_used=match.get("model_used", ""),
                    )
                note = await publish_gitlab_message(
                    mr_iid,
                    comment,
                    rule.get("gitlab_comment_mode", "note"),
                )
                gitlab_discussion_id = str((note or {}).get("id") or "")
                gitlab_sent = True
            except Exception as e:
                error += f"GitLab: {e}\n"

        conn = get_db()
        conn.execute(
            """INSERT INTO notification_log
               (rule_id, mr_iid, mr_title, mr_url, file_path, file_content,
                teams_sent, email_sent, gitlab_sent, gitlab_discussion_id, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule["id"],
                mr_iid,
                mr_title,
                mr_url,
                file_path,
                file_content,
                int(teams_sent),
                int(email_sent),
                int(gitlab_sent),
                gitlab_discussion_id,
                error.strip(),
            ),
        )
        conn.commit()
        conn.close()
