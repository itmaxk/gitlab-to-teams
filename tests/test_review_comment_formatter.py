import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_comment_formatter import format_gitlab_review_comment


HEADER = "## \u0421\u0432\u043e\u0434\u043a\u0430 AI-\u0440\u0435\u0432\u044c\u044e \u043a\u043e\u0434\u0430"
WARNING_LABEL = "### \u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f (1)"
BUG_LABEL = "[\u0411\u0430\u0433]"
SUGGESTION_LABEL = "\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u044f:"
CLEAN_REVIEW = "\u0417\u0430\u043c\u0435\u0442\u043d\u044b\u0445 \u043f\u0440\u043e\u0431\u043b\u0435\u043c \u0432 \u043f\u0440\u043e\u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c diff \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
ANALYZED_FILES = "\u041f\u0440\u043e\u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0444\u0430\u0439\u043b\u043e\u0432: 3/5"
SKIPPED_FILES = "\u041f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e \u0444\u0430\u0439\u043b\u043e\u0432 \u0431\u0435\u0437 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e\u0433\u043e diff: 2"
INCOMPLETE_DIFF = "\u041d\u0435 \u0432\u0441\u0435 \u0444\u0430\u0439\u043b\u044b \u0438\u043c\u0435\u043b\u0438 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0439 diff \u0434\u043b\u044f \u0430\u043d\u0430\u043b\u0438\u0437\u0430"


def test_format_gitlab_review_comment_uses_russian_labels_and_suggestion():
    comment = format_gitlab_review_comment(
        mr_iid=42,
        mr_title="Improve review publishing",
        findings=[
            {
                "severity": "warning",
                "category": "bug",
                "file_path": "services/review.py",
                "line": 18,
                "message": "\u041f\u043e\u0442\u0435\u043d\u0446\u0438\u0430\u043b\u044c\u043d\u044b\u0439 \u043f\u0440\u043e\u043f\u0443\u0441\u043a \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u043d\u0430 null",
                "suggestion": "\u0414\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0437\u0430\u0449\u0438\u0442\u0443 \u043e\u0442 \u043f\u0443\u0441\u0442\u043e\u0433\u043e \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f \u043f\u0435\u0440\u0435\u0434 \u043e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435\u043c",
            }
        ],
        summary={
            "errors": 0,
            "warnings": 1,
            "info": 0,
            "total": 1,
            "files_total": 3,
            "files_analyzed": 3,
            "files_skipped": 0,
        },
        model_used="qwen2.5-coder:14b",
    )

    assert HEADER in comment
    assert WARNING_LABEL in comment
    assert BUG_LABEL in comment
    assert SUGGESTION_LABEL in comment
    assert "Warnings" not in comment
    assert "Suggestion:" not in comment


def test_format_gitlab_review_comment_handles_clean_review():
    comment = format_gitlab_review_comment(
        mr_iid=7,
        mr_title="Clean diff",
        findings=[],
        summary={"errors": 0, "warnings": 0, "info": 0, "total": 0},
        model_used="local-model",
    )

    assert CLEAN_REVIEW in comment
    assert "No notable issues were found" not in comment


def test_format_gitlab_review_comment_mentions_skipped_files():
    comment = format_gitlab_review_comment(
        mr_iid=8,
        mr_title="Partial diff",
        findings=[],
        summary={
            "errors": 0,
            "warnings": 0,
            "info": 0,
            "total": 0,
            "files_total": 5,
            "files_analyzed": 3,
            "files_skipped": 2,
            "truncated": True,
        },
        model_used="local-model",
    )

    assert ANALYZED_FILES in comment
    assert SKIPPED_FILES in comment
    assert INCOMPLETE_DIFF in comment
