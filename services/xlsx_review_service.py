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
GROUPED_COLUMN_CHANGE_THRESHOLD = 3
GROUPED_ROW_CHANGE_THRESHOLD = 3


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


def extract_workbook_cells(content: bytes) -> dict[str, dict[int, dict[int, str]]]:
    try:
        with ZipFile(BytesIO(content)) as zip_file:
            shared_strings = _read_shared_strings(zip_file)
            workbook_root = _read_xml(zip_file, "xl/workbook.xml")
            rels_root = _read_xml(zip_file, "xl/_rels/workbook.xml.rels")

            relationships = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in rels_root.findall("r:Relationship", XLSX_REL_NAMESPACE)
            }

            result: dict[str, dict[int, dict[int, str]]] = {}
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
                rows: dict[int, dict[int, str]] = {}
                for row in sheet_root.findall("x:sheetData/x:row", XLSX_NAMESPACE):
                    row_index = int(row.attrib.get("r", "0") or "0")
                    row_cells: dict[int, str] = {}
                    for cell in row.findall("x:c", XLSX_NAMESPACE):
                        column_index = _cell_reference_to_column(cell.attrib.get("r", "A1"))
                        row_cells[column_index] = _parse_cell_value(cell, shared_strings)
                    rows[row_index] = row_cells
                result[sheet_name] = rows
            return result
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"Не удалось прочитать xlsx: {exc}") from exc


def extract_workbook_rows(content: bytes) -> dict[str, dict[int, str]]:
    workbook_cells = extract_workbook_cells(content)
    return {
        sheet_name: {
            row_index: _format_row(cells)
            for row_index, cells in rows.items()
        }
        for sheet_name, rows in workbook_cells.items()
    }


def _column_label(sheet_rows: dict[int, dict[int, str]], column_index: int) -> str:
    for row_number in sorted(sheet_rows):
        value = (sheet_rows[row_number].get(column_index) or "").strip()
        if value:
            return value
    return ""


def _column_descriptor(sheet_rows: dict[int, dict[int, str]], column_index: int) -> str:
    letter = _column_index_to_letters(column_index)
    label = _column_label(sheet_rows, column_index)
    if label:
        return f"{letter} ('{label}')"
    return letter


def _sheet_columns(sheet_rows: dict[int, dict[int, str]]) -> set[int]:
    return {column for cells in sheet_rows.values() for column in cells}


def _preview_column_values(sheet_rows: dict[int, dict[int, str]], column_index: int, limit: int = 3) -> str:
    parts: list[str] = []
    for row_number in sorted(sheet_rows):
        value = (sheet_rows[row_number].get(column_index) or "").strip()
        if not value:
            continue
        parts.append(f"{_column_index_to_letters(column_index)}{row_number}={value}")
        if len(parts) >= limit:
            break
    return "; ".join(parts) if parts else "нет непустых значений"


def _compress_row_numbers(row_numbers: list[int]) -> str:
    if not row_numbers:
        return ""
    ranges: list[str] = []
    start = row_numbers[0]
    end = row_numbers[0]
    for row_number in row_numbers[1:]:
        if row_number == end + 1:
            end = row_number
            continue
        ranges.append(f"{start}-{end}" if start != end else str(start))
        start = end = row_number
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ", ".join(ranges)


def _preview_rows(sheet_rows: dict[int, dict[int, str]], row_numbers: list[int], limit: int = 3) -> str:
    parts: list[str] = []
    for row_number in row_numbers[:limit]:
        parts.append(f"{row_number}: {_format_row(sheet_rows.get(row_number, {})) or 'пусто'}")
    return "; ".join(parts) if parts else "нет данных"


def _decorate_finding(
    finding: dict,
    comparison_ref: str,
    is_master_comparison: bool,
) -> dict:
    result = dict(finding)
    result["comparison_ref"] = comparison_ref
    result["comparison_kind"] = "master" if is_master_comparison else "target"
    return result


