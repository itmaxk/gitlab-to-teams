import os
from urllib.parse import quote

import httpx


def _gitlab_base() -> str:
    return os.getenv("GITLAB_URL", "").rstrip("/")


def _gitlab_headers() -> dict[str, str]:
    return {
        "PRIVATE-TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _gitlab_project_path() -> str:
    return quote(os.getenv("GITLAB_PROJECT", ""), safe="")


async def post_merge_request_note(mr_iid: int, body: str) -> dict:
    url = f"{_gitlab_base()}/api/v4/projects/{_gitlab_project_path()}/merge_requests/{mr_iid}/notes"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_gitlab_headers(), json={"body": body})
        resp.raise_for_status()
    return resp.json()
