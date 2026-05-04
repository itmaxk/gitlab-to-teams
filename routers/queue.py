import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_db
from services.gitlab_client import (
    get_project_id,
    get_mr_by_iid,
    create_branch,
    cherry_pick_commit,
    create_merge_request,
    approve_merge_request,
    merge_merge_request,
    find_mrs_by_source_branches,
    search_merge_requests,
    find_merged_mrs_by_branches,
    project_web_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])

JIRA_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")


class SearchJiraRequest(BaseModel):
    jira_ids: list[str]
    state: str = "merged"


class CompareBranchesRequest(BaseModel):
    jira_ids: list[str]
    source_branch: str = "master"
    target_branches: list[str] = []
    state: str = "merged"


class LoadMRsRequest(BaseModel):
    mr_ids: list[int]


class LoadFilteredRequest(BaseModel):
    mr_ids: list[int]
    target_branch: str


class CherryPickRequest(BaseModel):
    merge_commit_sha: str
    target_branch: str
    source_mr_id: int
    source_mr_title: str = ""


class SaveSessionItem(BaseModel):
    mr_id: int
    mr_title: str = ""
    mr_url: str = ""
    author: str = ""
    merged_at: str = ""
    merge_commit_sha: str = ""
    cherry_pick_branch: str = ""
    mr_create_url: str = ""
    cherry_pick_mr_url: str = ""
    cherry_pick_merged_at: str = ""


class SaveSessionRequest(BaseModel):
    name: str = ""
    target_branch: str
    items: list[SaveSessionItem]


def _mr_info(mr: dict) -> dict:
    return {
        "mr_id": mr["iid"],
        "title": mr.get("title", ""),
        "web_url": mr.get("web_url", ""),
        "state": mr.get("state", ""),
        "merged_at": mr.get("merged_at"),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
    }


def _title_has_exact_jira(title: str, jira_id: str) -> bool:
    return any(match.group(1) == jira_id for match in JIRA_RE.finditer(title or ""))


@router.post("/search-jira")
async def search_jira(data: SearchJiraRequest):
    """Ищет MR по Jira ID в title."""
    project_id = await get_project_id()
    results = []
    seen_iids = set()
    for jira_id in data.jira_ids:
        jira_id = jira_id.strip()
        if not jira_id:
            continue
        try:
            mrs = await search_merge_requests(project_id, jira_id, state=data.state)
            for mr in mrs:
                if mr["iid"] not in seen_iids:
                    seen_iids.add(mr["iid"])
                    results.append({
                        "jira_id": jira_id,
                        "mr_id": mr["iid"],
                        "title": mr["title"],
                        "web_url": mr["web_url"],
                        "state": mr["state"],
                        "merged_at": mr.get("merged_at"),
                        "target_branch": mr.get("target_branch", ""),
                    })
        except Exception as e:
            logger.warning("Ошибка поиска MR по Jira %s: %s", jira_id, e)
            results.append({
                "jira_id": jira_id,
                "mr_id": None,
                "title": "",
                "web_url": "",
                "state": "error",
                "error": str(e),
            })
    return {"results": results}


@router.post("/compare-branches")
async def compare_branches(data: CompareBranchesRequest):
    """Сверяет наличие merged MR по Jira ID в базовой и целевых ветках."""
    jira_ids = []
    seen_jira = set()
    for jira_id in data.jira_ids:
        normalized = jira_id.strip().upper()
        if normalized and normalized not in seen_jira:
            seen_jira.add(normalized)
            jira_ids.append(normalized)

    source_branch = data.source_branch.strip() or "master"
    target_branches = []
    seen_branches = {source_branch}
    for branch in data.target_branches:
        branch = branch.strip()
        if branch and branch not in seen_branches:
            seen_branches.add(branch)
            target_branches.append(branch)

    if not jira_ids:
        return {"error": "Укажите хотя бы один Jira ID"}
    if not target_branches:
        return {"error": "Укажите хотя бы одну ветку для сравнения"}

    project_id = await get_project_id()
    rows = []
    missing_by_branch: dict[str, list[int]] = {branch: [] for branch in target_branches}
    candidate_ids = []

    for jira_id in jira_ids:
        found_by_branch: dict[str, list[dict]] = {
            source_branch: [],
            **{branch: [] for branch in target_branches},
        }
        try:
            mrs = await search_merge_requests(project_id, jira_id, state=data.state, per_page=50)
        except Exception as e:
            logger.warning("Ошибка сравнения MR по Jira %s: %s", jira_id, e)
            rows.append({
                "jira_id": jira_id,
                "source_branch": source_branch,
                "target_branches": target_branches,
                "source_mrs": [],
                "targets": {
                    branch: {"status": "error", "mrs": [], "can_cherry_pick": False}
                    for branch in target_branches
                },
                "error": str(e),
            })
            continue

        for mr in mrs:
            if mr.get("state") != data.state:
                continue
            if not _title_has_exact_jira(mr.get("title", ""), jira_id):
                continue
            branch = mr.get("target_branch", "")
            if branch in found_by_branch:
                found_by_branch[branch].append(_mr_info(mr))

        source_mrs = found_by_branch[source_branch]
        source_mr_ids = [mr["mr_id"] for mr in source_mrs]
        targets = {}
        for branch in target_branches:
            branch_mrs = found_by_branch[branch]
            has_mr = bool(branch_mrs)
            can_cherry_pick = bool(source_mrs) and not has_mr
            if can_cherry_pick:
                for mr_id in source_mr_ids:
                    if mr_id not in missing_by_branch[branch]:
                        missing_by_branch[branch].append(mr_id)
                    if mr_id not in candidate_ids:
                        candidate_ids.append(mr_id)
            targets[branch] = {
                "status": "merged" if has_mr else "missing",
                "mrs": branch_mrs,
                "can_cherry_pick": can_cherry_pick,
                "source_mr_ids": source_mr_ids if can_cherry_pick else [],
            }

        rows.append({
            "jira_id": jira_id,
            "source_branch": source_branch,
            "target_branches": target_branches,
            "source_mrs": source_mrs,
            "targets": targets,
        })

    return {
        "rows": rows,
        "source_branch": source_branch,
        "target_branches": target_branches,
        "missing_by_branch": missing_by_branch,
        "candidate_mr_ids": candidate_ids,
    }


