import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from services.rules_engine import _extract_file_references, evaluate_rules_for_mr


def test_extract_file_references_supports_handlebars():
    refs = _extract_file_references(
        "Templates: `user-card.handlebars`, migrate_001.sql, readme.md"
    )

    assert "user-card.handlebars" in refs
    assert "migrate_001.sql" in refs
    assert "readme.md" in refs


def test_rule_matches_model_change_without_postgres_script(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM notification_rules WHERE name = ?",
        ("MR changed model without postgres script",),
    ).fetchone()
    conn.close()

    assert row is not None

    async def get_content(_: str) -> str:
        return "class CustomerModel: pass"

    matches = asyncio.run(
        evaluate_rules_for_mr(
            [row["id"]],
            ["model/customer/profile.py"],
            get_content,
        )
    )

    assert len(matches) == 1
    assert matches[0]["file_path"] == "model/customer/profile.py"
    assert matches[0]["rule"]["file_check_mode"] == "absent_any"


def test_rule_skips_when_postgres_script_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM notification_rules WHERE name = ?",
        ("MR changed model without postgres script",),
    ).fetchone()
    conn.close()

    assert row is not None

    async def get_content(_: str) -> str:
        return "class CustomerModel: pass"

    matches = asyncio.run(
        evaluate_rules_for_mr(
            [row["id"]],
            [
                "model/customer/profile.py",
                "database/postgres/migration/20260330_alter_customer.sql",
            ],
            get_content,
        )
    )

    assert matches == []


def test_empty_contains_rule_does_not_fetch_every_matching_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, match_type,
           target_branch, mr_state, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Broad path only rule",
            "Matches by changed file path without reading every file body",
            1,
            "configuration/**",
            "",
            "contains",
            "master",
            "opened",
            0,
        ),
    )
    rule_id = conn.execute(
        "SELECT id FROM notification_rules WHERE name = ?",
        ("Broad path only rule",),
    ).fetchone()["id"]
    conn.commit()
    conn.close()

    fetched_files = []

    async def get_content(file_path: str) -> str:
        fetched_files.append(file_path)
        return "content"

    matches = asyncio.run(
        evaluate_rules_for_mr(
            [rule_id],
            [
                "configuration/apply.js",
                "configuration/mapping.js",
                "README.md",
            ],
            get_content,
        )
    )

    assert [match["file_path"] for match in matches] == [
        "configuration/apply.js",
        "configuration/mapping.js",
    ]
    assert fetched_files == []
