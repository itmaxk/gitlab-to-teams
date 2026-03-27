import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notification_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            file_pattern TEXT DEFAULT 'changelogs/unreleased/*.md',
            content_match TEXT DEFAULT 'type: breaking',
            match_type TEXT DEFAULT 'contains',
            teams_webhook_url TEXT DEFAULT '',
            send_email INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS email_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            rule_id INTEGER NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
            UNIQUE(email, rule_id)
        );

        CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            mr_iid INTEGER,
            mr_title TEXT DEFAULT '',
            mr_url TEXT DEFAULT '',
            file_path TEXT DEFAULT '',
            file_content TEXT DEFAULT '',
            teams_sent INTEGER DEFAULT 0,
            email_sent INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );
    """)
    conn.close()


def seed_default_rule():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM notification_rules").fetchone()[0]
    if count == 0:
        conn.execute(
            """INSERT INTO notification_rules (name, description, file_pattern, content_match, match_type)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "Breaking Changes",
                "Уведомление о breaking changes при merge MR",
                "changelogs/unreleased/*.md",
                "type: breaking",
                "contains",
            ),
        )
        conn.commit()
    conn.close()
