import asyncio
import base64
import logging
import os
from datetime import date

import httpx

logger = logging.getLogger(__name__)

_sem = asyncio.Semaphore(5)


def _base_url() -> str:
    return os.getenv("JIRA_URL", "").rstrip("/")


def _auth_headers() -> dict[str, str]:
    token = os.getenv("JIRA_TOKEN", "")
    if ":" in token:
        encoded = base64.b64encode(token.encode()).decode()
        auth = f"Basic {encoded}"
    else:
        auth = f"Bearer {token}"
    return {
        "Authorization": auth,
        "Content-Type": "application/json",
    }


async def _get(path: str, params: dict | None = None) -> dict | list:
    url = f"{_base_url()}{path}"
    async with _sem:
        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            resp = await client.get(url, headers=_auth_headers(), params=params)
            if resp.status_code >= 400:
                body = resp.text
                logger.error("JIRA GET %s: %s %s", path, resp.status_code, body)
                raise RuntimeError(f"Jira API {resp.status_code}: {body[:300]}")
            return resp.json()


async def search_issues(jql: str, fields: str = "key,summary,project", start_at: int = 0, max_results: int = 100) -> dict:
    return await _get("/rest/api/2/search", {
        "jql": jql,
        "fields": fields,
        "startAt": start_at,
        "maxResults": max_results,
    })


async def get_issue_worklogs(issue_key: str, start_at: int = 0, max_results: int = 1000) -> dict:
    return await _get(f"/rest/api/2/issue/{issue_key}/worklog", {
        "startAt": start_at,
        "maxResults": max_results,
    })


async def get_all_worklogs_for_project(
    project: str, date_from: str, date_to: str,
) -> dict[str, list[dict]]:
    """Собирает все ворклоги по проекту за период, группирует по автору.

    Returns: {author_key: [{issue_key, date, seconds, project, display_name, email}]}
    """
    jql = (
        f'project = "{project}" '
        f'AND worklogDate >= "{date_from}" AND worklogDate <= "{date_to}"'
    )
    issues: list[dict] = []
    start_at = 0
    while True:
        data = await search_issues(jql, start_at=start_at)
        issues.extend(data.get("issues", []))
        total = data.get("total", 0)
        start_at += len(data.get("issues", []))
        if start_at >= total:
            break

    result: dict[str, list[dict]] = {}
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)

    for issue in issues:
        issue_key = issue["key"]
        issue_project = issue["fields"]["project"]["key"]
        wl_start = 0
        while True:
            wl_data = await get_issue_worklogs(issue_key, start_at=wl_start)
            worklogs = wl_data.get("worklogs", [])
            for wl in worklogs:
                started = wl.get("started", "")[:10]
                if not started:
                    continue
                wl_date = date.fromisoformat(started)
                if wl_date < d_from or wl_date > d_to:
                    continue
                author = wl.get("author", {})
                author_key = author.get("accountId") or author.get("key") or author.get("name", "")
                if not author_key:
                    continue
                entry = {
                    "issue_key": issue_key,
                    "date": started,
                    "seconds": wl.get("timeSpentSeconds", 0),
                    "project": issue_project,
                    "display_name": author.get("displayName", ""),
                    "email": author.get("emailAddress", ""),
                }
                result.setdefault(author_key, []).append(entry)

            total_wl = wl_data.get("total", 0)
            wl_start += len(worklogs)
            if wl_start >= total_wl:
                break

    return result


async def get_worklogs_for_users_all_projects(
    user_ids: list[str], date_from: str, date_to: str,
) -> dict[str, list[dict]]:
    """Для каждого пользователя ищет ворклоги во всех проектах за период."""
    result: dict[str, list[dict]] = {}
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)

    for user_id in user_ids:
        jql = (
            f'worklogAuthor = "{user_id}" '
            f'AND worklogDate >= "{date_from}" AND worklogDate <= "{date_to}"'
        )
        issues: list[dict] = []
        start_at = 0
        while True:
            data = await search_issues(jql, start_at=start_at)
            issues.extend(data.get("issues", []))
            total = data.get("total", 0)
            start_at += len(data.get("issues", []))
            if start_at >= total:
                break

        entries: list[dict] = []
        for issue in issues:
            issue_key = issue["key"]
            issue_project = issue["fields"]["project"]["key"]
            wl_start = 0
            while True:
                wl_data = await get_issue_worklogs(issue_key, start_at=wl_start)
                worklogs = wl_data.get("worklogs", [])
                for wl in worklogs:
                    author = wl.get("author", {})
                    wl_author = author.get("accountId") or author.get("key") or author.get("name", "")
                    if wl_author != user_id:
                        continue
                    started = wl.get("started", "")[:10]
                    if not started:
                        continue
                    wl_date = date.fromisoformat(started)
                    if wl_date < d_from or wl_date > d_to:
                        continue
                    entries.append({
                        "issue_key": issue_key,
                        "date": started,
                        "seconds": wl.get("timeSpentSeconds", 0),
                        "project": issue_project,
                        "display_name": author.get("displayName", ""),
                        "email": author.get("emailAddress", ""),
                    })
                total_wl = wl_data.get("total", 0)
                wl_start += len(worklogs)
                if wl_start >= total_wl:
                    break

        result[user_id] = entries

    return result
