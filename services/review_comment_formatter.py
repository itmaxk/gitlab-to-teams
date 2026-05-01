from collections import defaultdict


SEVERITY_META = {
    "error": {"label": "\u041a\u0440\u0438\u0442\u0438\u0447\u043d\u044b\u0435"},
    "warning": {"label": "\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f"},
    "info": {"label": "\u0418\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u044f"},
}

CATEGORY_LABELS = {
    "bug": "\u0411\u0430\u0433",
    "security": "\u0411\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u044c",
    "performance": "\u041f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u044c",
    "style": "\u0421\u0442\u0438\u043b\u044c",
    "logic": "\u041b\u043e\u0433\u0438\u043a\u0430",
    "xlsx": "XLSX",
    "general": "\u041e\u0431\u0449\u0435\u0435",
}

SEVERITY_ORDER = ["error", "warning", "info"]

HEADER = "## \u0421\u0432\u043e\u0434\u043a\u0430 AI-\u0440\u0435\u0432\u044c\u044e \u043a\u043e\u0434\u0430"
XLSX_HEADER = "## XLSX Analyzer"
UNKNOWN_FILE = "\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0444\u0430\u0439\u043b"
SUGGESTION_LABEL = "\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u044f:"
CLEAN_REVIEW = "\u0417\u0430\u043c\u0435\u0442\u043d\u044b\u0445 \u043f\u0440\u043e\u0431\u043b\u0435\u043c \u0432 \u043f\u0440\u043e\u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c diff \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
ANALYZED_FILES = "\u041f\u0440\u043e\u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0444\u0430\u0439\u043b\u043e\u0432"
SKIPPED_FILES = "\u041f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e \u0444\u0430\u0439\u043b\u043e\u0432 \u0431\u0435\u0437 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e\u0433\u043e diff"
INCOMPLETE_DIFF = "\u041d\u0435 \u0432\u0441\u0435 \u0444\u0430\u0439\u043b\u044b \u0438\u043c\u0435\u043b\u0438 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0439 diff \u0434\u043b\u044f \u0430\u043d\u0430\u043b\u0438\u0437\u0430"


def _normalize_severity(value: str | None) -> str:
    severity = str(value or "info").lower()
    return severity if severity in SEVERITY_META else "info"


def _translate_category(value: str | None) -> str:
    category = str(value or "general").lower()
    return CATEGORY_LABELS.get(category, CATEGORY_LABELS["general"])


def format_gitlab_review_comment(
    mr_iid: int,
    mr_title: str,
    findings: list[dict],
    summary: dict,
    model_used: str,
) -> str:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for finding in findings:
        grouped[_normalize_severity(finding.get("severity"))].append(finding)

    is_xlsx_review = str(model_used or "").startswith("xlsx-diff:")
    header = XLSX_HEADER if is_xlsx_review else HEADER

    lines = [
        header,
        "",
        f"- \u041a\u0440\u0438\u0442\u0438\u0447\u043d\u044b\u0445: {summary.get('errors', 0)}",
        f"- \u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0439: {summary.get('warnings', 0)}",
        f"- \u0418\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u043e\u043d\u043d\u044b\u0445: {summary.get('info', 0)}",
        f"- \u0412\u0441\u0435\u0433\u043e \u0437\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u0439: {summary.get('total', 0)}",
    ]

    files_total = summary.get("files_total")
    files_analyzed = summary.get("files_analyzed")
    if files_total is not None and files_analyzed is not None:
        lines.append(f"- {ANALYZED_FILES}: {files_analyzed}/{files_total}")

    files_skipped = summary.get("files_skipped")
    if files_skipped:
        lines.append(f"- {SKIPPED_FILES}: {files_skipped}")

    if summary.get("truncated"):
        lines.append(f"- {INCOMPLETE_DIFF}")

    lines.append("")

    if not findings:
        lines.append(CLEAN_REVIEW)
        return "\n".join(lines)

    for severity in SEVERITY_ORDER:
        items = grouped.get(severity, [])
        if not items:
            continue
        lines.extend([
            f"### {SEVERITY_META[severity]['label']} ({len(items)})",
            "",
        ])
        for index, finding in enumerate(items, start=1):
            file_path = finding.get("file_path") or UNKNOWN_FILE
            line = finding.get("line")
            location = f"`{file_path}:{line}`" if line else f"`{file_path}`"
            category = _translate_category(finding.get("category"))
            message = str(finding.get("message", "")).strip()
            suggestion = str(finding.get("suggestion") or "").strip()
            lines.append(f"{index}. {location} [{category}] {message}")
            if suggestion:
                lines.append(f"   {SUGGESTION_LABEL}")
                lines.append(f"   {suggestion}")
        lines.append("")

    return "\n".join(lines)
