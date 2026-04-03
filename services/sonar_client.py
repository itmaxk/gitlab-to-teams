import logging
import os
import re
from urllib.parse import urlparse, parse_qs

import httpx

logger = logging.getLogger(__name__)


def _sonar_base_url() -> str:
    return os.getenv("SONAR_URL", "").rstrip("/")


def _sonar_headers() -> dict[str, str]:
    token = os.getenv("SONAR_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def parse_sonar_url(url: str) -> dict:
    """Извлекает projectKey, pullRequest, issueStatuses из URL SonarQube."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    project_key = params.get("id", [None])[0]
    pull_request = params.get("pullRequest", [None])[0]
    issue_statuses = params.get("issueStatuses", ["OPEN"])[0]
    if not project_key or not pull_request:
        raise ValueError('URL SonarQube должен содержать параметры "id" и "pullRequest"')
    return {
        "project_key": project_key,
        "pull_request": pull_request,
        "issue_statuses": issue_statuses,
    }


async def fetch_sonar_issues(sonar_url: str) -> dict:
    """Получает issues из SonarQube API. Возвращает {issues, total, formatted}."""
    parsed = parse_sonar_url(sonar_url)
    api_url = f"{_sonar_base_url()}/api/issues/search"
    params = {
        "components": parsed["project_key"],
        "pullRequest": parsed["pull_request"],
        "issueStatuses": parsed["issue_statuses"],
        "ps": "100",
    }
    logger.info("Fetching SonarQube issues: %s params=%s", api_url, params)
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(api_url, headers=_sonar_headers(), params=params)
        resp.raise_for_status()
    data = resp.json()
    issues = data.get("issues", [])
    total = data.get("total", len(issues))
    return {
        "issues": issues,
        "total": total,
        "formatted": format_issues(issues, total),
    }


def format_issues(issues: list[dict], total: int) -> str:
    """Форматирует issues, группируя по severity."""
    if not issues:
        return "No issues found."

    severity_order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
    grouped: dict[str, list[dict]] = {}
    for issue in sorted(issues, key=lambda i: severity_order.index(i.get("severity", "INFO"))
                        if i.get("severity") in severity_order else 99):
        sev = issue.get("severity", "UNKNOWN")
        grouped.setdefault(sev, []).append(issue)

    lines = [f"Total Issues: {total}\n"]
    for sev in severity_order:
        group = grouped.get(sev)
        if not group:
            continue
        lines.append("━" * 38)
        lines.append(f"{sev} ({len(group)} issues)")
        lines.append("━" * 38)
        lines.append("")
        for issue in group:
            component = issue.get("component", "")
            line_num = issue.get("line")
            loc = f"{component}:{line_num}" if line_num else component
            lines.append(f"  {loc}")
            lines.append(f"   Message: {issue.get('message', '')}")
            lines.append(f"   Status: {issue.get('status', '')}")
            lines.append("")
    return "\n".join(lines)


def build_sonar_url(mr_id: int | str) -> str:
    """Генерирует URL SonarQube по MR ID из env-переменных."""
    base = _sonar_base_url()
    project = os.getenv("SONAR_PROJECT", "")
    return f"{base}/project/issues?id={project}&pullRequest={mr_id}&issueStatuses=OPEN"


SEVERITY_COLORS = {
    "BLOCKER": "#991b1b",
    "CRITICAL": "#dc2626",
    "MAJOR": "#ea580c",
    "MINOR": "#eab308",
    "INFO": "#facc15",
}

SEVERITY_EMOJI = {
    "BLOCKER": "\U0001F534",   # red circle
    "CRITICAL": "\U0001F525",  # fire
    "MAJOR": "\U0001F7E0",     # orange circle
    "MINOR": "\U0001F7E1",     # yellow circle
    "INFO": "\u2B50",          # star
}


def format_gitlab_comment(sonar_url: str, formatted_issues: str, raw_issues: list[dict] | None = None) -> str:
    """Формирует markdown-комментарий для GitLab MR с цветными значками."""
    if raw_issues:
        return _format_gitlab_comment_rich(sonar_url, raw_issues)
    return (
        "## SonarQube Analysis Results\n\n"
        f"[View Analysis on SonarQube]({sonar_url})\n\n"
        "### Issues Found\n\n"
        f"```\n{formatted_issues}\n```\n"
    )


def _format_gitlab_comment_rich(sonar_url: str, issues: list[dict]) -> str:
    """Формирует rich-markdown комментарий с emoji по severity."""
    severity_order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
    grouped: dict[str, list[dict]] = {}
    for issue in issues:
        sev = issue.get("severity", "INFO")
        grouped.setdefault(sev, []).append(issue)

    total = len(issues)
    lines = [
        "## SonarQube Analysis Results\n",
        f"[View Analysis on SonarQube]({sonar_url})\n",
        f"**Total Issues: {total}**\n",
    ]

    for sev in severity_order:
        group = grouped.get(sev)
        if not group:
            continue
        emoji = SEVERITY_EMOJI.get(sev, "\u2753")
        lines.append(f"### {emoji} {sev} ({len(group)})\n")
        lines.append("| | File | Message |")
        lines.append("|---|---|---|")
        for issue in group:
            component = issue.get("component", "")
            short = component.split(":")[-1] if ":" in component else component
            line_num = issue.get("line")
            loc = f"`{short}:{line_num}`" if line_num else f"`{short}`"
            msg = issue.get("message", "").replace("|", "\\|")
            lines.append(f"| {emoji} | {loc} | {msg} |")
        lines.append("")

    return "\n".join(lines)


def extract_sonar_link(description: str | None) -> str | None:
    """Извлекает ссылку на SonarQube из описания MR."""
    if not description:
        return None
    sonar_base = _sonar_base_url()
    if not sonar_base:
        return None
    pattern = re.escape(sonar_base) + r"[^\s\)\]\"']+"
    match = re.search(pattern, description)
    return match.group(0) if match else None
