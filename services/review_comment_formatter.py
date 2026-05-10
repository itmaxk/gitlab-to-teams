from collections import defaultdict
from html import escape


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

CATEGORY_ICONS = {
    "bug": "\U0001f41e",
    "security": "\U0001f512",
    "performance": "\u26a1",
    "style": "\U0001f58c\ufe0f",
    "logic": "\U0001f9e0",
    "xlsx": "\U0001f4ca",
    "general": "\U0001f4cc",
}

SEVERITY_ORDER = ["error", "warning", "info"]

HEADER = "## AI (for preview only)"
XLSX_HEADER = "## XLSX Analyzer"
UNKNOWN_FILE = "\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0444\u0430\u0439\u043b"
SUGGESTION_LABEL = "\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u044f:"
CLEAN_REVIEW = "\u0417\u0430\u043c\u0435\u0442\u043d\u044b\u0445 \u043f\u0440\u043e\u0431\u043b\u0435\u043c \u0432 \u043f\u0440\u043e\u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c diff \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
ANALYZED_FILES = "\u041f\u0440\u043e\u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0444\u0430\u0439\u043b\u043e\u0432"
SKIPPED_FILES = "\u041f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e \u0444\u0430\u0439\u043b\u043e\u0432 \u0431\u0435\u0437 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e\u0433\u043e diff"
INCOMPLETE_DIFF = "\u041d\u0435 \u0432\u0441\u0435 \u0444\u0430\u0439\u043b\u044b \u0438\u043c\u0435\u043b\u0438 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0439 diff \u0434\u043b\u044f \u0430\u043d\u0430\u043b\u0438\u0437\u0430"
XLSX_ROWS_DETAILS = "\u0421\u0442\u0440\u043e\u043a\u0438 XLSX"
XLSX_ROW_COLUMN = "\u0421\u0442\u0440\u043e\u043a\u0430"
FINDING_DISCUSSION_HEADER = "## AI review finding"
RESOLVE_REQUEST = "\u041f\u043e\u0441\u043b\u0435 \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0430\u0436\u043c\u0438\u0442\u0435 Resolve \u0432 \u044d\u0442\u043e\u0439 \u043d\u0438\u0442\u0438."

EMAIL_SEVERITY_META = {
    "error": {"label": "\u041a\u0440\u0438\u0442\u0438\u0447\u043d\u043e", "bg": "#fee2e2", "text": "#991b1b", "border": "#fecaca"},
    "warning": {"label": "\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0435", "bg": "#fef3c7", "text": "#92400e", "border": "#fde68a"},
    "info": {"label": "\u0418\u043d\u0444\u043e", "bg": "#dbeafe", "text": "#1e40af", "border": "#bfdbfe"},
}


