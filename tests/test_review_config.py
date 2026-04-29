import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_config import is_review_llm_configured


def test_review_llm_configured_with_url_only(monkeypatch):
    monkeypatch.setenv("REVIEW_API_URL", "http://127.0.0.1:11434/v1/chat/completions")
    monkeypatch.delenv("REVIEW_API_KEY", raising=False)

    assert is_review_llm_configured() is True


def test_review_llm_not_configured_without_url(monkeypatch):
    monkeypatch.delenv("REVIEW_API_URL", raising=False)
    monkeypatch.setenv("REVIEW_API_KEY", "unused")

    assert is_review_llm_configured() is False
