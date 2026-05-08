import asyncio
import json
import logging
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from db import get_db
from models import (
    ReviewInstructionItemCreate,
    ReviewInstructionItemUpdate,
    ReviewProjectProfilePreviewRequest,
    ReviewProjectProfileRequest,
    ReviewPublishRequest,
    ReviewRequest,
    ReviewSettingsUpdate,
    XlsxReviewRequest,
)
from services.gitlab_notes import post_merge_request_note
from services.review_comment_formatter import format_gitlab_review_comment
from services.review_service import LLMRateLimitError, review_mr
from services.review_project_context import (
    preview_project_graph_context,
    validate_profile_json,
)
from services.xlsx_review_service import review_xlsx_mr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])
REVIEW_JOBS: dict[str, dict] = {}


def _normalize_instruction_type(raw_value: str) -> str:
    return "exclude" if str(raw_value or "").strip().lower() == "exclude" else "include"


def _load_review_instruction_items(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, instruction_text, instruction_type, sort_order, created_at, updated_at
        FROM review_instruction_items
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "instruction_type": _normalize_instruction_type(row["instruction_type"]),
        }
        for row in rows
    ]


def _build_legacy_review_instructions(items: list[dict]) -> str:
    include_items = [
        item["instruction_text"].strip()
        for item in items
        if item["instruction_type"] == "include" and item["instruction_text"].strip()
    ]
    exclude_items = [
        item["instruction_text"].strip()
        for item in items
        if item["instruction_type"] == "exclude" and item["instruction_text"].strip()
    ]
    parts: list[str] = []
    if include_items:
        parts.append("Учитывать в ревью:\n- " + "\n- ".join(include_items))
    if exclude_items:
        parts.append("Не учитывать в ревью:\n- " + "\n- ".join(exclude_items))
    return "\n\n".join(parts)


def _sync_review_instruction_cache(conn) -> list[dict]:
    items = _load_review_instruction_items(conn)
    legacy_text = _build_legacy_review_instructions(items)
    conn.execute(
        """
        UPDATE review_settings
        SET review_instructions = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """,
        (legacy_text,),
    )
    return items


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


def _serialize_profile(row) -> dict:
    data = dict(row)
    for key in ("enabled", "is_default", "graph_context_enabled"):
        data[key] = bool(data.get(key))
    try:
        data["profile_json"] = json.loads(data.get("profile_json") or "{}")
    except json.JSONDecodeError:
        data["profile_json"] = {}
    return data


def _save_profile_payload(conn, req: ReviewProjectProfileRequest, profile_id: int | None = None) -> int:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Название профиля не должно быть пустым")
    errors = validate_profile_json(req.profile_json)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    if req.is_default:
        conn.execute("UPDATE review_project_profiles SET is_default = 0")
    payload = (
        name,
        req.description.strip(),
        1 if req.enabled else 0,
        1 if req.is_default else 0,
        req.project_root.strip(),
        req.config_path.strip() or "configuration/@config-rgsl",
        req.sql_target.strip() or "PostgreSQL 17.5+",
        1 if req.graph_context_enabled else 0,
        req.graph_context_max_files,
        json.dumps(req.profile_json, ensure_ascii=False),
    )
    if profile_id is None:
        cur = conn.execute(
            """
            INSERT INTO review_project_profiles (
                name, description, enabled, is_default, project_root, config_path,
                sql_target, graph_context_enabled, graph_context_max_files, profile_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        return int(cur.lastrowid)
    conn.execute(
        """
        UPDATE review_project_profiles
        SET name = ?, description = ?, enabled = ?, is_default = ?,
            project_root = ?, config_path = ?, sql_target = ?,
            graph_context_enabled = ?, graph_context_max_files = ?,
            profile_json = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (*payload, profile_id),
    )
    return profile_id


