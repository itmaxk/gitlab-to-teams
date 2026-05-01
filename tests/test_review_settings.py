import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from models import ReviewSettingsUpdate
from routers import review


def test_review_settings_include_persistent_review_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-settings.db")
    db.init_db()
    db.seed_review_settings()

    settings = review.get_settings()

    assert settings["system_prompt"]
    assert settings["review_instructions"] == ""


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