def _escape_markdown_table_cell(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", "<br>")
    return text.replace("|", "\\|").strip()


def _column_name_to_number(name: str) -> int:
    result = 0
    for char in name.upper():
        if "A" <= char <= "Z":
            result = result * 26 + (ord(char) - ord("A") + 1)
    return result


def _format_xlsx_rows_table(rows: list[dict]) -> list[str]:
    columns = sorted({
        str(cell.get("column") or "")
        for row in rows
        for cell in row.get("cells", [])
        if cell.get("column")
    }, key=_column_name_to_number)
    if not rows or not columns:
        return []

    lines = [
        "<details>",
        f"<summary>{XLSX_ROWS_DETAILS}</summary>",
        "",
        "| " + " | ".join([XLSX_ROW_COLUMN, *columns]) + " |",
        "| " + " | ".join(["---", *["---" for _ in columns]]) + " |",
    ]
    for row in rows:
        values = {
            str(cell.get("column") or ""): _escape_markdown_table_cell(cell.get("value"))
            for cell in row.get("cells", [])
        }
        row_number = _escape_markdown_table_cell(row.get("row"))
        lines.append("| " + " | ".join([row_number, *[values.get(column, "") for column in columns]]) + " |")
    lines.extend(["", "</details>"])
    return lines


def _normalize_severity(value: str | None) -> str:
    severity = str(value or "info").lower()
    return severity if severity in SEVERITY_META else "info"


def _translate_category(value: str | None) -> str:
    category = str(value or "general").lower()
    return CATEGORY_LABELS.get(category, CATEGORY_LABELS["general"])


def _category_icon(value: str | None) -> str:
    category = str(value or "general").lower()
    return CATEGORY_ICONS.get(category, CATEGORY_ICONS["general"])


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
    ]

    lines.extend([
        f"- \u041a\u0440\u0438\u0442\u0438\u0447\u043d\u044b\u0445: {summary.get('errors', 0)}",
        f"- \u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0439: {summary.get('warnings', 0)}",
        f"- \u0418\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u043e\u043d\u043d\u044b\u0445: {summary.get('info', 0)}",
        f"- \u0412\u0441\u0435\u0433\u043e \u0437\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u0439: {summary.get('total', 0)}",
    ])

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
            category_icon = _category_icon(finding.get("category"))
            message = str(finding.get("message", "")).strip()
            suggestion = str(finding.get("suggestion") or "").strip()
            lines.extend([
                f"{index}. {location}",
                f"   {category_icon} **{category}**",
                "",
                f"   {message}",
            ])
            if suggestion:
                lines.append("")
                lines.append(f"   **{SUGGESTION_LABEL}**")
                lines.append(f"   {suggestion}")
            xlsx_rows = finding.get("xlsx_rows") or []
            if xlsx_rows:
                lines.append("")
                lines.extend(f"   {line}" if line else "" for line in _format_xlsx_rows_table(xlsx_rows))
            lines.append("")
        lines.append("")

    return "\n".join(lines)


def format_gitlab_finding_discussion(
    finding: dict,
    *,
    finding_number: int,
    model_used: str,
) -> str:
    file_path = finding.get("file_path") or UNKNOWN_FILE
    line = finding.get("line")
    location = f"`{file_path}:{line}`" if line else f"`{file_path}`"
    severity = _normalize_severity(finding.get("severity"))
    category = _translate_category(finding.get("category"))
    category_icon = _category_icon(finding.get("category"))
    message = str(finding.get("message", "")).strip()
    suggestion = str(finding.get("suggestion") or "").strip()

    lines = [
        FINDING_DISCUSSION_HEADER,
        "",
        f"- \u0417\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u0435: #{finding_number}",
        f"- \u0421\u0435\u0440\u044c\u0435\u0437\u043d\u043e\u0441\u0442\u044c: {SEVERITY_META[severity]['label']}",
        f"- \u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f: {category_icon} {category}",
        f"- \u0410\u0442\u0440\u0438\u0431\u0443\u0442: {location}",
        f"- \u041c\u043e\u0434\u0435\u043b\u044c: `{model_used or '-'}'",
        "",
        message or "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u0437\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u044f \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e.",
    ]
    if suggestion:
        lines.extend([
            "",
            f"**{SUGGESTION_LABEL}**",
            suggestion,
        ])

    xlsx_rows = finding.get("xlsx_rows") or []
    if xlsx_rows:
        lines.append("")
        lines.extend(_format_xlsx_rows_table(xlsx_rows))

    lines.extend([
        "",
        RESOLVE_REQUEST,
    ])
    return "\n".join(lines)


def _summary_badge(label: str, value: object, bg: str, text: str, border: str) -> str:
    return (
        f'<span style="display:inline-block;margin:0 8px 8px 0;padding:7px 11px;'
        f'border-radius:8px;background:{bg};color:{text};border:1px solid {border};'
        f'font-size:13px;font-weight:700">{escape(label)}: {escape(str(value or 0))}</span>'
    )


