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
            target_branch TEXT DEFAULT 'master',
            mr_state TEXT DEFAULT 'merged',
            poll_interval_seconds INTEGER DEFAULT 0,
            content_exclude TEXT DEFAULT '',
            file_check_enabled INTEGER DEFAULT 0,
            file_check_path_prefix TEXT DEFAULT '',
            file_check_mode TEXT DEFAULT 'present',
            action_type TEXT DEFAULT 'notify',
            send_teams INTEGER DEFAULT 1,
            teams_webhook_url TEXT DEFAULT '',
            send_email INTEGER DEFAULT 0,
            send_gitlab INTEGER DEFAULT 0,
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
            gitlab_sent INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS processed_mrs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            mr_iid INTEGER NOT NULL,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
            UNIQUE(rule_id, mr_iid)
        );

        CREATE TABLE IF NOT EXISTS cherry_pick_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            target_branch TEXT NOT NULL,
            mr_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cherry_pick_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            mr_iid INTEGER NOT NULL,
            mr_title TEXT DEFAULT '',
            mr_url TEXT DEFAULT '',
            author TEXT DEFAULT '',
            merged_at TEXT DEFAULT '',
            merge_commit_sha TEXT DEFAULT '',
            cherry_pick_branch TEXT DEFAULT '',
            mr_create_url TEXT DEFAULT '',
            cherry_pick_mr_url TEXT DEFAULT '',
            cherry_pick_merged_at TEXT DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES cherry_pick_sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS jira_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL UNIQUE,
            display_name TEXT DEFAULT '',
            email_address TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS report_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL UNIQUE,
            auto_send_enabled INTEGER DEFAULT 0,
            auto_send_day INTEGER DEFAULT 1,
            auto_send_time TEXT DEFAULT '09:00',
            auto_send_schedules TEXT DEFAULT '',
            send_email INTEGER DEFAULT 0,
            email_recipients TEXT DEFAULT '',
            teams_webhook_url TEXT DEFAULT '',
            missing_time_auto_notify INTEGER DEFAULT 0,
            missing_time_interval_days INTEGER DEFAULT 0,
            last_auto_sent_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_vacations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            note TEXT DEFAULT '',
            FOREIGN KEY (account_id) REFERENCES jira_users(account_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS holiday_overrides (
            dt TEXT PRIMARY KEY,
            day_type INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS review_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            system_prompt TEXT NOT NULL,
            review_instructions TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS code_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mr_iid INTEGER NOT NULL,
            mr_title TEXT DEFAULT '',
            mr_url TEXT DEFAULT '',
            model_used TEXT DEFAULT '',
            custom_prompt TEXT DEFAULT '',
            findings_json TEXT DEFAULT '[]',
            summary_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS saved_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module TEXT NOT NULL,
            name TEXT NOT NULL,
            mr_ids TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS polled_mrs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mr_iid INTEGER NOT NULL,
            mr_title TEXT DEFAULT '',
            mr_url TEXT DEFAULT '',
            mr_state TEXT DEFAULT '',
            mr_author TEXT DEFAULT '',
            source_branch TEXT DEFAULT '',
            target_branch TEXT DEFAULT '',
            mr_created_at TEXT DEFAULT '',
            changed_files_count INTEGER DEFAULT 0,
            rules_checked INTEGER DEFAULT 0,
            rules_matched INTEGER DEFAULT 0,
            success INTEGER DEFAULT 1,
            error TEXT DEFAULT '',
            polled_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    _migrate(conn)
    _seed_rule_if_missing(
        conn,
        {
            "name": "MR changed model without postgres script",
            "description": "If files inside model subfolders changed and there is no SQL script under database/postgres, notify that a table migration script is required",
            "file_pattern": "model/*/*",
            "content_match": "",
            "match_type": "contains",
            "target_branch": "master",
            "mr_state": "opened",
            "file_check_enabled": 1,
            "file_check_path_prefix": "database/postgres",
            "file_check_mode": "absent_any",
            "send_teams": 1,
            "send_email": 1,
            "enabled": 1,
        },
    )

    conn.close()


def _migrate(conn: sqlite3.Connection):
    """Добавляет новые колонки если БД уже существовала."""
    cursor = conn.execute("PRAGMA table_info(notification_rules)")
    columns = {row[1] for row in cursor.fetchall()}

    migrations = {
        "target_branch": "ALTER TABLE notification_rules ADD COLUMN target_branch TEXT DEFAULT 'master'",
        "mr_state": "ALTER TABLE notification_rules ADD COLUMN mr_state TEXT DEFAULT 'merged'",
        "poll_interval_seconds": "ALTER TABLE notification_rules ADD COLUMN poll_interval_seconds INTEGER DEFAULT 0",
        "file_check_enabled": "ALTER TABLE notification_rules ADD COLUMN file_check_enabled INTEGER DEFAULT 0",
        "file_check_path_prefix": "ALTER TABLE notification_rules ADD COLUMN file_check_path_prefix TEXT DEFAULT ''",
        "content_exclude": "ALTER TABLE notification_rules ADD COLUMN content_exclude TEXT DEFAULT ''",
        "file_check_mode": "ALTER TABLE notification_rules ADD COLUMN file_check_mode TEXT DEFAULT 'present'",
        "send_teams": "ALTER TABLE notification_rules ADD COLUMN send_teams INTEGER DEFAULT 1",
        "send_gitlab": "ALTER TABLE notification_rules ADD COLUMN send_gitlab INTEGER DEFAULT 0",
        "action_type": "ALTER TABLE notification_rules ADD COLUMN action_type TEXT DEFAULT 'notify'",
        "seed_key": "ALTER TABLE notification_rules ADD COLUMN seed_key TEXT DEFAULT ''",
    }

    need_seed_key_backfill = "seed_key" not in columns
    for col, sql in migrations.items():
        if col not in columns:
            conn.execute(sql)

    if need_seed_key_backfill:
        seed_key_map = {
            "Breaking Changes": "breaking_changes",
            "Changelog должен быть breaking": "changelog_should_be_breaking",
            "MR нет инструкции breaking": "mr_no_breaking_instruction",
            "MR не найден файл": "mr_file_not_found",
        }
        for name, key in seed_key_map.items():
            conn.execute(
                "UPDATE notification_rules SET seed_key = ? WHERE name = ? AND (seed_key = '' OR seed_key IS NULL)",
                (key, name),
            )

    # Миграция cherry_pick_items
    try:
        cursor2 = conn.execute("PRAGMA table_info(cherry_pick_items)")
        cp_columns = {row[1] for row in cursor2.fetchall()}
        if cp_columns and "cherry_pick_mr_url" not in cp_columns:
            conn.execute(
                "ALTER TABLE cherry_pick_items ADD COLUMN cherry_pick_mr_url TEXT DEFAULT ''"
            )
        if cp_columns and "cherry_pick_merged_at" not in cp_columns:
            conn.execute(
                "ALTER TABLE cherry_pick_items ADD COLUMN cherry_pick_merged_at TEXT DEFAULT ''"
            )
    except Exception:
        pass

    # Миграция cherry_pick_sessions
    try:
        cursor3 = conn.execute("PRAGMA table_info(cherry_pick_sessions)")
        cs_columns = {row[1] for row in cursor3.fetchall()}
        if cs_columns and "name" not in cs_columns:
            conn.execute(
                "ALTER TABLE cherry_pick_sessions ADD COLUMN name TEXT DEFAULT ''"
            )
    except Exception:
        pass

    # Миграция report_settings — добавить auto_send_schedules
    try:
        cursor_rs = conn.execute("PRAGMA table_info(report_settings)")
        rs_columns = {row[1] for row in cursor_rs.fetchall()}
        if rs_columns and "auto_send_schedules" not in rs_columns:
            conn.execute(
                "ALTER TABLE report_settings ADD COLUMN auto_send_schedules TEXT DEFAULT ''"
            )
    except Exception:
        pass

    try:
        cursor_review = conn.execute("PRAGMA table_info(review_settings)")
        review_columns = {row[1] for row in cursor_review.fetchall()}
        if review_columns and "review_instructions" not in review_columns:
            conn.execute(
                "ALTER TABLE review_settings ADD COLUMN review_instructions TEXT DEFAULT ''"
            )
    except Exception:
        pass

    try:
        cursor_nl = conn.execute("PRAGMA table_info(notification_log)")
        nl_columns = {row[1] for row in cursor_nl.fetchall()}
        if nl_columns and "gitlab_sent" not in nl_columns:
            conn.execute(
                "ALTER TABLE notification_log ADD COLUMN gitlab_sent INTEGER DEFAULT 0"
            )
    except Exception:
        pass

    conn.commit()


def seed_default_rule():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM notification_rules").fetchone()[0]
    if count == 0:
        conn.execute(
            """INSERT INTO notification_rules
               (name, description, file_pattern, content_match, match_type, target_branch, mr_state, seed_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "Breaking Changes",
                "Уведомление о breaking changes при merge MR",
                "changelogs/unreleased/*.md",
                "type: breaking",
                "contains",
                "master",
                "merged",
                "breaking_changes",
            ),
        )
        conn.commit()

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "changelog_should_be_breaking",
            "name": "Changelog должен быть breaking",
            "description": "Changelog содержит script/service/publish, но тип не breaking — нужно исправить на type: breaking",
            "file_pattern": "changelogs/unreleased/*.md",
            "content_match": r"(?i)(script|service|publish|паблиш)",
            "content_exclude": r"type:\s*breaking",
            "match_type": "regex",
            "target_branch": "master",
            "mr_state": "opened",
            "send_teams": 1,
            "send_email": 1,
            "enabled": 0,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "mr_no_breaking_instruction",
            "name": "MR нет инструкции breaking",
            "description": "Breaking change без ссылки на .sql файл или без упоминания etlService",
            "file_pattern": "changelogs/unreleased/*.md",
            "content_match": "type: breaking",
            "content_exclude": r"(?=.*\.sql)(?=.*etlservice)",
            "match_type": "contains",
            "target_branch": "master",
            "mr_state": "opened",
            "send_teams": 1,
            "send_email": 1,
            "enabled": 0,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "mr_file_not_found",
            "name": "MR не найден файл",
            "description": "Breaking change со ссылкой на .sql файл, но файл отсутствует в MR",
            "file_pattern": "changelogs/unreleased/*.md",
            "content_match": "type: breaking",
            "match_type": "contains",
            "target_branch": "master",
            "mr_state": "opened",
            "file_check_enabled": 1,
            "file_check_path_prefix": "database/postgres/migration",
            "file_check_mode": "absent",
            "send_teams": 1,
            "send_email": 1,
            "enabled": 0,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "auto_xlsx_review",
            "name": "XLSX ревью нового MR",
            "description": "Автоматический XLSX-ревью для новых MR (не merged, не Draft). Результат оставляется комментарием в GitLab.",
            "file_pattern": "*.xlsx",
            "content_match": "",
            "match_type": "contains",
            "target_branch": "master",
            "mr_state": "opened",
            "action_type": "xlsx_review",
            "send_gitlab": 1,
            "send_teams": 0,
            "send_email": 0,
            "enabled": 1,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "auto_code_review",
            "name": "Code review нового MR",
            "description": "Автоматическое AI-ревью кода для новых MR (не merged, не Draft). Результат оставляется комментарием в GitLab.",
            "file_pattern": "*",
            "content_match": "",
            "match_type": "contains",
            "target_branch": "master",
            "mr_state": "opened",
            "action_type": "code_review",
            "send_gitlab": 1,
            "send_teams": 0,
            "send_email": 0,
            "enabled": 1,
        },
    )

    conn.close()


def seed_report_settings():
    conn = get_db()
    for rt in ("time_logging", "overtime"):
        exists = conn.execute(
            "SELECT 1 FROM report_settings WHERE report_type = ?", (rt,)
        ).fetchone()
        if not exists:
            conn.execute("INSERT INTO report_settings (report_type) VALUES (?)", (rt,))
    conn.commit()
    conn.close()


DEFAULT_REVIEW_PROMPT = """You are an expert code reviewer. Analyze the provided merge request diff and identify potential issues.

Focus on:
- Bugs and logical errors
- Security vulnerabilities (SQL injection, XSS, hardcoded secrets, etc.)
- Performance issues
- Error handling problems
- Race conditions and concurrency issues

Do NOT focus on:
- Code style or formatting
- Minor naming suggestions
- Adding comments or documentation

Respond ONLY with a valid JSON array of findings. Each finding must have this structure:
{
  "severity": "error" | "warning" | "info",
  "category": "bug" | "security" | "performance" | "style" | "logic",
  "file_path": "path/to/file",
  "line": null or line_number,
  "message": "Description of the issue",
  "suggestion": "How to fix it" or null
}

If there are no issues, return an empty array: []
"""


def seed_review_settings():
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM review_settings WHERE id = 1").fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO review_settings (id, system_prompt) VALUES (1, ?)",
            (DEFAULT_REVIEW_PROMPT,),
        )
        conn.commit()
    conn.close()


def _seed_rule_if_missing(conn: sqlite3.Connection, rule: dict):
    seed_key = rule.pop("seed_key", None) or rule["name"]
    exists = conn.execute(
        "SELECT 1 FROM notification_rules WHERE seed_key = ?", (seed_key,)
    ).fetchone()
    if exists:
        return
    rule["seed_key"] = seed_key
    cols = ", ".join(rule.keys())
    placeholders = ", ".join("?" for _ in rule)
    conn.execute(
        f"INSERT INTO notification_rules ({cols}) VALUES ({placeholders})",
        list(rule.values()),
    )
    conn.commit()
