from unittest.mock import AsyncMock, patch

import pytest

from services.pipeline_check import check_pipeline_job_failed


@pytest.mark.anyio
async def test_returns_failed_when_job_failed():
    pipelines = [{"id": 42}]
    jobs = [
        {"name": "build", "status": "success", "web_url": "https://gitlab.com/job/1"},
        {"name": "changelog:validate", "status": "failed", "web_url": "https://gitlab.com/job/2"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        failed, url = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert failed is True
    assert url == "https://gitlab.com/job/2"


@pytest.mark.anyio
async def test_returns_not_failed_when_job_succeeded():
    pipelines = [{"id": 42}]
    jobs = [
        {"name": "changelog:validate", "status": "success", "web_url": "https://gitlab.com/job/1"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        failed, url = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert failed is False
    assert url is None


@pytest.mark.anyio
async def test_returns_not_failed_when_no_pipelines():
    with patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=[]):
        failed, url = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert failed is False
    assert url is None


@pytest.mark.anyio
async def test_returns_not_failed_when_job_not_found():
    pipelines = [{"id": 42}]
    jobs = [
        {"name": "build", "status": "success", "web_url": "https://gitlab.com/job/1"},
    ]
    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
    ):
        failed, url = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert failed is False
    assert url is None


@pytest.mark.anyio
async def test_returns_not_failed_on_api_error():
    with patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, side_effect=Exception("API error")):
        failed, url = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert failed is False
    assert url is None


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
        failed, url = await check_pipeline_job_failed(1, 10, "changelog:validate")
    assert failed is True
    assert url == "https://gitlab.com/job/recent"