def format_review_email_html(review: dict) -> str:
    findings = review.get("findings", []) or []
    summary = review.get("summary", {}) or {}
    mr_title = str(review.get("mr_title") or "")
    mr_url = str(review.get("mr_url") or "")
    model_used = str(review.get("model_used") or "")
    mr_iid = review.get("mr_iid") or ""

    rows = []
    for index, finding in enumerate(findings, start=1):
        severity = _normalize_severity(finding.get("severity"))
        severity_meta = EMAIL_SEVERITY_META[severity]
        category = _translate_category(finding.get("category"))
        file_path = str(finding.get("file_path") or UNKNOWN_FILE)
        line = finding.get("line")
        location = f"{file_path}:{line}" if line else file_path
        suggestion = str(finding.get("suggestion") or "").strip()
        rows.append(
            "<tr>"
            f'<td style="padding:12px 14px;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px">{index}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid #e2e8f0">'
            f'<span style="display:inline-block;padding:4px 8px;border-radius:999px;'
            f'background:{severity_meta["bg"]};color:{severity_meta["text"]};'
            f'border:1px solid {severity_meta["border"]};font-size:12px;font-weight:700">'
            f'{severity_meta["label"]}</span></td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid #e2e8f0;color:#334155;font-size:13px">{escape(category)}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid #e2e8f0;color:#475569;font-family:Consolas,Monaco,monospace;font-size:12px;word-break:break-all">{escape(location)}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid #e2e8f0;color:#0f172a;font-size:13px;line-height:1.45">{escape(str(finding.get("message") or ""))}</td>'
            f'<td style="padding:12px 14px;border-bottom:1px solid #e2e8f0;color:#166534;font-size:13px;line-height:1.45">{escape(suggestion) if suggestion else "&mdash;"}</td>'
            "</tr>"
        )

    if not rows:
        rows.append(
            '<tr><td colspan="6" style="padding:22px;text-align:center;color:#16a34a;'
            'border-bottom:1px solid #e2e8f0;font-size:14px">'
            f"{escape(CLEAN_REVIEW)}</td></tr>"
        )

    mr_link = (
        f'<a href="{escape(mr_url, quote=True)}" style="color:#2563eb;text-decoration:none;font-weight:700">!{escape(str(mr_iid))}</a>'
        if mr_url else f"!{escape(str(mr_iid))}"
    )

    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;background:#f8fafc;color:#0f172a;font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:1120px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
      <div style="padding:22px 24px;background:#0f172a;color:#ffffff">
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#93c5fd;font-weight:700">AI review</div>
        <h1 style="margin:8px 0 6px;font-size:22px;line-height:1.3">{mr_link} {escape(mr_title)}</h1>
        <div style="font-size:13px;color:#cbd5e1">Модель: {escape(model_used or "-")}</div>
      </div>
      <div style="padding:18px 24px 10px">
        {_summary_badge("Критичных", summary.get("errors", 0), "#fee2e2", "#991b1b", "#fecaca")}
        {_summary_badge("Предупреждений", summary.get("warnings", 0), "#fef3c7", "#92400e", "#fde68a")}
        {_summary_badge("Инфо", summary.get("info", 0), "#dbeafe", "#1e40af", "#bfdbfe")}
        {_summary_badge("Всего", summary.get("total", len(findings)), "#f1f5f9", "#334155", "#e2e8f0")}
      </div>
      <div style="padding:0 24px 24px;overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden">
          <thead>
            <tr style="background:#f1f5f9;color:#475569;text-align:left">
              <th style="padding:10px 14px;font-size:12px;border-bottom:1px solid #e2e8f0">#</th>
              <th style="padding:10px 14px;font-size:12px;border-bottom:1px solid #e2e8f0">Уровень</th>
              <th style="padding:10px 14px;font-size:12px;border-bottom:1px solid #e2e8f0">Категория</th>
              <th style="padding:10px 14px;font-size:12px;border-bottom:1px solid #e2e8f0">Атрибут</th>
              <th style="padding:10px 14px;font-size:12px;border-bottom:1px solid #e2e8f0">Ошибка</th>
              <th style="padding:10px 14px;font-size:12px;border-bottom:1px solid #e2e8f0">Рекомендация</th>
            </tr>
          </thead>
          <tbody>
            {"".join(rows)}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>"""
