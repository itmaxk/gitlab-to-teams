import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from models import (
    ReviewInstructionItemCreate,
    ReviewInstructionItemUpdate,
    ReviewProjectProfilePreviewRequest,
    ReviewProjectProfileRequest,
    ReviewSettingsUpdate,
)
from routers import review
from services.review_project_context import default_adinsure_profile_json


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


def test_review_project_profile_seed_and_active_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-profiles.db")
    db.init_db()
    db.seed_review_settings()

    profiles = review.list_project_profiles()
    settings = review.get_settings()

    assert len(profiles) == 1
    assert profiles[0]["name"] == "AdInsure implementation"
    assert profiles[0]["profile_json"]["rules"]
    assert settings["active_project_profile_id"] == profiles[0]["id"]


def test_review_project_profile_crud_validate_and_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-profile-crud.db")
    db.init_db()
    db.seed_review_settings()

    project_root = tmp_path / "impl"
    config_root = project_root / "configuration" / "@config-rgsl"
    provider_dir = config_root / "pkg" / "dataProvider" / "database" / "Provider"
    source_dir = config_root / "pkg" / "dataSource" / "Source"
    provider_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    (provider_dir / "configuration.json").write_text('{"version":"1"}', encoding="utf-8")
    (provider_dir / "query.postgres.handlebars").write_text("select 1", encoding="utf-8")
    (source_dir / "configuration.json").write_text(
        '{"dataProvider":{"codeName":"Provider","type":"DatabaseDataProvider"}}',
        encoding="utf-8",
    )

    profile_json = default_adinsure_profile_json()
    created = review.create_project_profile(
        ReviewProjectProfileRequest(
            name="Custom constructor",
            project_root=str(project_root),
            config_path="configuration/@config-rgsl",
            sql_target="PostgreSQL 18+",
            profile_json=profile_json,
        )
    )

    validation = review.validate_project_profile(
        created["id"],
        ReviewProjectProfileRequest(
            name="Custom constructor",
            project_root=str(project_root),
            profile_json=profile_json,
        ),
    )
    preview = review.preview_project_profile_context(
        created["id"],
        ReviewProjectProfilePreviewRequest(
            changed_paths=[
                "configuration/@config-rgsl/pkg/dataSource/Source/configuration.json",
            ]
        ),
    )
    active = review.set_active_project_profile(created["id"])

    assert validation["ok"] is True
    assert active["active_project_profile_id"] == created["id"]
    assert preview["sql_target"] == "PostgreSQL 18+"
    assert any("query.postgres.handlebars" in item["path"] for item in preview["related_files"])
