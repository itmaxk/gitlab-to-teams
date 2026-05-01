import asyncio
import sys
from html import escape
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import httpx
from services import xlsx_review_service


def _build_xlsx_bytes(sheets: dict[str, dict[int, dict[str, str]]]) -> bytes:
    content_types = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    workbook_sheets: list[str] = []
    workbook_rels: list[str] = []

    buffer = BytesIO()
    with ZipFile(buffer, "w") as zip_file:
        zip_file.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )

        for index, (sheet_name, rows) in enumerate(sheets.items(), start=1):
            content_types.append(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
            workbook_sheets.append(
                f'<sheet name="{escape(sheet_name)}" sheetId="{index}" r:id="rId{index}"/>'
            )
            workbook_rels.append(
                f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            )

            row_xml: list[str] = []
            for row_number, cells in rows.items():
                cell_xml: list[str] = []
                for cell_ref, value in cells.items():
                    cell_xml.append(
                        f'<c r="{cell_ref}{row_number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                    )
                row_xml.append(f'<row r="{row_number}">{"".join(cell_xml)}</row>')

            zip_file.writestr(
                f"xl/worksheets/sheet{index}.xml",
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f'<sheetData>{"".join(row_xml)}</sheetData>'
                    "</worksheet>"
                ),
            )

        content_types.append("</Types>")
        zip_file.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{"".join(workbook_sheets)}</sheets>'
                "</workbook>"
            ),
        )
        zip_file.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{"".join(workbook_rels)}'
                "</Relationships>"
            ),
        )
        zip_file.writestr("[Content_Types].xml", "".join(content_types))

    return buffer.getvalue()


def test_extract_workbook_rows_reads_sheet_rows():
    workbook = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "1000000000"},
        }
    })

    rows = xlsx_review_service.extract_workbook_rows(workbook)

    assert rows["Rates"][1] == "A=Code | B=Value"
    assert rows["Rates"][2] == "A=IDGV03VTB | B=1000000000"


def test_build_xlsx_diff_findings_detects_cell_change():
    findings = xlsx_review_service.build_xlsx_diff_findings(
        {"Rates": {2: {1: "IDGV03VTB", 2: "100000000"}}},
        {"Rates": {2: {1: "IDGV03VTB", 2: "1000000000"}}},
        "config/rates.xlsx",
    )

    assert findings == [
        {
            "severity": "info",
            "category": "xlsx",
            "file_path": "config/rates.xlsx",
            "line": 2,
            "message": "Лист 'Rates', ячейка B2 изменена.",
            "suggestion": "Было: 100000000; Стало: 1000000000",
            "comparison_ref": "master",
            "comparison_kind": "target",
        }
    ]


def test_build_xlsx_diff_findings_reports_deleted_column_once():
    findings = xlsx_review_service.build_xlsx_diff_findings(
        {
            "Rates": {
                1: {1: "Code", 2: "Value", 3: "Coeff"},
                2: {1: "A", 2: "10", 3: "1"},
                3: {1: "B", 2: "20", 3: "1"},
            }
        },
        {
            "Rates": {
                1: {1: "Code", 2: "Value"},
                2: {1: "A", 2: "10"},
                3: {1: "B", 2: "20"},
            }
        },
        "config/rates.xlsx",
    )

    assert len(findings) == 1
    assert findings[0]["message"] == "Лист 'Rates', столбец C ('Coeff') удален."


def test_build_xlsx_diff_findings_groups_many_deleted_rows():
    findings = xlsx_review_service.build_xlsx_diff_findings(
        {
            "_HitPolicy": {
                1: {1: "UNIQUE"},
                2: {1: "FIRST"},
                3: {1: "PRIORITY"},
                4: {1: "ANY"},
            }
        },
        {"_HitPolicy": {}},
        "config/rates.xlsx",
    )

    assert len(findings) == 1
    assert findings[0]["message"] == "Лист '_HitPolicy', удалены строки 1-4."


