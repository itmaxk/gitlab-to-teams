import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from models import ReviewInstructionItemCreate, ReviewInstructionItemUpdate, ReviewSettingsUpdate
from routers import review


def test_review_settings_include_persistent_review_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-settings.db")
    db.init_db()
    db.seed_review_settings()

    settings = review.get_settings()

    assert settings["system_prompt"]
    assert settings["review_instructions"] == ""
    assert settings["review_instruction_items"] == []


def test_update_review_settings_persists_review_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-settings.db")
    db.init_db()
    db.seed_review_settings()

    review.update_settings(
        ReviewSettingsUpdate(
            system_prompt="System prompt",
            review_instructions="Focus on business rules.\nIgnore formatting.",
        )
    )

    settings = review.get_settings()

    assert settings["system_prompt"] == "System prompt"
    assert settings["review_instructions"] == "Focus on business rules.\nIgnore formatting."


def test_review_instruction_items_support_crud_and_legacy_text(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-instruction-items.db")
    db.init_db()
    db.seed_review_settings()

    created = review.create_instruction_item(
        ReviewInstructionItemCreate(
            instruction_text="Check business rules",
            instruction_type="include",
        )
    )
    review.create_instruction_item(
        ReviewInstructionItemCreate(
            instruction_text="Ignore formatting",
            instruction_type="exclude",
        )
    )
    review.update_instruction_item(
        created["id"],
        ReviewInstructionItemUpdate(
            instruction_text="Check critical business rules",
            instruction_type="include",
        ),
    )

    settings = review.get_settings()

    assert len(settings["review_instruction_items"]) == 2
    assert "Check critical business rules" in settings["review_instructions"]
    assert "Ignore formatting" in settings["review_instructions"]

    review.delete_instruction_item(created["id"])
    remaining = review.get_instruction_items()

    assert len(remaining) == 1
    assert remaining[0]["instruction_type"] == "exclude"
