import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_comment_formatter import format_gitlab_review_comment


def test_format_gitlab_review_comment_includes_signature_and_summary():
    comment = format_gitlab_review_comment(
        mr_iid=42,
        mr_title="Improve review publishing",
        findings=[
            {
                "severity": "warning",
                "category": "bug",
                "file_path": "services/review.py",
                "line": 18,
                "message": "Potential null handling gap",
                "suggestion": "Guard empty value before access",
            }
        ],
        summary={"errors": 0, "warnings": 1, "info": 0, "total": 1, "files_total": 3, "files_analyzed": 3},
        model_used="qwen2.5-coder:14b",
    )

    assert "## AI Code Review Summary" in comment
    assert "Merge Request: !42 Improve review publishing" in comment
    assert "### 🟠 Warnings (1)" in comment
    assert "Suggestion: Guard empty value before access" in comment
    assert "_AI (for preview only)_" in comment


def test_format_gitlab_review_comment_handles_clean_review():
    comment = format_gitlab_review_comment(
        mr_iid=7,
        mr_title="Clean diff",
        findings=[],
        summary={"errors": 0, "warnings": 0, "info": 0, "total": 0},
        model_used="local-model",
    )

    assert "No notable issues were found" in comment
    assert "_AI (for preview only)_" in comment
