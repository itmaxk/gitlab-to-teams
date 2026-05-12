import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.title_check import is_title_valid

ERROR_MSG = "MR Title должен иметь формат JIRA-TASK: Short description"


def test_valid_jira_task_title():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Economic parameters", "master")
    assert valid is True
    assert msg == ""


def test_valid_jira_task_with_release_number():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Economic parameters R102", "release/102")
    assert valid is True
    assert msg == ""


def test_valid_jira_task_release_dash_rel():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Fix bug -rel102", "release/102")
    assert valid is True
    assert msg == ""


def test_valid_jira_task_release_parens():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Some text (R102)", "release/102")
    assert valid is True
    assert msg == ""


def test_valid_jira_task_release_full_word():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Some release102", "release/102")
    assert valid is True
    assert msg == ""


def test_valid_jira_task_release_square_bracket_rel():
    valid, msg = is_title_valid(
        "ADIRGSLSUPP-6764: ELLPVTB revert old code [rel102]",
        "release/102",
    )
    assert valid is True
    assert msg == ""


def test_draft_prefix_skip():
    valid, msg = is_title_valid("Draft: work in progress", "master")
    assert valid is True
    assert msg == ""


def test_draft_case_insensitive():
    valid, msg = is_title_valid("DRAFT something", "master")
    assert valid is True
    assert msg == ""


def test_draft_mixed_case():
    valid, msg = is_title_valid("draft: misc", "master")
    assert valid is True
    assert msg == ""


def test_cyrillic_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Исправление бага", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_cyrillic_single_letter_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Fix буква", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_missing_colon_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752 Economic parameters", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_space_before_colon_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752 : Economic parameters", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_missing_space_after_colon_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752:Economic parameters", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_multiple_spaces_after_colon_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752:  Economic parameters", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_missing_number_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP: Economic parameters", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_lowercase_project_rejected():
    valid, msg = is_title_valid("adirgslsupp-6752: Economic parameters", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_no_description_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: ", "master")
    assert valid is False
    assert ERROR_MSG in msg


def test_release_branch_missing_release_number():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Fix bug", "release/102")
    assert valid is False
    assert "102" in msg
    assert ERROR_MSG in msg


def test_release_branch_wrong_number():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Fix bug R99", "release/102")
    assert valid is False


def test_master_branch_no_release_required():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Fix bug", "master")
    assert valid is True
    assert msg == ""


def test_wildcard_branch_no_release_required():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Fix bug", "*")
    assert valid is True
    assert msg == ""


def test_wildcard_branch_with_cyrillic_rejected():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Исправление", "*")
    assert valid is False


def test_release_number_at_end_with_paren():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Desc (R102)", "release/102")
    assert valid is True


def test_release_number_at_end_with_space_and_paren():
    valid, msg = is_title_valid("ADIRGSLSUPP-6752: Desc (R102) ", "release/102")
    assert valid is True
