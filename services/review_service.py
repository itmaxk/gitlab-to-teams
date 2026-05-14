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
from services.gitlab_client import get_file_content, get_mr_diff, get_project_id
from services.review_project_context import build_project_graph_context, get_review_project_settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_DIFF_CHARS = 60000
DEFAULT_REVIEW_BATCH_MAX_CHARS = 20000
DEFAULT_REVIEW_LLM_READ_TIMEOUT = 120.0
DEFAULT_REVIEW_LLM_MAX_ATTEMPTS = 5
DEFAULT_REVIEW_LLM_MAX_RETRY_DELAY = 60.0
DEFAULT_REVIEW_FILE_CONTEXT_MAX_CHARS = 20000
ALLOWED_REVIEW_SEVERITIES = {"error", "warning", "info"}
ALLOWED_REVIEW_CATEGORIES = {
    "bug",
    "security",
    "performance",
    "maintainability",
    "constructor-link",
    "sql",
    "schema-mapping",
    "ui-component",
    "test-risk",
    "logic",
    "xlsx",
    "general",
}
ALLOWED_REVIEW_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_REVIEW_SOURCES = {"diff", "full_file_context", "graph_context", "final_pass"}


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
REVIEW_FILE_CONTEXT_MAX_CHARS = _read_int_env(
    "REVIEW_FILE_CONTEXT_MAX_CHARS", DEFAULT_REVIEW_FILE_CONTEXT_MAX_CHARS
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


def _constructor_node_key(path: str) -> tuple[str, str, str] | None:
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    try:
        idx = parts.index("@config-rgsl")
    except ValueError:
        return None
    if len(parts) <= idx + 3:
        return None
    package = parts[idx + 1]
    kind = parts[idx + 2]
    code_name = parts[idx + 3]
    if not package or not kind or not code_name:
        return None
    return package, kind, code_name


def _sort_changes_for_review(changes: list[dict]) -> list[dict]:
    indexed = list(enumerate(changes))

    def sort_key(item: tuple[int, dict]) -> tuple[int, str, str, str, int]:
        index, change = item
        path = change.get("new_path") or change.get("old_path") or ""
        node_key = _constructor_node_key(path)
        if node_key:
            return (0, node_key[0], node_key[1], node_key[2], index)
        return (1, path, "", "", index)

    return [change for _, change in sorted(indexed, key=sort_key)]


def _build_diff_batches(changes: list[dict], max_chars: int | None = None) -> list[str]:
    max_chars = max_chars or REVIEW_BATCH_MAX_CHARS
    batches: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for change in _sort_changes_for_review(changes):
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


def _truncate_file_context(content: str, max_chars: int | None = None) -> str:
    max_chars = max_chars or REVIEW_FILE_CONTEXT_MAX_CHARS
    if len(content) <= max_chars:
        return content
    return content[:max_chars].rstrip() + "\n... [file context truncated]"


async def _load_review_file_contexts(
    project_id: int,
    mr_data: dict,
    changes: list[dict],
) -> dict[str, str]:
    ref = mr_data.get("source_ref") or mr_data.get("source_branch", "")
    if not ref:
        return {}

    contexts: dict[str, str] = {}
    for change in changes:
        if change.get("deleted_file"):
            continue
        path = change.get("new_path") or change.get("old_path") or ""
        if not path or path in contexts:
            continue
        try:
            content = await get_file_content(project_id, path, ref)
        except Exception as exc:
            logger.warning("Failed to load review file context for %s at %s: %s", path, ref, exc)
            continue
        contexts[path] = _truncate_file_context(content)

    return contexts


def _changed_paths_in_diff_text(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip()
        path = re.sub(r" \(part \d+/\d+\)$", "", path)
        if path and path != "/dev/null" and path not in paths:
            paths.append(path)
    return paths


def _build_file_context_text(file_contexts: dict[str, str], diff_text: str) -> str:
    parts = []
    for path in _changed_paths_in_diff_text(diff_text):
        content = file_contexts.get(path)
        if not content:
            continue
        parts.append(f"### {path}\n```text\n{content}\n```")
    return "\n\n".join(parts)


def _detect_review_areas(changed_paths: list[str]) -> dict:
    areas = {
        "ui_component": False,
        "sql_datasource": False,
        "schema_mapping": False,
        "constructor_links": False,
    }
    evidence: dict[str, list[str]] = {key: [] for key in areas}
    for raw_path in changed_paths:
        path = raw_path.replace("\\", "/")
        lower = path.lower()
        if any(token in path for token in ("/UI/", "/ClientAction/")) or "/component/" in lower or "/view/" in lower:
            areas["ui_component"] = True
            evidence["ui_component"].append(raw_path)
        if "query.postgres.handlebars" in lower or "/datasource/" in lower or "/dataprovider/" in lower:
            areas["sql_datasource"] = True
            evidence["sql_datasource"].append(raw_path)
        if any(name in lower for name in ("inputschema.json", "resultschema.json", "inputmapping.js", "resultmapping.js", "dataschema.json", "mapping.js")):
            areas["schema_mapping"] = True
            evidence["schema_mapping"].append(raw_path)
        if "/configuration.json" in lower or any(token in lower for token in ("/etlservice/", "/route/", "/integrationservice/", "/sinkgroup/", "/printoutrelation/", "/notification/")):
            areas["constructor_links"] = True
            evidence["constructor_links"].append(raw_path)

    labels = []
    if areas["ui_component"]:
        labels.append("UI/Component")
    if areas["sql_datasource"]:
        labels.append("SQL/DataSource")
    if areas["schema_mapping"]:
        labels.append("Schema/Mapping")
    if areas["constructor_links"]:
        labels.append("Constructor links")

    return {"areas": areas, "labels": labels, "evidence": evidence}


def _build_mixed_review_checklist(review_areas: dict) -> str:
    labels = review_areas.get("labels") or ["Mixed AdInsure"]
    checks = [
        "## Mixed AdInsure UI/Component + SQL/DataSource review focus",
        "Detected review areas: " + ", ".join(labels),
        "- Treat this MR as mixed AdInsure implementation code. UI/Component and SQL/DataSource changes may be part of one behavior chain.",
        "- Primary goal: find runtime/configuration regression risks introduced by the diff, not style issues.",
        "- Check cross-layer consistency: UI filters/results/actions/components -> view/dataExport/document configuration -> dataSource input/result schemas -> inputMapping/resultMapping -> query.postgres.handlebars -> dataProvider.",
    ]
    areas = review_areas.get("areas") or {}
    if areas.get("ui_component"):
        checks.extend([
            "- UI/Component: verify UI fields/actions/components against schemas, dataSource result fields, translations, visibility/required logic, and client action field usage.",
            "- Do not report old UI issues unless the changed diff makes them relevant.",
        ])
    if areas.get("sql_datasource"):
        checks.extend([
            "- SQL/DataSource: verify PostgreSQL 17.5+ syntax, SQL aliases, parameters, dataProvider link, and consistency with inputSchema/resultSchema and mappings.",
            "- Do not require multi-db compatibility unless explicitly requested.",
        ])
    if areas.get("schema_mapping"):
        checks.append("- Schema/Mapping: verify inputMapping parameters, resultMapping fields, schema required fields, null handling, dates, amounts, and enum/code-table values.")
    if areas.get("constructor_links"):
        checks.append("- Constructor links: verify dataSource/dataProvider, sink/source mappings, sinkGroup refs, printout/notification template links, and component owners when related context is available.")
    checks.append("- If a finding crosses layers, set `chain` to a short path such as `UI -> dataSource -> SQL`.")
    return "\n".join(checks)


def _build_batch_message(
    mr_data: dict,
    files_changed: int,
    batch_index: int,
    batch_total: int,
    diff_text: str,
    saved_instructions: str,
    custom_prompt: str,
    file_context_text: str = "",
    project_graph_context_text: str = "",
    review_areas: dict | None = None,
) -> str:
    review_areas = review_areas or {"labels": [], "areas": {}}
    user_message = f"""## Merge Request
- Title: {mr_data['title']}
- Author: {mr_data['author']}
- Source: {mr_data['source_branch']} -> {mr_data['target_branch']}
- Files changed: {files_changed}
- Review batch: {batch_index}/{batch_total}

{_build_mixed_review_checklist(review_areas)}

## Changed code — primary review target
Create normal findings only for risks introduced by these changed lines/files.
{diff_text}"""

    if file_context_text.strip():
        user_message += f"""

## Full file context after change — reference only
Use this section to validate symbols, imports, requires, module-scope constants, mappings, schemas, and surrounding code that may be outside the diff.
Do not create findings only because old unrelated code in this section could be improved.
{file_context_text.strip()}"""

    if project_graph_context_text.strip():
        user_message += f"""

{project_graph_context_text.strip()}

## Constructor Graph Checks"""
        user_message += "\n- Validate related project graph files and unresolved links. Related files are reference context; report them only when they create risk for changed behavior."

    user_message += """

## Output requirements
- Return only a JSON array.
- Each item must have: `severity`, `category`, `file_path`, `line`, `message`, `suggestion`, `confidence`, `evidence`, `source`, `chain`.
- Allowed severity: `error`, `warning`, `info`.
- Allowed category: `bug`, `security`, `performance`, `maintainability`, `constructor-link`, `sql`, `schema-mapping`, `ui-component`, `test-risk`, `logic`, `general`.
- Allowed confidence: `high`, `medium`, `low`.
- Allowed source: `diff`, `full_file_context`, `graph_context`.
- Use `source=diff` for findings directly caused by changed code; use `graph_context` only for cross-file constructor risks.
- Write all human-readable text in fields `message`, `suggestion`, and `evidence` in Russian.
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


def _normalize_choice(raw_value: object, allowed: set[str], default: str) -> str:
    value = str(raw_value or "").strip().lower()
    return value if value in allowed else default


def _normalize_line(raw_value: object) -> int | None:
    if raw_value in (None, ""):
        return None
    try:
        line = int(raw_value)
    except (TypeError, ValueError):
        return None
    return line if line > 0 else None


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
        message = str(finding.get("message") or "").strip()
        if not message:
            continue
        valid.append({
            "severity": _normalize_choice(finding.get("severity"), ALLOWED_REVIEW_SEVERITIES, "info"),
            "category": _normalize_choice(finding.get("category"), ALLOWED_REVIEW_CATEGORIES, "general"),
            "file_path": str(finding.get("file_path") or "").strip(),
            "line": _normalize_line(finding.get("line")),
            "message": message,
            "suggestion": str(finding.get("suggestion") or "").strip() or None,
            "confidence": _normalize_choice(finding.get("confidence"), ALLOWED_REVIEW_CONFIDENCE, "medium"),
            "evidence": str(finding.get("evidence") or "").strip(),
            "source": _normalize_choice(finding.get("source"), ALLOWED_REVIEW_SOURCES, "diff"),
            "chain": str(finding.get("chain") or "").strip(),
        })
    return valid


def _deduplicate_findings(findings: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for finding in findings:
        key = (
            finding.get("severity"),
            finding.get("category"),
            finding.get("file_path"),
            finding.get("line"),
            finding.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _filter_findings_by_known_files(findings: list[dict], known_paths: set[str]) -> list[dict]:
    if not known_paths:
        return findings
    filtered: list[dict] = []
    normalized_known = {path.replace("\\", "/") for path in known_paths if path}
    for finding in findings:
        path = str(finding.get("file_path") or "").replace("\\", "/")
        if path and path not in normalized_known:
            finding = {**finding, "confidence": "low"}
            evidence = finding.get("evidence") or ""
            finding["evidence"] = (evidence + " Файл не найден среди измененных или связанных файлов; требуется ручная проверка.").strip()
        filtered.append(finding)
    return filtered


def _compute_summary(
    findings: list[dict],
    files_total: int,
    files_analyzed: int,
    truncated: bool,
    skipped_files: int = 0,
    project_graph_context: dict | None = None,
    review_areas: dict | None = None,
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
        "project_graph_context": project_graph_context or {},
        "review_areas": review_areas or {},
    }


def _build_final_review_message(
    mr_data: dict,
    changed_paths: list[str],
    findings: list[dict],
    review_areas: dict,
    project_graph_context_summary: dict,
) -> str:
    return f"""## Final consolidation pass
You are consolidating findings from previous batch reviews for one MR.
Return only a JSON array using the same finding schema.

## Merge Request
- Title: {mr_data['title']}
- Source: {mr_data['source_branch']} -> {mr_data['target_branch']}

{_build_mixed_review_checklist(review_areas)}

## Changed files
{json.dumps(changed_paths, ensure_ascii=False, indent=2)}

## Batch findings to consolidate
{json.dumps(findings, ensure_ascii=False, indent=2)}

## Project graph context summary
{json.dumps(project_graph_context_summary, ensure_ascii=False, indent=2)}

## Consolidation rules
- Remove duplicates and weak speculative findings.
- Preserve only findings that are actionable and tied to changed behavior.
- Upgrade severity only when evidence is strong.
- Use `source=final_pass` only for findings added or materially changed during consolidation.
- Keep human-readable text in Russian.
"""


async def _consolidate_findings(
    system_prompt: str,
    mr_data: dict,
    changed_paths: list[str],
    findings: list[dict],
    review_areas: dict,
    project_graph_context_summary: dict,
    known_paths: set[str],
) -> list[dict]:
    if not findings:
        return findings
    user_message = _build_final_review_message(
        mr_data,
        changed_paths,
        findings,
        review_areas,
        project_graph_context_summary,
    )
    try:
        consolidated = _parse_findings(await _call_llm(system_prompt, user_message))
    except Exception as exc:
        logger.warning("Final review consolidation failed, using batch findings: %s", exc)
        return _deduplicate_findings(findings)
    if not consolidated:
        return _deduplicate_findings(findings)
    return _deduplicate_findings(_filter_findings_by_known_files(consolidated, known_paths))


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
    force_refresh_diff: bool = False,
) -> dict:
    project_id = await get_project_id()
    if force_refresh_diff:
        mr_data = await get_mr_diff(project_id, mr_iid, force_refresh=True)
    else:
        mr_data = await get_mr_diff(project_id, mr_iid)

    changes = mr_data["changes"]
    non_empty = [change for change in changes if change.get("diff")]
    skipped_files = max(0, len(changes) - len(non_empty))
    diff_batches = _build_diff_batches(non_empty)
    total_batches = max(1, len(diff_batches))
    file_contexts = await _load_review_file_contexts(project_id, mr_data, non_empty)
    project_settings = get_review_project_settings()
    changed_paths = [
        change.get("new_path") or change.get("old_path") or ""
        for change in non_empty
    ]
    review_areas = _detect_review_areas(changed_paths)
    project_graph_context = build_project_graph_context(changed_paths, project_settings)
    project_graph_context_text = project_graph_context.to_prompt_text()
    project_graph_context_summary = project_graph_context.to_summary()

    system_prompt = _get_system_prompt()
    saved_instructions = _build_saved_instructions_text()
    findings: list[dict] = []

    await _report_progress(progress_callback, 0, total_batches)

    truncated = bool(mr_data.get("overflow")) or skipped_files > 0

    if not diff_batches:
        summary = _compute_summary(
            findings,
            len(changes),
            0,
            truncated,
            skipped_files,
            project_graph_context_summary,
            review_areas,
        )
    else:
        for batch_index, diff_text in enumerate(diff_batches, start=1):
            file_context_text = _build_file_context_text(file_contexts, diff_text)
            user_message = _build_batch_message(
                mr_data=mr_data,
                files_changed=len(changes),
                batch_index=batch_index,
                batch_total=total_batches,
                diff_text=diff_text,
                saved_instructions=saved_instructions,
                custom_prompt=custom_prompt,
                file_context_text=file_context_text,
                project_graph_context_text=project_graph_context_text,
                review_areas=review_areas,
            )
            llm_response = await _call_llm(system_prompt, user_message)
            findings.extend(_parse_findings(llm_response))
            await _report_progress(progress_callback, batch_index, total_batches)

        known_paths = set(changed_paths) | set(file_contexts) | {
            item.get("path", "") for item in project_graph_context_summary.get("related_files", [])
        }
        findings = _filter_findings_by_known_files(_deduplicate_findings(findings), known_paths)
        findings = await _consolidate_findings(
            system_prompt,
            mr_data,
            changed_paths,
            findings,
            review_areas,
            project_graph_context_summary,
            known_paths,
        )

        summary = _compute_summary(
            findings,
            len(changes),
            len(non_empty),
            truncated,
            skipped_files,
            project_graph_context_summary,
            review_areas,
        )

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
