import difflib
import logging
import os
import re
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("GITLAB_URL", "").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "PRIVATE-TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _project_path() -> str:
    return quote(os.getenv("GITLAB_PROJECT", ""), safe="")


async def get_project_id() -> int:
    """Return the numeric project ID for GITLAB_PROJECT."""
    url = f"{_base_url()}/api/v4/projects/{_project_path()}"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
    return resp.json()["id"]


async def get_merge_requests(
    project_id: int,
    state: str = "merged",
    target_branch: str = "master",
    per_page: int = 20,
) -> list[dict]:
    """Return merge requests filtered by state and target branch."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    params = {
        "state": state,
        "target_branch": target_branch,
        "order_by": "updated_at",
        "sort": "desc",
        "per_page": per_page,
    }
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
    return resp.json()


async def get_mr_changes(project_id: int, mr_iid: int) -> list[str]:
    """Return changed file paths for a merge request."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    return [change["new_path"] for change in data.get("changes", []) if change.get("new_path")]


async def get_mr_by_iid(project_id: int, mr_iid: int) -> dict:
    """Return a single merge request by IID."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
    return resp.json()


async def create_branch(project_id: int, branch_name: str, ref: str) -> dict:
    """Create a repository branch from ref."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/branches"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_headers(), json={
            "branch": branch_name,
            "ref": ref,
        })
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
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        resp = await client.post(url, headers=_headers(), json={
            "branch": target_branch,
        })
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_headers(), json={
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        })
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_headers())
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
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        for attempt in range(retries):
            resp = await client.put(url, headers=_headers())
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        for branch in source_branches:
            resp = await client.get(url, headers=_headers(), params={
                "source_branch": branch,
                "per_page": 1,
            })
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        for branch in source_branches:
            resp = await client.get(url, headers=_headers(), params={
                "source_branch": branch,
                "target_branch": target_branch,
                "state": "merged",
                "per_page": 1,
            })
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params=params)
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        while True:
            resp = await client.get(url, headers=_headers(), params=params)
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params=params)
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
    resp = await client.get(url, headers=_headers(), params={"ref": ref})
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


async def get_mr_diff(project_id: int, mr_iid: int) -> dict:
    """Return MR metadata plus per-file diffs.

    We first ask GitLab for `changes` with `access_raw_diffs=true` so collapsed
    files are less likely to come back empty. If GitLab still returns empty diffs
    or marks the response as overflowed, we fetch `raw_diffs` and fill the gaps.
    """
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        resp = await client.get(
            url,
            headers=_headers(),
            params={"access_raw_diffs": "true", "unidiff": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
        changes = [
            {
                "old_path": c.get("old_path", ""),
                "new_path": c.get("new_path", ""),
                "diff": c.get("diff", ""),
                "new_file": c.get("new_file", False),
                "deleted_file": c.get("deleted_file", False),
                "renamed_file": c.get("renamed_file", False),
            }
            for c in data.get("changes", [])
        ]

        if data.get("overflow") or any(not change["diff"] for change in changes):
            raw_url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/raw_diffs"
            raw_resp = await client.get(raw_url, headers=_headers())
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
            old_bytes = await _get_file_bytes_or_none(client, project_id, old_path, data.get("target_branch", ""))
            new_bytes = await _get_file_bytes_or_none(client, project_id, new_path, data.get("source_branch", ""))
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
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params={"ref": ref})
        resp.raise_for_status()
    return resp.text


async def get_file_bytes(project_id: int, file_path: str, ref: str) -> bytes:
    """Return repository file content as raw bytes."""
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params={"ref": ref})
        resp.raise_for_status()
    return resp.content
