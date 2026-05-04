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
