import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import poller


def test_get_mr_file_content_prefers_source_branch(monkeypatch):
    calls = []

    async def fake_get_file_content(project_id, file_path, ref):
        calls.append((project_id, file_path, ref))
        return "source content"

    monkeypatch.setattr(poller, "get_file_content", fake_get_file_content)

    result = asyncio.run(
        poller._get_mr_file_content(
            26,
            101,
            "configuration/app/errorMapping.js",
            "feature/send-request-info",
            "master",
        )
    )

    assert result == "source content"
    assert calls == [
        (26, "configuration/app/errorMapping.js", "feature/send-request-info")
    ]


def test_get_mr_file_content_falls_back_to_target_branch(monkeypatch):
    calls = []

    async def fake_get_file_content(project_id, file_path, ref):
        calls.append((project_id, file_path, ref))
        if ref == "feature/send-request-info":
            raise RuntimeError("404")
        return "target content"

    monkeypatch.setattr(poller, "get_file_content", fake_get_file_content)

    result = asyncio.run(
        poller._get_mr_file_content(
            26,
            101,
            "configuration/app/errorMapping.js",
            "feature/send-request-info",
            "master",
        )
    )

    assert result == "target content"
    assert calls == [
        (26, "configuration/app/errorMapping.js", "feature/send-request-info"),
        (26, "configuration/app/errorMapping.js", "master"),
    ]
