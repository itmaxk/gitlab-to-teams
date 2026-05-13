import difflib
import asyncio
import logging
import os
import re
import time
from functools import wraps
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Connection pool for GitLab API requests
_limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> httpx.AsyncClient:
    """Return shared AsyncClient with connection pooling."""
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    client_closed = bool(getattr(_client, "is_closed", False))
    if _client is None or client_closed or (_client_loop is not None and _client_loop is not loop):
        _client = httpx.AsyncClient(verify=False, limits=_limits)
        _client_loop = loop
    return _client


async def close_client() -> None:
    """Close the shared HTTP client. Call on application shutdown."""
    global _client, _client_loop
    if _client is not None:
        try:
            await _client.aclose()
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise
        _client = None
        _client_loop = None


def _base_url() -> str:
    return os.getenv("GITLAB_URL", "").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "PRIVATE-TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _project_path() -> str:
    return quote(os.getenv("GITLAB_PROJECT", ""), safe="")


_MR_DIFF_CACHE_TTL_SECONDS = 600  # 10 minutes
_MR_DIFF_CACHE_MAX_SIZE = 100


def _ttl_cache(ttl_seconds: int, max_size: int):
    """Decorator for TTL-based caching with LRU eviction."""
    def decorator(func):
        cache: dict[tuple, tuple] = {}

        @wraps(func)
        async def wrapper(*args, **kwargs):
            force_refresh = bool(kwargs.get("force_refresh", False))
            # Create cache key from positional args (project_id, mr_iid)
            key = tuple(args[:2]) if len(args) >= 2 else tuple(args)

            # Check cache
            now = time.time()
            if not force_refresh and key in cache:
                result, timestamp = cache[key]
                if now - timestamp < ttl_seconds:
                    logger.debug("Cache hit for %s", key)
                    return result
                # Expired, remove
                del cache[key]

            # Call function
            result = await func(*args, **kwargs)

            # Store in cache
            cache[key] = (result, now)

            # LRU eviction if cache too large
            if len(cache) > max_size:
                # Remove oldest entry
                oldest_key = min(cache.keys(), key=lambda k: cache[k][1])
                del cache[oldest_key]

            return result

        # Attach cache management functions
        wrapper._cache = cache
        wrapper._cache_ttl = ttl_seconds

        def clear_cache():
            """Clear all cached entries."""
            cache.clear()
            logger.info("Cleared MR diff cache")

        wrapper.clear_cache = clear_cache

        def cache_info():
            """Return cache statistics."""
            now = time.time()
            valid = sum(1 for _, ts in cache.values() if now - ts < ttl_seconds)
            return {
                "size": len(cache),
                "valid": valid,
                "expired": len(cache) - valid,
                "max_size": max_size,
                "ttl_seconds": ttl_seconds,
            }

        wrapper.cache_info = cache_info

        return wrapper
    return decorator


def clear_mr_diff_cache() -> None:
    """Clear the MR diff cache. Can be called externally."""
    clear_cache = getattr(get_mr_diff, "clear_cache", None)
    if clear_cache is not None:
        clear_cache()


def get_mr_diff_cache_info() -> dict:
    """Return MR diff cache statistics."""
    cache_info = getattr(get_mr_diff, "cache_info", None)
    if cache_info is None:
        return {
            "size": 0,
            "valid": 0,
            "expired": 0,
            "max_size": _MR_DIFF_CACHE_MAX_SIZE,
            "ttl_seconds": _MR_DIFF_CACHE_TTL_SECONDS,
        }
    return cache_info()


async def get_project_id() -> int:
    """Return the numeric project ID for GITLAB_PROJECT."""
    url = f"{_base_url()}/api/v4/projects/{_project_path()}"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


