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
        if "## Final consolidation pass" in user_message:
            return '[{"severity":"warning","category":"bug","file_path":"a.py","line":1,"message":"A issue","suggestion":null,"confidence":"high","evidence":"Diff A","source":"final_pass","chain":""},{"severity":"info","category":"logic","file_path":"b.py","line":2,"message":"B note","suggestion":"Check branch","confidence":"medium","evidence":"Diff B","source":"final_pass","chain":""}]'
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

    assert len(llm_calls) >= 3
    assert any("## Final consolidation pass" in call for call in llm_calls)
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


def test_parse_findings_normalizes_structured_fields():
    findings = review_service._parse_findings(
        '[{"severity":"critical","category":"unknown","file_path":"a.js","line":"7",'
        '"message":"Issue","suggestion":"Fix","confidence":"certain","evidence":"Because",'
        '"source":"other","chain":"UI -> dataSource -> SQL"}]'
    )

    assert findings == [
        {
            "severity": "info",
            "category": "general",
            "file_path": "a.js",
            "line": 7,
            "message": "Issue",
            "suggestion": "Fix",
            "confidence": "medium",
            "evidence": "Because",
            "source": "diff",
            "chain": "UI -> dataSource -> SQL",
        }
    ]


def test_detect_review_areas_for_mixed_ui_sql_changes():
    review_areas = review_service._detect_review_areas([
        "configuration/@config-rgsl/contract/view/ContractSearch/UI/FiltersContent.json",
        "configuration/@config-rgsl/contract/dataSource/ContractSearchDataSource/query.postgres.handlebars",
        "configuration/@config-rgsl/contract/dataSource/ContractSearchDataSource/resultMapping.js",
    ])

    assert review_areas["areas"]["ui_component"] is True
    assert review_areas["areas"]["sql_datasource"] is True
    assert review_areas["areas"]["schema_mapping"] is True
    assert "UI/Component" in review_areas["labels"]
    assert "SQL/DataSource" in review_areas["labels"]


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


def test_review_mr_can_force_refresh_gitlab_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_review_settings()

    async def fake_get_project_id():
        return 77

    calls = []

    async def fake_get_mr_diff(project_id, mr_iid, **kwargs):
        calls.append((project_id, mr_iid, kwargs))
        return {
            "title": "Fresh MR",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/fresh",
            "source_ref": "fresh-head-sha",
            "target_branch": "main",
            "web_url": "https://example.test/mr/20",
            "overflow": False,
            "changes": [],
        }

    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)

    result = asyncio.run(review_service.review_mr(20, force_refresh_diff=True))

    assert result["mr"]["title"] == "Fresh MR"
    assert calls == [(77, 20, {"force_refresh": True})]


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

    assert "Write all human-readable text in fields `message`, `suggestion`, and `evidence` in Russian." in message
    assert "Mixed AdInsure UI/Component + SQL/DataSource review focus" in message
    assert "Never return `source=full_file_context`" in message
    assert "Allowed source: `diff`, `full_file_context`, `graph_context`" not in message
    assert "Absence from a diff hunk is not evidence" in message
    assert "Do not report missing constants, states, imports, variables, object properties, exports, or functions based only on their absence from a diff hunk." in message


def test_build_batch_message_requires_full_context_check_before_missing_state_finding():
    message = review_service._build_batch_message(
        mr_data={
            "title": "State constants MR",
            "author": "Dev",
            "source_branch": "feature/states",
            "target_branch": "main",
        },
        files_changed=1,
        batch_index=1,
        batch_total=1,
        diff_text=(
            "--- configuration/@config-rgsl/life-insurance/lib/lifeInsuranceRequestConstants.js\n"
            "+++ configuration/@config-rgsl/life-insurance/lib/lifeInsuranceRequestConstants.js\n"
            "@@ -28,3 +28,4 @@\n"
            "+    CICEmployeeAnalysis: 'CICEmployeeAnalysis',"
        ),
        saved_instructions="",
        custom_prompt="",
        file_context_text=(
            "### configuration/@config-rgsl/life-insurance/lib/lifeInsuranceRequestConstants.js\n"
            "```text\n"
            "const documentStates = {\n"
            "    CICEmployeeAnalysis: 'CICEmployeeAnalysis',\n"
            "    CICAnalysisCorrection: 'CICAnalysisCorrection',\n"
            "};\n"
            "```"
        ),
    )

    assert "Use this section to disprove missing-symbol findings" in message
    assert "if a declaration/export/property/state exists here, do not report it as missing" in message


def test_build_postgresql_review_context_extracts_added_sql():
    context = review_service._build_postgresql_review_context([
        {
            "old_path": "configuration/@config-rgsl/acc-base/dataProvider/database/Test/query.postgres.handlebars",
            "new_path": "configuration/@config-rgsl/acc-base/dataProvider/database/Test/query.postgres.handlebars",
            "diff": "@@ -1 +1,2 @@\n-select * from t\n+select id, payload::jsonb from t where id = @id\n+order by created_at desc",
        },
        {
            "old_path": "app.py",
            "new_path": "app.py",
            "diff": "@@ -1 +1 @@\n+print('skip')",
        },
    ])

    assert "## PostgreSQL 17.5 deep SQL review target" in context
    assert "query.postgres.handlebars" in context
    assert "select id, payload::jsonb from t where id = @id" in context
    assert "print('skip')" not in context


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


