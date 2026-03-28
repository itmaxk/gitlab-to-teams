import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.gitlab_client import (
    get_project_id,
    get_mr_by_iid,
    create_branch,
    cherry_pick_commit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])


class LoadMRsRequest(BaseModel):
    mr_iids: list[int]


class CherryPickRequest(BaseModel):
    merge_commit_sha: str
    target_branch: str


class CreateBranchRequest(BaseModel):
    branch_name: str
    ref: str = "master"


class BatchCherryPickRequest(BaseModel):
    merge_commit_shas: list[str]
    target_branch: str


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


@router.post("/branch")
async def api_create_branch(data: CreateBranchRequest):
    """Создаёт новую ветку в GitLab."""
    project_id = await get_project_id()
    try:
        result = await create_branch(project_id, data.branch_name, data.ref)
        return {"status": "ok", "branch": result["name"]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cherry-pick")
async def api_cherry_pick(data: CherryPickRequest):
    """Cherry-pick одного коммита в указанную ветку."""
    project_id = await get_project_id()
    try:
        result = await cherry_pick_commit(
            project_id, data.merge_commit_sha, data.target_branch,
        )
        return {"status": "ok", "commit": result.get("id", "")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cherry-pick-batch")
async def api_cherry_pick_batch(data: BatchCherryPickRequest):
    """Cherry-pick списка коммитов последовательно в указанную ветку."""
    project_id = await get_project_id()
    results = []
    for sha in data.merge_commit_shas:
        try:
            result = await cherry_pick_commit(project_id, sha, data.target_branch)
            results.append({"sha": sha, "status": "ok", "commit": result.get("id", "")})
        except Exception as e:
            results.append({"sha": sha, "status": "error", "error": str(e)})
    return {"results": results}
