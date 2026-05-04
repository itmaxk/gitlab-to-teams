import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import queue


def test_compare_branches_returns_missing_release_candidates(monkeypatch):
    async def fake_get_project_id():
        return 42

    async def fake_search_merge_requests(project_id, search, state="merged", per_page=20):
        assert project_id == 42
        assert state == "merged"
        assert per_page == 50
        return [
            {
                "iid": 10,
                "title": "PROJ-123 Add queue compare",
                "web_url": "https://gitlab.example/mr/10",
                "state": "merged",
                "merged_at": "2026-05-01T10:00:00Z",
                "source_branch": "feature/proj-123",
                "target_branch": "master",
            },
            {
                "iid": 11,
                "title": "PROJ-123 Add queue compare release/102",
                "web_url": "https://gitlab.example/mr/11",
                "state": "merged",
                "merged_at": "2026-05-02T10:00:00Z",
                "source_branch": "cherry-pick-abcdef12",
                "target_branch": "release/102",
            },
            {
                "iid": 12,
                "title": "PROJ-1234 Similar but different Jira",
                "web_url": "https://gitlab.example/mr/12",
                "state": "merged",
                "merged_at": "2026-05-03T10:00:00Z",
                "source_branch": "feature/proj-1234",
                "target_branch": "release/101",
            },
        ]

    monkeypatch.setattr(queue, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(queue, "search_merge_requests", fake_search_merge_requests)

    result = asyncio.run(
        queue.compare_branches(
            queue.CompareBranchesRequest(
                jira_ids=["PROJ-123"],
                source_branch="master",
                target_branches=["release/102", "release/101"],
            )
        )
    )

    assert result["candidate_mr_ids"] == [10]
    assert result["missing_by_branch"] == {
        "release/102": [],
        "release/101": [10],
    }
    row = result["rows"][0]
    assert row["source_mrs"][0]["mr_id"] == 10
    assert row["targets"]["release/102"]["status"] == "merged"
    assert row["targets"]["release/101"]["status"] == "missing"
    assert row["targets"]["release/101"]["source_mr_ids"] == [10]


def test_compare_branches_requires_target_branch(monkeypatch):
    result = asyncio.run(
        queue.compare_branches(
            queue.CompareBranchesRequest(
                jira_ids=["PROJ-123"],
                source_branch="master",
                target_branches=[],
            )
        )
    )

    assert result == {"error": "Укажите хотя бы одну ветку для сравнения"}
