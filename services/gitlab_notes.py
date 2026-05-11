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


async def list_merge_request_notes(mr_iid: int) -> list[dict]:
    url = f"{_gitlab_base()}/api/v4/projects/{_gitlab_project_path()}/merge_requests/{mr_iid}/notes"
    params: dict[str, str | int] = {
        "per_page": 100,
        "page": 1,
        "sort": "desc",
        "order_by": "created_at",
    }
    notes: list[dict] = []
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        while True:
            resp = await client.get(url, headers=_gitlab_headers(), params=params)
            resp.raise_for_status()
            notes.extend(resp.json())
            next_page = resp.headers.get("x-next-page", "")
            if not next_page:
                break
            params["page"] = int(next_page)
    return notes


async def delete_merge_request_note(mr_iid: int, note_id: int | str) -> None:
    url = (
        f"{_gitlab_base()}/api/v4/projects/{_gitlab_project_path()}"
        f"/merge_requests/{mr_iid}/notes/{quote(str(note_id), safe='')}"
    )
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.delete(url, headers=_gitlab_headers())
        resp.raise_for_status()


async def post_merge_request_discussion(mr_iid: int, body: str) -> dict:
    url = f"{_gitlab_base()}/api/v4/projects/{_gitlab_project_path()}/merge_requests/{mr_iid}/discussions"
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.post(url, headers=_gitlab_headers(), json={"body": body})
        resp.raise_for_status()
    return resp.json()


async def resolve_merge_request_discussion(mr_iid: int, discussion_id: str) -> dict:
    url = (
        f"{_gitlab_base()}/api/v4/projects/{_gitlab_project_path()}"
        f"/merge_requests/{mr_iid}/discussions/{quote(str(discussion_id), safe='')}"
    )
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.put(
            url,
            headers=_gitlab_headers(),
            json={"resolved": True},
        )
        resp.raise_for_status()
    return resp.json()
