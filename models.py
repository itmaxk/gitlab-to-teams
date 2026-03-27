from pydantic import BaseModel, EmailStr


class RuleCreate(BaseModel):
    name: str
    description: str = ""
    file_pattern: str = "changelogs/unreleased/*.md"
    content_match: str = "type: breaking"
    match_type: str = "contains"
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
    match_type: str
    teams_webhook_url: str
    send_email: bool
    created_at: str
    updated_at: str
    emails: list[str] = []


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
