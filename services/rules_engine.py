import fnmatch
import re
from typing import Any, Callable, Awaitable

from db import get_db


async def evaluate_rules(
    changed_files: list[str],
    get_content: Callable[[str], Awaitable[str]],
) -> list[dict[str, Any]]:
    """
    Проверяет изменённые файлы на соответствие включённым правилам.
    Возвращает список совпадений: [{rule, file_path, file_content}, ...]
    """
    conn = get_db()
    rules = conn.execute(
        """SELECT r.*, GROUP_CONCAT(e.email) as emails
           FROM notification_rules r
           LEFT JOIN email_recipients e ON e.rule_id = r.id
           WHERE r.enabled = 1
           GROUP BY r.id"""
    ).fetchall()
    conn.close()

    results = []
    content_cache: dict[str, str] = {}

    for rule in rules:
        pattern = rule["file_pattern"]
        for file_path in changed_files:
            if not fnmatch.fnmatch(file_path, pattern):
                continue

            if file_path not in content_cache:
                try:
                    content_cache[file_path] = await get_content(file_path)
                except Exception:
                    continue

            content = content_cache[file_path]
            if _match_content(content, rule["content_match"], rule["match_type"]):
                emails = rule["emails"].split(",") if rule["emails"] else []
                results.append({
                    "rule": dict(rule),
                    "file_path": file_path,
                    "file_content": content,
                    "emails": emails,
                })

    return results


def _match_content(content: str, match_value: str, match_type: str) -> bool:
    if match_type == "contains":
        return match_value in content
    elif match_type == "regex":
        return bool(re.search(match_value, content))
    elif match_type == "exact":
        return content.strip() == match_value.strip()
    return False
