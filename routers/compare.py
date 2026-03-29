import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from services.gitlab_client import get_project_id, get_all_merged_mrs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compare", tags=["compare"])

JIRA_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")


class CompareRequest(BaseModel):
    branches: list[str]
    date_from: str
    date_to: str = ""


@router.post("/run")
async def run_compare(data: CompareRequest):
    """Сравнивает наличие JIRA-задач в MR по веткам за период."""
    branches = [b.strip() for b in data.branches if b.strip()]
    if len(branches) < 1:
        return {"error": "Укажите хотя бы одну ветку"}

    date_from = data.date_from
    date_to = data.date_to or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "T" not in date_from:
        date_from += "T00:00:00Z"
    if "T" not in date_to:
        date_to += "T23:59:59Z"

    project_id = await get_project_id()

    # Fetch MRs for all branches in parallel
    tasks = [
        get_all_merged_mrs(project_id, branch, date_from, date_to)
        for branch in branches
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    jira_url = os.getenv("JIRA_URL", "").rstrip("/")

    # branch -> list of MR dicts filtered by merged_at
    branch_mrs: dict[str, list[dict]] = {}
    total_mrs = 0
    for branch, result in zip(branches, results):
        if isinstance(result, Exception):
            logger.warning("Ошибка загрузки MR для %s: %s", branch, result)
            branch_mrs[branch] = []
            continue
        # Filter by merged_at within date range
        filtered = []
        for mr in result:
            merged_at = mr.get("merged_at") or ""
            if merged_at and date_from <= merged_at <= date_to:
                filtered.append(mr)
        branch_mrs[branch] = filtered
        total_mrs += len(filtered)

    # Group by JIRA ID
    # jira_id -> branch -> list of MR info
    jira_map: dict[str, dict[str, list[dict]]] = {}
    no_jira: list[dict] = []  # MRs without JIRA ID

    for branch in branches:
        for mr in branch_mrs[branch]:
            title = mr.get("title", "")
            match = JIRA_RE.search(title)
            source_branch = mr.get("source_branch", "")

            mr_info = {
                "mr_iid": mr["iid"],
                "mr_title": title,
                "mr_url": mr.get("web_url", ""),
                "source_branch": source_branch,
                "merged_at": mr.get("merged_at", ""),
                "author": mr.get("author", {}).get("name", ""),
            }

            if match:
                jira_id = match.group(1)
                if jira_id not in jira_map:
                    jira_map[jira_id] = {}
                if branch not in jira_map[jira_id]:
                    jira_map[jira_id][branch] = []
                jira_map[jira_id][branch].append(mr_info)
            else:
                no_jira.append({**mr_info, "branch": branch})

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
