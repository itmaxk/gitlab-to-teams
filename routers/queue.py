import logging
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
    project_web_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])


class LoadMRsRequest(BaseModel):
    mr_iids: list[int]


class CherryPickRequest(BaseModel):
    merge_commit_sha: str
    target_branch: str
    source_mr_iid: int
    source_mr_title: str = ""


class SaveSessionItem(BaseModel):
    mr_iid: int
    mr_title: str = ""
    mr_url: str = ""
    author: str = ""
    merged_at: str = ""
    merge_commit_sha: str = ""
    cherry_pick_branch: str = ""
    mr_create_url: str = ""


class SaveSessionRequest(BaseModel):
    target_branch: str
    items: list[SaveSessionItem]


@router.post("/load")
async def load_mrs(data: LoadMRsRequest):
    """Загружает информацию о MR по списку IID и сортирует по дате мержа."""
    project_id = await get_project_id()
    mrs = []
    errors = []
    for iid in data.mr_iids:
        try:
            mr = await get_mr_by_iid(project_id, iid)
            mrs.append({
                "iid": mr["iid"],
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
            logger.warning("Не удалось загрузить MR !%s: %s", iid, e)
            errors.append({"iid": iid, "error": str(e)})

    mrs.sort(key=lambda m: m["merged_at"] or "")
    return {"mrs": mrs, "errors": errors}


@router.post("/cherry-pick")
async def api_cherry_pick(data: CherryPickRequest):
    """Cherry-pick: создаёт ветку от target, cherry-pick коммита, возвращает URL создания MR."""
    logger.info("cherry-pick request: mr=!%s title=%r target=%s", data.source_mr_iid, data.source_mr_title, data.target_branch)
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
    logger.info("auto-cherry-pick: mr=!%s title=%r target=%s", data.source_mr_iid, data.source_mr_title, data.target_branch)
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

    mr_iid = mr["iid"]
    mr_url = mr["web_url"]

    # 4. Approve
    try:
        await approve_merge_request(project_id, mr_iid)
    except Exception as e:
        logger.warning("approve failed for !%s: %s (continuing to merge)", mr_iid, e)

    # 5. Merge
    try:
        await merge_merge_request(project_id, mr_iid)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"MR !{mr_iid} создан, но ошибка merge: {e}")

    return {
        "status": "ok",
        "cherry_pick_branch": cp_branch,
        "mr_iid": mr_iid,
        "mr_url": mr_url,
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
        "INSERT INTO cherry_pick_sessions (target_branch, mr_count) VALUES (?, ?)",
        (data.target_branch, len(data.items)),
    )
    session_id = cur.lastrowid
    for item in data.items:
        conn.execute(
            """INSERT INTO cherry_pick_items
               (session_id, mr_iid, mr_title, mr_url, author, merged_at,
                merge_commit_sha, cherry_pick_branch, mr_create_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, item.mr_iid, item.mr_title, item.mr_url,
                item.author, item.merged_at, item.merge_commit_sha,
                item.cherry_pick_branch, item.mr_create_url,
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
