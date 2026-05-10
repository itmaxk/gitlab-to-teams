import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_comment_formatter import format_gitlab_finding_discussion


SUGGESTION_LABEL = "\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u044f:"
RESOLVE_REQUEST = "\u041f\u043e\u0441\u043b\u0435 \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0430\u0436\u043c\u0438\u0442\u0435 Resolve"


def test_format_gitlab_finding_discussion_requests_resolve_for_single_finding():
    comment = format_gitlab_finding_discussion(
        {
            "severity": "error",
            "category": "logic",
            "file_path": "configuration/@config-rgsl/attr/configuration.json",
            "line": 27,
            "message": "\u0410\u0442\u0440\u0438\u0431\u0443\u0442 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0435\u0442 \u043d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 type.",
            "suggestion": "\u0417\u0430\u043c\u0435\u043d\u0438\u0442\u0435 type \u043d\u0430 string.",
        },
        finding_number=3,
        model_used="local-model",
    )

    assert comment.startswith("## AI review finding")
    assert "- \u0417\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u0435: #3" in comment
    assert "- \u0410\u0442\u0440\u0438\u0431\u0443\u0442: `configuration/@config-rgsl/attr/configuration.json:27`" in comment
    assert f"**{SUGGESTION_LABEL}**" in comment
    assert RESOLVE_REQUEST in comment