async def get_merge_requests(
    project_id: int,
    state: str = "merged",
    target_branch: str | None = None,
    per_page: int = 100,
    updated_after: str = "",
    order_by: str = "updated_at",
) -> list[dict]:
    """Return merge requests filtered by state and target branch.

    If target_branch is None, all branches are included (no branch filter).
    """
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    params: dict[str, str | int] = {
        "state": state,
        "order_by": order_by,
        "sort": "desc",
        "per_page": per_page,
        "page": 1,
    }
    if target_branch:
        params["target_branch"] = target_branch
    if updated_after:
        params["updated_after"] = updated_after
    result = []
    client = _get_client()
    while True:
        resp = await client.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        result.extend(data)
        next_page = resp.headers.get("x-next-page", "")
        if not next_page:
            break
        params["page"] = int(next_page)
    return result


async def get_mr_changes(project_id: int, mr_iid: int) -> list[str]:
    """Return changed file paths for a merge request."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [change["new_path"] for change in data.get("changes", []) if change.get("new_path")]


async def get_mr_by_iid(project_id: int, mr_iid: int) -> dict:
    """Return a single merge request by IID."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


async def create_branch(project_id: int, branch_name: str, ref: str) -> dict:
    """Create a repository branch from ref."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/branches"
    client = _get_client()
    resp = await client.post(
        url,
        headers=_headers(),
        json={"branch": branch_name, "ref": ref},
        timeout=30,
    )
    if resp.status_code >= 400:
        body = resp.text
        logger.error("create_branch %s from %s -> %s %s", branch_name, ref, resp.status_code, body)
        try:
            msg = resp.json().get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


async def cherry_pick_commit(project_id: int, sha: str, target_branch: str) -> dict:
    """Cherry-pick a commit into the target branch."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/commits/{sha}/cherry_pick"
    client = _get_client()
    resp = await client.post(
        url,
        headers=_headers(),
        json={"branch": target_branch},
        timeout=60,
    )
    if resp.status_code >= 400:
        body = resp.text
        logger.error("cherry_pick %s -> %s: %s %s", sha, target_branch, resp.status_code, body)
        try:
            msg = resp.json().get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


def project_web_url() -> str:
    """Return the project web URL."""
    return f"{_base_url()}/{os.getenv('GITLAB_PROJECT', '')}"


async def create_merge_request(
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
) -> dict:
    """Create a merge request through the GitLab API."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    client = _get_client()
    resp = await client.post(
        url,
        headers=_headers(),
        json={
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        body = resp.text
        logger.error("create_mr %s -> %s: %s %s", source_branch, target_branch, resp.status_code, body)
        try:
            msg = resp.json().get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


async def approve_merge_request(project_id: int, mr_iid: int) -> dict:
    """Approve a merge request through the GitLab API."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/approve"
    client = _get_client()
    resp = await client.post(url, headers=_headers(), timeout=30)
    if resp.status_code >= 400:
        body = resp.text
        logger.error("approve_mr !%s: %s %s", mr_iid, resp.status_code, body)
        try:
            msg = resp.json().get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


async def merge_merge_request(
    project_id: int, mr_iid: int, retries: int = 12, delay: float = 5.0,
) -> dict:
    """Merge a merge request, retrying while GitLab reports 405 not ready."""
    import asyncio

    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/merge"
    client = _get_client()
    for attempt in range(retries):
        resp = await client.put(url, headers=_headers(), timeout=60)
        if resp.status_code < 400:
            return resp.json()
        if resp.status_code == 405 and attempt < retries - 1:
            logger.info("merge_mr !%s: 405 not ready, retry %d/%d in %.0fs", mr_iid, attempt + 1, retries, delay)
            await asyncio.sleep(delay)
            continue
        body = resp.text
        logger.error("merge_mr !%s: %s %s", mr_iid, resp.status_code, body)
        try:
            msg = resp.json().get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"{resp.status_code}: {msg}")
    return {}