def _build_column_findings(
    before_sheet: dict[int, dict[int, str]],
    after_sheet: dict[int, dict[int, str]],
    file_path: str,
    sheet_name: str,
) -> list[dict]:
    if not before_sheet or not after_sheet:
        return []

    findings: list[dict] = []
    before_columns = _sheet_columns(before_sheet)
    after_columns = _sheet_columns(after_sheet)

    for column_index in sorted(before_columns - after_columns):
        findings.append({
            "severity": "info",
            "category": "xlsx",
            "file_path": file_path,
            "line": None,
            "message": f"Лист '{sheet_name}', столбец {_column_descriptor(before_sheet, column_index)} удален.",
            "suggestion": f"Примеры удаленных значений: {_preview_column_values(before_sheet, column_index)}",
        })

    for column_index in sorted(after_columns - before_columns):
        findings.append({
            "severity": "info",
            "category": "xlsx",
            "file_path": file_path,
            "line": None,
            "message": f"Лист '{sheet_name}', столбец {_column_descriptor(after_sheet, column_index)} добавлен.",
            "suggestion": f"Примеры новых значений: {_preview_column_values(after_sheet, column_index)}",
        })

    return findings


def _group_cell_changes(
    before_sheet: dict[int, dict[int, str]],
    after_sheet: dict[int, dict[int, str]],
    common_columns: set[int],
) -> tuple[list[dict], set[tuple[int, int]]]:
    by_column: dict[int, list[dict]] = {}

    common_rows = sorted(set(before_sheet) & set(after_sheet))
    for row_number in common_rows:
        before_cells = before_sheet[row_number]
        after_cells = after_sheet[row_number]
        for column_index in sorted(common_columns):
            before_value = before_cells.get(column_index, "")
            after_value = after_cells.get(column_index, "")
            if before_value == after_value:
                continue
            by_column.setdefault(column_index, []).append({
                "row": row_number,
                "before": before_value,
                "after": after_value,
            })

    grouped_findings: list[dict] = []
    covered_cells: set[tuple[int, int]] = set()

    for column_index, changes in sorted(by_column.items()):
        if len(changes) < GROUPED_COLUMN_CHANGE_THRESHOLD:
            continue
        row_list = ", ".join(str(item["row"]) for item in changes[:8])
        if len(changes) > 8:
            row_list += ", ..."
        preview = "; ".join(
            f"{_column_index_to_letters(column_index)}{item['row']}: {item['before'] or 'пусто'} -> {item['after'] or 'пусто'}"
            for item in changes[:3]
        )
        grouped_findings.append({
            "severity": "info",
            "category": "xlsx",
            "file_path": "",
            "line": changes[0]["row"],
            "message": f"Столбец {_column_index_to_letters(column_index)} изменен в {len(changes)} строках ({row_list}).",
            "suggestion": preview,
        })
        covered_cells.update((item["row"], column_index) for item in changes)

    return grouped_findings, covered_cells


def _build_grouped_row_findings(
    before_sheet: dict[int, dict[int, str]],
    after_sheet: dict[int, dict[int, str]],
    file_path: str,
    sheet_name: str,
) -> tuple[list[dict], set[int], set[int]]:
    deleted_rows = sorted(set(before_sheet) - set(after_sheet))
    added_rows = sorted(set(after_sheet) - set(before_sheet))
    findings: list[dict] = []
    covered_deleted: set[int] = set()
    covered_added: set[int] = set()

    if len(deleted_rows) >= GROUPED_ROW_CHANGE_THRESHOLD:
        findings.append({
            "severity": "info",
            "category": "xlsx",
            "file_path": file_path,
            "line": deleted_rows[0],
            "message": f"Лист '{sheet_name}', удалены строки {_compress_row_numbers(deleted_rows)}.",
            "suggestion": f"Примеры удаленных строк: {_preview_rows(before_sheet, deleted_rows)}",
        })
        covered_deleted.update(deleted_rows)

    if len(added_rows) >= GROUPED_ROW_CHANGE_THRESHOLD:
        findings.append({
            "severity": "info",
            "category": "xlsx",
            "file_path": file_path,
            "line": added_rows[0],
            "message": f"Лист '{sheet_name}', добавлены строки {_compress_row_numbers(added_rows)}.",
            "suggestion": f"Примеры новых строк: {_preview_rows(after_sheet, added_rows)}",
        })
        covered_added.update(added_rows)

    return findings, covered_deleted, covered_added


