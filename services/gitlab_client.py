import os
from urllib.parse import quote

import httpx


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
        resp.raise_for_status()
    return resp.json()


async def cherry_pick_commit(project_id: int, sha: str, target_branch: str) -> dict:
    """Cherry-pick коммита в указанную ветку."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/commits/{sha}/cherry_pick"
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        resp = await client.post(url, headers=_headers(), json={
            "branch": target_branch,
        })
        resp.raise_for_status()
    return resp.json()


def project_web_url() -> str:
    """Возвращает web-URL проекта для построения ссылок на MR."""
    return f"{_base_url()}/{os.getenv('GITLAB_PROJECT', '')}"


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


async def get_file_content(project_id: int, file_path: str, ref: str) -> str:
    """Получает содержимое файла из репозитория GitLab."""
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params={"ref": ref})
        resp.raise_for_status()
    return resp.text
