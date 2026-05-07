import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from routers.rules import copy_rule


def test_copy_rule_preserves_content_exclude_and_teams_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    cur = conn.execute(
        """
        INSERT INTO notification_rules
          (name, description, enabled, file_pattern, content_match, content_exclude,
           match_type, target_branch, mr_state, action_type, send_teams, send_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Rule with local exclusions",
            "Copied rule should keep behavior-changing fields",
            1,
            "*.md",
            "type: breaking",
            "type:\\s*internal",
            "regex",
            "master",
            "opened",
            "notify",
            0,
            1,
        ),
    )
    rule_id = cur.lastrowid
    conn.commit()
    conn.close()

    copied = copy_rule(rule_id)

    assert copied["enabled"] is False
    assert copied["content_exclude"] == "type:\\s*internal"
    assert copied["send_teams"] is False
    assert copied["send_email"] is True
