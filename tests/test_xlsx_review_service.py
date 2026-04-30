import asyncio
import sys
from html import escape
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
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


def test_build_xlsx_diff_findings_detects_row_change():
    findings = xlsx_review_service.build_xlsx_diff_findings(
        {"Rates": {2: "A=IDGV03VTB | B=100000000"}},
        {"Rates": {2: "A=IDGV03VTB | B=1000000000"}},
        "config/rates.xlsx",
    )

    assert findings == [
        {
            "severity": "info",
            "category": "xlsx",
            "file_path": "config/rates.xlsx",
            "line": 2,
            "message": "Лист 'Rates', строка 2 изменена.",
            "suggestion": "Было: A=IDGV03VTB | B=100000000; Стало: A=IDGV03VTB | B=1000000000",
        }
    ]


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
    assert result["findings"][0]["category"] == "xlsx"
    assert result["findings"][0]["line"] == 2
    assert progress[0] == (0, 1, "")
    assert progress[-1] == (1, 1, "config/rates.xlsx")
