import logging
import re

from fastapi import APIRouter, HTTPException

from models import DatabaseRequest
from services.gitlab_client import get_mr_diff, get_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/database", tags=["database"])

# Folders to scan for database-related changes
_DB_PATH_PATTERNS = [
    re.compile(r"(^|/)model(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)database/postgres(/|$)", re.IGNORECASE),
]


def _parse_mr_iid(raw: str) -> int:
    raw = raw.strip()
    m = re.search(r'/merge_requests/(\d+)', raw)
    if m:
        return int(m.group(1))
    digits = re.sub(r'[^0-9]', '', raw)
    if digits:
        return int(digits)
    raise ValueError(f"Cannot parse MR IID from: {raw}")


def _is_db_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in _DB_PATH_PATTERNS:
        if pattern.search(normalized):
            return True
    return False


def _analyze_sql_diff(diff_text: str, file_path: str, new_file: bool, deleted_file: bool) -> dict:
    """Analyze SQL/migration diff for column and table operations."""
    if new_file:
        return _analyze_sql_content(diff_text, is_new=True)
    if deleted_file:
        return {"action": "deleted", "description": "Файл удалён", "operations": []}

    added_lines = []
    removed_lines = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed_lines.append(line[1:])

    added_text = "\n".join(added_lines)
    removed_text = "\n".join(removed_lines)

    operations = []

    # Detect CREATE TABLE
    for m in re.finditer(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?(\w+(?:\.\w+)?)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "create_table",
            "table": m.group(1),
            "description": f"Создание таблицы {m.group(1)}",
        })

    # Detect DROP TABLE
    for m in re.finditer(
        r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`"\[]?(\w+(?:\.\w+)?)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "drop_table",
            "table": m.group(1),
            "description": f"Удаление таблицы {m.group(1)}",
        })

    # Detect ADD COLUMN
    for m in re.finditer(
        r'ADD\s+(?:COLUMN\s+)?[`"\[]?(\w+)[`"\]]?\s+(\w[\w\s()]*)',
        added_text, re.IGNORECASE
    ):
        col_name = m.group(1)
        col_type = m.group(2).strip().split()[0] if m.group(2).strip() else ""
        # Skip SQL keywords that aren't column names
        if col_name.upper() in ("CONSTRAINT", "INDEX", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"):
            continue
        operations.append({
            "type": "add_column",
            "column": col_name,
            "column_type": col_type,
            "description": f"Добавление колонки {col_name} ({col_type})",
        })

    # Detect DROP COLUMN
    for m in re.finditer(
        r'DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?[`"\[]?(\w+)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "drop_column",
            "column": m.group(1),
            "description": f"Удаление колонки {m.group(1)}",
        })

    # Detect ALTER COLUMN / MODIFY COLUMN
    for m in re.finditer(
        r'(?:ALTER|MODIFY)\s+(?:COLUMN\s+)?[`"\[]?(\w+)[`"\]]?\s+(?:SET\s+|TYPE\s+)?(\w[\w\s()]*)',
        added_text, re.IGNORECASE
    ):
        col_name = m.group(1)
        new_def = m.group(2).strip().split()[0] if m.group(2).strip() else ""
        if col_name.upper() in ("TABLE", "INDEX", "CONSTRAINT"):
            continue
        operations.append({
            "type": "alter_column",
            "column": col_name,
            "new_definition": new_def,
            "description": f"Изменение колонки {col_name} → {new_def}",
        })

    # Detect RENAME COLUMN
    for m in re.finditer(
        r'RENAME\s+COLUMN\s+[`"\[]?(\w+)[`"\]]?\s+TO\s+[`"\[]?(\w+)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "rename_column",
            "old_name": m.group(1),
            "new_name": m.group(2),
            "description": f"Переименование колонки {m.group(1)} → {m.group(2)}",
        })

    # Detect RENAME TABLE
    for m in re.finditer(
        r'RENAME\s+(?:TABLE\s+)?[`"\[]?(\w+(?:\.\w+)?)[`"\]]?\s+TO\s+[`"\[]?(\w+(?:\.\w+)?)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "rename_table",
            "old_name": m.group(1),
            "new_name": m.group(2),
            "description": f"Переименование таблицы {m.group(1)} → {m.group(2)}",
        })

    # Detect CREATE/DROP INDEX
    for m in re.finditer(
        r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?(\w+)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "create_index",
            "index": m.group(1),
            "description": f"Создание индекса {m.group(1)}",
        })

    for m in re.finditer(
        r'DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?[`"\[]?(\w+)[`"\]]?',
        added_text, re.IGNORECASE
    ):
        operations.append({
            "type": "drop_index",
            "index": m.group(1),
            "description": f"Удаление индекса {m.group(1)}",
        })

    # For model files (non-SQL) — detect property/field changes
    lower_path = file_path.lower()
    if not lower_path.endswith(".sql"):
        model_ops = _analyze_model_diff(added_lines, removed_lines, file_path)
        operations.extend(model_ops)

    if not operations:
        return {
            "action": "modified",
            "description": "Файл изменён (без распознанных операций с колонками/таблицами)",
            "operations": [],
            "diff_stats": {
                "added_lines": len(added_lines),
                "removed_lines": len(removed_lines),
            },
        }

    return {
        "action": "modified",
        "operations": operations,
    }


