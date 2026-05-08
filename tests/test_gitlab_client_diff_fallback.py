import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import gitlab_client


class _FakeResponse:
    def __init__(
        self,
        *,
        json_data=None,
        text="",
        content=b"",
        status_code=200,
        headers=None,
    ):
        self._json_data = json_data
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        if not self._responses:
            raise AssertionError(f"Unexpected GET {url}")
        response = self._responses.pop(0)
        response.url = url
        response.params = params
        return response


def test_get_mr_diff_fills_empty_changes_from_raw_diffs(monkeypatch):
    changes_payload = {
        "title": "Large MR",
        "description": "",
        "author": {"name": "Dev"},
        "source_branch": "feature/raw",
        "target_branch": "main",
        "web_url": "https://example.test/mr/12",
        "overflow": False,
        "changes": [
            {
                "old_path": "a.py",
                "new_path": "a.py",
                "diff": "@@ -1 +1 @@\n-old\n+new",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            },
            {
                "old_path": "b.py",
                "new_path": "b.py",
                "diff": "",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            },
        ],
    }
    raw_diff_text = """diff --git a/b.py b/b.py
index 1111111..2222222 100644
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-before
+after
"""

    responses = [
        _FakeResponse(json_data=changes_payload),
        _FakeResponse(text=raw_diff_text),
    ]

    monkeypatch.setattr(
        gitlab_client.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses),
    )

    result = asyncio.run(gitlab_client.get_mr_diff(1, 12))

    assert len(result["changes"]) == 2
    assert result["changes"][0]["diff"]
    assert result["changes"][1]["diff"] == "@@ -1 +1 @@\n-before\n+after"


def test_get_merge_requests_follows_pagination(monkeypatch):
    responses = [
        _FakeResponse(
            json_data=[{"iid": 1}],
            headers={"x-next-page": "2"},
        ),
        _FakeResponse(
            json_data=[{"iid": 2}],
            headers={"x-next-page": ""},
        ),
    ]

    monkeypatch.setattr(gitlab_client, "_base_url", lambda: "https://gitlab.example.test")
    monkeypatch.setattr(gitlab_client, "_headers", lambda: {})
    monkeypatch.setattr(
        gitlab_client.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses),
    )

    result = asyncio.run(gitlab_client.get_merge_requests(1, state="opened"))

    assert [mr["iid"] for mr in result] == [1, 2]


def test_parse_raw_diffs_extracts_body_by_file_pair():
    raw_text = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-one
+two
diff --git a/b.bin b/b.bin
Binary files a/b.bin and b/b.bin differ
"""

    parsed = gitlab_client._parse_raw_diffs(raw_text)

    assert parsed[("a.py", "a.py")] == "@@ -1 +1 @@\n-one\n+two"
    assert parsed[("b.bin", "b.bin")] == "Binary files a/b.bin and b/b.bin differ"


def test_get_mr_diff_builds_synthetic_diff_when_gitlab_returns_nothing(monkeypatch):
    changes_payload = {
        "title": "Missing diff MR",
        "description": "",
        "author": {"name": "Dev"},
        "source_branch": "feature/fallback",
        "target_branch": "main",
        "web_url": "https://example.test/mr/13",
        "overflow": True,
        "changes": [
            {
                "old_path": "c.py",
                "new_path": "c.py",
                "diff": "",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            },
        ],
    }

    responses = [
        _FakeResponse(json_data=changes_payload),
        _FakeResponse(text=""),
        _FakeResponse(content=b"before = 1\n"),
        _FakeResponse(content=b"before = 2\n"),
    ]

    monkeypatch.setattr(
        gitlab_client.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses),
    )

    result = asyncio.run(gitlab_client.get_mr_diff(1, 13))

    assert len(result["changes"]) == 1
    assert "@@ -1 +1 @@" in result["changes"][0]["diff"]
    assert "-before = 1" in result["changes"][0]["diff"]
    assert "+before = 2" in result["changes"][0]["diff"]


def test_get_mr_diff_synthetic_fallback_uses_diff_refs_instead_of_branch_names(monkeypatch):
    changes_payload = {
        "title": "Merged MR with deleted source branch",
        "source_branch": "feature/deleted-after-merge",
        "target_branch": "master",
        "sha": "sha-from-mr",
        "diff_refs": {
            "base_sha": "base-sha",
            "head_sha": "head-sha",
        },
        "changes": [
            {
                "old_path": "config.json",
                "new_path": "config.json",
                "diff": "",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            },
        ],
    }

    responses = [
        _FakeResponse(json_data=changes_payload),
        _FakeResponse(text=""),
        _FakeResponse(content=b"old\n"),
        _FakeResponse(content=b"new\n"),
    ]

    monkeypatch.setattr(
        gitlab_client.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses),
    )

    result = asyncio.run(gitlab_client.get_mr_diff(1, 14))

    assert result["source_ref"] == "head-sha"
    assert result["changes"][0]["diff"]
    assert responses[2].params == {"ref": "base-sha"}
    assert responses[3].params == {"ref": "head-sha"}


def test_get_mr_diff_synthetic_fallback_skips_missing_side_for_added_and_deleted_files(monkeypatch):
    changes_payload = {
        "title": "Added and deleted files",
        "source_branch": "feature/change-files",
        "target_branch": "master",
        "diff_refs": {
            "base_sha": "base-sha",
            "head_sha": "head-sha",
        },
        "changes": [
            {
                "old_path": "new.json",
                "new_path": "new.json",
                "diff": "",
                "new_file": True,
                "deleted_file": False,
                "renamed_file": False,
            },
            {
                "old_path": "old.json",
                "new_path": "old.json",
                "diff": "",
                "new_file": False,
                "deleted_file": True,
                "renamed_file": False,
            },
        ],
    }

    responses = [
        _FakeResponse(json_data=changes_payload),
        _FakeResponse(text=""),
        _FakeResponse(content=b"new file\n"),
        _FakeResponse(content=b"old file\n"),
    ]

    monkeypatch.setattr(
        gitlab_client.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses),
    )

    result = asyncio.run(gitlab_client.get_mr_diff(1, 15))

    assert len(result["changes"]) == 2
    assert responses[2].params == {"ref": "head-sha"}
    assert responses[3].params == {"ref": "base-sha"}
