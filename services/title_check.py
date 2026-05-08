import re

_TITLE_ERROR_MSG = "MR Title должен иметь формат JIRA-TASK: Description with some text"
_JIRA_TASK_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+: \S.*")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_RELEASE_NUM_RE = re.compile(r"(\d+)")


def is_title_valid(title: str, target_branch: str = "master") -> tuple[bool, str]:
    title = title.strip()

    if title.lower().startswith("draft"):
        return True, ""

    if _CYRILLIC_RE.search(title):
        return False, _TITLE_ERROR_MSG

    if not _JIRA_TASK_RE.match(title):
        return False, _TITLE_ERROR_MSG

    if target_branch and target_branch not in ("master", "*"):
        release_match = _RELEASE_NUM_RE.search(target_branch)
        if release_match:
            release_num = release_match.group(1)
            if not re.search(rf"{release_num}\s*\)?\s*$", title):
                return False, (
                    f"{_TITLE_ERROR_MSG} "
                    f"(отсутствует номер релиза {release_num} для ветки {target_branch})"
                )

    return True, ""
