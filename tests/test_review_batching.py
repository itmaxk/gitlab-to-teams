import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from services import review_service


def test_build_diff_batches_splits_large_review_without_losing_files():
    changes = [
        {"old_path": "a.py", "new_path": "a.py", "diff": "A" * 40},
        {"old_path": "b.py", "new_path": "b.py", "diff": "B" * 40},
        {"old_path": "c.py", "new_path": "c.py", "diff": "C" * 120},
    ]

    batches = review_service._build_diff_batches(changes, max_chars=90)

    assert len(batches) >= 3
    assert any("--- a.py" in batch for batch in batches)
    assert any("--- b.py" in batch for batch in batches)
    assert any("--- c.py" in batch for batch in batches)
    assert all(len(batch) <= 90 for batch in batches)


def test_review_mr_processes_multiple_batches_and_marks_full_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_review_settings()

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Large MR",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/batch",
            "target_branch": "main",
            "web_url": "https://example.test/mr/9",
            "changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": "A" * 40, "new_file": False, "deleted_file": False, "renamed_file": False},
                {"old_path": "b.py", "new_path": "b.py", "diff": "B" * 40, "new_file": False, "deleted_file": False, "renamed_file": False},
                {"old_path": "c.py", "new_path": "c.py", "diff": "C" * 40, "new_file": False, "deleted_file": False, "renamed_file": False},
            ],
        }

    llm_calls = []
    progress_updates = []

    async def fake_call_llm(system_prompt, user_message):
        llm_calls.append(user_message)
        if "a.py" in user_message:
            return '[{"severity":"warning","category":"bug","file_path":"a.py","line":1,"message":"A issue","suggestion":null}]'
        if "b.py" in user_message:
            return '[{"severity":"info","category":"logic","file_path":"b.py","line":2,"message":"B note","suggestion":"Check branch"}]'
        return "[]"

    monkeypatch.setattr(review_service, "MAX_DIFF_CHARS", 90)
    monkeypatch.setattr(review_service, "REVIEW_BATCH_MAX_CHARS", 90)
    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(review_service, "_call_llm", fake_call_llm)

    async def progress_callback(current_batch, total_batches):
        progress_updates.append((current_batch, total_batches))

    result = asyncio.run(review_service.review_mr(9, "focus on bugs", progress_callback=progress_callback))

    assert len(llm_calls) >= 2
    assert result["summary"]["files_total"] == 3
    assert result["summary"]["files_analyzed"] == 3
    assert result["summary"]["truncated"] is False
    assert result["summary"]["total"] == 2
    assert {finding["file_path"] for finding in result["findings"]} == {"a.py", "b.py"}
    assert progress_updates[0][0] == 0
    assert progress_updates[-1][0] == progress_updates[-1][1]


def test_parse_findings_handles_none_response():
    assert review_service._parse_findings(None) == []


def test_resolve_batch_max_chars_defaults_to_safer_limit(monkeypatch):
    monkeypatch.delenv("REVIEW_MAX_DIFF_CHARS", raising=False)
    monkeypatch.delenv("REVIEW_BATCH_MAX_CHARS", raising=False)

    assert review_service._resolve_batch_max_chars() == 20000


def test_resolve_batch_max_chars_caps_explicit_value_to_max_diff(monkeypatch):
    monkeypatch.setenv("REVIEW_MAX_DIFF_CHARS", "15000")
    monkeypatch.setenv("REVIEW_BATCH_MAX_CHARS", "40000")

    assert review_service._resolve_batch_max_chars() == 15000


def test_build_batch_message_requires_russian_human_text():
    message = review_service._build_batch_message(
        mr_data={
            "title": "Test MR",
            "author": "Dev",
            "source_branch": "feature/x",
            "target_branch": "main",
        },
        files_changed=2,
        batch_index=1,
        batch_total=3,
        diff_text="--- a.py\n+++ a.py\n+print('ok')",
        custom_prompt="",
    )

    assert "Write all human-readable text in fields `message` and `suggestion` in Russian." in message
