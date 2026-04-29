from collections import defaultdict


SEVERITY_META = {
    "error": {"emoji": "🔴", "label": "Errors"},
    "warning": {"emoji": "🟠", "label": "Warnings"},
    "info": {"emoji": "🔵", "label": "Info"},
}

CATEGORY_ICONS = {
    "bug": "🐞",
    "security": "🔐",
    "performance": "⚡",
    "style": "🎨",
    "logic": "🧠",
    "general": "📌",
}

SEVERITY_ORDER = ["error", "warning", "info"]


def format_gitlab_review_comment(
    mr_iid: int,
    mr_title: str,
    findings: list[dict],
    summary: dict,
    model_used: str,
) -> str:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for finding in findings:
        severity = str(finding.get("severity", "info")).lower()
        if severity not in SEVERITY_META:
            severity = "info"
        grouped[severity].append(finding)

    lines = [
        "## AI Code Review Summary (for preview only)",
        "",
        f"- Errors: {summary.get('errors', 0)}",
        f"- Warnings: {summary.get('warnings', 0)}",
        f"- Info: {summary.get('info', 0)}",
        f"- Total findings: {summary.get('total', 0)}",
    ]

    files_total = summary.get("files_total")
    files_analyzed = summary.get("files_analyzed")
    if files_total is not None and files_analyzed is not None:
        lines.append(f"- Files analyzed: {files_analyzed}/{files_total}")
    if summary.get("truncated"):
        lines.append("- Diff was truncated before analysis")

    lines.append("")

    if not findings:
        lines.append("No notable issues were found in the analyzed diff.")
        return "\n".join(lines)

    for severity in SEVERITY_ORDER:
        items = grouped.get(severity, [])
        if not items:
            continue
        meta = SEVERITY_META[severity]
        lines.extend([
            f"### {meta['emoji']} {meta['label']} ({len(items)})",
            "",
        ])
        for index, finding in enumerate(items, start=1):
            file_path = finding.get("file_path") or "unknown file"
            line = finding.get("line")
            location = f"`{file_path}:{line}`" if line else f"`{file_path}`"
            category = str(finding.get("category", "general")).lower()
            category_icon = CATEGORY_ICONS.get(category, CATEGORY_ICONS["general"])
            message = finding.get("message", "").strip()
            suggestion = (finding.get("suggestion") or "").strip()
            lines.append(f"{index}. {location} {category_icon} {message}")
            if suggestion:
                lines.append(f"   💡 Suggestion:")
                lines.append(f"      {suggestion}")
        lines.append("")

    return "\n".join(lines)
