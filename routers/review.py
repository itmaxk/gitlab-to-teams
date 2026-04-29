import asyncio
import json
import logging
import re
import uuid

from fastapi import APIRouter, HTTPException

from db import get_db
from models import ReviewPublishRequest, ReviewRequest, ReviewSettingsUpdate
from services.gitlab_notes import post_merge_request_note
from services.review_comment_formatter import format_gitlab_review_comment
from services.review_service import review_mr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])
REVIEW_JOBS: dict[str, dict] = {}


def _parse_mr_iid(mr_input: str) -> int:
    mr_input = mr_input.strip()
    url_match = re.search(r"/merge_requests/(\d+)", mr_input)
    if url_match:
        return int(url_match.group(1))
    digits = re.sub(r"[^0-9]", "", mr_input)
    if digits:
        return int(digits)
    raise ValueError(f"Не удалось определить IID MR из значения: {mr_input}")


def _load_review_record(review_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM code_reviews WHERE id = ?", (review_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Ревью не найдено")

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


def _translate_review_error(exc: Exception) -> str:
    message = str(exc)
    if "REVIEW_API_URL not configured" in message:
        return "Не настроен REVIEW_API_URL для LLM-ревью"
    if "429" in message:
        return "LLM API временно ограничил запросы (429). Попробуйте позже или уменьшите размер diff."
    if "Cannot parse" in message or "Не удалось определить IID" in message:
        return message
    if message:
        return f"Ошибка LLM-ревью: {message}"
    return "Неизвестная ошибка LLM-ревью"


async def _run_review_job(job_id: str, mr_iid: int, custom_prompt: str) -> None:
    job = REVIEW_JOBS[job_id]
    job["status"] = "running"
    job["message"] = "Подготовка батчей..."

    async def progress_callback(current_batch: int, total_batches: int) -> None:
        current = min(current_batch, total_batches)
        job["current_batch"] = current
        job["total_batches"] = total_batches
        if current == 0:
            job["message"] = f"Подготовлено батчей: {total_batches}"
        else:
            job["message"] = f"Анализ батча {current}/{total_batches}"

    try:
        result = await review_mr(mr_iid, custom_prompt, progress_callback=progress_callback)
        job["status"] = "completed"
        job["result"] = result
        job["message"] = "Ревью завершено"
        job["current_batch"] = job.get("total_batches", 0)
    except Exception as exc:
        logger.exception("Review failed for MR !%s", mr_iid)
        job["status"] = "failed"
        job["error"] = _translate_review_error(exc)
        job["message"] = "Ревью завершилось с ошибкой"


@router.post("/run")
async def run_review(req: ReviewRequest):
    try:
        mr_iid = _parse_mr_iid(req.mr_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        return await review_mr(mr_iid, req.custom_prompt)
    except Exception as exc:
        logger.exception("Review failed for MR !%s", req.mr_input)
        raise HTTPException(status_code=500, detail=_translate_review_error(exc))


@router.post("/start")
async def start_review(req: ReviewRequest):
    try:
        mr_iid = _parse_mr_iid(req.mr_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = uuid.uuid4().hex
    REVIEW_JOBS[job_id] = {
        "status": "queued",
        "message": "Задача поставлена в очередь",
        "current_batch": 0,
        "total_batches": 0,
        "result": None,
        "error": None,
    }
    asyncio.create_task(_run_review_job(job_id, mr_iid, req.custom_prompt))
    return {"job_id": job_id}


@router.get("/status/{job_id}")
def get_review_status(job_id: str):
    job = REVIEW_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача ревью не найдена")
    return job


@router.get("/history")
def get_history():
    conn = get_db()
    rows = conn.execute(
        """SELECT id, mr_iid, mr_title, mr_url, model_used, summary_json, created_at
           FROM code_reviews ORDER BY created_at DESC LIMIT 100"""
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["summary"] = json.loads(item.pop("summary_json"))
        except (json.JSONDecodeError, KeyError):
            item["summary"] = {}
        result.append(item)
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
    except Exception as exc:
        logger.exception("Failed to publish review comment for review %s", req.review_id)
        raise HTTPException(status_code=502, detail=f"Ошибка отправки в GitLab: {exc}")

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
