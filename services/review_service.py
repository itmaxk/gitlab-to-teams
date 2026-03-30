import asyncio
import json
import logging
import os
import re

import httpx

from db import get_db
from services.gitlab_client import get_mr_diff, get_project_id

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = int(os.getenv("REVIEW_MAX_DIFF_CHARS", "60000"))


def _get_system_prompt() -> str:
    conn = get_db()
    row = conn.execute("SELECT system_prompt FROM review_settings WHERE id = 1").fetchone()
    conn.close()
    if row:
        return row["system_prompt"]
    return "You are a code reviewer. Return a JSON array of findings."


def _build_diff_text(changes: list[dict]) -> str:
    parts = []
    for c in changes:
        diff = c.get("diff", "")
        if not diff:
            continue
        header = f"--- {c['old_path']}\n+++ {c['new_path']}"
        parts.append(f"{header}\n{diff}")
    return "\n\n".join(parts)


def _truncate_diff(diff_text: str, max_chars: int = MAX_DIFF_CHARS) -> tuple[str, bool]:
    if len(diff_text) <= max_chars:
        return diff_text, False
    return diff_text[:max_chars] + "\n\n... (diff truncated due to size)", True


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

    timeout = httpx.Timeout(connect=10, read=600, write=10, pool=10)
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        for attempt in range(3):
            resp = await client.post(api_url, headers=headers, json=payload)
            if resp.status_code == 429:
                delay = (attempt + 1) * 10
                logger.warning("LLM rate limit (429), retry %d/3 in %ds", attempt + 1, delay)
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            break
        else:
            resp.raise_for_status()
        data = resp.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return content


def _parse_findings(raw: str) -> list[dict]:
    raw = raw.strip()
    json_match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not json_match:
        return []
    try:
        findings = json.loads(json_match.group())
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON: %s", raw[:200])
        return []

    valid = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        valid.append({
            "severity": f.get("severity", "info"),
            "category": f.get("category", "bug"),
            "file_path": f.get("file_path", ""),
            "line": f.get("line"),
            "message": f.get("message", ""),
            "suggestion": f.get("suggestion"),
        })
    return valid


def _compute_summary(findings: list[dict], files_total: int, files_analyzed: int, truncated: bool) -> dict:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    info = sum(1 for f in findings if f["severity"] == "info")
    return {
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "total": len(findings),
        "files_total": files_total,
        "files_analyzed": files_analyzed,
        "truncated": truncated,
    }


async def review_mr(mr_iid: int, custom_prompt: str = "") -> dict:
    project_id = await get_project_id()
    mr_data = await get_mr_diff(project_id, mr_iid)

    changes = mr_data["changes"]
    non_empty = [c for c in changes if c.get("diff")]
    diff_text = _build_diff_text(non_empty)
    diff_text, truncated = _truncate_diff(diff_text)

    system_prompt = _get_system_prompt()

    user_message = f"""## Merge Request
- Title: {mr_data['title']}
- Author: {mr_data['author']}
- Source: {mr_data['source_branch']} → {mr_data['target_branch']}
- Files changed: {len(changes)}

## Diff
{diff_text}"""

    if custom_prompt.strip():
        user_message += f"\n\n## Additional instructions from reviewer\n{custom_prompt.strip()}"

    llm_response = await _call_llm(system_prompt, user_message)
    findings = _parse_findings(llm_response)
    summary = _compute_summary(findings, len(changes), len(non_empty), truncated)

    model_used = os.getenv("REVIEW_MODEL", "gpt-4o")

    # Save to DB
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