async def find_mrs_by_source_branches(
    project_id: int, source_branches: list[str],
) -> list[dict]:
    """Find merge requests by source branch across any state."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    result = []
    client = _get_client()
    for branch in source_branches:
        resp = await client.get(
            url,
            headers=_headers(),
            params={"source_branch": branch, "per_page": 1},
            timeout=30,
        )
        if resp.status_code == 200:
            mrs = resp.json()
            if mrs:
                result.append(mrs[0])
    return result


async def find_merged_mrs_by_branches(
    project_id: int,
    source_branches: list[str],
    target_branch: str,
) -> set[str]:
    """Return source branches that already have a merged MR into target_branch."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    found = set()
    client = _get_client()
    for branch in source_branches:
        resp = await client.get(
            url,
            headers=_headers(),
            params={
                "source_branch": branch,
                "target_branch": target_branch,
                "state": "merged",
                "per_page": 1,
            },
            timeout=30,
        )
        if resp.status_code == 200 and resp.json():
            found.add(branch)
    return found


async def search_merge_requests(
    project_id: int,
    search: str,
    state: str = "merged",
    per_page: int = 20,
) -> list[dict]:
    """Search merge requests by title or description."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    params = {
        "search": search,
        "state": state,
        "per_page": per_page,
        "order_by": "updated_at",
        "sort": "desc",
    }
    client = _get_client()
    resp = await client.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


async def get_all_merged_mrs(
    project_id: int,
    target_branch: str,
    updated_after: str,
    updated_before: str,
    per_page: int = 100,
) -> list[dict]:
    """Return all merged merge requests in a date range, following pagination."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    params = {
        "state": "merged",
        "target_branch": target_branch,
        "updated_after": updated_after,
        "updated_before": updated_before,
        "order_by": "updated_at",
        "sort": "desc",
        "per_page": per_page,
        "page": 1,
    }
    result = []
    client = _get_client()
    while True:
        resp = await client.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        result.extend(data)
        next_page = resp.headers.get("x-next-page", "")
        if not next_page:
            break
        params["page"] = int(next_page)
    return result