def _translate_review_error(exc: Exception) -> str:
    message = str(exc)
    if "REVIEW_API_URL not configured" in message:
        return "Не настроен REVIEW_API_URL для LLM-ревью"
    if isinstance(exc, TimeoutError) or "timed out" in message.lower():
        return "LLM API слишком долго отвечает. Попробуйте уменьшить размер батча diff или снизить таймаут ожидания."
    if "429" in message:
        return "LLM API временно ограничил запросы (429). Попробуйте позже или уменьшите размер diff."
    if "Cannot parse" in message or "Не удалось определить IID" in message:
        return message
    if message:
        return f"Ошибка LLM-ревью: {message}"
    return "Неизвестная ошибка LLM-ревью"


def _serialize_job(job: dict) -> dict:
    return {
        "status": job.get("status"),
        "message": job.get("message"),
        "current_batch": job.get("current_batch", 0),
        "total_batches": job.get("total_batches", 0),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def _cancel_review_job(job_id: str) -> dict:
    job = REVIEW_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача ревью не найдена")

    if job.get("status") in {"completed", "failed", "canceled"}:
        return _serialize_job(job)

    job["status"] = "canceled"
    job["message"] = "Ревью остановлено пользователем"
    job["error"] = None
    task = job.get("task")
    if task and not task.done():
        task.cancel()
    return _serialize_job(job)


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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
        if job.get("status") == "canceled":
            return
        job["status"] = "completed"
        job["result"] = result
        job["message"] = "Ревью завершено"
        job["current_batch"] = job.get("total_batches", 0)
    except asyncio.CancelledError:
        job["status"] = "canceled"
        job["message"] = "Ревью остановлено пользователем"
        job["error"] = None
    except LLMRateLimitError as exc:
        logger.warning("Review rate-limited for MR !%s: %s", mr_iid, exc)
        job["status"] = "failed"
        job["error"] = _translate_review_error(exc)
        job["message"] = job["error"]
    except Exception as exc:
        logger.exception("Review failed for MR !%s", mr_iid)
        job["status"] = "failed"
        job["error"] = _translate_review_error(exc)
        job["message"] = "Ревью завершилось с ошибкой"


async def _run_xlsx_review_job(job_id: str, mr_iid: int, base_ref: str) -> None:
    job = REVIEW_JOBS[job_id]
    job["status"] = "running"
    job["message"] = "Поиск xlsx-файлов..."

    async def progress_callback(current_file: int, total_files: int, file_path: str) -> None:
        current = min(current_file, total_files)
        job["current_batch"] = current
        job["total_batches"] = total_files
        if total_files == 0:
            job["message"] = "XLSX-файлы в MR не найдены"
        elif current == 0:
            job["message"] = f"Найдено xlsx-файлов: {total_files}"
        else:
            job["message"] = f"Сравнение xlsx {current}/{total_files}: {file_path}"

    try:
        result = await review_xlsx_mr(mr_iid, base_ref, progress_callback=progress_callback)
        if job.get("status") == "canceled":
            return
        job["status"] = "completed"
        job["result"] = result
        job["message"] = "XLSX-ревью завершено"
        job["current_batch"] = job.get("total_batches", 0)
    except asyncio.CancelledError:
        job["status"] = "canceled"
        job["message"] = "XLSX-ревью остановлено пользователем"
        job["error"] = None
    except Exception as exc:
        logger.exception("XLSX review failed for MR !%s", mr_iid)
        job["status"] = "failed"
        job["error"] = _translate_review_error(exc)
        job["message"] = "XLSX-ревью завершилось с ошибкой"


@router.post("/run")
async def run_review(req: ReviewRequest):
    try:
        mr_iid = _parse_mr_iid(req.mr_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        return await review_mr(mr_iid, req.custom_prompt)
    except LLMRateLimitError as exc:
        logger.warning("Review rate-limited for MR !%s: %s", req.mr_input, exc)
        raise HTTPException(status_code=429, detail=_translate_review_error(exc))
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
    REVIEW_JOBS[job_id]["task"] = asyncio.create_task(
        _run_review_job(job_id, mr_iid, req.custom_prompt)
    )
    return {"job_id": job_id}


@router.post("/run-xlsx")
async def run_xlsx_review(req: XlsxReviewRequest):
    try:
        mr_iid = _parse_mr_iid(req.mr_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        return await review_xlsx_mr(mr_iid, req.base_ref or "")
    except Exception as exc:
        logger.exception("XLSX review failed for MR !%s", req.mr_input)
        raise HTTPException(status_code=500, detail=_translate_review_error(exc))


@router.post("/start-xlsx")
async def start_xlsx_review(req: XlsxReviewRequest):
    try:
        mr_iid = _parse_mr_iid(req.mr_input)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = uuid.uuid4().hex
    REVIEW_JOBS[job_id] = {
        "status": "queued",
        "message": "Задача xlsx-ревью поставлена в очередь",
        "current_batch": 0,
        "total_batches": 0,
        "result": None,
        "error": None,
    }
    REVIEW_JOBS[job_id]["task"] = asyncio.create_task(
        _run_xlsx_review_job(job_id, mr_iid, req.base_ref or "")
    )
    return {"job_id": job_id}


@router.post("/cancel/{job_id}")
def cancel_review(job_id: str):
    return _cancel_review_job(job_id)


@router.get("/stream/{job_id}")
async def stream_review_status(job_id: str, request: Request):
    job = REVIEW_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача ревью не найдена")

    async def event_stream():
        last_payload = None
        while True:
            if await request.is_disconnected():
                break

            current_job = REVIEW_JOBS.get(job_id)
            if current_job is None:
                yield _sse_event("error", {"detail": "Задача ревью не найдена"})
                break

            payload = _serialize_job(current_job)
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if serialized != last_payload:
                yield _sse_event("progress", payload)
                last_payload = serialized

            if payload["status"] in {"completed", "failed", "canceled"}:
                yield _sse_event("done", payload)
                break

            await asyncio.sleep(0.75)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status/{job_id}")
def get_review_status(job_id: str):
    job = REVIEW_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача ревью не найдена")
    return _serialize_job(job)


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


@router.get("/project-profiles")
def list_project_profiles():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM review_project_profiles ORDER BY is_default DESC, name ASC"
    ).fetchall()
    conn.close()
    return [_serialize_profile(row) for row in rows]


@router.post("/project-profiles")
def create_project_profile(req: ReviewProjectProfileRequest):
    conn = get_db()
    profile_id = _save_profile_payload(conn, req)
    conn.commit()
    row = conn.execute("SELECT * FROM review_project_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    return _serialize_profile(row)


@router.get("/project-profiles/{profile_id}")
def get_project_profile(profile_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM review_project_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Профиль проекта не найден")
    return _serialize_profile(row)


@router.put("/project-profiles/{profile_id}")
def update_project_profile(profile_id: int, req: ReviewProjectProfileRequest):
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM review_project_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(status_code=404, detail="Профиль проекта не найден")
    _save_profile_payload(conn, req, profile_id)
    conn.commit()
    row = conn.execute("SELECT * FROM review_project_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    return _serialize_profile(row)


@router.post("/project-profiles/{profile_id}/validate")
def validate_project_profile(profile_id: int, req: ReviewProjectProfileRequest):
    errors = validate_profile_json(req.profile_json)
    return {"ok": not errors, "errors": errors}


@router.post("/project-profiles/{profile_id}/preview-context")
def preview_project_profile_context(profile_id: int, req: ReviewProjectProfilePreviewRequest):
    try:
        context = preview_project_graph_context(profile_id, req.changed_paths)
    except ValueError:
        raise HTTPException(status_code=404, detail="Профиль проекта не найден")
    return context.to_summary()


@router.put("/settings/active-profile/{profile_id}")
def set_active_project_profile(profile_id: int):
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM review_project_profiles WHERE id = ? AND enabled = 1",
        (profile_id,),
    ).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(status_code=404, detail="Активный профиль проекта не найден")
    conn.execute(
        "UPDATE review_settings SET active_project_profile_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (profile_id,),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "active_project_profile_id": profile_id}


@router.get("/settings")
def get_settings():
    conn = get_db()
    row = conn.execute(
        """
        SELECT system_prompt, review_instructions, review_project_root,
               review_project_config_path, review_sql_target,
               review_graph_context_enabled, review_graph_context_max_files,
               active_project_profile_id,
               updated_at
        FROM review_settings
        WHERE id = 1
        """
    ).fetchone()
    items = _load_review_instruction_items(conn)
    conn.close()
    legacy_text = _build_legacy_review_instructions(items)
    if not row:
        return {
            "system_prompt": "",
            "review_instructions": legacy_text,
            "review_instruction_items": items,
            "review_project_root": "",
            "review_project_config_path": "configuration/@config-rgsl",
            "review_sql_target": "PostgreSQL 17.5+",
            "review_graph_context_enabled": True,
            "review_graph_context_max_files": 12,
            "active_project_profile_id": None,
            "updated_at": "",
        }
    data = dict(row)
    data["review_instruction_items"] = items
    data["review_instructions"] = legacy_text or data.get("review_instructions", "")
    data["review_graph_context_enabled"] = bool(data.get("review_graph_context_enabled", 1))
    return data


@router.put("/settings")
def update_settings(req: ReviewSettingsUpdate):
    conn = get_db()
    conn.execute(
        """
        UPDATE review_settings
        SET system_prompt = ?,
            review_instructions = ?,
            review_project_root = ?,
            review_project_config_path = ?,
            review_sql_target = ?,
            review_graph_context_enabled = ?,
            review_graph_context_max_files = ?,
            active_project_profile_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """,
        (
            req.system_prompt,
            req.review_instructions,
            req.review_project_root.strip(),
            req.review_project_config_path.strip() or "configuration/@config-rgsl",
            req.review_sql_target.strip() or "PostgreSQL 17.5+",
            1 if req.review_graph_context_enabled else 0,
            req.review_graph_context_max_files,
            req.active_project_profile_id,
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.get("/instructions")
def get_instruction_items():
    conn = get_db()
    items = _load_review_instruction_items(conn)
    conn.close()
    return items


@router.post("/instructions")
def create_instruction_item(req: ReviewInstructionItemCreate):
    instruction_text = req.instruction_text.strip()
    if not instruction_text:
        raise HTTPException(status_code=400, detail="Текст инструкции не должен быть пустым")

    instruction_type = _normalize_instruction_type(req.instruction_type)
    conn = get_db()
    next_sort_order_row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_sort_order FROM review_instruction_items"
    ).fetchone()
    next_sort_order = int(next_sort_order_row["next_sort_order"]) if next_sort_order_row else 1
    cur = conn.execute(
        """
        INSERT INTO review_instruction_items (instruction_text, instruction_type, sort_order)
        VALUES (?, ?, ?)
        """,
        (instruction_text, instruction_type, next_sort_order),
    )
    item_id = cur.lastrowid
    _sync_review_instruction_cache(conn)
    conn.commit()
    row = conn.execute(
        """
        SELECT id, instruction_text, instruction_type, sort_order, created_at, updated_at
        FROM review_instruction_items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()
    conn.close()
    return {
        **dict(row),
        "instruction_type": _normalize_instruction_type(row["instruction_type"]),
    }


@router.put("/instructions/{item_id}")
def update_instruction_item(item_id: int, req: ReviewInstructionItemUpdate):
    instruction_text = req.instruction_text.strip()
    if not instruction_text:
        raise HTTPException(status_code=400, detail="Текст инструкции не должен быть пустым")

    instruction_type = _normalize_instruction_type(req.instruction_type)
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM review_instruction_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(status_code=404, detail="Инструкция не найдена")

    conn.execute(
        """
        UPDATE review_instruction_items
        SET instruction_text = ?, instruction_type = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (instruction_text, instruction_type, item_id),
    )
    _sync_review_instruction_cache(conn)
    conn.commit()
    row = conn.execute(
        """
        SELECT id, instruction_text, instruction_type, sort_order, created_at, updated_at
        FROM review_instruction_items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()
    conn.close()
    return {
        **dict(row),
        "instruction_type": _normalize_instruction_type(row["instruction_type"]),
    }


@router.delete("/instructions/{item_id}")
def delete_instruction_item(item_id: int):
    conn = get_db()
    cur = conn.execute("DELETE FROM review_instruction_items WHERE id = ?", (item_id,))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Инструкция не найдена")
    _sync_review_instruction_cache(conn)
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
