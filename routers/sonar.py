import logging
import os
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from services.sonar_client import (
    fetch_sonar_issues,
    build_sonar_url,
    extract_sonar_link,
)
from services.sonar_publish import publish_sonar_issues_to_gitlab

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sonar"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# --------------- HTML page ---------------

@router.get("/sonar", response_class=HTMLResponse)
def sonar_page(request: Request):
    return templates.TemplateResponse(request, "sonar.html", {
        "sonar_url": os.getenv("SONAR_URL", ""),
        "sonar_project": os.getenv("SONAR_PROJECT", ""),
    })


# --------------- API ---------------

def _gitlab_base() -> str:
    return os.getenv("GITLAB_URL", "").rstrip("/")


def _gitlab_headers() -> dict[str, str]:
    return {
        "PRIVATE-TOKEN": os.getenv("GITLAB_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _gitlab_project_path() -> str:
    return os.getenv("GITLAB_PROJECT", "")


def _mr_web_url(mr_id: int | str) -> str:
    return f"{_gitlab_base()}/{_gitlab_project_path()}/-/merge_requests/{mr_id}"


@router.get("/api/sonar/config")
def sonar_config():
    """Возвращает конфигурацию для фронтенда."""
    return {
        "gitlab_url": _gitlab_base(),
        "gitlab_project": _gitlab_project_path(),
        "sonar_url": os.getenv("SONAR_URL", ""),
        "sonar_project": os.getenv("SONAR_PROJECT", ""),
    }


@router.get("/api/sonar/{mr_id}")
async def get_sonar_issues(mr_id: int, dry_run: bool = False):
    """Получает issues из SonarQube. При dry_run=false также постит комментарий в GitLab."""
    sonar_url = build_sonar_url(mr_id)
    gitlab_url = _mr_web_url(mr_id)

    # Попробуем извлечь ссылку на SonarQube из описания MR
    mr_sonar_url = await _extract_sonar_url_from_mr(mr_id)
    if mr_sonar_url:
        sonar_url = mr_sonar_url

    try:
        result = await fetch_sonar_issues(sonar_url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка SonarQube: {e}")

    if not dry_run:
        try:
            await _post_comment_to_mr(mr_id, sonar_url, result["formatted"], raw_issues=result["issues"])
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Issues получены ({result['total']}), но ошибка отправки в GitLab: {e}")

    return {
        "success": True,
        "message": "Issues получены" + ("" if dry_run else " и отправлены в GitLab"),
        "data": {
            "count": result["total"],
            "issues": result["formatted"],
            "sonar_url": sonar_url,
            "gitlab_url": gitlab_url,
            "dry_run": dry_run,
        },
    }


@router.post("/api/sonar/post-issues/{mr_id}")
async def post_sonar_issues(mr_id: int):
    """Получает issues из SonarQube и постит комментарий в GitLab MR."""
    sonar_url = build_sonar_url(mr_id)

    mr_sonar_url = await _extract_sonar_url_from_mr(mr_id)
    if mr_sonar_url:
        sonar_url = mr_sonar_url

    try:
        result = await fetch_sonar_issues(sonar_url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка SonarQube: {e}")

    try:
        await _post_comment_to_mr(mr_id, sonar_url, result["formatted"], raw_issues=result["issues"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Issues получены ({result['total']}), но ошибка отправки в GitLab: {e}")

    return {
        "success": True,
        "message": "Issues получены из SonarQube и отправлены в GitLab",
        "data": {
            "count": result["total"],
            "issues": result["formatted"],
            "sonar_url": sonar_url,
            "gitlab_url": _mr_web_url(mr_id),
        },
    }


@router.post("/api/sonar/fetch-issues")
async def fetch_issues_only(request: Request):
    """Получает issues по произвольному SonarQube URL (без отправки в GitLab)."""
    body = await request.json()
    sonar_url = body.get("sonar_url", "")
    if not sonar_url:
        raise HTTPException(status_code=400, detail="sonar_url обязателен")
    try:
        result = await fetch_sonar_issues(sonar_url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка SonarQube: {e}")
    return {
        "success": True,
        "data": {
            "count": result["total"],
            "issues": result["formatted"],
            "raw_issues": result["issues"],
            "sonar_url": sonar_url,
        },
    }


@router.post("/api/sonar/post-comment")
async def post_comment(request: Request):
    """Постит готовый текст issues как комментарий в GitLab MR."""
    body = await request.json()
    mr_id = body.get("mr_id")
    sonar_url = body.get("sonar_url", "")
    issues_text = body.get("issues", "")
    raw_issues = body.get("raw_issues")
    if not mr_id or not issues_text:
        raise HTTPException(status_code=400, detail="mr_id и issues обязательны")
    try:
        await _post_comment_to_mr(int(mr_id), sonar_url, issues_text, raw_issues=raw_issues)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка отправки в GitLab: {e}")
    return {"success": True, "message": "Комментарий отправлен в GitLab"}


# --------------- Helpers ---------------

async def _extract_sonar_url_from_mr(mr_id: int) -> str | None:
    """Извлекает ссылку на SonarQube из описания MR."""
    project_path = quote(_gitlab_project_path(), safe="")
    url = f"{_gitlab_base()}/api/v4/projects/{project_path}/merge_requests/{mr_id}"
    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.get(url, headers=_gitlab_headers())
            resp.raise_for_status()
        mr = resp.json()
        return extract_sonar_link(mr.get("description"))
    except Exception as e:
        logger.warning("Не удалось получить MR %s для извлечения sonar URL: %s", mr_id, e)
        return None


async def _post_comment_to_mr(
    mr_id: int, sonar_url: str, formatted_issues: str,
    raw_issues: list[dict] | None = None,
) -> None:
    """Постит комментарий с issues в GitLab MR."""
    await publish_sonar_issues_to_gitlab(
        mr_id,
        sonar_url,
        formatted_issues,
        raw_issues=raw_issues,
    )