async def get_branches(
    project_id: int, search: str = "", per_page: int = 100, page: int = 1,
) -> list[dict]:
    """Return repository branches, optionally filtered by search."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/branches"
    params = {"per_page": per_page, "page": page}
    if search:
        params["search"] = search
    client = _get_client()
    resp = await client.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _change_key(old_path: str, new_path: str) -> tuple[str, str]:
    return old_path or "", new_path or ""


def _extract_raw_diff_body(section_lines: list[str]) -> str:
    for index, line in enumerate(section_lines):
        if line.startswith("@@") or line.startswith("Binary files ") or line == "GIT binary patch":
            return "\n".join(section_lines[index:]).strip()
    return ""


def _parse_raw_diffs(raw_text: str) -> dict[tuple[str, str], str]:
    sections: dict[tuple[str, str], str] = {}
    current_key: tuple[str, str] | None = None
    current_lines: list[str] = []
    pattern = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')

    def flush() -> None:
        nonlocal current_key, current_lines
        if current_key is None:
            return
        body = _extract_raw_diff_body(current_lines)
        if body:
            sections[current_key] = body
        current_key = None
        current_lines = []

    for line in raw_text.splitlines():
        match = pattern.match(line)
        if match:
            flush()
            current_key = _change_key(match.group(1), match.group(2))
            current_lines = []
            continue
        if current_key is not None:
            current_lines.append(line)

    flush()
    return sections


async def _get_file_bytes_or_none(
    client: httpx.AsyncClient,
    project_id: int,
    file_path: str,
    ref: str,
) -> bytes | None:
    if not file_path or not ref:
        return None
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    resp = await client.get(url, headers=_headers(), params={"ref": ref}, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def _looks_binary(content: bytes | None) -> bool:
    return bool(content) and b"\x00" in content


def _decode_text(content: bytes | None) -> str:
    if content is None:
        return ""
    return content.decode("utf-8", errors="replace")


def _build_synthetic_diff(
    old_path: str,
    new_path: str,
    old_bytes: bytes | None,
    new_bytes: bytes | None,
) -> str:
    if _looks_binary(old_bytes) or _looks_binary(new_bytes):
        return "Binary files differ (synthetic fallback: GitLab did not return a textual diff)"

    old_text = _decode_text(old_bytes)
    new_text = _decode_text(new_bytes)
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=old_path or "/dev/null",
            tofile=new_path or "/dev/null",
            lineterm="",
        )
    )

    if len(diff_lines) >= 2 and diff_lines[0].startswith("--- ") and diff_lines[1].startswith("+++ "):
        diff_lines = diff_lines[2:]

    body = "\n".join(diff_lines).strip()
    if body:
        return body

    return "@@ -0,0 +0,0 @@\n # Synthetic fallback: file changed but GitLab did not provide a diff"


def _diff_fallback_refs(data: dict) -> tuple[str, str]:
    diff_refs = data.get("diff_refs") or {}
    old_ref = (
        diff_refs.get("base_sha")
        or diff_refs.get("start_sha")
        or data.get("target_branch", "")
    )
    new_ref = (
        diff_refs.get("head_sha")
        or data.get("sha")
        or data.get("source_branch", "")
    )
    return old_ref, new_ref


def _changes_from_diff_items(items: list[dict]) -> list[dict]:
    return [
        {
            "old_path": c.get("old_path", ""),
            "new_path": c.get("new_path", ""),
            "diff": c.get("diff", ""),
            "new_file": c.get("new_file", False),
            "deleted_file": c.get("deleted_file", False),
            "renamed_file": c.get("renamed_file", False),
        }
        for c in items
    ]


def _latest_version_payload(versions: list[dict]) -> dict | None:
    candidates = [
        version
        for version in versions
        if version.get("state") in {"collected", "overflow", "without_files", None}
    ]
    if not candidates:
        return None

    def sort_key(version: dict) -> tuple[str, int]:
        version_id = version.get("id") or 0
        try:
            version_id = int(version_id)
        except (TypeError, ValueError):
            version_id = 0
        return (str(version.get("created_at") or ""), version_id)

    return max(candidates, key=sort_key)


async def _get_latest_mr_version(
    client: httpx.AsyncClient,
    project_id: int,
    mr_iid: int,
) -> dict | None:
    versions_url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/versions"
    versions_resp = await client.get(versions_url, headers=_headers(), timeout=30)
    versions_resp.raise_for_status()
    versions = versions_resp.json()
    if not isinstance(versions, list):
        return None

    latest_version = _latest_version_payload(versions)
    if not latest_version or not latest_version.get("id"):
        return None

    version_url = (
        f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
        f"/versions/{latest_version['id']}"
    )
    version_resp = await client.get(
        version_url,
        headers=_headers(),
        params={"unidiff": "true"},
        timeout=60,
    )
    version_resp.raise_for_status()
    version_data = version_resp.json()
    if not isinstance(version_data, dict):
        return None
    return version_data


def _apply_version_diff(data: dict, version_data: dict | None) -> dict:
    if not version_data:
        return data

    diffs = version_data.get("diffs")
    if not isinstance(diffs, list):
        return data

    result = dict(data)
    result["changes"] = _changes_from_diff_items(diffs)
    result["overflow"] = bool(data.get("overflow")) or version_data.get("state") == "overflow"
    result["diff_refs"] = {
        **(data.get("diff_refs") or {}),
        "base_sha": version_data.get("base_commit_sha") or (data.get("diff_refs") or {}).get("base_sha"),
        "start_sha": version_data.get("start_commit_sha") or (data.get("diff_refs") or {}).get("start_sha"),
        "head_sha": version_data.get("head_commit_sha") or (data.get("diff_refs") or {}).get("head_sha"),
    }
    result["sha"] = version_data.get("head_commit_sha") or data.get("sha", "")
    return result


@_ttl_cache(ttl_seconds=_MR_DIFF_CACHE_TTL_SECONDS, max_size=_MR_DIFF_CACHE_MAX_SIZE)
async def get_mr_diff(project_id: int, mr_iid: int, force_refresh: bool = False) -> dict:
    """Return MR metadata plus per-file diffs.

    We first ask GitLab for `changes` with `access_raw_diffs=true` so collapsed
    files are less likely to come back empty. If GitLab still returns empty diffs
    or marks the response as overflowed, we fetch `raw_diffs` and fill the gaps.

    Results are cached for 10 minutes to improve performance.
    """
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    client = _get_client()
    resp = await client.get(
        url,
        headers=_headers(),
        params={"access_raw_diffs": "true", "unidiff": "true"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if force_refresh:
        data = _apply_version_diff(
            data,
            await _get_latest_mr_version(client, project_id, mr_iid),
        )

    changes = _changes_from_diff_items(data.get("changes", []))

    if data.get("overflow") or any(not change["diff"] for change in changes):
        raw_url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/raw_diffs"
        raw_resp = await client.get(raw_url, headers=_headers(), timeout=60)
        raw_resp.raise_for_status()
        raw_diffs = _parse_raw_diffs(raw_resp.text)
        for change in changes:
            if change["diff"]:
                continue
            raw_diff = raw_diffs.get(_change_key(change.get("old_path", ""), change.get("new_path", "")))
            if raw_diff:
                change["diff"] = raw_diff

    for change in changes:
        if change["diff"]:
            continue
        old_path = change.get("old_path", "")
        new_path = change.get("new_path", "")
        old_ref, new_ref = _diff_fallback_refs(data)
        old_bytes = None
        new_bytes = None
        if not change.get("new_file"):
            old_bytes = await _get_file_bytes_or_none(client, project_id, old_path, old_ref)
        if not change.get("deleted_file"):
            new_bytes = await _get_file_bytes_or_none(client, project_id, new_path, new_ref)
        change["diff"] = _build_synthetic_diff(old_path, new_path, old_bytes, new_bytes)

    return {
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "author": data.get("author", {}).get("name", ""),
        "source_branch": data.get("source_branch", ""),
        "source_ref": (
            data.get("diff_refs", {}).get("head_sha")
            or data.get("sha")
            or data.get("source_branch", "")
        ),
        "target_branch": data.get("target_branch", ""),
        "web_url": data.get("web_url", ""),
        "overflow": bool(data.get("overflow", False)),
        "changes": changes,
    }


async def get_file_content(project_id: int, file_path: str, ref: str) -> str:
    """Return repository file content as text."""
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), params={"ref": ref}, timeout=30)
    resp.raise_for_status()
    return resp.text


async def get_mr_pipelines(
    project_id: int,
    mr_iid: int,
) -> list[dict]:
    """Return pipelines for a merge request, most recent first."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/pipelines"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


async def get_pipeline_jobs(
    project_id: int,
    pipeline_id: int,
) -> list[dict]:
    """Return jobs for a pipeline."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs"
    params: dict[str, str | int] = {
        "per_page": 100,
        "page": 1,
        "include_retried": "true",
    }
    result = []
    client = _get_client()
    while True:
        resp = await client.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        result.extend(data)
        next_page = resp.headers.get("x-next-page", "")
        if not next_page:
            break
        params["page"] = int(next_page)
    return result


async def get_job_trace(project_id: int, job_id: int) -> str:
    """Return a CI job trace as text."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/jobs/{job_id}/trace"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.text


async def retry_job(project_id: int, job_id: int) -> dict:
    """Retry a CI job and return the new job payload."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/jobs/{job_id}/retry"
    client = _get_client()
    resp = await client.post(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


async def get_file_bytes(project_id: int, file_path: str, ref: str) -> bytes:
    """Return repository file content as raw bytes."""
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    client = _get_client()
    resp = await client.get(url, headers=_headers(), params={"ref": ref}, timeout=30)
    resp.raise_for_status()
    return resp.content