def test_review_mr_filters_findings_from_full_file_context_only(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_review_settings()

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid, **kwargs):
        return {
            "title": "Context-only variable MR",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/context-only",
            "source_ref": "latest-head-sha",
            "target_branch": "main",
            "web_url": "https://example.test/mr/22",
            "overflow": False,
            "changes": [
                {
                    "old_path": "mapping.js",
                    "new_path": "mapping.js",
                    "diff": "@@ -1 +1 @@\n-oldValue = input.value\n+newValue = input.value",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_content(project_id, file_path, ref):
        assert ref == "latest-head-sha"
        return "const variableOutsideMr = source.branch.only;\nnewValue = input.value;\n"

    async def fake_call_llm(system_prompt, user_message):
        return """[
            {
                "severity": "warning",
                "category": "logic",
                "file_path": "mapping.js",
                "line": 1,
                "message": "Проблема в измененной строке",
                "suggestion": "Исправить newValue",
                "confidence": "high",
                "evidence": "+newValue = input.value",
                "source": "diff",
                "chain": ""
            },
            {
                "severity": "warning",
                "category": "logic",
                "file_path": "mapping.js",
                "line": 10,
                "message": "Переменная variableOutsideMr не проверена",
                "suggestion": "Исправить variableOutsideMr",
                "confidence": "medium",
                "evidence": "const variableOutsideMr = source.branch.only;",
                "source": "full_file_context",
                "chain": ""
            }
        ]"""

    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(review_service, "get_file_content", fake_get_file_content)
    monkeypatch.setattr(review_service, "_call_llm", fake_call_llm)

    result = asyncio.run(review_service.review_mr(22, force_refresh_diff=True))

    assert [finding["source"] for finding in result["findings"]] == ["diff"]
    assert "newValue" in result["findings"][0]["suggestion"]
    assert "variableOutsideMr" not in result["findings"][0]["message"]


def test_review_mr_includes_project_graph_context_for_adinsure_configs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.seed_review_settings()

    project_root = tmp_path / "impl"
    config_root = project_root / "configuration" / "@config-rgsl"
    data_source_dir = config_root / "acc-base" / "dataSource" / "AllocationDataSource"
    data_provider_dir = config_root / "acc-base" / "dataProvider" / "database" / "AllocationDataProvider"
    data_source_dir.mkdir(parents=True)
    data_provider_dir.mkdir(parents=True)
    (data_source_dir / "configuration.json").write_text(
        '{"dataProvider":{"type":"DatabaseDataProvider","codeName":"AllocationDataProvider","version":"1"}}',
        encoding="utf-8",
    )
    (data_provider_dir / "configuration.json").write_text('{"version":"1"}', encoding="utf-8")
    (data_provider_dir / "query.postgres.handlebars").write_text(
        "select * from acc_impl.allocation where allocation_id = @allocationId",
        encoding="utf-8",
    )

    conn = db.get_db()
    conn.execute(
        """
        UPDATE review_settings
        SET review_project_root = ?,
            review_project_config_path = 'configuration/@config-rgsl',
            review_sql_target = 'PostgreSQL 17.5+',
            review_graph_context_enabled = 1,
            review_graph_context_max_files = 10
        WHERE id = 1
        """,
        (str(project_root),),
    )
    conn.commit()
    conn.close()

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Data source MR",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/ds",
            "source_ref": "head-sha",
            "target_branch": "main",
            "web_url": "https://example.test/mr/12",
            "overflow": False,
            "changes": [
                {
                    "old_path": "configuration/@config-rgsl/acc-base/dataSource/AllocationDataSource/configuration.json",
                    "new_path": "configuration/@config-rgsl/acc-base/dataSource/AllocationDataSource/configuration.json",
                    "diff": "@@ -1 +1 @@\n-{}\n+{}",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_content(project_id, file_path, ref):
        return "// source branch file"

    llm_calls = []

    async def fake_call_llm(system_prompt, user_message):
        llm_calls.append(user_message)
        return "[]"

    monkeypatch.setattr(review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(review_service, "get_file_content", fake_get_file_content)
    monkeypatch.setattr(review_service, "_call_llm", fake_call_llm)

    result = asyncio.run(review_service.review_mr(12))

    assert len(llm_calls) == 1
    assert "## AdInsure constructor graph context" in llm_calls[0]
    assert "## Constructor Graph Checks" in llm_calls[0]
    assert "query.postgres.handlebars" in llm_calls[0]
    assert result["summary"]["project_graph_context"]["sql_target"] == "PostgreSQL 17.5+"
    assert "## PostgreSQL 17.5 deep SQL review target" in llm_calls[0]
    assert result["summary"]["project_graph_context"]["related_files"]