def build_xlsx_diff_findings(
    base_rows: dict[str, dict[int, str]] | dict[str, dict[int, dict[int, str]]],
    head_rows: dict[str, dict[int, str]] | dict[str, dict[int, dict[int, str]]],
    file_path: str,
    *,
    comparison_ref: str = DEFAULT_XLSX_BASE_REF,
    is_master_comparison: bool = False,
) -> list[dict]:
    findings: list[dict] = []
    sheet_names = list(dict.fromkeys([*base_rows.keys(), *head_rows.keys()]))

    for sheet_name in sheet_names:
        before_sheet = base_rows.get(sheet_name, {})
        after_sheet = head_rows.get(sheet_name, {})
        before_present = sheet_name in base_rows
        after_present = sheet_name in head_rows

        if before_sheet and isinstance(next(iter(before_sheet.values())), str):
            before_sheet = {}
        if after_sheet and isinstance(next(iter(after_sheet.values())), str):
            after_sheet = {}

        if not before_present and not after_present:
            continue
        if not before_present:
            findings.append(_decorate_finding({
                "severity": "info",
                "category": "xlsx",
                "file_path": file_path,
                "line": None,
                "message": f"Лист '{sheet_name}' добавлен.",
                "suggestion": None,
            }, comparison_ref, is_master_comparison))
            continue
        if not after_present:
            findings.append(_decorate_finding({
                "severity": "info",
                "category": "xlsx",
                "file_path": file_path,
                "line": None,
                "message": f"Лист '{sheet_name}' удален.",
                "suggestion": None,
            }, comparison_ref, is_master_comparison))
            continue

        findings.extend(
            _decorate_finding(finding, comparison_ref, is_master_comparison)
            for finding in _build_column_findings(before_sheet, after_sheet, file_path, sheet_name)
        )

        common_columns = _sheet_columns(before_sheet) & _sheet_columns(after_sheet)
        grouped_changes, covered_cells = _group_cell_changes(before_sheet, after_sheet, common_columns)
        for finding in grouped_changes:
            finding["file_path"] = file_path
        findings.extend(
            _decorate_finding(finding, comparison_ref, is_master_comparison)
            for finding in grouped_changes
        )
        grouped_row_findings, covered_deleted_rows, covered_added_rows = _build_grouped_row_findings(
            before_sheet,
            after_sheet,
            file_path,
            sheet_name,
        )
        findings.extend(
            _decorate_finding(finding, comparison_ref, is_master_comparison)
            for finding in grouped_row_findings
        )

        row_numbers = sorted(set(before_sheet) | set(after_sheet))
        for row_number in row_numbers:
            before_cells = before_sheet.get(row_number)
            after_cells = after_sheet.get(row_number)
            if before_cells is None:
                if row_number in covered_added_rows:
                    continue
                findings.append(_decorate_finding({
                    "severity": "info",
                    "category": "xlsx",
                    "file_path": file_path,
                    "line": row_number,
                    "message": f"Лист '{sheet_name}', строка {row_number} добавлена.",
                    "suggestion": f"Стало: {_format_row(after_cells) or 'пусто'}",
                }, comparison_ref, is_master_comparison))
                continue
            if after_cells is None:
                if row_number in covered_deleted_rows:
                    continue
                findings.append(_decorate_finding({
                    "severity": "info",
                    "category": "xlsx",
                    "file_path": file_path,
                    "line": row_number,
                    "message": f"Лист '{sheet_name}', строка {row_number} удалена.",
                    "suggestion": f"Было: {_format_row(before_cells) or 'пусто'}",
                }, comparison_ref, is_master_comparison))
                continue

            if before_cells == after_cells:
                continue

            row_changed = False
            for column_index in sorted(common_columns):
                if (row_number, column_index) in covered_cells:
                    row_changed = True
                    continue
                before_value = before_cells.get(column_index, "")
                after_value = after_cells.get(column_index, "")
                if before_value == after_value:
                    continue
                row_changed = True
                findings.append(_decorate_finding({
                    "severity": "info",
                    "category": "xlsx",
                    "file_path": file_path,
                    "line": row_number,
                    "message": f"Лист '{sheet_name}', ячейка {_column_index_to_letters(column_index)}{row_number} изменена.",
                    "suggestion": f"Было: {before_value or 'пусто'}; Стало: {after_value or 'пусто'}",
                }, comparison_ref, is_master_comparison))

            if not row_changed:
                continue

    return findings


