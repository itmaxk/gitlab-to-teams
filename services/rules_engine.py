import fnmatch
import json
import re
from typing import Any, Callable, Awaitable

from db import get_db

GLOBAL_TITLE_SKIP_PATTERN = re.compile(
    r"(Changelog for version|\[skip_changelog\]|\[release_version_release\]|\[prepare_release_candidate\])",
    re.IGNORECASE,
)


async def evaluate_rules_for_mr(
    rule_ids: list[int],
    changed_files: list[str],
    get_content: Callable[[str], Awaitable[str]],
    mr_title: str = "",
) -> list[dict[str, Any]]:
    """
    Проверяет изменённые файлы на соответствие указанным правилам.
    Возвращает список совпадений: [{rule, file_path, file_content}, ...]
    """
    if mr_title and GLOBAL_TITLE_SKIP_PATTERN.search(mr_title):
        return []

    conn = get_db()
    placeholders = ",".join("?" for _ in rule_ids)
    rules = conn.execute(
        f"""SELECT r.*, GROUP_CONCAT(e.email) as emails
           FROM notification_rules r
           LEFT JOIN email_recipients e ON e.rule_id = r.id
           WHERE r.id IN ({placeholders}) AND r.enabled = 1
           GROUP BY r.id""",
        rule_ids,
    ).fetchall()
    conn.close()

    results = []
    content_cache: dict[str, str] = {}

    async def read_content(file_path: str) -> str:
        if file_path not in content_cache:
            content_cache[file_path] = await get_content(file_path)
        return content_cache[file_path]

    for rule_row in rules:
        rule = dict(rule_row)
        title_exclude = rule.get("title_exclude") or ""
        if title_exclude and mr_title:
            try:
                if re.search(title_exclude, mr_title, re.IGNORECASE):
                    continue
            except re.error:
                pass
        pattern = rule["file_pattern"]
        for file_path in changed_files:
            if not fnmatch.fnmatch(file_path, pattern):
                continue

            content: str | None = None
            if _match_content_requires_file(rule["content_match"], rule["match_type"]):
                try:
                    content = await read_content(file_path)
                except Exception:
                    continue
                if not _match_content(content, rule["content_match"], rule["match_type"]):
                    continue

            # Исключение: если content_exclude (regex, case-insensitive) совпал — пропускаем
            content_exclude = rule.get("content_exclude") or ""
            if content_exclude:
                try:
                    if content is None:
                        content = await read_content(file_path)
                    if re.search(content_exclude, content, re.IGNORECASE | re.DOTALL):
                        continue
                except re.error:
                    pass
                except Exception:
                    continue

            # Проверка наличия файла из changelog в MR
            file_check_mode = rule.get("file_check_mode") or "present"
            if rule["file_check_enabled"] and rule["file_check_path_prefix"]:
                prefix = rule["file_check_path_prefix"].rstrip("/")
                if file_check_mode in {"present_any", "absent_any"}:
                    has_file_under_prefix = _has_file_under_prefix(changed_files, prefix)
                    if file_check_mode == "present_any" and not has_file_under_prefix:
                        continue
                    if file_check_mode == "absent_any" and has_file_under_prefix:
                        continue
                    referenced_files = []
                else:
                    if content is None:
                        try:
                            content = await read_content(file_path)
                        except Exception:
                            continue
                    referenced_files = _extract_file_references(content)
                file_found = False
                for ref_file in referenced_files:
                    full_path = f"{prefix}/{ref_file}"
                    if full_path in changed_files:
                        file_found = True
                        break

                if file_check_mode == "present":
                    # Пропускаем если файл НЕ найден (нужен найденный)
                    if not file_found and referenced_files:
                        continue
                elif file_check_mode == "absent":
                    # Пропускаем если файл найден ИЛИ нет ссылок (нужен ненайденный)
                    if file_found or not referenced_files:
                        continue

            emails = rule["emails"].split(",") if rule["emails"] else []
            results.append({
                "rule": rule,
                "file_path": file_path,
                "file_content": content or "",
                "emails": emails,
            })

    return results


def _match_content_requires_file(match_value: str, match_type: str) -> bool:
    if match_type == "contains" and not match_value:
        return False
    return True


def _match_content(content: str, match_value: str, match_type: str) -> bool:
    if match_type == "contains":
        return match_value in content
    elif match_type == "regex":
        return bool(re.search(match_value, content))
    elif match_type == "exact":
        return content.strip() == match_value.strip()
    elif match_type == "json_additional_props":
        return _check_json_missing_additional_properties(content)
    return False


def _check_json_missing_additional_properties(content: str) -> bool:
    """
    Проверяет JSON: если есть "type": "object", то на том же уровне
    должен быть "additionalProperties": false.
    Возвращает True (= нарушение найдено) если хотя бы один объект
    с "type": "object" не имеет "additionalProperties": false.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return False

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                if node.get("additionalProperties") is not False:
                    return True
            for value in node.values():
                if _walk(value):
                    return True
        elif isinstance(node, list):
            for item in node:
                if _walk(item):
                    return True
        return False

    return _walk(data)


def _has_file_under_prefix(changed_files: list[str], prefix: str) -> bool:
    normalized_prefix = prefix.rstrip("/")
    prefix_with_slash = f"{normalized_prefix}/"
    return any(
        file_path == normalized_prefix or file_path.startswith(prefix_with_slash)
        for file_path in changed_files
    )


def _extract_file_references(content: str) -> list[str]:
    """
    Извлекает ссылки на файлы из содержимого changelog.
    Ищет паттерны вида: fileName.sql, fileName.py и т.д.
    Поддерживает форматы: `fileName.sql`, fileName.sql в строке.
    """
    # Ищем имена файлов с расширениями (в бэктиках или просто в тексте)
    pattern = r'`?([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)`?'
    matches = re.findall(pattern, content)
    # Фильтруем только файлы с типичными расширениями (не type: breaking и т.п.)
    extensions = {
        "sql", "py", "js", "ts", "sh", "yml", "yaml", "json", "xml",
        "csv", "txt", "md", "html", "handlebars", "css", "java", "go", "rs", "rb",
    }
    return [m for m in matches if m.rsplit(".", 1)[-1].lower() in extensions]
