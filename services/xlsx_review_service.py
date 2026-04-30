import inspect
import json
import logging
from io import BytesIO
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

import httpx

from db import get_db
from services.gitlab_client import get_file_bytes, get_mr_diff, get_project_id

logger = logging.getLogger(__name__)

XLSX_NAMESPACE = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_REL_NAMESPACE = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
DEFAULT_XLSX_BASE_REF = "master"


def _column_letters_to_index(value: str) -> int:
    result = 0
    for char in value.upper():
        if "A" <= char <= "Z":
            result = result * 26 + (ord(char) - ord("A") + 1)
    return result


def _column_index_to_letters(index: int) -> str:
    letters: list[str] = []
    current = max(1, index)
    while current:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _cell_reference_to_column(cell_ref: str) -> int:
    prefix = "".join(ch for ch in cell_ref if ch.isalpha())
    return _column_letters_to_index(prefix or "A")


def _read_xml(zip_file: ZipFile, path: str) -> ET.Element:
    with zip_file.open(path) as fh:
        return ET.parse(fh).getroot()


def _read_shared_strings(zip_file: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []

    root = _read_xml(zip_file, "xl/sharedStrings.xml")
    result: list[str] = []
    for item in root.findall("x:si", XLSX_NAMESPACE):
        text = "".join(node.text or "" for node in item.findall(".//x:t", XLSX_NAMESPACE))
        result.append(text)
    return result


def _parse_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    formula = cell.find("x:f", XLSX_NAMESPACE)
    if formula is not None:
        return f"={formula.text or ''}"

    cell_type = cell.attrib.get("t")
    inline_text = cell.find("x:is", XLSX_NAMESPACE)
    if inline_text is not None:
        return "".join(node.text or "" for node in inline_text.findall(".//x:t", XLSX_NAMESPACE))

    value_node = cell.find("x:v", XLSX_NAMESPACE)
    raw_value = (value_node.text or "") if value_node is not None else ""

    if cell_type == "s" and raw_value:
        try:
            return shared_strings[int(raw_value)]
        except (IndexError, ValueError):
            return raw_value
    if cell_type == "b":
        return "TRUE" if raw_value == "1" else "FALSE"
    return raw_value


def _format_row(cells: dict[int, str]) -> str:
    parts: list[str] = []
    for column_index in sorted(cells):
        value = cells[column_index].replace("\r", "").replace("\n", "\\n").strip()
        parts.append(f"{_column_index_to_letters(column_index)}={value}")
    return " | ".join(parts)


def extract_workbook_rows(content: bytes) -> dict[str, dict[int, str]]:
    try:
        with ZipFile(BytesIO(content)) as zip_file:
            shared_strings = _read_shared_strings(zip_file)
            workbook_root = _read_xml(zip_file, "xl/workbook.xml")
            rels_root = _read_xml(zip_file, "xl/_rels/workbook.xml.rels")

            relationships = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in rels_root.findall("r:Relationship", XLSX_REL_NAMESPACE)
            }

            result: dict[str, dict[int, str]] = {}
            for sheet in workbook_root.findall("x:sheets/x:sheet", XLSX_NAMESPACE):
                sheet_name = sheet.attrib.get("name", "Sheet")
                rel_id = sheet.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", ""
                )
                target = relationships.get(rel_id, "")
                if not target:
                    continue
                target_path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
                sheet_root = _read_xml(zip_file, target_path)
                rows: dict[int, str] = {}
                for row in sheet_root.findall("x:sheetData/x:row", XLSX_NAMESPACE):
                    row_index = int(row.attrib.get("r", "0") or "0")
                    row_cells: dict[int, str] = {}
                    for cell in row.findall("x:c", XLSX_NAMESPACE):
                        column_index = _cell_reference_to_column(cell.attrib.get("r", "A1"))
                        row_cells[column_index] = _parse_cell_value(cell, shared_strings)
                    rows[row_index] = _format_row(row_cells)
                result[sheet_name] = rows
            return result
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"Не удалось прочитать xlsx: {exc}") from exc