def test_build_xlsx_diff_findings_groups_many_column_value_changes():
    findings = xlsx_review_service.build_xlsx_diff_findings(
        {
            "Rates": {
                2: {1: "IDGV03VTB", 2: "100000000"},
                3: {1: "IDGV06VTB", 2: "50000000"},
                4: {1: "IDGV09VTB", 2: "30000000"},
                5: {1: "IDGV12VTB", 2: "5000000"},
            }
        },
        {
            "Rates": {
                2: {1: "IDGV03VTB", 2: "1000000000"},
                3: {1: "IDGV06VTB", 2: "1000000000"},
                4: {1: "IDGV09VTB", 2: "1000000000"},
                5: {1: "IDGV12VTB", 2: "1000000000"},
            }
        },
        "config/rates.xlsx",
    )

    assert len(findings) == 1
    assert findings[0]["message"].startswith("Столбец B изменен в 4 строках")


def test_review_xlsx_mr_compares_files_against_base_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xlsx-review.db")
    db.init_db()

    base_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "100000000"},
        }
    })
    head_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "1000000000"},
        }
    })

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Update xlsx",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/xlsx",
            "target_branch": "master",
            "web_url": "https://example.test/mr/12",
            "changes": [
                {
                    "old_path": "config/rates.xlsx",
                    "new_path": "config/rates.xlsx",
                    "diff": "",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_bytes(project_id, file_path, ref):
        if ref == "master":
            return base_bytes
        if ref == "feature/xlsx":
            return head_bytes
        raise AssertionError(ref)

    progress = []

    async def progress_callback(current_file, total_files, file_path):
        progress.append((current_file, total_files, file_path))

    monkeypatch.setattr(xlsx_review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(xlsx_review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(xlsx_review_service, "get_file_bytes", fake_get_file_bytes)

    result = asyncio.run(
        xlsx_review_service.review_xlsx_mr(
            12,
            "master",
            progress_callback=progress_callback,
        )
    )

    assert result["mr"]["comparison_ref"] == "master"
    assert result["summary"]["files_total"] == 1
    assert result["summary"]["total"] == 1
    assert result["summary"].get("master_comparison") is None
    assert result["findings"][0]["category"] == "xlsx"
    assert result["findings"][0]["line"] == 2
    assert progress[0] == (0, 1, "")
    assert progress[-1] == (1, 1, "config/rates.xlsx")


def test_review_xlsx_mr_uses_target_branch_automatically(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xlsx-review-auto.db")
    db.init_db()

    target_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "100000000"},
        }
    })
    head_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "1000000000"},
        }
    })
    master_bytes = head_bytes

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Update xlsx",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/xlsx",
            "target_branch": "release/1.0",
            "web_url": "https://example.test/mr/15",
            "changes": [
                {
                    "old_path": "config/rates.xlsx",
                    "new_path": "config/rates.xlsx",
                    "diff": "",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_bytes(project_id, file_path, ref):
        if ref == "release/1.0":
            return target_bytes
        if ref == "feature/xlsx":
            return head_bytes
        if ref == "master":
            return master_bytes
        raise AssertionError(ref)

    monkeypatch.setattr(xlsx_review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(xlsx_review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(xlsx_review_service, "get_file_bytes", fake_get_file_bytes)

    result = asyncio.run(xlsx_review_service.review_xlsx_mr(15, ""))

    assert result["mr"]["comparison_ref"] == "release/1.0"
    assert result["summary"]["master_comparison"]["enabled"] is True
    assert result["summary"]["master_comparison"]["ref"] == "master"
    assert result["summary"]["master_comparison"]["differs"] is False


def test_review_xlsx_mr_reports_master_differences_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xlsx-review-master.db")
    db.init_db()

    target_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "100000000"},
        }
    })
    head_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "100000000"},
        }
    })
    master_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "50000000"},
        }
    })

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Update xlsx",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/xlsx",
            "target_branch": "release/1.0",
            "web_url": "https://example.test/mr/16",
            "changes": [
                {
                    "old_path": "config/rates.xlsx",
                    "new_path": "config/rates.xlsx",
                    "diff": "",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_bytes(project_id, file_path, ref):
        if ref == "release/1.0":
            return target_bytes
        if ref == "feature/xlsx":
            return head_bytes
        if ref == "master":
            return master_bytes
        raise AssertionError(ref)

    monkeypatch.setattr(xlsx_review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(xlsx_review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(xlsx_review_service, "get_file_bytes", fake_get_file_bytes)

    result = asyncio.run(xlsx_review_service.review_xlsx_mr(16, ""))

    assert result["summary"]["master_comparison"]["differs"] is True
    assert result["summary"]["master_comparison"]["finding_count"] == 1
    assert result["findings"][0]["comparison_ref"] == "master"
    assert result["findings"][0]["comparison_kind"] == "master"


def test_review_xlsx_mr_uses_alternate_path_when_preferred_path_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xlsx-review-paths.db")
    db.init_db()

    same_bytes = _build_xlsx_bytes({
        "Rates": {
            1: {"A": "Code", "B": "Value"},
            2: {"A": "IDGV03VTB", "B": "100000000"},
        }
    })

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Rename-ish xlsx",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/xlsx",
            "target_branch": "master",
            "web_url": "https://example.test/mr/17",
            "changes": [
                {
                    "old_path": "config/old-name.xlsx",
                    "new_path": "config/new-name.xlsx",
                    "diff": "",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": True,
                }
            ],
        }

    async def fake_get_file_bytes(project_id, file_path, ref):
        if ref == "master" and file_path == "config/old-name.xlsx":
            return same_bytes
        if ref == "feature/xlsx" and file_path == "config/old-name.xlsx":
            return same_bytes
        if file_path == "config/new-name.xlsx":
            raise httpx.HTTPStatusError(
                "404",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://example.test")),
            )
        raise AssertionError((project_id, file_path, ref))

    monkeypatch.setattr(xlsx_review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(xlsx_review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(xlsx_review_service, "get_file_bytes", fake_get_file_bytes)

    result = asyncio.run(xlsx_review_service.review_xlsx_mr(17, ""))

    assert result["summary"]["total"] == 0


def test_review_xlsx_mr_reports_missing_branch_file_as_warning_not_deleted_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xlsx-review-missing.db")
    db.init_db()

    master_bytes = _build_xlsx_bytes({
        "DLPcoeffs": {
            1: {"A": "DLP coeffs"},
        },
        "_HitPolicy": {
            1: {"A": "UNIQUE"},
        },
    })

    async def fake_get_project_id():
        return 77

    async def fake_get_mr_diff(project_id, mr_iid):
        return {
            "title": "Broken source fetch",
            "description": "",
            "author": "Dev",
            "source_branch": "feature/xlsx",
            "target_branch": "master",
            "web_url": "https://example.test/mr/18",
            "changes": [
                {
                    "old_path": "configuration/@config-rgsl/investment-life-insurance/lib/DLPcoeffs.xlsx",
                    "new_path": "configuration/@config-rgsl/investment-life-insurance/lib/DLPcoeffs.xlsx",
                    "diff": "",
                    "new_file": False,
                    "deleted_file": False,
                    "renamed_file": False,
                }
            ],
        }

    async def fake_get_file_bytes(project_id, file_path, ref):
        if ref == "master":
            return master_bytes
        raise httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", "https://example.test"),
            response=httpx.Response(404, request=httpx.Request("GET", "https://example.test")),
        )

    monkeypatch.setattr(xlsx_review_service, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(xlsx_review_service, "get_mr_diff", fake_get_mr_diff)
    monkeypatch.setattr(xlsx_review_service, "get_file_bytes", fake_get_file_bytes)

    result = asyncio.run(xlsx_review_service.review_xlsx_mr(18, ""))

    assert result["summary"]["total"] == 1
    assert result["findings"][0]["severity"] == "warning"
    assert "не удалось прочитать" in result["findings"][0]["message"]
