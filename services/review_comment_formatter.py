from collections import defaultdict


SEVERITY_META = {
    "error": {"label": "Критичные"},
    "warning": {"label": "Предупреждения"},
    "info": {"label": "Информация"},
}

CATEGORY_LABELS = {
    "bug": "Баг",
    "security": "Безопасность",
    "performance": "Производительность",
    "style": "Стиль",
    "logic": "Логика",
    "xlsx": "XLSX",
    "general": "Общее",
}

SEVERITY_ORDER = ["error", "warning", "info"]


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

    lines = [
        "## Сводка AI-ревью кода",
        "",
        f"- Критичных: {summary.get('errors', 0)}",
        f"- Предупреждений: {summary.get('warnings', 0)}",
        f"- Информационных: {summary.get('info', 0)}",
        f"- Всего замечаний: {summary.get('total', 0)}",
    ]

    files_total = summary.get("files_total")
    files_analyzed = summary.get("files_analyzed")
    if files_total is not None and files_analyzed is not None:
        lines.append(f"- Проанализировано файлов: {files_analyzed}/{files_total}")
    if summary.get("truncated"):
        lines.append("- Diff был сокращён перед анализом")

    lines.append("")

    if not findings:
        lines.append("Заметных проблем в проанализированном diff не найдено.")
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
            file_path = finding.get("file_path") or "неизвестный файл"
            line = finding.get("line")
            location = f"`{file_path}:{line}`" if line else f"`{file_path}`"
            category = _translate_category(finding.get("category"))
            message = str(finding.get("message", "")).strip()
            suggestion = str(finding.get("suggestion") or "").strip()
            lines.append(f"{index}. {location} [{category}] {message}")
            if suggestion:
                lines.append("   Рекомендация:")
                lines.append(f"   {suggestion}")
        lines.append("")

    return "\n".join(lines)
