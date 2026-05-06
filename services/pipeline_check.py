import logging

from services.gitlab_client import get_mr_pipelines, get_pipeline_jobs

logger = logging.getLogger(__name__)


async def check_pipeline_job_failed(
    project_id: int,
    mr_iid: int,
    job_name: str,
) -> tuple[bool, str | None]:
    """Check if a named job failed in the latest pipeline of an MR.

    Returns (failed, job_web_url).
    If the job failed, ``failed`` is True and ``job_web_url`` points to the job.
    If the pipeline has no failed job with that name, returns (False, None).
    """
    try:
        pipelines = await get_mr_pipelines(project_id, mr_iid)
    except Exception:
        logger.exception("Failed to fetch pipelines for MR !%s", mr_iid)
        return False, None

    if not pipelines:
        logger.debug("No pipelines found for MR !%s", mr_iid)
        return False, None

    latest_pipeline = pipelines[0]
    pipeline_id = latest_pipeline.get("id")
    if not pipeline_id:
        return False, None

    try:
        jobs = await get_pipeline_jobs(project_id, pipeline_id)
    except Exception:
        logger.exception(
            "Failed to fetch jobs for pipeline %s (MR !%s)", pipeline_id, mr_iid
        )
        return False, None

    for job in jobs:
        if job.get("name") == job_name and job.get("status") == "failed":
            web_url = job.get("web_url", "")
            return True, web_url

    return False, None