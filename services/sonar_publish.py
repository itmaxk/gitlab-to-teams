import logging

from db import get_db
from services.gitlab_client import (
    get_mr_by_iid,
    get_mr_pipelines,
    get_pipeline_jobs,
)
from services.gitlab_notes import (
    delete_merge_request_note,
    list_merge_request_notes,
    post_merge_request_note,
)
from services.sonar_client import (
    build_sonar_url,
    extract_sonar_link,
    fetch_sonar_issues,
    format_gitlab_comment,
)

logger = logging.getLogger(__name__)

DEFAULT_SONAR_JOB_NAME = "config:sonar"
SONAR_COMMENT_MARKER = "## SonarQube Analysis Results"
SONAR_LOG_PREFIX = "sonar-job:"
PENDING_JOB_STATUSES = {"created", "pending", "running", "waiting_for_resource", "preparing"}


class SonarPublishResult:
    __slots__ = ("checked", "published", "skipped", "errors")

    def __init__(
        self,
        checked: int = 0,
        published: list[dict] | None = None,
        skipped: list[dict] | None = None,
        errors: list[str] | None = None,
    ):
        self.checked = checked
        self.published = published or []
        self.skipped = skipped or []
        self.errors = errors or []


def parse_sonar_job_name(raw_value: str) -> str:
    return (raw_value or "").strip() or DEFAULT_SONAR_JOB_NAME


def _sonar_job_log_path(job_id: int) -> str:
    return f"{SONAR_LOG_PREFIX}{job_id}"


def _was_sonar_job_published(rule_id: int, job_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM notification_log WHERE rule_id = ? AND file_path = ?",
        (rule_id, _sonar_job_log_path(job_id)),
    ).fetchone()
    conn.close()
    return row is not None


def _log_sonar_publish(
    rule_id: int,
    mr_iid: int,
    mr_title: str,
    mr_url: str,
    job_id: int,
    job_name: str,
    note_id: str,
    sonar_url: str,
) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO notification_log
           (rule_id, mr_iid, mr_title, mr_url, file_path, file_content,
            teams_sent, email_sent, gitlab_sent, gitlab_discussion_id, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_id,
            mr_iid,
            mr_title,
            mr_url,
            _sonar_job_log_path(job_id),
            f"{job_name}\n{sonar_url}",
            0,
            0,
            1,
            note_id,
            "",
        ),
    )
    conn.commit()
    conn.close()


async def resolve_sonar_url(project_id: int, mr_iid: int) -> str:
    sonar_url = build_sonar_url(mr_iid)
    try:
        mr = await get_mr_by_iid(project_id, mr_iid)
    except Exception as exc:
        logger.warning("Failed to fetch MR !%s for Sonar URL extraction: %s", mr_iid, exc)
        return sonar_url
    return extract_sonar_link(mr.get("description")) or sonar_url


async def delete_previous_sonar_notes(mr_iid: int) -> int:
    deleted = 0
    notes = await list_merge_request_notes(mr_iid)
    for note in notes:
        body = note.get("body") or ""
        note_id = note.get("id")
        if note_id and SONAR_COMMENT_MARKER in body:
            await delete_merge_request_note(mr_iid, note_id)
            deleted += 1
    return deleted


async def publish_sonar_issues_to_gitlab(
    mr_iid: int,
    sonar_url: str,
    formatted_issues: str,
    raw_issues: list[dict] | None = None,
) -> dict:
    comment = format_gitlab_comment(sonar_url, formatted_issues, raw_issues=raw_issues)
    await delete_previous_sonar_notes(mr_iid)
    return await post_merge_request_note(mr_iid, comment)


async def fetch_and_publish_sonar_issues(project_id: int, mr_iid: int) -> dict:
    sonar_url = await resolve_sonar_url(project_id, mr_iid)
    result = await fetch_sonar_issues(sonar_url)
    note = await publish_sonar_issues_to_gitlab(
        mr_iid,
        sonar_url,
        result["formatted"],
        raw_issues=result["issues"],
    )
    return {
        "count": result["total"],
        "issues": result["formatted"],
        "raw_issues": result["issues"],
        "sonar_url": sonar_url,
        "note": note,
    }


async def publish_sonar_issues_after_job(
    project_id: int,
    mr_iid: int,
    job_name: str,
    rule_id: int,
    mr_title: str = "",
    mr_url: str = "",
) -> SonarPublishResult:
    result = SonarPublishResult()

    try:
        pipelines = await get_mr_pipelines(project_id, mr_iid)
    except Exception:
        logger.exception("Failed to fetch pipelines for MR !%s", mr_iid)
        result.errors.append("pipelines_fetch_failed")
        return result

    if not pipelines:
        return result

    pipeline_id = pipelines[0].get("id")
    if not pipeline_id:
        return result

    try:
        jobs = await get_pipeline_jobs(project_id, pipeline_id)
    except Exception:
        logger.exception("Failed to fetch jobs for pipeline %s (MR !%s)", pipeline_id, mr_iid)
        result.errors.append("jobs_fetch_failed")
        return result

    target_job_name = parse_sonar_job_name(job_name)
    matching_jobs = [job for job in jobs if job.get("name") == target_job_name]
    if not matching_jobs:
        result.skipped.append({"job_name": target_job_name, "reason": "job_not_found"})
        return result

    job = max(matching_jobs, key=lambda item: int(item.get("id") or 0))
    job_id = int(job.get("id") or 0)
    status = job.get("status", "")
    result.checked += 1

    if not job_id:
        result.skipped.append({"job_name": target_job_name, "reason": "job_id_missing"})
        return result
    if status in PENDING_JOB_STATUSES:
        result.skipped.append({"job_id": job_id, "job_name": target_job_name, "reason": "job_not_finished"})
        return result
    if _was_sonar_job_published(rule_id, job_id):
        result.skipped.append({"job_id": job_id, "job_name": target_job_name, "reason": "already_published"})
        return result

    try:
        published = await fetch_and_publish_sonar_issues(project_id, mr_iid)
        note_id = str((published.get("note") or {}).get("id") or "")
        _log_sonar_publish(
            rule_id,
            mr_iid,
            mr_title,
            mr_url,
            job_id,
            target_job_name,
            note_id,
            published["sonar_url"],
        )
        result.published.append(
            {
                "job_id": job_id,
                "job_name": target_job_name,
                "note_id": note_id,
                "issues_count": published["count"],
            }
        )
    except Exception:
        logger.exception("Failed to publish Sonar issues for MR !%s after job %s", mr_iid, job_id)
        result.errors.append(f"publish_failed:{job_id}")

    return result
