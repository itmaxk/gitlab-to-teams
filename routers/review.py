import json
import logging
import re

from fastapi import APIRouter, HTTPException

from db import get_db
from models import ReviewPublishRequest, ReviewRequest, ReviewSettingsUpdate
from services.gitlab_notes import post_merge_request_note
from services.review_comment_formatter import format_gitlab_review_comment
from services.review_service import review_mr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])


def _parse_mr_iid(mr_input: str) -> int:
    mr_input = mr_input.strip()
    url_match = re.search(r'/merge_requests/(\d+)', mr_input)
    if url_match:
        return int(url_match.group(1))
    digits = re.sub(r'[^0-9]', '', mr_input)
    if digits:
        return int(digits)
    raise ValueError(f"Cannot parse MR IID from: {mr_input}")


def _load_review_record(review_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM code_reviews WHERE id = ?", (review_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Review not found")

    review = dict(row)
    try:
        review["findings"] = json.loads(review.pop("findings_json"))
    except (json.JSONDecodeError, KeyError):
        review["findings"] = []
    try:
        review["summary"] = json.loads(review.pop("summary_json"))
    except (json.JSONDecodeError, KeyError):
        review["summary"] = {}
    return review


@router.post("/run")
async def run_review(req: ReviewRequest):
    try:
        mr_iid = _parse_mr_iid(req.mr_input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = await review_mr(mr_iid, req.custom_prompt)
    except Exception as e:
        logger.exception("Review failed for MR !%s", req.mr_input)
        raise HTTPException(status_code=500, detail=str(e))

    return result


@router.get("/history")
def get_history():
    conn = get_db()
    rows = conn.execute(
        """SELECT id, mr_iid, mr_title, mr_url, model_used, summary_json, created_at
           FROM code_reviews ORDER BY created_at DESC LIMIT 100"""
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["summary"] = json.loads(d.pop("summary_json"))
        except (json.JSONDecodeError, KeyError):
            d["summary"] = {}
        result.append(d)
    return result


@router.get("/settings")
def get_settings():
    conn = get_db()
    row = conn.execute("SELECT system_prompt, updated_at FROM review_settings WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {"system_prompt": "", "updated_at": ""}
    return dict(row)


@router.put("/settings")
def update_settings(req: ReviewSettingsUpdate):
    conn = get_db()
    conn.execute(
        "UPDATE review_settings SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (req.system_prompt,),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/post-comment")
async def publish_review_comment(req: ReviewPublishRequest):
    review = _load_review_record(req.review_id)
    comment = format_gitlab_review_comment(
        mr_iid=review["mr_iid"],
        mr_title=review.get("mr_title", ""),
        findings=review.get("findings", []),
        summary=review.get("summary", {}),
        model_used=review.get("model_used", ""),
    )
    try:
        note = await post_merge_request_note(review["mr_iid"], comment)
    except Exception as e:
        logger.exception("Failed to publish review comment for review %s", req.review_id)
        raise HTTPException(status_code=502, detail=f"Ошибка отправки в GitLab: {e}")

    return {
        "ok": True,
        "message": "Комментарий отправлен в GitLab",
        "note_id": note.get("id"),
        "mr_iid": review["mr_iid"],
    }


@router.get("/{review_id}")
def get_review(review_id: int):
    return _load_review_record(review_id)


@router.delete("/{review_id}")
def delete_review(review_id: int):
    conn = get_db()
    conn.execute("DELETE FROM code_reviews WHERE id = ?", (review_id,))
    conn.commit()
    conn.close()
    return {"ok": True}