@router.post("/load")
async def load_mrs(data: LoadMRsRequest):
    """Загружает информацию о MR по списку ID и сортирует по дате мержа."""
    project_id = await get_project_id()
    mrs = []
    errors = []
    for mr_id in data.mr_ids:
        try:
            mr = await get_mr_by_iid(project_id, mr_id)
            mrs.append({
                "id": mr["iid"],
                "title": mr["title"],
                "state": mr["state"],
                "web_url": mr["web_url"],
                "author": mr.get("author", {}).get("name", ""),
                "source_branch": mr["source_branch"],
                "target_branch": mr["target_branch"],
                "merged_at": mr.get("merged_at"),
                "merge_commit_sha": mr.get("merge_commit_sha"),
            })
        except Exception as e:
            logger.warning("Не удалось загрузить MR !%s: %s", mr_id, e)
            errors.append({"id": mr_id, "error": str(e)})

    mrs.sort(key=lambda m: m["merged_at"] or "")
    return {"mrs": mrs, "errors": errors}


@router.post("/load-filtered")
async def load_mrs_filtered(data: LoadFilteredRequest):
    """Загружает MR и отфильтровывает уже cherry-picked в target_branch."""
    project_id = await get_project_id()
    mrs = []
    errors = []
    for mr_id in data.mr_ids:
        try:
            mr = await get_mr_by_iid(project_id, mr_id)
            mrs.append({
                "id": mr["iid"],
                "title": mr["title"],
                "state": mr["state"],
                "web_url": mr["web_url"],
                "author": mr.get("author", {}).get("name", ""),
                "source_branch": mr["source_branch"],
                "target_branch": mr["target_branch"],
                "merged_at": mr.get("merged_at"),
                "merge_commit_sha": mr.get("merge_commit_sha"),
            })
        except Exception as e:
            logger.warning("Не удалось загрузить MR !%s: %s", mr_id, e)
            errors.append({"id": mr_id, "error": str(e)})

    # Оставляем только MR, вмерженные НЕ в релизную ветку (т.е. в master)
    master_mrs = [mr for mr in mrs if mr["target_branch"] != data.target_branch]
    skipped_by_branch = len(mrs) - len(master_mrs)

    # Проверяем какие уже cherry-picked в релизную ветку
    cp_branches = []
    sha_to_mr = {}
    for mr in master_mrs:
        sha = mr.get("merge_commit_sha")
        if sha:
            branch = f"cherry-pick-{sha[:8]}"
            cp_branches.append(branch)
            sha_to_mr[branch] = mr["id"]

    already_picked = set()
    if cp_branches:
        merged_branches = await find_merged_mrs_by_branches(
            project_id, cp_branches, data.target_branch,
        )
        already_picked = {sha_to_mr[b] for b in merged_branches if b in sha_to_mr}

    filtered = [mr for mr in master_mrs if mr["id"] not in already_picked]
    filtered.sort(key=lambda m: m["merged_at"] or "")

    return {
        "mrs": filtered,
        "errors": errors,
        "filtered_count": len(already_picked) + skipped_by_branch,
    }


