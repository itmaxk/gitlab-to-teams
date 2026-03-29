import logging
import os
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
    """Получает числовой ID проекта по его пути (GITLAB_PROJECT)."""
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
    """Получает список MR по состоянию и целевой ветке, отсортированных по дате обновления."""
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
    """Возвращает список путей изменённых файлов в MR."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    return [change["new_path"] for change in data.get("changes", []) if change.get("new_path")]


async def get_mr_by_iid(project_id: int, mr_iid: int) -> dict:
    """Получает один MR по его IID."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
    return resp.json()


async def create_branch(project_id: int, branch_name: str, ref: str) -> dict:
    """Создаёт новую ветку от указанного ref."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/branches"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_headers(), json={
            "branch": branch_name,
            "ref": ref,
        })
        if resp.status_code >= 400:
            body = resp.text
            logger.error("create_branch %s from %s → %s %s", branch_name, ref, resp.status_code, body)
            try:
                msg = resp.json().get("message", body)
            except Exception:
                msg = body
            raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


async def cherry_pick_commit(project_id: int, sha: str, target_branch: str) -> dict:
    """Cherry-pick коммита в указанную ветку."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/commits/{sha}/cherry_pick"
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        resp = await client.post(url, headers=_headers(), json={
            "branch": target_branch,
        })
        if resp.status_code >= 400:
            body = resp.text
            logger.error("cherry_pick %s → %s: %s %s", sha, target_branch, resp.status_code, body)
            try:
                msg = resp.json().get("message", body)
            except Exception:
                msg = body
            raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


def project_web_url() -> str:
    """Возвращает web-URL проекта для построения ссылок на MR."""
    return f"{_base_url()}/{os.getenv('GITLAB_PROJECT', '')}"


async def create_merge_request(
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
) -> dict:
    """Создаёт MR через API."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_headers(), json={
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        })
        if resp.status_code >= 400:
            body = resp.text
            logger.error("create_mr %s → %s: %s %s", source_branch, target_branch, resp.status_code, body)
            try:
                msg = resp.json().get("message", body)
            except Exception:
                msg = body
            raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


async def approve_merge_request(project_id: int, mr_iid: int) -> dict:
    """Approve MR через API."""
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


async def merge_merge_request(project_id: int, mr_iid: int) -> dict:
    """Merge MR через API."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/merge"
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        resp = await client.put(url, headers=_headers())
        if resp.status_code >= 400:
            body = resp.text
            logger.error("merge_mr !%s: %s %s", mr_iid, resp.status_code, body)
            try:
                msg = resp.json().get("message", body)
            except Exception:
                msg = body
            raise RuntimeError(f"{resp.status_code}: {msg}")
    return resp.json()


async def find_mrs_by_source_branches(
    project_id: int, source_branches: list[str],
) -> list[dict]:
    """Ищет MR по списку source_branch (любое состояние)."""
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
    """Возвращает set source_branch для которых есть merged MR в target_branch."""
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
    """Ищет MR по тексту в title/description."""
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
    """Получает все merged MR для ветки за период с пагинацией."""
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
    """Получает список веток проекта, опционально фильтруя по search."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/branches"
    params = {"per_page": per_page, "page": page}
    if search:
        params["search"] = search
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
    return resp.json()


async def get_file_content(project_id: int, file_path: str, ref: str) -> str:
    """Получает содержимое файла из репозитория GitLab."""
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params={"ref": ref})
        resp.raise_for_status()
    return resp.text
