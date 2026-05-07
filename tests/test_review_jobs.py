import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import review as review_router


class FakeTask:
    def __init__(self):
        self.cancel_called = False

    def done(self):
        return False

    def cancel(self):
        self.cancel_called = True


def test_cancel_review_marks_running_job_canceled_and_cancels_task():
    review_router.REVIEW_JOBS.clear()
    task = FakeTask()
    review_router.REVIEW_JOBS["job-1"] = {
        "status": "running",
        "message": "Анализ батча 2/10",
        "current_batch": 2,
        "total_batches": 10,
        "result": None,
        "error": None,
        "task": task,
    }

    result = review_router.cancel_review("job-1")

    assert result["status"] == "canceled"
    assert result["message"] == "Ревью остановлено пользователем"
    assert task.cancel_called is True


def test_run_review_job_records_cancelled_error(monkeypatch):
    review_router.REVIEW_JOBS.clear()
    review_router.REVIEW_JOBS["job-2"] = {
        "status": "queued",
        "message": "",
        "current_batch": 0,
        "total_batches": 0,
        "result": None,
        "error": None,
    }

    async def fake_review_mr(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(review_router, "review_mr", fake_review_mr)

    asyncio.run(review_router._run_review_job("job-2", 15, ""))

    job = review_router.REVIEW_JOBS["job-2"]
    assert job["status"] == "canceled"
    assert job["message"] == "Ревью остановлено пользователем"
    assert job["error"] is None
