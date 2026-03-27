import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_changelog_email(
    recipients: list[str],
    mr_title: str,
    mr_url: str,
    file_path: str,
    file_content: str,
    rule_name: str,
) -> None:
    host = os.getenv("SMTP_HOST", "")
    if not host or not recipients:
        return

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{rule_name}] {mr_title}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    html = f"""\
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
  <h2>🔔 {rule_name}</h2>
  <table style="border-collapse: collapse;">
    <tr><td style="padding: 4px 12px 4px 0; font-weight: bold;">MR:</td><td><a href="{mr_url}">{mr_title}</a></td></tr>
    <tr><td style="padding: 4px 12px 4px 0; font-weight: bold;">Файл:</td><td>{file_path}</td></tr>
  </table>
  <hr style="margin: 16px 0;">
  <pre style="background: #f5f5f5; padding: 12px; border-radius: 4px; overflow-x: auto;">{file_content}</pre>
</body>
</html>"""

    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        if port == 587:
            server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())
