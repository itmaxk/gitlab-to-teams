import json
import logging
import re

from fastapi import APIRouter, HTTPException

from models import SchemaRequest
from services.gitlab_client import get_mr_by_iid, get_mr_diff, get_project_id
from services.json_diff_parser import parse_json_field_changes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schema", tags=["schema"])


def _parse_mr_iid(raw: str) -> int:
    raw = raw.strip()
    m = re.search(r'/merge_requests/(\d+)', raw)
    if m:
        return int(m.group(1))
    digits = re.sub(r'[^0-9]', '', raw)
    if digits:
        return int(digits)
    raise ValueError(f"Cannot parse MR IID from: {raw}")


def _classify_schema_file(file_path: str):
    """Classify a schema file by category and entity name.

    E.g. .../dataSource/GetPolicyAaCommissionDataSource/resultSchema.json
      -> {"category": "dataSource", "entity_name": "GetPolicyAaCommissionDataSource",
          "schema_file": "resultSchema.json"}
    """
    normalized = file_path.replace("\\", "/")
    categories = [
        ("dataSource", re.compile(r'/dataSource/([^/]+)/([^/]+\.json)$', re.IGNORECASE)),
        ("dataExport", re.compile(r'/dataExport/([^/]+)/([^/]+\.json)$', re.IGNORECASE)),
        ("component", re.compile(r'/component/([^/]+)/([^/]+\.json)$', re.IGNORECASE)),
    ]
    for category, pattern in categories:
        m = pattern.search(normalized)
        if m:
            return {
                "category": category,
                "entity_name": m.group(1),
                "schema_file": m.group(2),
            }
    return None


def _is_json_schema_file(path: str) -> bool:
    lower = path.lower()
    if not lower.endswith(".json"):
        return False
    parts = lower.replace("\\", "/").split("/")
    name = parts[-1]
    for part in parts:
        if "schema" in part:
            return True
    if "schema" in name:
        return True
    return True  # any .json file could be a schema — include all


def _parse_json_diff(diff_text: str) -> dict:
    """Parse unified diff and extract added/removed lines."""
    added = []
    removed = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return {"added_lines": added, "removed_lines": removed}


def _analyze_json_changes(diff_text: str, new_file: bool, deleted_file: bool) -> dict:
    """Analyze JSON diff and describe changes."""
    if new_file:
        return {"action": "created", "description": "Новый файл"}
    if deleted_file:
        return {"action": "deleted", "description": "Файл удалён"}

    parsed = _parse_json_diff(diff_text)
    added = "\n".join(parsed["added_lines"])
    removed = "\n".join(parsed["removed_lines"])

    # Try to parse as JSON fragments to find field-level changes
    changes = []

    # Detect added fields/properties
    for line in parsed["added_lines"]:
        line = line.strip().rstrip(",")
        m = re.match(r'^"([^"]+)"\s*:', line)
        if m:
            changes.append({"type": "added", "field": m.group(1), "value": line})

    # Detect removed fields/properties
    for line in parsed["removed_lines"]:
        line = line.strip().rstrip(",")
        m = re.match(r'^"([^"]+)"\s*:', line)
        if m:
            changes.append({"type": "removed", "field": m.group(1), "value": line})

    # Find modified fields (same key in both added and removed)
    added_fields = {c["field"] for c in changes if c["type"] == "added"}
    removed_fields = {c["field"] for c in changes if c["type"] == "removed"}
    modified_fields = added_fields & removed_fields

    result_changes = []
    for c in changes:
        if c["field"] in modified_fields:
            if c["type"] == "added":
                result_changes.append({
                    "type": "modified",
                    "field": c["field"],
                    "value": c["value"],
                })
        else:
            result_changes.append(c)

    if not result_changes:
        return {
            "action": "modified",
            "description": "Файл изменён",
            "diff": {
                "added_lines": parsed["added_lines"][:50],
                "removed_lines": parsed["removed_lines"][:50],
            },
        }

    return {
        "action": "modified",
        "changes": result_changes,
    }


@router.post("/analyze")
async def analyze_schemas(req: SchemaRequest):
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

        schema_files = []
        for change in mr_data["changes"]:
            path = change["new_path"] or change["old_path"]
            if not path.lower().endswith(".json"):
                continue

            analysis = _analyze_json_changes(
                change["diff"],
                change["new_file"],
                change["deleted_file"],
            )
            classification = _classify_schema_file(path)
            report_attributes = parse_json_field_changes(
                change["diff"],
                change["new_file"],
                change["deleted_file"],
            )
            schema_files.append({
                "file_path": path,
                **analysis,
                "classification": classification,
                "report_attributes": report_attributes,
            })

        results.append({
            "mr_iid": mr_iid,
            "mr_title": mr_data["title"],
            "mr_url": mr_data["web_url"],
            "author": mr_data["author"],
            "source_branch": mr_data["source_branch"],
            "target_branch": mr_data["target_branch"],
            "schema_files": schema_files,
        })

    # Summary: all unique files across all MRs
    all_files = {}
    for r in results:
        for f in r.get("schema_files", []):
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
