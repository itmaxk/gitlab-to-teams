import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import env_reload


def test_reload_dotenv_reads_repo_scoped_file(monkeypatch, tmp_path):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "REVIEW_API_URL=http://127.0.0.1:11434/v1/chat/completions\n"
        "REVIEW_MAX_DIFF_CHARS=12345\n"
        "REVIEW_BATCH_MAX_CHARS=23456\n"
        "REVIEW_LLM_READ_TIMEOUT=75\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(env_reload, "DOTENV_PATH", dotenv_file)
    monkeypatch.delenv("REVIEW_API_URL", raising=False)
    monkeypatch.delenv("REVIEW_MAX_DIFF_CHARS", raising=False)
    monkeypatch.delenv("REVIEW_BATCH_MAX_CHARS", raising=False)
    monkeypatch.delenv("REVIEW_LLM_READ_TIMEOUT", raising=False)

    env_reload.reload_dotenv()

    assert os.getenv("REVIEW_API_URL") == "http://127.0.0.1:11434/v1/chat/completions"
    assert os.getenv("REVIEW_MAX_DIFF_CHARS") == "12345"
    assert os.getenv("REVIEW_BATCH_MAX_CHARS") == "23456"
    assert os.getenv("REVIEW_LLM_READ_TIMEOUT") == "75"
