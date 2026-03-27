import os
import logging

from fastapi import APIRouter, Request, HTTPException

from services.gitlab_client import get_mr_changes, get_file_content
from services.rules_engine import evaluate_rules
from services.notification_dispatcher import dispatch_notifications

router = APIRouter(prefix="/api/webhook", tags=["webhook"])
logger = logging.getLogger(__name__)


@router.post("/gitlab")
async def gitlab_webhook(request: Request):
    secret = os.getenv("GITLAB_WEBHOOK_SECRET", "")
    if secret:
        token = request.headers.get("X-Gitlab-Token", "")
        if token != secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    body = await request.json()

    if body.get("object_kind") != "merge_request":
        return {"status": "ignored", "reason": "not a merge_request event"}

    attrs = body.get("object_attributes", {})
    if attrs.get("action") != "merge":
        return {"status": "ignored", "reason": f"action is '{attrs.get('action')}', not 'merge'"}

    project_id = body.get("project", {}).get("id")
    mr_iid = attrs.get("iid")
    mr_title = attrs.get("title", "")
    mr_url = attrs.get("url", "")
    target_branch = attrs.get("target_branch", "main")

    if not project_id or not mr_iid:
        raise HTTPException(status_code=400, detail="Missing project id or MR iid")

    try:
        changed_files = await get_mr_changes(project_id, mr_iid)
    except Exception as e:
        logger.error("Failed to get MR changes: %s", e)
        raise HTTPException(status_code=502, detail=f"GitLab API error: {e}")

    async def fetch_content(file_path: str) -> str:
        return await get_file_content(project_id, file_path, target_branch)

    matches = await evaluate_rules(changed_files, fetch_content)

    if matches:
        await dispatch_notifications(matches, mr_iid, mr_title, mr_url)

    return {
        "status": "ok",
        "files_checked": len(changed_files),
        "notifications_sent": len(matches),
    }