@router.post("/cherry-pick")
async def api_cherry_pick(data: CherryPickRequest):
    """Cherry-pick: создаёт ветку от target, cherry-pick коммита, возвращает URL создания MR."""
    logger.info("cherry-pick request: mr=!%s title=%r target=%s", data.source_mr_id, data.source_mr_title, data.target_branch)
    project_id = await get_project_id()
    sha_short = data.merge_commit_sha[:8]
    cp_branch = f"cherry-pick-{sha_short}"

    try:
        await create_branch(project_id, cp_branch, data.target_branch)
    except Exception as e:
        error_str = str(e)
        if "Branch already exists" in error_str or "already exists" in error_str:
            pass
        else:
            raise HTTPException(status_code=400, detail=f"Ошибка создания ветки: {error_str}")

    try:
        await cherry_pick_commit(project_id, data.merge_commit_sha, cp_branch)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка cherry-pick: {e}")

    web_url = project_web_url()
    source_enc = quote(cp_branch, safe="")
    target_enc = quote(data.target_branch, safe="")
    mr_create_url = (
        f"{web_url}/-/merge_requests/new"
        f"?merge_request%5Bsource_branch%5D={source_enc}"
        f"&merge_request%5Btarget_branch%5D={target_enc}"
        f"&merge_request%5Btitle%5D={quote(data.source_mr_title + ' ' + data.target_branch, safe='/')}"
    )

    return {
        "status": "ok",
        "cherry_pick_branch": cp_branch,
        "mr_create_url": mr_create_url,
    }


@router.post("/auto-cherry-pick")
async def api_auto_cherry_pick(data: CherryPickRequest):
    """Cherry-pick + создание MR + approve + merge — всё автоматически."""
    logger.info("auto-cherry-pick: mr=!%s title=%r target=%s", data.source_mr_id, data.source_mr_title, data.target_branch)
    project_id = await get_project_id()
    sha_short = data.merge_commit_sha[:8]
    cp_branch = f"cherry-pick-{sha_short}"

    # 1. Создаём ветку
    try:
        await create_branch(project_id, cp_branch, data.target_branch)
    except Exception as e:
        error_str = str(e)
        if "already exists" in error_str:
            pass
        else:
            raise HTTPException(status_code=400, detail=f"Ошибка создания ветки: {error_str}")

    # 2. Cherry-pick
    try:
        await cherry_pick_commit(project_id, data.merge_commit_sha, cp_branch)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка cherry-pick: {e}")

    # 3. Создаём MR
    mr_title = f"{data.source_mr_title} {data.target_branch}".strip()
    try:
        mr = await create_merge_request(project_id, cp_branch, data.target_branch, mr_title)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка создания MR: {e}")

    mr_id = mr["iid"]
    mr_url = mr["web_url"]

    # 4. Approve
    try:
        await approve_merge_request(project_id, mr_id)
    except Exception as e:
        logger.warning("approve failed for !%s: %s (continuing to merge)", mr_id, e)

    # 5. Merge
    try:
        merge_result = await merge_merge_request(project_id, mr_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"MR !{mr_id} создан, но ошибка merge: {e}")

    return {
        "status": "ok",
        "cherry_pick_branch": cp_branch,
        "mr_id": mr_id,
        "mr_url": mr_url,
        "merged_at": merge_result.get("merged_at", ""),
    }


class CheckCherryPicksRequest(BaseModel):
    source_branches: list[str]


@router.post("/check")
async def check_cherry_picks(data: CheckCherryPicksRequest):
    """Проверяет состояние cherry-pick MR по source_branch."""
    if not data.source_branches:
        return {"mrs": []}
    project_id = await get_project_id()
    mrs = await find_mrs_by_source_branches(project_id, data.source_branches)
    return {
        "mrs": [
            {
                "source_branch": mr["source_branch"],
                "state": mr["state"],
                "web_url": mr["web_url"],
            }
            for mr in mrs
        ],
    }


@router.post("/save")
def save_session(data: SaveSessionRequest):
    """Сохраняет сессию cherry-pick в БД для истории."""
    if not data.items:
        raise HTTPException(status_code=400, detail="Нет элементов для сохранения")

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO cherry_pick_sessions (name, target_branch, mr_count) VALUES (?, ?, ?)",
        (data.name, data.target_branch, len(data.items)),
    )
    session_id = cur.lastrowid
    for item in data.items:
        conn.execute(
            """INSERT INTO cherry_pick_items
               (session_id, mr_iid, mr_title, mr_url, author, merged_at,
                merge_commit_sha, cherry_pick_branch, mr_create_url,
                cherry_pick_mr_url, cherry_pick_merged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, item.mr_id, item.mr_title, item.mr_url,
                item.author, item.merged_at, item.merge_commit_sha,
                item.cherry_pick_branch, item.mr_create_url,
                item.cherry_pick_mr_url, item.cherry_pick_merged_at,
            ),
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "session_id": session_id}


@router.get("/history")
def get_history():
    """Возвращает список сессий cherry-pick."""
    conn = get_db()
    sessions = conn.execute(
        "SELECT * FROM cherry_pick_sessions ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(s) for s in sessions]


@router.get("/history/{session_id}")
def get_session(session_id: int):
    """Возвращает детали сессии cherry-pick."""
    conn = get_db()
    session = conn.execute(
        "SELECT * FROM cherry_pick_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    items = conn.execute(
        "SELECT * FROM cherry_pick_items WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    return {"session": dict(session), "items": [dict(i) for i in items]}


@router.delete("/history/{session_id}")
def delete_session(session_id: int):
    """Удаляет сессию cherry-pick."""
    conn = get_db()
    conn.execute("DELETE FROM cherry_pick_items WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM cherry_pick_sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}
