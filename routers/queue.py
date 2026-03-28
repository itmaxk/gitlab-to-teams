import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.gitlab_client import (
    get_project_id,
    get_mr_by_iid,
    create_branch,
    cherry_pick_commit,
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


class CheckCherryPicksRequest(BaseModel):
    source_branches: list[str]


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
    project_id = await get_project_id()
    sha_short = data.merge_commit_sha[:8]
    cp_branch = f"cherry-pick-{sha_short}-into-{data.target_branch}"

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
        f"&merge_request%5Btitle%5D=Cherry-pick+!{data.source_mr_iid}+into+{target_enc}"
    )

    return {
        "status": "ok",
        "cherry_pick_branch": cp_branch,
        "mr_create_url": mr_create_url,
    }


@router.post("/check")
async def check_cherry_picks(data: CheckCherryPicksRequest):
    """Проверяет состояние MR по списку source_branch (для отслеживания cherry-pick MR)."""
    if not data.source_branches:
        return {"mrs": []}
    project_id = await get_project_id()
    mrs = await find_mrs_by_source_branches(project_id, data.source_branches)
    return {
        "mrs": [
            {
                "iid": mr["iid"],
                "state": mr["state"],
                "source_branch": mr["source_branch"],
                "web_url": mr["web_url"],
                "merged_at": mr.get("merged_at"),
            }
            for mr in mrs
        ],
    }
