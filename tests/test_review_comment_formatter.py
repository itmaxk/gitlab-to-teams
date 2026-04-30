import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_comment_formatter import format_gitlab_review_comment


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
                "message": "Потенциальный пропуск проверки на null",
                "suggestion": "Добавьте защиту от пустого значения перед обращением",
            }
        ],
        summary={
            "errors": 0,
            "warnings": 1,
            "info": 0,
            "total": 1,
            "files_total": 3,
            "files_analyzed": 3,
        },
        model_used="qwen2.5-coder:14b",
    )

    assert "## Сводка AI-ревью кода" in comment
    assert "### Предупреждения (1)" in comment
    assert "[Баг] Потенциальный пропуск проверки на null" in comment
    assert "Рекомендация:" in comment
    assert "Добавьте защиту от пустого значения перед обращением" in comment
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

    assert "Заметных проблем в проанализированном diff не найдено." in comment
    assert "No notable issues were found" not in comment
