import asyncio
import difflib
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services.gitlab_client import (
    get_project_id,
    get_all_merged_mrs,
    get_branches,
    get_mr_by_iid,
    get_mr_diff,
    search_merge_requests,
    clear_mr_diff_cache,
    get_mr_diff_cache_info,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compare", tags=["compare"])

JIRA_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")
MANUAL_PICK_DIFF_THRESHOLD = 0.8
MANUAL_PICK_TITLE_HINT_THRESHOLD = 0.7


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
    include_change_stats: bool = True


def _mr_to_info(mr: dict, *, in_range: bool = True) -> dict:
    """Преобразует MR из GitLab API в унифицированный формат."""
    return {
        "mr_iid": mr["iid"],
        "mr_title": mr.get("title", ""),
        "mr_url": mr.get("web_url", ""),
        "mr_state": mr.get("state", ""),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
        "merged_at": mr.get("merged_at", ""),
        "merge_commit_sha": mr.get("merge_commit_sha") or "",
        "author": mr.get("author", {}).get("name", ""),
        "in_range": in_range,
        "cherry_pick_key": "",
        "cherry_pick_of": None,
        "cherry_picked_to": [],
        "cherry_pick_group": None,
        "similar_pick_group": None,
        "similar_pick_matches": [],
        "change_stats": {
            "file_count": None,
            "total_changed_lines": None,
            "files": [],
            "signature": [],
            "error": "",
            "loaded": False,
        },
    }


def _changed_line_count(diff: str) -> int:
    count = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def _change_stats_from_diff(diff_data: dict) -> dict:
    files = []
    total = 0
    signature = []
    for change in diff_data.get("changes", []):
        path = change.get("new_path") or change.get("old_path") or ""
        diff = change.get("diff", "")
        changed_lines = _changed_line_count(diff)
        total += changed_lines
        signature.extend(_diff_signature(path, diff))
        files.append({
            "path": path,
            "changed_lines": changed_lines,
        })
    files.sort(key=lambda item: (-item["changed_lines"], item["path"]))
    return {
        "file_count": len(files),
        "total_changed_lines": total,
        "files": files,
        "signature": sorted(signature),
        "error": "",
        "loaded": True,
    }


def _diff_signature(path: str, diff: str) -> list[str]:
    result = []
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if not (line.startswith("+") or line.startswith("-")):
            continue
        normalized = re.sub(r"\s+", " ", line[1:].strip()).lower()
        if not normalized:
            continue
        result.append(f"{path}|{line[:1]}|{normalized}")
    return result


def _multiset_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    shared = sum((left_counts & right_counts).values())
    return (2 * shared) / (len(left) + len(right))


def _title_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(
        None,
        (left or "").strip().lower(),
        (right or "").strip().lower(),
    ).ratio()


def _iter_mr_infos(jira_map: dict, no_jira: list):
    for branch_data in jira_map.values():
        for mrs in branch_data.values():
            yield from mrs
    yield from no_jira


async def _attach_change_stats(jira_map: dict, no_jira: list, project_id: int) -> None:
    by_iid = {}
    for mr_info in _iter_mr_infos(jira_map, no_jira):
        by_iid.setdefault(mr_info["mr_iid"], []).append(mr_info)

    semaphore = asyncio.Semaphore(16)

    async def load_one(mr_iid: int):
        async with semaphore:
            try:
                diff_data = await get_mr_diff(project_id, mr_iid)
                return mr_iid, _change_stats_from_diff(diff_data)
            except Exception as exc:
                logger.warning("Не удалось загрузить diff для MR !%s: %s", mr_iid, exc)
                return mr_iid, {
                    "file_count": None,
                    "total_changed_lines": None,
                    "files": [],
                    "signature": [],
                    "error": str(exc),
                    "loaded": False,
                }

    if not by_iid:
        return

    results = await asyncio.gather(*(load_one(mr_iid) for mr_iid in by_iid))
    for mr_iid, stats in results:
        for mr_info in by_iid[mr_iid]:
            mr_info["change_stats"] = stats


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


def _cherry_pick_key_from_source_branch(source_branch: str) -> str:
    prefix = "cherry-pick-"
    if not source_branch.startswith(prefix):
        return ""
    return source_branch[len(prefix):].strip().lower()


def _cherry_pick_key_from_merge_sha(merge_commit_sha: str) -> str:
    return (merge_commit_sha or "").strip()[:8].lower()


def _annotate_cherry_pick_links(branch_data: dict[str, list[dict]]) -> None:
    mr_refs: list[dict] = []
    branch_by_iid: dict[int, str] = {}
    source_by_key: dict[str, list[dict]] = {}
    graph: dict[int, set[int]] = {}

    for branch, mrs in branch_data.items():
        for mr in mrs:
            mr_refs.append(mr)
            branch_by_iid[mr["mr_iid"]] = branch
            graph.setdefault(mr["mr_iid"], set())
            mr.setdefault("cherry_pick_key", "")
            mr["cherry_pick_of"] = None
            mr["cherry_picked_to"] = []
            mr["cherry_pick_group"] = None
            mr["similar_pick_group"] = None
            mr["similar_pick_matches"] = []

            source_key = _cherry_pick_key_from_merge_sha(mr.get("merge_commit_sha", ""))
            if source_key:
                mr["cherry_pick_key"] = source_key
                source_by_key.setdefault(source_key, []).append(mr)

    for mr in mr_refs:
        cp_key = _cherry_pick_key_from_source_branch(mr.get("source_branch", ""))
        if not cp_key:
            continue
        mr["cherry_pick_key"] = cp_key
        source_mrs = source_by_key.get(cp_key, [])
        if not source_mrs:
            continue

        source_mr = source_mrs[0]
        mr["cherry_pick_of"] = {
            "mr_iid": source_mr["mr_iid"],
            "mr_url": source_mr["mr_url"],
            "target_branch": branch_by_iid.get(source_mr["mr_iid"], source_mr.get("target_branch", "")),
        }
        for source in source_mrs:
            source["cherry_picked_to"].append({
                "mr_iid": mr["mr_iid"],
                "mr_url": mr["mr_url"],
                "target_branch": branch_by_iid.get(mr["mr_iid"], mr.get("target_branch", "")),
            })
            graph.setdefault(source["mr_iid"], set()).add(mr["mr_iid"])
            graph.setdefault(mr["mr_iid"], set()).add(source["mr_iid"])

    group = 1
    visited: set[int] = set()
    mr_by_iid = {mr["mr_iid"]: mr for mr in mr_refs}
    for start_iid in sorted(graph):
        if start_iid in visited or not graph[start_iid]:
            continue
        stack = [start_iid]
        component = []
        visited.add(start_iid)
        while stack:
            iid = stack.pop()
            component.append(iid)
            for next_iid in graph.get(iid, set()):
                if next_iid in visited:
                    continue
                visited.add(next_iid)
                stack.append(next_iid)
        for iid in component:
            mr_by_iid[iid]["cherry_pick_group"] = group
        group += 1

    for mr in mr_refs:
        mr["cherry_picked_to"].sort(key=lambda item: (item["target_branch"], item["mr_iid"]))
        if mr["cherry_pick_of"] and mr["cherry_pick_group"]:
            mr["cherry_pick_of"] = {
                **mr["cherry_pick_of"],
                "group": mr["cherry_pick_group"],
            }
        if mr["cherry_pick_group"]:
            mr["cherry_picked_to"] = [
                {**item, "group": mr["cherry_pick_group"]}
                for item in mr["cherry_picked_to"]
            ]


def _annotate_similar_diff_links(branch_data: dict[str, list[dict]]) -> None:
    candidates = [
        mr
        for mrs in branch_data.values()
        for mr in mrs
        if not mr.get("cherry_pick_group")
        and mr.get("change_stats", {}).get("loaded")
        and mr.get("change_stats", {}).get("signature")
    ]
    if len(candidates) < 2:
        return

    graph: dict[int, set[int]] = {mr["mr_iid"]: set() for mr in candidates}
    edge_meta: dict[tuple[int, int], dict] = {}

    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            if left.get("target_branch") == right.get("target_branch"):
                continue
            left_signature = left.get("change_stats", {}).get("signature", [])
            right_signature = right.get("change_stats", {}).get("signature", [])
            diff_score = _multiset_similarity(left_signature, right_signature)
            if diff_score < MANUAL_PICK_DIFF_THRESHOLD:
                continue

            title_score = _title_similarity(left.get("mr_title", ""), right.get("mr_title", ""))
            graph[left["mr_iid"]].add(right["mr_iid"])
            graph[right["mr_iid"]].add(left["mr_iid"])
            key = tuple(sorted((left["mr_iid"], right["mr_iid"])))
            edge_meta[key] = {
                "diff_similarity": round(diff_score, 3),
                "title_similarity": round(title_score, 3),
                "title_hint": title_score >= MANUAL_PICK_TITLE_HINT_THRESHOLD,
            }

    existing_groups = [
        mr.get("cherry_pick_group") or 0
        for mrs in branch_data.values()
        for mr in mrs
    ]
    group = max(existing_groups, default=0) + 1
    visited: set[int] = set()
    mr_by_iid = {mr["mr_iid"]: mr for mr in candidates}

    for start_iid in sorted(graph):
        if start_iid in visited or not graph[start_iid]:
            continue
        stack = [start_iid]
        component = []
        visited.add(start_iid)
        while stack:
            iid = stack.pop()
            component.append(iid)
            for next_iid in graph.get(iid, set()):
                if next_iid in visited:
                    continue
                visited.add(next_iid)
                stack.append(next_iid)

        for iid in component:
            mr = mr_by_iid[iid]
            mr["similar_pick_group"] = group
            matches = []
            for other_iid in sorted(graph[iid]):
                other = mr_by_iid[other_iid]
                key = tuple(sorted((iid, other_iid)))
                matches.append({
                    "mr_iid": other["mr_iid"],
                    "mr_url": other["mr_url"],
                    "target_branch": other.get("target_branch", ""),
                    **edge_meta[key],
                    "group": group,
                })
            mr["similar_pick_matches"] = matches
        group += 1


@router.post("/clear-cache")
async def clear_cache():
    """Очищает кэш diff-данных MR. Используйте для принудительного обновления."""
    clear_mr_diff_cache()
    return {"status": "ok", "message": "Кэш очищен"}


@router.get("/cache-info")
async def cache_info():
    """Возвращает информацию о состоянии кэша diff-данных."""
    info = get_mr_diff_cache_info()
    return {"status": "ok", "cache": info}


@router.get("/default-branches")
async def default_branches():
    """Возвращает master + две последние release ветки."""
    project_id = await get_project_id()
    # Paginate through all branches matching "release/" to find latest releases.
    release_re = re.compile(r"^release/(\d+)$")
    releases = []
    page = 1
    while True:
        batch = await get_branches(project_id, search="release/", per_page=100, page=page)
        if not batch:
            break
        for b in batch:
            m = release_re.match(b["name"])
            if m:
                releases.append((int(m.group(1)), b["name"]))
        if len(batch) < 100:
            break
        page += 1
    result = ["master"]
    result.extend(name for _, name in sorted(releases, reverse=True)[:2])
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
            search_merge_requests(project_id, jid, state="all", per_page=50)
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
            search_merge_requests(project_id, jid, state="all", per_page=50)
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
                mr_info = _mr_to_info(mr, in_range=False)
                if jid not in jira_map:
                    jira_map[jid] = {}
                if tb not in jira_map[jid]:
                    jira_map[jid][tb] = []
                jira_map[jid][tb].append(mr_info)

    if data.include_change_stats:
        await _attach_change_stats(jira_map, no_jira, project_id)

    # Build comparison rows
    rows = []
    for jira_id in sorted(jira_map.keys()):
        branch_data = jira_map[jira_id]
        _annotate_cherry_pick_links(branch_data)
        if data.include_change_stats:
            _annotate_similar_diff_links(branch_data)
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
        "change_stats_loaded": data.include_change_stats,
    }
