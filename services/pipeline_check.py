import logging

from services.gitlab_client import get_mr_pipelines, get_pipeline_jobs

logger = logging.getLogger(__name__)


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