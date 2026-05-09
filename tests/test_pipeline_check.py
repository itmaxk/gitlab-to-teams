from unittest.mock import AsyncMock, patch
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from services.pipeline_check import (
    PipelineCheckResult,
    check_pipeline_job_failed,
    parse_retry_job_names,
    retry_failed_config_jobs,
    should_retry_config_job_trace,
)


def test_parse_retry_job_names_defaults_to_config_jobs():
    assert parse_retry_job_names("") == ["config:check-uncommitted", "config:validate"]


def test_parse_retry_job_names_accepts_common_separators():
    assert parse_retry_job_names("config:validate; build\nconfig:validate, test") == [
        "config:validate",
        "build",
        "test",
    ]


def test_should_retry_trace_when_marker_is_last_message():
    trace = "install\n[5/5] Building fresh packages...\n"
    assert should_retry_config_job_trace(trace) is True


def test_should_retry_trace_when_error_follows_marker():
    trace = "[5/5] Building fresh packages...\nERROR: Job failed: exit code 1\n"
    assert should_retry_config_job_trace(trace) is True


def test_should_retry_trace_when_gitlab_boilerplate_follows_marker():
    trace = (
        "[5/5] Building fresh packages...\n"
        "section_end:1710000000:step_script\r\x1b[0K\n"
        "section_start:1710000001:upload_artifacts_on_failure\r\x1b[0K\n"
        "Uploading artifacts for failed job\n"
        "WARNING: target/logs: no matching files\n"
        "ERROR: No files to upload\n"
        "section_end:1710000002:upload_artifacts_on_failure\r\x1b[0K\n"
        "section_start:1710000003:cleanup_file_variables\r\x1b[0K\n"
        "Cleaning up project directory and file based variables\n"
        "section_end:1710000004:cleanup_file_variables\r\x1b[0K\n"
        "ERROR: Job failed: exit code 1\n"
    )
    assert should_retry_config_job_trace(trace) is True


def test_should_retry_trace_when_tls_socket_disconnect_error_present():
    trace = (
        "yarn install\n"
        "error Error: Client network socket disconnected before secure TLS "
        "connection was established\n"
        "ERROR: Job failed: exit code 1\n"
    )
    assert should_retry_config_job_trace(trace) is True


def test_should_not_retry_trace_when_other_output_follows_marker():
    trace = "[5/5] Building fresh packages...\nDone in 3.2s\nERROR: Job failed"
    assert should_retry_config_job_trace(trace) is False


def test_should_not_retry_trace_without_marker():
    assert should_retry_config_job_trace("ERROR: Job failed") is False


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


@pytest.mark.anyio
async def test_retries_failed_config_job_when_trace_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_retry_fresh_packages",),
    ).fetchone()["id"]
    conn.close()

    pipelines = [{"id": 42, "status": "failed"}]
    jobs = [
        {"id": 101, "name": "config:validate", "status": "failed", "web_url": "https://gitlab.com/job/101"},
        {"id": 102, "name": "build", "status": "failed", "web_url": "https://gitlab.com/job/102"},
    ]
    retry_mock = AsyncMock(return_value={"id": 201})

    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
        patch("services.pipeline_check.get_job_trace", new_callable=AsyncMock, return_value="[5/5] Building fresh packages...\n"),
        patch("services.pipeline_check.retry_job", retry_mock),
    ):
        result = await retry_failed_config_jobs(1, 10, ["config:validate"], rule_id)

    assert result.checked == 1
    assert result.retried == [
        {
            "job_id": 101,
            "retried_job_id": 201,
            "job_name": "config:validate",
            "job_web_url": "https://gitlab.com/job/101",
        }
    ]
    retry_mock.assert_awaited_once_with(1, 101)

    conn = db.get_db()
    row = conn.execute(
        "SELECT retried_job_id FROM pipeline_job_retry_log WHERE rule_id = ? AND job_id = ?",
        (rule_id, 101),
    ).fetchone()
    conn.close()
    assert row["retried_job_id"] == 201


@pytest.mark.anyio
async def test_does_not_retry_same_failed_job_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_retry_fresh_packages",),
    ).fetchone()["id"]
    conn.close()

    pipelines = [{"id": 42, "status": "failed"}]
    jobs = [
        {"id": 101, "name": "config:validate", "status": "failed", "web_url": "https://gitlab.com/job/101"},
    ]
    retry_mock = AsyncMock(return_value={"id": 201})

    with (
        patch("services.pipeline_check.get_mr_pipelines", new_callable=AsyncMock, return_value=pipelines),
        patch("services.pipeline_check.get_pipeline_jobs", new_callable=AsyncMock, return_value=jobs),
        patch("services.pipeline_check.get_job_trace", new_callable=AsyncMock, return_value="[5/5] Building fresh packages...\n"),
        patch("services.pipeline_check.retry_job", retry_mock),
    ):
        first = await retry_failed_config_jobs(1, 10, ["config:validate"], rule_id)
        second = await retry_failed_config_jobs(1, 10, ["config:validate"], rule_id)

    assert len(first.retried) == 1
    assert second.retried == []
    assert second.skipped == [
        {"job_id": 101, "job_name": "config:validate", "reason": "already_retried"}
    ]
    retry_mock.assert_awaited_once_with(1, 101)


def test_seeded_config_retry_rule_uses_ten_minute_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_default_rule()

    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM notification_rules WHERE seed_key = ?",
        ("pipeline_config_retry_fresh_packages",),
    ).fetchone()
    conn.close()

    assert row["action_type"] == "pipeline_job_retry"
    assert row["mr_state"] == "opened"
    assert row["target_branch"] == "*"
    assert row["poll_interval_seconds"] == 600
    assert row["content_match"] == "config:check-uncommitted,config:validate"
