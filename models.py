from pydantic import BaseModel, Field


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
    project_keys: str = "*"
    file_check_enabled: bool = False
    file_check_path_prefix: str = ""
    file_check_mode: str = "present"
    title_exclude: str = ""
    action_type: str = "notify"
    send_teams: bool = True
    teams_webhook_url: str = ""
    send_email: bool = False
    send_gitlab: bool = False
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
    title_exclude: str
    action_type: str
    send_teams: bool
    teams_webhook_url: str
    send_email: bool
    send_gitlab: bool
    created_at: str
    updated_at: str
    emails: list[str] = []


class ReportRequest(BaseModel):
    year: int = Field(ge=2020, le=2100)
    month: int = Field(ge=1, le=12)


class OvertimeDebugRequest(BaseModel):
    year: int = Field(ge=2020, le=2100)
    month: int = Field(ge=1, le=12)
    issue_key: str
    # Опциональные фильтры диагностики: если заданы, подробная информация
    # выводится только для указанного пользователя Jira и/или даты.
    user_id: str | None = None
    date: str | None = None


class NotifyMissingRequest(BaseModel):
    user_ids: list[str]
    year: int = Field(ge=2020, le=2100)
    month: int = Field(ge=1, le=12)


class SendReportRequest(BaseModel):
    year: int = Field(ge=2020, le=2100)
    month: int = Field(ge=1, le=12)
    emails: list[str]
    rows: list[dict] = []
    project: str = ""


class VacationCreateRequest(BaseModel):
    date_from: str
    date_to: str
    note: str = ""


class ReportSettingsUpdate(BaseModel):
    auto_send_enabled: bool = False
    auto_send_day: int = 1
    auto_send_time: str = "09:00"
    auto_send_schedules: str = ""  # JSON array: [{"day":1,"time":"09:00"}, ...]
    send_email: bool = False
    email_recipients: str = ""
    teams_webhook_url: str = ""
    missing_time_auto_notify: bool = False
    missing_time_interval_days: int = 0


class ReviewRequest(BaseModel):
    mr_input: str
    custom_prompt: str = ""


class XlsxReviewRequest(BaseModel):
    mr_input: str
    base_ref: str = ""


class ReviewFinding(BaseModel):
    severity: str
    category: str
    file_path: str
    line: int | None = None
    message: str
    suggestion: str | None = None


class ReviewSettingsUpdate(BaseModel):
    system_prompt: str
    review_instructions: str = ""
    active_project_profile_id: int | None = None
    review_project_root: str = ""
    review_project_config_path: str = "configuration/@config-rgsl"
    review_sql_target: str = "PostgreSQL 17.5+"
    review_graph_context_enabled: bool = True
    review_graph_context_max_files: int = Field(default=12, ge=1, le=50)


class ReviewProjectProfileRequest(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    is_default: bool = False
    project_root: str = ""
    config_path: str = "configuration/@config-rgsl"
    sql_target: str = "PostgreSQL 17.5+"
    graph_context_enabled: bool = True
    graph_context_max_files: int = Field(default=12, ge=1, le=50)
    profile_json: dict


class ReviewProjectProfilePreviewRequest(BaseModel):
    changed_paths: list[str]


class ReviewInstructionItemCreate(BaseModel):
    instruction_text: str
    instruction_type: str = "include"


class ReviewInstructionItemUpdate(BaseModel):
    instruction_text: str
    instruction_type: str = "include"


class ReviewPublishRequest(BaseModel):
    review_id: int


class ReviewPublishFindingRequest(BaseModel):
    review_id: int
    finding_index: int = Field(ge=0)


class ReviewEmailRequest(BaseModel):
    review_id: int
    recipients: list[str]


class ReviewRunEmailRequest(BaseModel):
    mr_input: str
    recipients: list[str]
    custom_prompt: str = ""


class SchemaRequest(BaseModel):
    mr_ids: list[str]
    target_branch: str = "master"


class DatabaseRequest(BaseModel):
    mr_ids: list[str]
    target_branch: str = "master"


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
    gitlab_sent: bool
    error: str
    created_at: str
