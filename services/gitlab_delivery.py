from __future__ import annotations

from collections import defaultdict
from typing import Any

from services.gitlab_notes import post_merge_request_discussion, post_merge_request_note


class _SafeTemplateContext(defaultdict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_gitlab_message(template: str, context: dict[str, Any]) -> str:
    values = _SafeTemplateContext(str)
    values.update({key: "" if value is None else str(value) for key, value in context.items()})
    return (template or "").format_map(values).strip()


def gitlab_comment_mode(rule: dict[str, Any], default: str = "note") -> str:
    mode = str(rule.get("gitlab_comment_mode") or default or "note").strip().lower()
    if mode in {"thread", "resolve_thread", "resolvable_thread"}:
        mode = "discussion"
    return "discussion" if mode == "discussion" else "note"


async def publish_gitlab_message(
    mr_iid: int,
    body: str,
    mode: str = "note",
) -> dict:
    if gitlab_comment_mode({"gitlab_comment_mode": mode}) == "discussion":
        return await post_merge_request_discussion(mr_iid, body)
    return await post_merge_request_note(mr_iid, body)
