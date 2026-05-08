import logging
import re

from db import get_db
from services.gitlab_client import get_job_trace, get_mr_pipelines, get_pipeline_jobs, retry_job

logger = logging.getLogger(__name__)

CONFIG_BUILD_MARKER = "[5/5] Building fresh packages..."
TLS_SOCKET_DISCONNECT_ERROR = (
    "error Error: Client network socket disconnected before secure TLS connection "
    "was established"
)
DEFAULT_RETRY_JOB_NAMES = ("config:check-uncommitted", "config:validate")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class PipelineCheckResult:
    """Result of checking a pipeline job.

    - failed=True:  the named job exists and has status "failed".
    - completed=True:  the pipeline finished and the named job was found
      but did not fail (it succeeded, was cancelled, etc.).
    - Both False:  pipeline not found, still running, or the named job
      does not exist yet — the caller should retry on the next poll.
    """

    __slots__ = ("failed", "completed", "job_web_url")

    def __init__(
        self, failed: bool = False, completed: bool = False, job_web_url: str | None = None
    ):
        self.failed = failed
        self.completed = completed
        self.job_web_url = job_web_url


class PipelineRetryResult:
    """Result of checking failed pipeline jobs for automatic retry."""

    __slots__ = ("checked", "retried", "skipped", "errors")

    def __init__(
        self,
        checked: int = 0,
        retried: list[dict] | None = None,
        skipped: list[dict] | None = None,
        errors: list[str] | None = None,
    ):
        self.checked = checked
        self.retried = retried or []
        self.skipped = skipped or []
        self.errors = errors or []


def parse_retry_job_names(raw_value: str) -> list[str]:
    """Parse configured CI job names from comma/newline/semicolon separated text."""
    names = [
        part.strip()
        for part in re.split(r"[,;\n]+", raw_value or "")
        if part.strip()
    ]
    if not names:
        names = list(DEFAULT_RETRY_JOB_NAMES)
    return list(dict.fromkeys(names))


def _normalize_trace(trace: str) -> str:
    text = _ANSI_RE.sub("", trace or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def should_retry_config_job_trace(trace: str) -> bool:
    """Return True when the trace stops at the fresh packages marker."""
    normalized = _normalize_trace(trace)
    if TLS_SOCKET_DISCONNECT_ERROR in normalized:
        return True

    marker_index = normalized.rfind(CONFIG_BUILD_MARKER)
    if marker_index < 0:
        return False
    suffix = normalized[marker_index + len(CONFIG_BUILD_MARKER):].strip()
    return not suffix or suffix.startswith("ERROR: Job failed")


def _was_job_retried(rule_id: int, job_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM pipeline_job_retry_log WHERE rule_id = ? AND job_id = ?",
        (rule_id, job_id),
    ).fetchone()
    conn.close()
    return row is not None


def _log_job_retry(
    rule_id: int,
    mr_iid: int,
    pipeline_id: int,
    job: dict,
    retried_job: dict,
) -> None:
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO pipeline_job_retry_log
           (rule_id, mr_iid, pipeline_id, job_id, retried_job_id, job_name, job_web_url)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_id,
            mr_iid,
            pipeline_id,
            int(job.get("id") or 0),
            int(retried_job.get("id") or 0),
            job.get("name", ""),
            job.get("web_url", ""),
        ),
    )
    conn.commit()
    conn.close()


