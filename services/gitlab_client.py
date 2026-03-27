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


async def get_mr_changes(project_id: int, mr_iid: int) -> list[str]:
    """Возвращает список путей изменённых файлов в MR."""
    url = f"{_base_url()}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    return [change["new_path"] for change in data.get("changes", []) if change.get("new_path")]


async def get_file_content(project_id: int, file_path: str, ref: str) -> str:
    """Получает содержимое файла из репозитория GitLab."""
    encoded_path = quote(file_path, safe="")
    url = f"{_base_url()}/api/v4/projects/{project_id}/repository/files/{encoded_path}/raw"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=_headers(), params={"ref": ref})
        resp.raise_for_status()
    return resp.text