def _analyze_sql_content(diff_text: str, is_new: bool = False) -> dict:
    """Analyze full SQL content (for new files)."""
    # Extract only added lines from diff
    lines = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    text = "\n".join(lines)

    operations = []

    # Find CREATE TABLE and extract columns
    for m in re.finditer(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?(\w+(?:\.\w+)?)[`"\]]?\s*\((.*?)\)',
        text, re.IGNORECASE | re.DOTALL
    ):
        table_name = m.group(1)
        body = m.group(2)
        columns = []
        for col_m in re.finditer(
            r'[`"\[]?(\w+)[`"\]]?\s+((?:character\s+varying|[\w()]+))',
            body, re.IGNORECASE
        ):
            col_name = col_m.group(1)
            col_type = col_m.group(2)
            if col_name.upper() in (
                "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT",
                "INDEX", "KEY", "CREATE", "TABLE", "NOT", "NULL", "DEFAULT",
            ):
                continue
            columns.append({"name": col_name, "type": col_type})

        operations.append({
            "type": "create_table",
            "table": table_name,
            "columns": columns,
            "description": f"Создание таблицы {table_name} ({len(columns)} колонок)",
        })

    if not operations:
        operations.append({
            "type": "new_file",
            "description": "Новый файл",
        })

    return {
        "action": "created",
        "operations": operations,
    }


def _analyze_model_diff(added_lines: list[str], removed_lines: list[str], file_path: str) -> list[dict]:
    """Analyze model file changes (Java, Kotlin, C#, etc.) for field/property changes."""
    operations = []

    # Common patterns for field declarations in various languages
    # Java/Kotlin: private String fieldName;  or val fieldName: String
    # C#: public string FieldName { get; set; }
    field_pattern = re.compile(
        r'(?:private|public|protected|internal)?\s*'
        r'(?:val|var|static|final|readonly)?\s*'
        r'(\w+)\s+(\w+)\s*[;={]'
    )

    # Handlebars/SQL column reference: {{columnName}} or column_name type
    column_pattern = re.compile(r'(\w+)\s+(INTEGER|TEXT|VARCHAR|BOOLEAN|REAL|BLOB|TIMESTAMP|BIGINT|SMALLINT|NUMERIC|DECIMAL|DATE|UUID)', re.IGNORECASE)

    added_fields = {}
    removed_fields = {}

    for line in added_lines:
        stripped = line.strip()
        m = field_pattern.search(stripped)
        if m:
            added_fields[m.group(2)] = m.group(1)
        m2 = column_pattern.search(stripped)
        if m2:
            added_fields[m2.group(1)] = m2.group(2)

    for line in removed_lines:
        stripped = line.strip()
        m = field_pattern.search(stripped)
        if m:
            removed_fields[m.group(2)] = m.group(1)
        m2 = column_pattern.search(stripped)
        if m2:
            removed_fields[m2.group(1)] = m2.group(2)

    # Fields in both = modified, only in added = new, only in removed = deleted
    all_fields = set(added_fields.keys()) | set(removed_fields.keys())
    for field in sorted(all_fields):
        in_added = field in added_fields
        in_removed = field in removed_fields
        if in_added and in_removed:
            if added_fields[field] != removed_fields[field]:
                operations.append({
                    "type": "alter_column",
                    "column": field,
                    "old_type": removed_fields[field],
                    "new_definition": added_fields[field],
                    "description": f"Изменение поля {field}: {removed_fields[field]} → {added_fields[field]}",
                })
        elif in_added:
            operations.append({
                "type": "add_column",
                "column": field,
                "column_type": added_fields[field],
                "description": f"Добавление поля {field} ({added_fields[field]})",
            })
        elif in_removed:
            operations.append({
                "type": "drop_column",
                "column": field,
                "description": f"Удаление поля {field}",
            })

    return operations


@router.post("/analyze")
async def analyze_database(req: DatabaseRequest):
    try:
        project_id = await get_project_id()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GitLab connection error: {e}")

    results = []

    for raw_id in req.mr_ids:
        try:
            mr_iid = _parse_mr_iid(raw_id)
        except ValueError as e:
            results.append({"mr_input": raw_id, "error": str(e)})
            continue

        try:
            mr_data = await get_mr_diff(project_id, mr_iid)
        except Exception as e:
            results.append({"mr_input": raw_id, "mr_iid": mr_iid, "error": str(e)})
            continue

        db_files = []
        for change in mr_data["changes"]:
            path = change["new_path"] or change["old_path"]
            if not _is_db_file(path):
                continue

            analysis = _analyze_sql_diff(
                change["diff"],
                path,
                change["new_file"],
                change["deleted_file"],
            )
            db_files.append({
                "file_path": path,
                **analysis,
            })

        results.append({
            "mr_iid": mr_iid,
            "mr_title": mr_data["title"],
            "mr_url": mr_data["web_url"],
            "author": mr_data["author"],
            "source_branch": mr_data["source_branch"],
            "target_branch": mr_data["target_branch"],
            "db_files": db_files,
        })

    # Summary: all unique files across all MRs
    all_files = {}
    for r in results:
        for f in r.get("db_files", []):
            path = f["file_path"]
            if path not in all_files:
                all_files[path] = []
            all_files[path].append({
                "mr_iid": r["mr_iid"],
                "mr_title": r["mr_title"],
                **{k: v for k, v in f.items() if k != "file_path"},
            })

    return {
        "mrs": results,
        "summary": all_files,
        "total_mrs": len([r for r in results if "error" not in r]),
        "total_files": len(all_files),
    }