async def check_pipeline_job_failed(
    project_id: int,
    mr_iid: int,
    job_name: str,
) -> PipelineCheckResult:
    """Check if a named job failed in the latest pipeline of an MR.

    Returns a PipelineCheckResult:
    - failed=True:       job found with status "failed" (job_web_url set).
    - completed=True:    pipeline finished, job exists but did not fail.
    - Both False:        pipeline not found / still running / job not yet
                         created — caller should retry later.
    """
    try:
        pipelines = await get_mr_pipelines(project_id, mr_iid)
    except Exception:
        logger.exception("Failed to fetch pipelines for MR !%s", mr_iid)
        return PipelineCheckResult()

    if not pipelines:
        logger.debug("No pipelines found for MR !%s", mr_iid)
        return PipelineCheckResult()

    latest_pipeline = pipelines[0]
    pipeline_id = latest_pipeline.get("id")
    if not pipeline_id:
        return PipelineCheckResult()

    pipeline_status = latest_pipeline.get("status", "")
    if pipeline_status in ("running", "pending", "created", "waiting_for_resource"):
        logger.debug(
            "Pipeline %s for MR !%s is still running (status=%s), will retry",
            pipeline_id,
            mr_iid,
            pipeline_status,
        )
        return PipelineCheckResult()

    try:
        jobs = await get_pipeline_jobs(project_id, pipeline_id)
    except Exception:
        logger.exception(
            "Failed to fetch jobs for pipeline %s (MR !%s)", pipeline_id, mr_iid
        )
        return PipelineCheckResult()

    for job in jobs:
        if job.get("name") == job_name:
            status = job.get("status", "")
            if status == "failed":
                return PipelineCheckResult(failed=True, job_web_url=job.get("web_url"))
            return PipelineCheckResult(completed=True)

    logger.debug(
        "Job %r not found in pipeline %s (MR !%s), will retry",
        job_name,
        pipeline_id,
        mr_iid,
    )
    return PipelineCheckResult()


async def retry_failed_config_jobs(
    project_id: int,
    mr_iid: int,
    job_names: list[str],
    rule_id: int,
) -> PipelineRetryResult:
    """Retry selected failed jobs when their trace matches the config build hang."""
    result = PipelineRetryResult()
    target_names = set(job_names or DEFAULT_RETRY_JOB_NAMES)

    try:
        pipelines = await get_mr_pipelines(project_id, mr_iid)
    except Exception:
        logger.exception("Failed to fetch pipelines for MR !%s", mr_iid)
        result.errors.append("pipelines_fetch_failed")
        return result

    if not pipelines:
        return result

    latest_pipeline = pipelines[0]
    pipeline_id = latest_pipeline.get("id")
    if not pipeline_id:
        return result

    try:
        jobs = await get_pipeline_jobs(project_id, pipeline_id)
    except Exception:
        logger.exception(
            "Failed to fetch jobs for pipeline %s (MR !%s)", pipeline_id, mr_iid
        )
        result.errors.append("jobs_fetch_failed")
        return result

    for job in jobs:
        job_name = job.get("name", "")
        job_id = int(job.get("id") or 0)
        if job_name not in target_names or job.get("status") != "failed" or not job_id:
            continue

        result.checked += 1
        if _was_job_retried(rule_id, job_id):
            result.skipped.append({"job_id": job_id, "job_name": job_name, "reason": "already_retried"})
            continue

        try:
            trace = await get_job_trace(project_id, job_id)
        except Exception:
            logger.exception("Failed to fetch trace for job %s (MR !%s)", job_id, mr_iid)
            result.errors.append(f"trace_fetch_failed:{job_id}")
            continue

        if not should_retry_config_job_trace(trace):
            result.skipped.append({"job_id": job_id, "job_name": job_name, "reason": "trace_not_matched"})
            continue

        try:
            retried_job = await retry_job(project_id, job_id)
            _log_job_retry(rule_id, mr_iid, int(pipeline_id), job, retried_job)
            result.retried.append(
                {
                    "job_id": job_id,
                    "retried_job_id": retried_job.get("id"),
                    "job_name": job_name,
                    "job_web_url": job.get("web_url", ""),
                }
            )
            logger.info(
                "Retried job %s (%s) for MR !%s pipeline %s",
                job_id,
                job_name,
                mr_iid,
                pipeline_id,
            )
        except Exception:
            logger.exception("Failed to retry job %s (MR !%s)", job_id, mr_iid)
            result.errors.append(f"retry_failed:{job_id}")

    return result