def _compute_summary(
    findings: list[dict],
    files_total: int,
    files_analyzed: int,
    master_comparison: dict | None = None,
) -> dict:
    summary = {
        "errors": 0,
        "warnings": 0,
        "info": len(findings),
        "total": len(findings),
        "files_total": files_total,
        "files_analyzed": files_analyzed,
        "truncated": False,
    }
    if master_comparison is not None:
        summary["master_comparison"] = master_comparison
    return summary


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


def _resolve_primary_comparison_ref(mr_data: dict, requested_ref: str) -> str:
    if requested_ref.strip():
        return requested_ref.strip()
    return (mr_data.get("target_branch") or DEFAULT_XLSX_BASE_REF).strip() or DEFAULT_XLSX_BASE_REF


async def _analyze_xlsx_change_set(
    project_id: int,
    mr_data: dict,
    xlsx_changes: list[dict],
    comparison_ref: str,
    *,
    is_master_comparison: bool = False,
    progress_callback=None,
) -> list[dict]:
    findings: list[dict] = []
    total_files = len(xlsx_changes)
    await _report_progress(progress_callback, 0, total_files, "")

    for index, change in enumerate(xlsx_changes, start=1):
        file_path = change.get("new_path") or change.get("old_path") or "unknown.xlsx"
        await _report_progress(progress_callback, index, total_files, file_path)

        base_path = change.get("old_path") or file_path
        head_path = change.get("new_path") or file_path
        base_bytes = await _load_file_bytes_or_none(project_id, base_path, comparison_ref)
        head_bytes = await _load_file_bytes_or_none(project_id, head_path, mr_data["source_branch"])

        if base_bytes is None and head_bytes is None:
            findings.append(_decorate_finding({
                "severity": "warning",
                "category": "xlsx",
                "file_path": file_path,
                "line": None,
                "message": f"Файл '{file_path}' не удалось прочитать ни из '{comparison_ref}', ни из '{mr_data['source_branch']}'.",
                "suggestion": None,
            }, comparison_ref, is_master_comparison))
            continue

        base_sheet_cells = extract_workbook_cells(base_bytes) if base_bytes is not None else {}
        head_sheet_cells = extract_workbook_cells(head_bytes) if head_bytes is not None else {}
        findings.extend(
            build_xlsx_diff_findings(
                base_sheet_cells,
                head_sheet_cells,
                file_path,
                comparison_ref=comparison_ref,
                is_master_comparison=is_master_comparison,
            )
        )

    return findings


def _build_master_comparison_summary(
    target_branch: str,
    master_findings: list[dict],
) -> dict | None:
    if target_branch == DEFAULT_XLSX_BASE_REF:
        return None
    return {
        "enabled": True,
        "ref": DEFAULT_XLSX_BASE_REF,
        "differs": bool(master_findings),
        "finding_count": len(master_findings),
    }


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

    primary_ref = _resolve_primary_comparison_ref(mr_data, base_ref)
    target_branch = (mr_data.get("target_branch") or DEFAULT_XLSX_BASE_REF).strip() or DEFAULT_XLSX_BASE_REF

    findings = await _analyze_xlsx_change_set(
        project_id,
        mr_data,
        xlsx_changes,
        primary_ref,
        is_master_comparison=False,
        progress_callback=progress_callback,
    )

    master_findings: list[dict] = []
    if target_branch != DEFAULT_XLSX_BASE_REF:
        master_findings = await _analyze_xlsx_change_set(
            project_id,
            mr_data,
            xlsx_changes,
            DEFAULT_XLSX_BASE_REF,
            is_master_comparison=True,
            progress_callback=None,
        )
        findings.extend(master_findings)

    summary = _compute_summary(
        findings,
        len(xlsx_changes),
        len(xlsx_changes),
        master_comparison=_build_master_comparison_summary(target_branch, master_findings),
    )

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO code_reviews (mr_iid, mr_title, mr_url, model_used, custom_prompt, findings_json, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            mr_iid,
            mr_data["title"],
            mr_data["web_url"],
            f"xlsx-diff:{primary_ref}",
            f"xlsx compare against {primary_ref}",
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
            "files_changed": len(xlsx_changes),
            "comparison_ref": primary_ref,
            "review_kind": "xlsx",
        },
        "findings": findings,
        "summary": summary,
        "model_used": f"xlsx-diff:{primary_ref}",
    }
