import asyncio
import base64
import logging
import os
from datetime import date

import httpx

logger = logging.getLogger(__name__)

_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(5)
    return _sem


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


async def _get_with_client(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | list:
    url = f"{_base_url()}{path}"
    async with _get_sem():
        resp = await client.get(url, headers=_auth_headers(), params=params)
        if resp.status_code >= 400:
            body = resp.text
            logger.error("JIRA GET %s: %s %s", path, resp.status_code, body)
            raise RuntimeError(f"Jira API {resp.status_code}: {body[:300]}")
        return resp.json()


async def _get(path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        return await _get_with_client(client, path, params)


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


def _extract_worklogs(worklogs: list[dict], issue_key: str, issue_project: str,
                       d_from: date, d_to: date,
                       author_filter: str | None = None) -> list[dict]:
    """Извлекает и фильтрует ворклоги по дате и опционально по автору."""
    entries = []
    for wl in worklogs:
        author = wl.get("author", {})
        author_key = author.get("accountId") or author.get("key") or author.get("name", "")
        if not author_key:
            continue
        if author_filter and author_key != author_filter:
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
            "author_key": author_key,
        })
    return entries


async def _fetch_all_issues(client: httpx.AsyncClient, jql: str) -> list[dict]:
    """Пагинированный поиск задач по JQL."""
    issues: list[dict] = []
    start_at = 0
    while True:
        data = await _get_with_client(client, "/rest/api/2/search", {
            "jql": jql,
            "fields": "key,summary,project",
            "startAt": start_at,
            "maxResults": 100,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        total = data.get("total", 0)
        start_at += len(batch)
        if start_at >= total:
            break
    return issues


async def _fetch_issue_worklogs(client: httpx.AsyncClient, issue_key: str) -> list[dict]:
    """Пагинированная загрузка ворклогов задачи."""
    all_worklogs: list[dict] = []
    wl_start = 0
    while True:
        wl_data = await _get_with_client(client, f"/rest/api/2/issue/{issue_key}/worklog", {
            "startAt": wl_start,
            "maxResults": 1000,
        })
        worklogs = wl_data.get("worklogs", [])
        all_worklogs.extend(worklogs)
        total_wl = wl_data.get("total", 0)
        wl_start += len(worklogs)
        if wl_start >= total_wl:
            break
    return all_worklogs


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
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        issues = await _fetch_all_issues(client, jql)

        result: dict[str, list[dict]] = {}
        for issue in issues:
            issue_key = issue["key"]
            issue_project = issue["fields"]["project"]["key"]
            worklogs = await _fetch_issue_worklogs(client, issue_key)
            for entry in _extract_worklogs(worklogs, issue_key, issue_project, d_from, d_to):
                result.setdefault(entry["author_key"], []).append(entry)

    return result


async def get_worklogs_for_users_all_projects(
    user_ids: list[str], date_from: str, date_to: str,
) -> dict[str, list[dict]]:
    """Для каждого пользователя ищет ворклоги во всех проектах за период."""
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    result: dict[str, list[dict]] = {}

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        for user_id in user_ids:
            jql = (
                f'worklogAuthor = "{user_id}" '
                f'AND worklogDate >= "{date_from}" AND worklogDate <= "{date_to}"'
            )
            issues = await _fetch_all_issues(client, jql)

            entries: list[dict] = []
            for issue in issues:
                issue_key = issue["key"]
                issue_project = issue["fields"]["project"]["key"]
                worklogs = await _fetch_issue_worklogs(client, issue_key)
                entries.extend(_extract_worklogs(
                    worklogs, issue_key, issue_project, d_from, d_to,
                    author_filter=user_id,
                ))

            result[user_id] = entries

    return result
