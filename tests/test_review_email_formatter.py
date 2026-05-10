import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_comment_formatter import format_review_email_html


def test_format_review_email_html_renders_findings_table():
    html = format_review_email_html(
        {
            "mr_iid": 42,
            "mr_title": "Improve attributes",
            "mr_url": "https://gitlab.example/project/-/merge_requests/42",
            "model_used": "local-model",
            "summary": {"errors": 1, "warnings": 0, "info": 0, "total": 1},
            "findings": [
                {
                    "severity": "error",
                    "category": "logic",
                    "file_path": "config/attribute.json",
                    "line": 11,
                    "message": "Bad attribute",
                    "suggestion": "Fix attribute",
                }
            ],
        }
    )

    assert "<table" in html
    assert "Improve attributes" in html
    assert "Критичных: 1" in html
    assert "config/attribute.json:11" in html
    assert "Bad attribute" in html
    assert "Fix attribute" in html
