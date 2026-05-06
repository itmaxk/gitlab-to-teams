from unittest.mock import AsyncMock, patch

import pytest

from services.pipeline_check import PipelineCheckResult, check_pipeline_job_failed


@pytest.mark.anyio
async def test_returns_failed_when_job_failed():
    pipelines = [{"id": 42, "status": "failed"}]
    jobs = [
        {"name": "build", "status": "success", "web_url": "https://gitlab.com/job/1"},
        {"name": "changelog:validate", "status": "failed", "web_url": "https://gitlab.com/job/2"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is True
    assert result.completed is False
    assert result.job_web_url == "https://gitlab.com/job/2"


@pytest.mark.anyio
async def test_returns_completed_when_job_succeeded():
    pipelines = [{"id": 42, "status": "success"}]
    jobs = [
        {"name": "changelog:validate", "status": "success", "web_url": "https://gitlab.com/job/1"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is False
    assert result.completed is True


@pytest.mark.anyio
async def test_returns_pending_when_no_pipelines():
    with patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=[]):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is False
    assert result.completed is False


@pytest.mark.anyio
async def test_returns_pending_when_job_not_found():
    pipelines = [{"id": 42, "status": "success"}]
    jobs = [
        {"name": "build", "status": "success", "web_url": "https://gitlab.com/job/1"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is False
    assert result.completed is False


@pytest.mark.anyio
async def test_returns_pending_on_api_error():
    with patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, side_effect=Exception("API error")):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is False
    assert result.completed is False


@pytest.mark.anyio
async def test_returns_pending_when_pipeline_running():
    pipelines = [{"id": 42, "status": "running"}]
    with patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is False
    assert result.completed is False


@pytest.mark.anyio
async def test_uses_most_recent_pipeline():
    pipelines = [
        {"id": 2, "status": "success"},
        {"id": 1, "status": "failed"},
    ]
    jobs_pipeline_2 = [
        {"name": "changelog:validate", "status": "failed", "web_url": "https://gitlab.com/job/recent"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs_pipeline_2),
    ):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is True
    assert result.job_web_url == "https://gitlab.com/job/recent"


@pytest.mark.anyio
async def test_returns_completed_when_job_canceled():
    pipelines = [{"id": 42, "status": "canceled"}]
    jobs = [
        {"name": "changelog:validate", "status": "canceled", "web_url": "https://gitlab.com/job/1"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        result = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert result.failed is False
    assert result.completed is True