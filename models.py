from pydantic import BaseModel


class RuleCreate(BaseModel):
    name: str
    description: str = ""
    file_pattern: str = "changelogs/unreleased/*.md"
    content_match: str = "type: breaking"
    content_exclude: str = ""
    match_type: str = "contains"
    target_branch: str = "master"
    mr_state: str = "merged"
    poll_interval_seconds: int = 0
    file_check_enabled: bool = False
    file_check_path_prefix: str = ""
    file_check_mode: str = "present"
    send_teams: bool = True
    teams_webhook_url: str = ""
    send_email: bool = False
    emails: list[str] = []


class RuleUpdate(RuleCreate):
    enabled: bool = True


class RuleOut(BaseModel):
    id: int
    name: str
    description: str
    enabled: bool
    file_pattern: str
    content_match: str
    content_exclude: str
    match_type: str
    target_branch: str
    mr_state: str
    poll_interval_seconds: int
    file_check_enabled: bool
    file_check_path_prefix: str
    file_check_mode: str
    send_teams: bool
    teams_webhook_url: str
    send_email: bool
    created_at: str
    updated_at: str
    emails: list[str] = []


class ReportRequest(BaseModel):
    year: int
    month: int


class NotifyMissingRequest(BaseModel):
    user_ids: list[str]
    year: int
    month: int


class SendReportRequest(BaseModel):
    year: int
    month: int
    emails: list[str]
    rows: list[dict] = []
    project: str = ""


class ReportSettingsUpdate(BaseModel):
    auto_send_enabled: bool = False
    auto_send_day: int = 1
    auto_send_time: str = "09:00"
    send_email: bool = False
    email_recipients: str = ""
    teams_webhook_url: str = ""
    missing_time_auto_notify: bool = False
    missing_time_interval_days: int = 0


class LogOut(BaseModel):
    id: int
    rule_id: int
    rule_name: str = ""
    mr_iid: int
    mr_title: str
    mr_url: str
    file_path: str
    file_content: str
    teams_sent: bool
    email_sent: bool
    error: str
    created_at: str
