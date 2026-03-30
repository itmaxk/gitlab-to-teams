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
