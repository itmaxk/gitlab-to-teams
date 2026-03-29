import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services.gitlab_client import (
    get_project_id,
    get_all_merged_mrs,
    get_branches,
    get_mr_by_iid,
    search_merge_requests,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compare", tags=["compare"])

JIRA_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")


def _parse_dt(s: str) -> Optional[datetime]:
    """Парсит ISO дату из GitLab (с миллисекундами, Z или +00:00)."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class CompareRequest(BaseModel):
    branches: list[str]
    date_from: str = ""
    date_to: str = ""
    jira_ids: list[str] = []
    mr_ids: list[int] = []


def _mr_to_info(mr: dict, *, in_range: bool = True) -> dict:
    """Преобразует MR из GitLab API в унифицированный формат."""
    return {
        "mr_iid": mr["iid"],
        "mr_title": mr.get("title", ""),
        "mr_url": mr.get("web_url", ""),
        "source_branch": mr.get("source_branch", ""),
        "merged_at": mr.get("merged_at", ""),
        "author": mr.get("author", {}).get("name", ""),
        "in_range": in_range,
    }


def _add_to_jira_map(
    jira_map: dict, no_jira: list, mr: dict, branch: str, *, in_range: bool = True,
):
    """Добавляет MR в jira_map (по JIRA ID) или в no_jira."""
    title = mr.get("title", "")
    match = JIRA_RE.search(title)
    mr_info = _mr_to_info(mr, in_range=in_range)

    if match:
        jira_id = match.group(1)
        if jira_id not in jira_map:
            jira_map[jira_id] = {}
        if branch not in jira_map[jira_id]:
            jira_map[jira_id][branch] = []
        # Avoid duplicates by iid
        if not any(m["mr_iid"] == mr_info["mr_iid"] for m in jira_map[jira_id][branch]):
            jira_map[jira_id][branch].append(mr_info)
    else:
        no_jira.append({**mr_info, "branch": branch})


@router.get("/default-branches")
async def default_branches():
    """Возвращает master + последнюю release ветку."""
    project_id = await get_project_id()
    branches = await get_branches(project_id, search="release/")
    # Find latest branch matching exactly "release/{number}"
    release_re = re.compile(r"^release/(\d+)$")
    latest = None
    latest_num = -1
    for b in branches:
        m = release_re.match(b["name"])
        if m:
            num = int(m.group(1))
            if num > latest_num:
                latest_num = num
                latest = b["name"]
    result = ["master"]
    if latest:
        result.append(latest)
    return {"branches": result}


@router.post("/run")
async def run_compare(data: CompareRequest):
    """Сравнивает наличие JIRA-задач в MR по веткам за период."""
    branches = [b.strip() for b in data.branches if b.strip()]
    if len(branches) < 1:
        return {"error": "Укажите хотя бы одну ветку"}

    has_date_range = bool(data.date_from)
    has_ids = bool(data.jira_ids) or bool(data.mr_ids)
    if not has_date_range and not has_ids:
        return {"error": "Укажите диапазон дат или список JIRA/MR ID"}

    project_id = await get_project_id()
    jira_url = os.getenv("JIRA_URL", "").rstrip("/")

    jira_map: dict[str, dict[str, list[dict]]] = {}
    no_jira: list[dict] = []
    total_mrs = 0

    # --- Phase 1: Date range search ---
    if has_date_range:
        date_from = data.date_from
        date_to = data.date_to or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if "T" not in date_from:
            date_from += "T00:00:00Z"
        if "T" not in date_to:
            date_to += "T23:59:59Z"

        dt_from = _parse_dt(date_from)
        dt_to = _parse_dt(date_to)

        tasks = [
            get_all_merged_mrs(project_id, branch, date_from, date_to)
            for branch in branches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for branch, result in zip(branches, results):
            if isinstance(result, Exception):
                logger.warning("Ошибка загрузки MR для %s: %s", branch, result)
                continue
            for mr in result:
                merged_dt = _parse_dt(mr.get("merged_at") or "")
                if merged_dt and dt_from and dt_to and dt_from <= merged_dt <= dt_to:
                    _add_to_jira_map(jira_map, no_jira, mr, branch, in_range=True)
                    total_mrs += 1

    # --- Phase 2: Search by JIRA IDs ---
    if data.jira_ids:
        jira_ids = [j.strip() for j in data.jira_ids if j.strip()]
        search_tasks = [
            search_merge_requests(project_id, jid, state="merged", per_page=50)
            for jid in jira_ids
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        for jid, result in zip(jira_ids, search_results):
            if isinstance(result, Exception):
                logger.warning("Ошибка поиска MR по %s: %s", jid, result)
                continue
            for mr in result:
                tb = mr.get("target_branch", "")
                if tb not in branches:
                    continue
                title = mr.get("title", "")
                m = JIRA_RE.search(title)
                if not m or m.group(1) != jid:
                    continue
                if mr.get("state") != "merged":
                    continue
                _add_to_jira_map(jira_map, no_jira, mr, tb, in_range=True)
                total_mrs += 1

    # --- Phase 3: Search by MR IDs ---
    if data.mr_ids:
        mr_tasks = [get_mr_by_iid(project_id, mr_id) for mr_id in data.mr_ids]
        mr_results = await asyncio.gather(*mr_tasks, return_exceptions=True)

        for mr_id, result in zip(data.mr_ids, mr_results):
            if isinstance(result, Exception):
                logger.warning("Не удалось загрузить MR !%s: %s", mr_id, result)
                continue
            tb = result.get("target_branch", "")
            if tb not in branches:
                continue
            _add_to_jira_map(jira_map, no_jira, result, tb, in_range=True)
            total_mrs += 1

    # Backfill: for JIRA IDs found in some branches but missing in others,
    # search GitLab to find MRs merged outside the date range
    gaps: list[tuple[str, str]] = []  # (jira_id, branch)
    for jira_id, branch_data in jira_map.items():
        for branch in branches:
            if branch not in branch_data:
                gaps.append((jira_id, branch))

    if gaps:
        # Deduplicate searches: group by jira_id to avoid searching same ID multiple times
        jira_ids_to_search = {jira_id for jira_id, _ in gaps}
        search_tasks = [
            search_merge_requests(project_id, jid, state="merged", per_page=50)
            for jid in jira_ids_to_search
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # Index found MRs by (jira_id, target_branch)
        for jid, result in zip(jira_ids_to_search, search_results):
            if isinstance(result, Exception):
                logger.warning("Ошибка поиска MR по %s: %s", jid, result)
                continue
            for mr in result:
                tb = mr.get("target_branch", "")
                if tb not in branches:
                    continue
                if jid in jira_map and tb in jira_map[jid]:
                    continue  # already have data for this combo
                title = mr.get("title", "")
                if not JIRA_RE.search(title) or JIRA_RE.search(title).group(1) != jid:
                    continue  # search returned unrelated MR
                if mr.get("state") != "merged":
                    continue
                mr_info = {
                    "mr_iid": mr["iid"],
                    "mr_title": title,
                    "mr_url": mr.get("web_url", ""),
                    "source_branch": mr.get("source_branch", ""),
                    "merged_at": mr.get("merged_at", ""),
                    "author": mr.get("author", {}).get("name", ""),
                    "in_range": False,
                }
                if jid not in jira_map:
                    jira_map[jid] = {}
                if tb not in jira_map[jid]:
                    jira_map[jid][tb] = []
                jira_map[jid][tb].append(mr_info)

    # Build comparison rows
    rows = []
    for jira_id in sorted(jira_map.keys()):
        branch_data = jira_map[jira_id]
        branch_info = {}
        for branch in branches:
            mrs_in_branch = branch_data.get(branch, [])
            if not mrs_in_branch:
                branch_info[branch] = {"status": "missing", "mrs": []}
            else:
                # Classify: cherry-pick or direct/manual
                classified = []
                for m in mrs_in_branch:
                    sb = m["source_branch"]
                    if sb.startswith("cherry-pick-"):
                        status = "cherry-pick"
                    else:
                        status = "direct"
                    classified.append({**m, "status": status})

                # If all are cherry-picks, overall status is cherry-pick
                # If any is direct (non-cherry-pick), it could be direct or manual
                has_direct = any(c["status"] == "direct" for c in classified)
                has_cp = any(c["status"] == "cherry-pick" for c in classified)

                if has_direct and has_cp:
                    overall = "direct"
                elif has_direct:
                    # Check if this is the first branch (origin) or a release branch
                    if branch == branches[0]:
                        overall = "direct"
                    else:
                        overall = "manual"
                else:
                    overall = "cherry-pick"

                branch_info[branch] = {"status": overall, "mrs": classified}

        row = {
            "jira_id": jira_id,
            "jira_url": f"{jira_url}/browse/{jira_id}" if jira_url else "",
            "branches": branch_info,
        }
        rows.append(row)

    # No-JIRA items grouped by branch
    no_jira_by_branch: dict[str, list[dict]] = {}
    for item in no_jira:
        branch = item.pop("branch")
        if branch not in no_jira_by_branch:
            no_jira_by_branch[branch] = []
        no_jira_by_branch[branch].append(item)

    return {
        "rows": rows,
        "no_jira": no_jira_by_branch,
        "branches": branches,
        "total_mrs": total_mrs,
    }
