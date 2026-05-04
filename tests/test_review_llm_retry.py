import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import review_service


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, api_url, headers=None, json=None):
        self.requests.append((api_url, headers, json))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _llm_response(status_code: int, *, headers: dict | None = None, content: str = "[]"):
    request = httpx.Request("POST", "https://openrouter.test/chat/completions")
    if status_code >= 400:
        return httpx.Response(status_code, request=request, headers=headers)
    return httpx.Response(
        status_code,
        request=request,
        headers=headers,
        json={"choices": [{"message": {"content": content}}]},
    )


def test_call_llm_retries_429_using_retry_after(monkeypatch):
    sleeps = []
    fake_client = _FakeAsyncClient(
        [
            _llm_response(429, headers={"Retry-After": "2"}),
            _llm_response(200, content='[{"message":"ok"}]'),
        ]
    )

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setenv("REVIEW_API_URL", "https://openrouter.test/chat/completions")
    monkeypatch.setattr(review_service, "REVIEW_LLM_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(review_service.httpx, "AsyncClient", lambda **kwargs: fake_client)
    monkeypatch.setattr(review_service.asyncio, "sleep", fake_sleep)

    result = asyncio.run(review_service._call_llm("system", "user"))

    assert result == '[{"message":"ok"}]'
    assert sleeps == [2.0]
    assert len(fake_client.requests) == 2


def test_call_llm_raises_rate_limit_error_after_429_attempts(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _llm_response(429),
            _llm_response(429),
        ]
    )

    async def fake_sleep(delay):
        return None

    monkeypatch.setenv("REVIEW_API_URL", "https://openrouter.test/chat/completions")
    monkeypatch.setattr(review_service, "REVIEW_LLM_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(review_service.httpx, "AsyncClient", lambda **kwargs: fake_client)
    monkeypatch.setattr(review_service.asyncio, "sleep", fake_sleep)

    try:
        asyncio.run(review_service._call_llm("system", "user"))
    except review_service.LLMRateLimitError as exc:
        assert "429" in str(exc)
        assert "2 attempts" in str(exc)
    else:
        raise AssertionError("Expected LLMRateLimitError")

    assert len(fake_client.requests) == 2
