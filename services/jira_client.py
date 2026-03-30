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


def _author_identifier_values(author: dict) -> list[str]:
    values = [
        author.get("accountId", ""),
        author.get("key", ""),
        author.get("name", ""),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


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
        author_candidates = _author_identifier_values(author)
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
            "author_account_id": author.get("accountId", ""),
            "author_key_field": author.get("key", ""),
            "author_name": author.get("name", ""),
            "author_candidates": author_candidates,
        })
    return entries


def _match_candidate_entries(entries: list[dict], candidate: str) -> list[dict]:
    return [
        entry
        for entry in entries
        if candidate and candidate in entry.get("author_candidates", [])
    ]


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


def _dedupe_worklog_entries(entries: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    for entry in entries:
        key = (
            entry.get("issue_key", ""),
            entry.get("date", ""),
            entry.get("seconds", 0),
            entry.get("project", ""),
            entry.get("display_name", ""),
            entry.get("email", ""),
            entry.get("author_key", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


async def get_worklogs_for_users_all_projects_by_candidates(
    user_candidates: dict[str, list[str]], date_from: str, date_to: str,
) -> dict[str, list[dict]]:
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    result: dict[str, list[dict]] = {}

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        for user_id, candidates in user_candidates.items():
            entries: list[dict] = []
            unique_candidates = [
                candidate for candidate in dict.fromkeys(candidates) if candidate
            ]
            for candidate in unique_candidates:
                jql = (
                    f'worklogAuthor = "{candidate}" '
                    f'AND worklogDate >= "{date_from}" AND worklogDate <= "{date_to}"'
                )
                issues = await _fetch_all_issues(client, jql)

                for issue in issues:
                    issue_key = issue["key"]
                    issue_project = issue["fields"]["project"]["key"]
                    worklogs = await _fetch_issue_worklogs(client, issue_key)
                    extracted_entries = _extract_worklogs(
                        worklogs, issue_key, issue_project, d_from, d_to
                    )
                    entries.extend(_match_candidate_entries(extracted_entries, candidate))

            result[user_id] = _dedupe_worklog_entries(entries)

    return result


async def diagnose_worklog_author_candidates(
    candidate_ids: list[str], date_from: str, date_to: str, issue_key: str = "",
) -> dict[str, dict]:
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    result: dict[str, dict] = {}
    unique_candidates = [candidate for candidate in dict.fromkeys(candidate_ids) if candidate]

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        for candidate in unique_candidates:
            jql_parts = []
            if issue_key:
                jql_parts.append(f'issue = "{issue_key}"')
            jql_parts.append(f'worklogAuthor = "{candidate}"')
            jql_parts.append(f'worklogDate >= "{date_from}"')
            jql_parts.append(f'worklogDate <= "{date_to}"')
            jql = " AND ".join(jql_parts)
            issues = await _fetch_all_issues(client, jql)

            strict_entries: list[dict] = []
            candidate_matched_entries: list[dict] = []
            for issue in issues:
                issue_key = issue["key"]
                issue_project = issue["fields"]["project"]["key"]
                worklogs = await _fetch_issue_worklogs(client, issue_key)
                extracted_entries = _extract_worklogs(
                    worklogs,
                    issue_key,
                    issue_project,
                    d_from,
                    d_to,
                )
                strict_entries.extend(
                    _extract_worklogs(
                        worklogs,
                        issue_key,
                        issue_project,
                        d_from,
                        d_to,
                        author_filter=candidate,
                    )
                )
                candidate_matched_entries.extend(
                    _match_candidate_entries(extracted_entries, candidate)
                )

            result[candidate] = {
                "issue_key_filter": issue_key,
                "issues_found": len(issues),
                "issue_keys": sorted({issue["key"] for issue in issues}),
                "strict_entry_count": len(strict_entries),
                "strict_hours": round(
                    sum(entry["seconds"] for entry in strict_entries) / 3600, 1
                ),
                "candidate_match_entry_count": len(candidate_matched_entries),
                "candidate_match_hours": round(
                    sum(entry["seconds"] for entry in candidate_matched_entries) / 3600,
                    1,
                ),
                "candidate_match_issue_keys": sorted(
                    {entry["issue_key"] for entry in candidate_matched_entries}
                ),
            }

    return result