def build_xlsx_diff_findings(
    base_rows: dict[str, dict[int, str]],
    head_rows: dict[str, dict[int, str]],
    file_path: str,
) -> list[dict]:
    findings: list[dict] = []
    sheet_names = list(dict.fromkeys([*base_rows.keys(), *head_rows.keys()]))

    for sheet_name in sheet_names:
        before_sheet = base_rows.get(sheet_name, {})
        after_sheet = head_rows.get(sheet_name, {})
        row_numbers = sorted(set(before_sheet) | set(after_sheet))

        for row_number in row_numbers:
            before_value = before_sheet.get(row_number)
            after_value = after_sheet.get(row_number)
            if before_value == after_value:
                continue

            if before_value is None:
                message = f"Лист '{sheet_name}', строка {row_number} добавлена."
            elif after_value is None:
                message = f"Лист '{sheet_name}', строка {row_number} удалена."
            else:
                message = f"Лист '{sheet_name}', строка {row_number} изменена."

            findings.append({
                "severity": "info",
                "category": "xlsx",
                "file_path": file_path,
                "line": row_number,
                "message": message,
                "suggestion": f"Было: {before_value or 'пусто'}; Стало: {after_value or 'пусто'}",
            })

    return findings


def _compute_summary(findings: list[dict], files_total: int, files_analyzed: int) -> dict:
    return {
        "errors": 0,
        "warnings": 0,
        "info": len(findings),
        "total": len(findings),
        "files_total": files_total,
        "files_analyzed": files_analyzed,
        "truncated": False,
    }


async def _report_progress(progress_callback, current_file: int, total_files: int, file_path: str = "") -> None:
    if progress_callback is None:
        return
    result = progress_callback(current_file, total_files, file_path)
    if inspect.isawaitable(result):
        await result


async def _load_file_bytes_or_none(project_id: int, file_path: str, ref: str) -> bytes | None:
    try:
        return await get_file_bytes(project_id, file_path, ref)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


async def review_xlsx_mr(
    mr_iid: int,
    base_ref: str = DEFAULT_XLSX_BASE_REF,
    progress_callback=None,
) -> dict:
    project_id = await get_project_id()
    mr_data = await get_mr_diff(project_id, mr_iid)
    xlsx_changes = [
        change for change in mr_data["changes"]
        if (change.get("new_path") or change.get("old_path") or "").lower().endswith(".xlsx")
    ]
    findings: list[dict] = []

    total_files = len(xlsx_changes)
    await _report_progress(progress_callback, 0, total_files, "")

    for index, change in enumerate(xlsx_changes, start=1):
        file_path = change.get("new_path") or change.get("old_path") or "unknown.xlsx"
        await _report_progress(progress_callback, index, total_files, file_path)

        base_path = change.get("old_path") or file_path
        head_path = change.get("new_path") or file_path
        base_bytes = await _load_file_bytes_or_none(project_id, base_path, base_ref)
        head_bytes = await _load_file_bytes_or_none(project_id, head_path, mr_data["source_branch"])

        if base_bytes is None and head_bytes is None:
            findings.append({
                "severity": "warning",
                "category": "xlsx",
                "file_path": file_path,
                "line": None,
                "message": f"Файл '{file_path}' не удалось прочитать ни из '{base_ref}', ни из '{mr_data['source_branch']}'.",
                "suggestion": None,
            })
            continue

        base_rows = extract_workbook_rows(base_bytes) if base_bytes is not None else {}
        head_rows = extract_workbook_rows(head_bytes) if head_bytes is not None else {}
        findings.extend(build_xlsx_diff_findings(base_rows, head_rows, file_path))

    summary = _compute_summary(findings, total_files, total_files)

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO code_reviews (mr_iid, mr_title, mr_url, model_used, custom_prompt, findings_json, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            mr_iid,
            mr_data["title"],
            mr_data["web_url"],
            f"xlsx-diff:{base_ref}",
            f"xlsx compare against {base_ref}",
            json.dumps(findings, ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
        ),
    )
    review_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": review_id,
        "mr": {
            "iid": mr_iid,
            "title": mr_data["title"],
            "author": mr_data["author"],
            "source_branch": mr_data["source_branch"],
            "target_branch": mr_data["target_branch"],
            "web_url": mr_data["web_url"],
            "files_changed": total_files,
            "comparison_ref": base_ref,
            "review_kind": "xlsx",
        },
        "findings": findings,
        "summary": summary,
        "model_used": f"xlsx-diff:{base_ref}",
    }
