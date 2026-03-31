"""Shared parser for extracting structured field information from JSON unified diffs."""

import json
import re


def _extract_diff_lines(diff_text: str):
    """Split unified diff into added and removed lines."""
    added = []
    removed = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added, removed


def _extract_fields_from_lines(lines: list[str]) -> dict[str, dict]:
    """Extract JSON field definitions from diff lines.

    Handles both single-line and multi-line JSON property definitions:
      "fieldName": { "type": "number", "description": "..." }
    or:
      "fieldName": {
        "type": "number",
        "description": "..."
      }

    Returns dict: field_name -> {"type", "description", "format"}.
    """
    fields = {}
    current_field = None
    accumulator = []
    brace_depth = 0

    for line in lines:
        stripped = line.strip().rstrip(",")

        if current_field is None:
            # Look for a field definition start: "fieldName": ...
            m = re.match(r'^"([^"]+)"\s*:\s*(.*)', stripped)
            if not m:
                continue
            field_name = m.group(1)
            rest = m.group(2).strip()

            # Simple scalar value (not an object)
            if not rest.startswith("{"):
                # Could be: "fieldName": "value" or "fieldName": true
                fields[field_name] = {"type": "", "description": "", "format": ""}
                # Try to extract type if it looks like "fieldName": "string"
                sm = re.match(r'^"([^"]*)"', rest)
                if sm:
                    fields[field_name]["type"] = sm.group(1)
                continue

            # Object value — check if single-line
            open_count = rest.count("{")
            close_count = rest.count("}")
            if open_count <= close_count:
                # Single-line object: { "type": "number", "description": "..." }
                fields[field_name] = _parse_field_object(rest)
            else:
                # Multi-line object — start accumulating
                current_field = field_name
                accumulator = [rest]
                brace_depth = open_count - close_count
        else:
            # Accumulating multi-line object
            accumulator.append(stripped)
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                full_text = " ".join(accumulator)
                fields[current_field] = _parse_field_object(full_text)
                current_field = None
                accumulator = []
                brace_depth = 0

    return fields


def _parse_field_object(text: str) -> dict:
    """Parse a JSON object fragment and extract type, description, format."""
    result = {"type": "", "description": "", "format": ""}

    # Try parsing as valid JSON first
    clean = text.strip().rstrip(",")
    try:
        obj = json.loads(clean)
        if isinstance(obj, dict):
            result["type"] = str(obj.get("type", ""))
            result["description"] = str(obj.get("description", ""))
            fmt = obj.get("format", "") or obj.get("x-format", "")
            result["format"] = str(fmt)
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: regex extraction
    m = re.search(r'"type"\s*:\s*"([^"]*)"', text)
    if m:
        result["type"] = m.group(1)
    m = re.search(r'"description"\s*:\s*"([^"]*)"', text)
    if m:
        result["description"] = m.group(1)
    m = re.search(r'"format"\s*:\s*"([^"]*)"', text)
    if m:
        result["format"] = m.group(1)
    if not result["format"]:
        m = re.search(r'"x-format"\s*:\s*"([^"]*)"', text)
        if m:
            result["format"] = m.group(1)

    return result


def parse_json_field_changes(
    diff_text: str, new_file: bool, deleted_file: bool,
) -> list[dict]:
    """Parse a unified diff of a JSON file and return structured field changes.

    Returns list of dicts with keys: field, action, type, description, format.
    action is one of: "added", "removed", "modified".
    """
    added_lines, removed_lines = _extract_diff_lines(diff_text)

    if new_file:
        # All lines are additions
        fields = _extract_fields_from_lines(added_lines)
        return [
            {
                "field": name,
                "action": "added",
                **info,
            }
            for name, info in fields.items()
        ]

    if deleted_file:
        fields = _extract_fields_from_lines(removed_lines)
        return [
            {
                "field": name,
                "action": "removed",
                **info,
            }
            for name, info in fields.items()
        ]

    added_fields = _extract_fields_from_lines(added_lines)
    removed_fields = _extract_fields_from_lines(removed_lines)

    all_names = set(added_fields.keys()) | set(removed_fields.keys())
    results = []
    for name in sorted(all_names):
        in_added = name in added_fields
        in_removed = name in removed_fields
        if in_added and in_removed:
            # Use the new (added) version's info
            results.append({"field": name, "action": "modified", **added_fields[name]})
        elif in_added:
            results.append({"field": name, "action": "added", **added_fields[name]})
        else:
            results.append({"field": name, "action": "removed", **removed_fields[name]})

    return results
