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
            "source_ref": "head-sha",
            "target_branch": "main",
            "web_url": "https://example.test/mr/9",
            "overflow": False,
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

    async def fake_get_file_content(project_id, file_path, ref):
        assert ref == "head-sha"
        return f"// full content for {file_path}"

    monkeypatch.setattr(review_service, "MAX_DIFF_CHARS", 90)
    monkeypatch.setattr(review_service, "REVIEW_BATCH_MAX_CHARS", 90)
    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(review_service, "get_file_content", fake_get_file_content)
    monkeypatch.setattr(review_service, "_call_llm", fake_call_llm)

    async def progress_callback(current_batch, total_batches):
        progress_updates.append((current_batch, total_batches))

    result = asyncio.run(review_service.review_mr(9, "focus on bugs", progress_callback=progress_callback))

    assert len(llm_calls) >= 2
    assert result["summary"]["files_total"] == 3
    assert result["summary"]["files_analyzed"] == 3
    assert result["summary"]["files_skipped"] == 0
    assert result["summary"]["truncated"] is False
    assert result["summary"]["total"] == 2
    assert {finding["file_path"] for finding in result["findings"]} == {"a.py", "b.py"}
    assert progress_updates[0][0] == 0
    assert progress_updates[-1][0] == progress_updates[-1][1]


def test_parse_findings_handles_none_response():
    assert review_service._parse_findings(None) == []


def test_review_mr_marks_files_without_diff_as_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_review_settings()

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Sparse diff MR",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/sparse",
            "source_ref": "head-sha",
            "target_branch": "main",
            "web_url": "https://example.test/mr/10",
            "overflow": False,
            "changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": "A" * 40, "new_file": False, "deleted_file": False, "renamed_file": False},
                {"old_path": "b.bin", "new_path": "b.bin", "diff": "", "new_file": False, "deleted_file": False, "renamed_file": False},
            ],
        }

    async def fake_call_llm(system_prompt, user_message):
        return "[]"

    async def fake_get_file_content(project_id, file_path, ref):
        return f"// full content for {file_path}"

    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(review_service, "get_file_content", fake_get_file_content)
    monkeypatch.setattr(review_service, "_call_llm", fake_call_llm)

    result = asyncio.run(review_service.review_mr(10))

    assert result["mr"]["files_changed"] == 2
    assert result["summary"]["files_total"] == 2
    assert result["summary"]["files_analyzed"] == 1
    assert result["summary"]["files_skipped"] == 1
    assert result["summary"]["truncated"] is True


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
        saved_instructions="",
        custom_prompt="",
    )

    assert "Write all human-readable text in fields `message` and `suggestion` in Russian." in message


def test_build_batch_message_includes_saved_review_instruction_lists():
    message = review_service._build_batch_message(
        mr_data={
            "title": "Test MR",
            "author": "Dev",
            "source_branch": "feature/x",
            "target_branch": "main",
        },
        files_changed=2,
        batch_index=1,
        batch_total=1,
        diff_text="--- a.py\n+++ a.py\n+print('ok')",
        saved_instructions="Учитывать в ревью:\n- Проверяй бизнес-логику\n\nНе учитывать в ревью:\n- Не комментируй форматирование",
        custom_prompt="",
    )

    assert "## Saved review instructions" in message
    assert "Проверяй бизнес-логику" in message
    assert "Не комментируй форматирование" in message


def test_build_file_context_text_includes_only_files_from_batch():
    diff_text = "--- a.js\n+++ a.js\n+changed\n\n--- b.js\n+++ b.js\n+changed"
    file_contexts = {
        "a.js": "const a = 1;",
        "c.js": "const c = 1;",
    }

    context_text = review_service._build_file_context_text(file_contexts, diff_text)

    assert "### a.js" in context_text
    assert "const a = 1;" in context_text
    assert "c.js" not in context_text


def test_review_mr_includes_full_file_context_for_imports_outside_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_review_settings()

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Signature MR",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/signature",
            "source_ref": "head-sha",
            "target_branch": "main",
            "web_url": "https://example.test/mr/11",
            "overflow": False,
            "changes": [
                {
                    "old_path": "mapping.js",
                    "new_path": "mapping.js",
                    "diff": (
                        "@@ -7,1 +7,1 @@\n"
                        "-const isElectronicSignature = requestBody?.signatureForm == signatureForm.electronicSignature;\n"
                        "+const isElectronicSignature = [requestBody?.signatureForm, sinkResult.body?.request?.signatureForm].includes(signatureForm.electronicSignature);"
                    ),
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_content(project_id, file_path, ref):
        assert (project_id, file_path, ref) == (77, "mapping.js", "head-sha")
        return (
            "'use strict';\n\n"
            "const { signatureForm } = require('@config-rgsl/life-insurance/lib/lifeInsuranceRequestConstants');\n\n"
            "module.exports = function mapping(sinkResult, sinkExchange) {\n"
            "    const isElectronicSignature = [requestBody?.signatureForm, sinkResult.body?.request?.signatureForm].includes(signatureForm.electronicSignature);\n"
            "};\n"
        )

    llm_calls = []

    async def fake_call_llm(system_prompt, user_message):
        llm_calls.append(user_message)
        return "[]"

    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(review_service, "get_file_content", fake_get_file_content)
    monkeypatch.setattr(review_service, "_call_llm", fake_call_llm)

    asyncio.run(review_service.review_mr(11))

    assert len(llm_calls) == 1
    assert "## Full file context after change" in llm_calls[0]
    assert "const { signatureForm } = require(" in llm_calls[0]
