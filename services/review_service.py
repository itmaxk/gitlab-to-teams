import asyncio
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

MAX_DIFF_CHARS = int(os.getenv("REVIEW_MAX_DIFF_CHARS", "60000"))
REVIEW_BATCH_MAX_CHARS = int(os.getenv("REVIEW_BATCH_MAX_CHARS", str(MAX_DIFF_CHARS)))


def _get_system_prompt() -> str:
    conn = get_db()
    row = conn.execute("SELECT system_prompt FROM review_settings WHERE id = 1").fetchone()
    conn.close()
    if row:
        return row["system_prompt"]
    return "You are a code reviewer. Return a JSON array of findings."


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

    timeout = httpx.Timeout(connect=10, read=600, write=30, pool=10)
    last_exc = None
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        for attempt in range(3):
            try:
                resp = await client.post(api_url, headers=headers, json=payload)
            except (httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                delay = (attempt + 1) * 10
                logger.warning("LLM network error (%s), retry %d/3 in %ds", type(e).__name__, attempt + 1, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 429:
                delay = (attempt + 1) * 10
                logger.warning("LLM rate limit (429), retry %d/3 in %ds", attempt + 1, delay)
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            last_exc = None
            break
        else:
            if last_exc:
                raise last_exc
            resp.raise_for_status()
        data = resp.json()

    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def _parse_findings(raw: str) -> list[dict]:
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


def _compute_summary(findings: list[dict], files_total: int, files_analyzed: int, truncated: bool) -> dict:
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
    diff_batches = _build_diff_batches(non_empty)
    total_batches = max(1, len(diff_batches))

    system_prompt = _get_system_prompt()
    findings: list[dict] = []

    await _report_progress(progress_callback, 0, total_batches)

    if not diff_batches:
        summary = _compute_summary(findings, len(changes), 0, False)
    else:
        for batch_index, diff_text in enumerate(diff_batches, start=1):
            user_message = _build_batch_message(
                mr_data=mr_data,
                files_changed=len(changes),
                batch_index=batch_index,
                batch_total=total_batches,
                diff_text=diff_text,
                custom_prompt=custom_prompt,
            )
            llm_response = await _call_llm(system_prompt, user_message)
            findings.extend(_parse_findings(llm_response))
            await _report_progress(progress_callback, batch_index, total_batches)

        summary = _compute_summary(findings, len(changes), len(non_empty), False)

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
