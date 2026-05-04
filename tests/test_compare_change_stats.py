import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import compare


def test_changed_line_count_ignores_diff_file_headers():
    diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
-old line
+new line
+added line
 unchanged
"""

    assert compare._changed_line_count(diff) == 3


def test_change_stats_from_diff_counts_files_and_lines():
    stats = compare._change_stats_from_diff({
        "changes": [
            {
                "old_path": "app.py",
                "new_path": "app.py",
                "diff": "@@ -1 +1 @@\n-old\n+new\n",
            },
            {
                "old_path": "old.txt",
                "new_path": "docs/new.txt",
                "diff": "@@ -0,0 +1,2 @@\n+one\n+two\n",
            },
        ]
    })

    assert stats["file_count"] == 2
    assert stats["total_changed_lines"] == 4
    assert stats["files"] == [
        {"path": "app.py", "changed_lines": 2},
        {"path": "docs/new.txt", "changed_lines": 2},
    ]


def test_attach_change_stats_reuses_single_diff_load_per_mr(monkeypatch):
    calls = []

    async def fake_get_mr_diff(project_id, mr_iid):
        calls.append((project_id, mr_iid))
        return {
            "changes": [
                {
                    "old_path": "a.py",
                    "new_path": "a.py",
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ]
        }

    monkeypatch.setattr(compare, "get_mr_diff", fake_get_mr_diff)

    mr_info = compare._mr_to_info({
        "iid": 7,
        "title": "PROJ-1 Title",
        "web_url": "https://gitlab.example/mr/7",
        "source_branch": "feature/proj-1",
        "merged_at": "2026-05-01T10:00:00Z",
        "author": {"name": "User"},
    })
    jira_map = {"PROJ-1": {"master": [mr_info], "release/1": [mr_info]}}

    asyncio.run(compare._attach_change_stats(jira_map, [], 42))

    assert calls == [(42, 7)]
    assert mr_info["change_stats"]["file_count"] == 1
    assert mr_info["change_stats"]["total_changed_lines"] == 2


def test_run_compare_can_skip_change_stats(monkeypatch):
    async def fake_get_project_id():
        return 42

    async def fake_get_mr_by_iid(project_id, mr_id):
        return {
            "iid": mr_id,
            "title": "PROJ-1 Title",
            "web_url": f"https://gitlab.example/mr/{mr_id}",
            "state": "merged",
            "source_branch": "feature/proj-1",
            "target_branch": "master",
            "merged_at": "2026-05-01T10:00:00Z",
            "author": {"name": "User"},
        }

    async def fail_attach(*args, **kwargs):
        raise AssertionError("change stats should not be loaded")

    monkeypatch.setattr(compare, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(compare, "get_mr_by_iid", fake_get_mr_by_iid)
    monkeypatch.setattr(compare, "_attach_change_stats", fail_attach)

    result = asyncio.run(
        compare.run_compare(
            compare.CompareRequest(
                branches=["master"],
                mr_ids=[7],
                include_change_stats=False,
            )
        )
    )

    mr = result["rows"][0]["branches"]["master"]["mrs"][0]
    assert result["change_stats_loaded"] is False
    assert mr["change_stats"]["loaded"] is False


def test_default_branches_returns_two_latest_release_branches(monkeypatch):
    async def fake_get_project_id():
        return 42

    async def fake_get_branches(project_id, search="", per_page=100, page=1):
        assert project_id == 42
        assert search == "release/"
        if page == 1:
            return [
                {"name": "release/101"},
                {"name": "release/103"},
                {"name": "release/not-number"},
            ] + [{"name": f"feature/{i}"} for i in range(97)]
        if page == 2:
            return [
                {"name": "release/102"},
            ]
        return []

    monkeypatch.setattr(compare, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(compare, "get_branches", fake_get_branches)

    result = asyncio.run(compare.default_branches())

    assert result == {"branches": ["master", "release/103", "release/102"]}


def test_annotate_cherry_pick_links_marks_source_and_release_mrs():
    source_mr = compare._mr_to_info({
        "iid": 10,
        "title": "PROJ-1 Source",
        "web_url": "https://gitlab.example/mr/10",
        "source_branch": "feature/proj-1",
        "target_branch": "master",
        "merge_commit_sha": "abcdef1234567890",
        "merged_at": "2026-05-01T10:00:00Z",
        "author": {"name": "User"},
    })
    release_mr = compare._mr_to_info({
        "iid": 11,
        "title": "PROJ-1 Cherry-pick",
        "web_url": "https://gitlab.example/mr/11",
        "source_branch": "cherry-pick-abcdef12",
        "target_branch": "release/103",
        "merge_commit_sha": "fedcba9876543210",
        "merged_at": "2026-05-02T10:00:00Z",
        "author": {"name": "User"},
    })
    branch_data = {
        "master": [source_mr],
        "release/103": [release_mr],
    }

    compare._annotate_cherry_pick_links(branch_data)

    assert branch_data["master"][0]["cherry_picked_to"] == [{
        "mr_iid": 11,
        "mr_url": "https://gitlab.example/mr/11",
        "target_branch": "release/103",
    }]
    assert branch_data["release/103"][0]["cherry_pick_of"] == {
        "mr_iid": 10,
        "mr_url": "https://gitlab.example/mr/10",
        "target_branch": "master",
    }
