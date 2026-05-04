import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import inspect
import json
import logging
import os
import re
from typing import Callable

import httpx

from db import get_db
from services.gitlab_client import get_mr_diff, get_project_id

logger = logging.getLogger(__name__)

DEFAULT_MAX_DIFF_CHARS = 60000
DEFAULT_REVIEW_BATCH_MAX_CHARS = 20000
DEFAULT_REVIEW_LLM_READ_TIMEOUT = 120.0
DEFAULT_REVIEW_LLM_MAX_ATTEMPTS = 5
DEFAULT_REVIEW_LLM_MAX_RETRY_DELAY = 60.0


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %s", name, raw, default)
        return default


def _read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1.0, float(raw))
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %s", name, raw, default)
        return default


def _resolve_batch_max_chars() -> int:
    max_diff_chars = _read_int_env("REVIEW_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS)
    configured = os.getenv("REVIEW_BATCH_MAX_CHARS", "").strip()
    if not configured:
        return min(max_diff_chars, DEFAULT_REVIEW_BATCH_MAX_CHARS)

    try:
        batch_max_chars = max(1, int(configured))
    except ValueError:
        logger.warning(
            "Invalid integer for REVIEW_BATCH_MAX_CHARS=%r, using safe default", configured
        )
        return min(max_diff_chars, DEFAULT_REVIEW_BATCH_MAX_CHARS)

    return min(batch_max_chars, max_diff_chars)


MAX_DIFF_CHARS = _read_int_env("REVIEW_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS)
REVIEW_BATCH_MAX_CHARS = _resolve_batch_max_chars()
REVIEW_LLM_READ_TIMEOUT = _read_float_env(
    "REVIEW_LLM_READ_TIMEOUT", DEFAULT_REVIEW_LLM_READ_TIMEOUT
)
REVIEW_LLM_MAX_ATTEMPTS = _read_int_env(
    "REVIEW_LLM_MAX_ATTEMPTS", DEFAULT_REVIEW_LLM_MAX_ATTEMPTS
)
REVIEW_LLM_MAX_RETRY_DELAY = _read_float_env(
    "REVIEW_LLM_MAX_RETRY_DELAY", DEFAULT_REVIEW_LLM_MAX_RETRY_DELAY
)


class LLMRateLimitError(RuntimeError):
    pass


def _parse_retry_after_seconds(raw_value: str | None) -> float | None:
    if not raw_value:
        return None

    raw_value = raw_value.strip()
    try:
        delay = float(raw_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()

    if delay < 0:
        return None
    return min(delay, REVIEW_LLM_MAX_RETRY_DELAY)


def _llm_retry_delay(response: httpx.Response | None, attempt_index: int) -> float:
    retry_after = _parse_retry_after_seconds(
        response.headers.get("Retry-After") if response is not None else None
    )
    if retry_after is not None:
        return retry_after
    return min((attempt_index + 1) * 10, REVIEW_LLM_MAX_RETRY_DELAY)


def _get_system_prompt() -> str:
    conn = get_db()
    row = conn.execute("SELECT system_prompt FROM review_settings WHERE id = 1").fetchone()
    conn.close()
    if row:
        return row["system_prompt"]
    return (
        "Ты опытный ревьюер кода. "
        "Верни JSON-массив замечаний. "
        "Поля message и suggestion всегда пиши на русском языке. "
        "Имена файлов, идентификаторы, названия переменных и фрагменты кода не переводи."
    )


def _get_saved_review_instruction_items() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT instruction_text, instruction_type
        FROM review_instruction_items
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()
    conn.close()
    return [
        {
            "instruction_text": (row["instruction_text"] or "").strip(),
            "instruction_type": "exclude"
            if str(row["instruction_type"] or "").strip().lower() == "exclude"
            else "include",
        }
        for row in rows
        if (row["instruction_text"] or "").strip()
    ]


def _build_saved_instructions_text() -> str:
    items = _get_saved_review_instruction_items()
    if not items:
        return ""

    include_items = [
        item["instruction_text"]
        for item in items
        if item["instruction_type"] == "include"
    ]
    exclude_items = [
        item["instruction_text"]
        for item in items
        if item["instruction_type"] == "exclude"
    ]
    parts: list[str] = []
    if include_items:
        parts.append("Учитывать в ревью:\n- " + "\n- ".join(include_items))
    if exclude_items:
        parts.append("Не учитывать в ревью:\n- " + "\n- ".join(exclude_items))
    return "\n\n".join(parts)


def _build_change_text(change: dict, *, part: int | None = None, total_parts: int | None = None) -> str:
    suffix = ""
    if part is not None and total_parts is not None and total_parts > 1:
        suffix = f" (part {part}/{total_parts})"
    header = f"--- {change['old_path']}\n+++ {change['new_path']}{suffix}"
    return f"{header}\n{change.get('diff', '')}"


def _split_change_into_parts(change: dict, max_chars: int) -> list[str]:
    full_text = _build_change_text(change)
    if len(full_text) <= max_chars:
        return [full_text]

    header = f"--- {change['old_path']}\n+++ {change['new_path']}"
    header_overhead = len(header) + len("\n") + len(" (part 999/999)")
    chunk_size = max(1, max_chars - header_overhead)
    diff = change.get("diff", "")
    diff_parts = [diff[i:i + chunk_size] for i in range(0, len(diff), chunk_size)] or [""]
    total_parts = len(diff_parts)

    return [
        _build_change_text({**change, "diff": diff_part}, part=index, total_parts=total_parts)
        for index, diff_part in enumerate(diff_parts, start=1)
    ]


def _build_diff_batches(changes: list[dict], max_chars: int | None = None) -> list[str]:
    max_chars = max_chars or REVIEW_BATCH_MAX_CHARS
    batches: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for change in changes:
        if not change.get("diff"):
            continue

        for change_text in _split_change_into_parts(change, max_chars):
            separator_len = 2 if current_parts else 0
            next_len = current_len + separator_len + len(change_text)
            if current_parts and next_len > max_chars:
                batches.append("\n\n".join(current_parts))
                current_parts = [change_text]
                current_len = len(change_text)
                continue

            current_parts.append(change_text)
            current_len = next_len

    if current_parts:
        batches.append("\n\n".join(current_parts))

    return batches


def _build_batch_message(
    mr_data: dict,
    files_changed: int,
    batch_index: int,
    batch_total: int,
    diff_text: str,
    saved_instructions: str,
    custom_prompt: str,
) -> str:
    user_message = f"""## Merge Request
- Title: {mr_data['title']}
- Author: {mr_data['author']}
- Source: {mr_data['source_branch']} -> {mr_data['target_branch']}
- Files changed: {files_changed}
- Review batch: {batch_index}/{batch_total}

## Diff
{diff_text}"""

    user_message += """

## Output requirements
- Return only a JSON array.
- Write all human-readable text in fields `message` and `suggestion` in Russian.
- Do not translate file paths, identifiers, branch names, config keys, or code fragments.
"""

    if saved_instructions.strip():
        user_message += f"\n\n## Saved review instructions\n{saved_instructions.strip()}"

    if custom_prompt.strip():
        user_message += f"\n\n## Additional instructions from reviewer\n{custom_prompt.strip()}"

    return user_message


async def _call_llm(system_prompt: str, user_message: str) -> str:
    api_url = os.getenv("REVIEW_API_URL", "")
    api_key = os.getenv("REVIEW_API_KEY", "")
    model = os.getenv("REVIEW_MODEL", "gpt-4o")

    if not api_url:
        raise ValueError("REVIEW_API_URL not configured")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
    }

    timeout = httpx.Timeout(connect=10, read=REVIEW_LLM_READ_TIMEOUT, write=30, pool=10)
    last_exc: Exception | None = None
    last_rate_limit_response: httpx.Response | None = None
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        for attempt in range(REVIEW_LLM_MAX_ATTEMPTS):
            is_last_attempt = attempt == REVIEW_LLM_MAX_ATTEMPTS - 1
            try:
                resp = await client.post(api_url, headers=headers, json=payload)
            except (httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                last_rate_limit_response = None
                if is_last_attempt:
                    break
                delay = _llm_retry_delay(None, attempt)
                logger.warning(
                    "LLM network error (%s), retry %d/%d in %.0fs",
                    type(e).__name__,
                    attempt + 1,
                    REVIEW_LLM_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 429:
                last_rate_limit_response = resp
                if is_last_attempt:
                    break
                delay = _llm_retry_delay(resp, attempt)
                logger.warning(
                    "LLM rate limit (429), retry %d/%d in %.0fs",
                    attempt + 1,
                    REVIEW_LLM_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            last_exc = None
            last_rate_limit_response = None
            break
        else:
            resp = None

        if last_rate_limit_response is not None:
            retry_after = _parse_retry_after_seconds(
                last_rate_limit_response.headers.get("Retry-After")
            )
            retry_hint = (
                f" Retry after {retry_after:.0f}s."
                if retry_after is not None
                else ""
            )
            raise LLMRateLimitError(
                f"LLM API rate limit exceeded after {REVIEW_LLM_MAX_ATTEMPTS} attempts (429)."
                f"{retry_hint}"
            )
        if last_exc:
            if isinstance(last_exc, httpx.ReadTimeout):
                raise TimeoutError(
                    f"LLM request timed out after {REVIEW_LLM_READ_TIMEOUT:.0f}s"
                ) from last_exc
            raise last_exc
        data = resp.json()

    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def _parse_findings(raw: str | None) -> list[dict]:
    if not raw:
        return []

    raw = raw.strip()
    json_match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not json_match:
        return []
    try:
        findings = json.loads(json_match.group())
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON: %s", raw[:200])
        return []

    valid = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        valid.append({
            "severity": finding.get("severity", "info"),
            "category": finding.get("category", "bug"),
            "file_path": finding.get("file_path", ""),
            "line": finding.get("line"),
            "message": finding.get("message", ""),
            "suggestion": finding.get("suggestion"),
        })
    return valid


def _compute_summary(
    findings: list[dict],
    files_total: int,
    files_analyzed: int,
    truncated: bool,
    skipped_files: int = 0,
) -> dict:
    errors = sum(1 for finding in findings if finding["severity"] == "error")
    warnings = sum(1 for finding in findings if finding["severity"] == "warning")
    info = sum(1 for finding in findings if finding["severity"] == "info")
    return {
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "total": len(findings),
        "files_total": files_total,
        "files_analyzed": files_analyzed,
        "files_skipped": max(0, skipped_files),
        "truncated": truncated,
    }


async def _report_progress(
    progress_callback: Callable[[int, int], object] | None,
    current_batch: int,
    total_batches: int,
) -> None:
    if progress_callback is None:
        return
    result = progress_callback(current_batch, total_batches)
    if inspect.isawaitable(result):
        await result


async def review_mr(
    mr_iid: int,
    custom_prompt: str = "",
    progress_callback: Callable[[int, int], object] | None = None,
) -> dict:
    project_id = await get_project_id()
    mr_data = await get_mr_diff(project_id, mr_iid)

    changes = mr_data["changes"]
    non_empty = [change for change in changes if change.get("diff")]
    skipped_files = max(0, len(changes) - len(non_empty))
    diff_batches = _build_diff_batches(non_empty)
    total_batches = max(1, len(diff_batches))

    system_prompt = _get_system_prompt()
    saved_instructions = _build_saved_instructions_text()
    findings: list[dict] = []

    await _report_progress(progress_callback, 0, total_batches)

    truncated = bool(mr_data.get("overflow")) or skipped_files > 0

    if not diff_batches:
        summary = _compute_summary(findings, len(changes), 0, truncated, skipped_files)
    else:
        for batch_index, diff_text in enumerate(diff_batches, start=1):
            user_message = _build_batch_message(
                mr_data=mr_data,
                files_changed=len(changes),
                batch_index=batch_index,
                batch_total=total_batches,
                diff_text=diff_text,
                saved_instructions=saved_instructions,
                custom_prompt=custom_prompt,
            )
            llm_response = await _call_llm(system_prompt, user_message)
            findings.extend(_parse_findings(llm_response))
            await _report_progress(progress_callback, batch_index, total_batches)

        summary = _compute_summary(findings, len(changes), len(non_empty), truncated, skipped_files)

    model_used = os.getenv("REVIEW_MODEL", "gpt-4o")

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO code_reviews (mr_iid, mr_title, mr_url, model_used, custom_prompt, findings_json, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            mr_iid,
            mr_data["title"],
            mr_data["web_url"],
            model_used,
            custom_prompt,
            json.dumps(findings, ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
        ),
    )
    review_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": review_id,
        "mr": {
            "iid": mr_iid,
            "title": mr_data["title"],
            "author": mr_data["author"],
            "source_branch": mr_data["source_branch"],
            "target_branch": mr_data["target_branch"],
            "web_url": mr_data["web_url"],
            "files_changed": len(changes),
        },
        "findings": findings,
        "summary": summary,
        "model_used": model_used,
    }
