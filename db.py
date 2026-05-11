import json
import sqlite3
from pathlib import Path

from services.rule_store import (
    backfill_rule_schema,
    create_rule_schema,
    upsert_rule_aggregate,
)

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
            project_keys TEXT DEFAULT '*',
            content_exclude TEXT DEFAULT '',
            file_check_enabled INTEGER DEFAULT 0,
            file_check_path_prefix TEXT DEFAULT '',
            file_check_mode TEXT DEFAULT 'present',
            title_exclude TEXT DEFAULT '',
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
            gitlab_discussion_id TEXT DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS pipeline_job_retry_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            mr_iid INTEGER NOT NULL,
            pipeline_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            retried_job_id INTEGER DEFAULT 0,
            job_name TEXT DEFAULT '',
            job_web_url TEXT DEFAULT '',
            retried_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
            UNIQUE(rule_id, job_id)
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
            last_missing_notify_at TEXT DEFAULT '',
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
            review_project_root TEXT DEFAULT '',
            review_project_config_path TEXT DEFAULT 'configuration/@config-rgsl',
            review_sql_target TEXT DEFAULT 'PostgreSQL 17.5+',
            review_graph_context_enabled INTEGER DEFAULT 1,
            review_graph_context_max_files INTEGER DEFAULT 12,
            active_project_profile_id INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS review_project_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed_key TEXT UNIQUE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            is_default INTEGER DEFAULT 0,
            project_root TEXT DEFAULT '',
            config_path TEXT DEFAULT 'configuration/@config-rgsl',
            sql_target TEXT DEFAULT 'PostgreSQL 17.5+',
            graph_context_enabled INTEGER DEFAULT 1,
            graph_context_max_files INTEGER DEFAULT 12,
            profile_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS review_instruction_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instruction_text TEXT NOT NULL,
            instruction_type TEXT NOT NULL DEFAULT 'include',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
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

        CREATE TABLE IF NOT EXISTS global_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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
            mr_merged_at TEXT DEFAULT '',
            changed_files_count INTEGER DEFAULT 0,
            rules_checked INTEGER DEFAULT 0,
            rules_matched INTEGER DEFAULT 0,
            success INTEGER DEFAULT 1,
            error TEXT DEFAULT '',
            polled_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    _migrate(conn)

    conn.close()


def _migrate(conn: sqlite3.Connection):
    """Добавляет новые колонки если БД уже существовала."""
    cursor = conn.execute("PRAGMA table_info(notification_rules)")
    columns = {row[1] for row in cursor.fetchall()}

    migrations = {
        "target_branch": "ALTER TABLE notification_rules ADD COLUMN target_branch TEXT DEFAULT 'master'",
        "mr_state": "ALTER TABLE notification_rules ADD COLUMN mr_state TEXT DEFAULT 'merged'",
        "poll_interval_seconds": "ALTER TABLE notification_rules ADD COLUMN poll_interval_seconds INTEGER DEFAULT 0",
        "project_keys": "ALTER TABLE notification_rules ADD COLUMN project_keys TEXT DEFAULT '*'",
        "file_check_enabled": "ALTER TABLE notification_rules ADD COLUMN file_check_enabled INTEGER DEFAULT 0",
        "file_check_path_prefix": "ALTER TABLE notification_rules ADD COLUMN file_check_path_prefix TEXT DEFAULT ''",
        "content_exclude": "ALTER TABLE notification_rules ADD COLUMN content_exclude TEXT DEFAULT ''",
        "file_check_mode": "ALTER TABLE notification_rules ADD COLUMN file_check_mode TEXT DEFAULT 'present'",
        "send_teams": "ALTER TABLE notification_rules ADD COLUMN send_teams INTEGER DEFAULT 1",
        "send_gitlab": "ALTER TABLE notification_rules ADD COLUMN send_gitlab INTEGER DEFAULT 0",
        "action_type": "ALTER TABLE notification_rules ADD COLUMN action_type TEXT DEFAULT 'notify'",
        "seed_key": "ALTER TABLE notification_rules ADD COLUMN seed_key TEXT DEFAULT NULL",
        "title_exclude": "ALTER TABLE notification_rules ADD COLUMN title_exclude TEXT DEFAULT ''",
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
            "MR changed model without postgres script": "mr_model_no_postgres",
        }
        for name, key in seed_key_map.items():
            conn.execute(
                "UPDATE notification_rules SET seed_key = ? WHERE name = ? AND (seed_key = '' OR seed_key IS NULL)",
                (key, name),
            )

    conn.execute(
        "UPDATE notification_rules SET seed_key = NULL WHERE seed_key = ''"
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_rules_seed_key "
        "ON notification_rules(seed_key)"
    )
    create_rule_schema(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jira_users_display_name "
        "ON jira_users(display_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_vacations_account_period "
        "ON user_vacations(account_id, date_from, date_to)"
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
        review_setting_migrations = {
            "review_project_root": "ALTER TABLE review_settings ADD COLUMN review_project_root TEXT DEFAULT ''",
            "review_project_config_path": "ALTER TABLE review_settings ADD COLUMN review_project_config_path TEXT DEFAULT 'configuration/@config-rgsl'",
            "review_sql_target": "ALTER TABLE review_settings ADD COLUMN review_sql_target TEXT DEFAULT 'PostgreSQL 17.5+'",
            "review_graph_context_enabled": "ALTER TABLE review_settings ADD COLUMN review_graph_context_enabled INTEGER DEFAULT 1",
            "review_graph_context_max_files": "ALTER TABLE review_settings ADD COLUMN review_graph_context_max_files INTEGER DEFAULT 12",
            "active_project_profile_id": "ALTER TABLE review_settings ADD COLUMN active_project_profile_id INTEGER",
        }
        for col, sql in review_setting_migrations.items():
            if review_columns and col not in review_columns:
                conn.execute(sql)
    except Exception:
        pass

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_project_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seed_key TEXT UNIQUE,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                project_root TEXT DEFAULT '',
                config_path TEXT DEFAULT 'configuration/@config-rgsl',
                sql_target TEXT DEFAULT 'PostgreSQL 17.5+',
                graph_context_enabled INTEGER DEFAULT 1,
                graph_context_max_files INTEGER DEFAULT 12,
                profile_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    except Exception:
        pass

    try:
        cursor_rs2 = conn.execute("PRAGMA table_info(report_settings)")
        rs2_columns = {row[1] for row in cursor_rs2.fetchall()}
        if rs2_columns and "last_missing_notify_at" not in rs2_columns:
            conn.execute(
                "ALTER TABLE report_settings ADD COLUMN last_missing_notify_at TEXT DEFAULT ''"
            )
    except Exception:
        pass

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_instruction_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instruction_text TEXT NOT NULL,
                instruction_type TEXT NOT NULL DEFAULT 'include',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            UPDATE review_instruction_items
            SET instruction_type = 'include'
            WHERE instruction_type NOT IN ('include', 'exclude') OR instruction_type IS NULL
            """
        )
        legacy_row = conn.execute(
            "SELECT review_instructions FROM review_settings WHERE id = 1"
        ).fetchone()
        legacy_text = (legacy_row["review_instructions"] if legacy_row else "") or ""
        items_exist = conn.execute(
            "SELECT 1 FROM review_instruction_items LIMIT 1"
        ).fetchone()
        if legacy_text.strip() and not items_exist:
            conn.execute(
                """
                INSERT INTO review_instruction_items (instruction_text, instruction_type, sort_order)
                VALUES (?, 'include', 1)
                """,
                (legacy_text.strip(),),
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
        if nl_columns and "gitlab_discussion_id" not in nl_columns:
            conn.execute(
                "ALTER TABLE notification_log ADD COLUMN gitlab_discussion_id TEXT DEFAULT ''"
            )
    except Exception:
        pass

    try:
        cursor_pm = conn.execute("PRAGMA table_info(polled_mrs)")
        pm_columns = {row[1] for row in cursor_pm.fetchall()}
        if pm_columns and "mr_merged_at" not in pm_columns:
            conn.execute(
                "ALTER TABLE polled_mrs ADD COLUMN mr_merged_at TEXT DEFAULT ''"
            )
    except Exception:
        pass

    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS global_settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
    except Exception:
        pass

    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pipeline_job_retry_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                mr_iid INTEGER NOT NULL,
                pipeline_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                retried_job_id INTEGER DEFAULT 0,
                job_name TEXT DEFAULT '',
                job_web_url TEXT DEFAULT '',
                retried_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
                UNIQUE(rule_id, job_id)
            )"""
        )
    except Exception:
        pass

    backfill_rule_schema(conn)
    conn.commit()


def seed_global_settings():
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM global_settings WHERE key = 'global_title_excludes'"
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO global_settings (key, value) VALUES (?, ?)",
            ("global_title_excludes", "Changelog for version\n[skip_changelog]\n[release_version_release]\n[prepare_release_candidate]"),
        )
        conn.commit()
    conn.close()


def get_global_setting(key: str, default: str = "") -> str:
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM global_settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_global_setting(key: str, value: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO global_settings (key, value, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
        (key, value),
    )
    conn.commit()
    conn.close()


def seed_default_rule():
    conn = get_db()
    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "mr_model_no_postgres",
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

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "breaking_changes",
            "name": "Breaking Changes",
            "description": "Уведомление о breaking changes при merge MR",
            "file_pattern": "changelogs/unreleased/*.md",
            "content_match": "type: breaking",
            "match_type": "contains",
            "target_branch": "master",
            "mr_state": "merged",
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "changelog_should_be_breaking",
            "name": "Changelog должен быть breaking",
            "description": "Changelog содержит /etl-services/, script, скрипт, выполнит, publish, паблиш, но тип не breaking — нужно исправить на type: breaking",
            "file_pattern": "changelogs/unreleased/*.md",
            "content_match": r"(?i)(/etl-services/|script|скрипт|выполнит|publish|паблиш)",
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
            "title_exclude": "prepare_release_candidate",
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
            "title_exclude": "prepare_release_candidate",
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

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "mr_title_check",
            "name": "Проверка заголовка MR",
            "description": "Проверяет формат заголовка MR: JIRA-TASK: Short description. Заголовок не должен содержать русские буквы. Для release-веток требует номер релиза в конце заголовка. Пропускает MR со статусом Draft.",
            "file_pattern": "*",
            "content_match": "",
            "match_type": "contains",
            "target_branch": "*",
            "mr_state": "opened",
            "action_type": "title_check",
            "send_gitlab": 1,
            "send_teams": 0,
            "send_email": 0,
            "enabled": 1,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "pipeline_changelog_validate",
            "name": "Проверка changelog:validate",
            "description": "Проверяет, что job changelog:validate в pipeline MR прошёл успешно. Если job упал — создаёт discussion с упоминанием assignee.",
            "file_pattern": "*",
            "content_match": "changelog:validate",
            "match_type": "contains",
            "target_branch": "*",
            "mr_state": "opened",
            "action_type": "pipeline_check",
            "send_gitlab": 1,
            "send_teams": 0,
            "send_email": 0,
            "enabled": 1,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "pipeline_config_retry_fresh_packages",
            "name": "Retry config jobs after fresh packages stall",
            "description": "Retries failed config:check-uncommitted and config:validate jobs when their trace stops after [5/5] Building fresh packages... or has a TLS socket disconnect error.",
            "file_pattern": "*",
            "content_match": "config:check-uncommitted,config:validate",
            "match_type": "contains",
            "target_branch": "*",
            "mr_state": "opened",
            "poll_interval_seconds": 600,
            "action_type": "pipeline_job_retry",
            "send_gitlab": 0,
            "send_teams": 0,
            "send_email": 0,
            "enabled": 1,
        },
    )

    _seed_rule_if_missing(
        conn,
        {
            "seed_key": "pipeline_config_sonar_publish_issues",
            "name": "Publish Sonar issues after config:sonar",
            "description": "After config:sonar finishes in the latest MR pipeline, fetch SonarQube issues for the MR and publish a fresh GitLab comment, deleting the previous Sonar comment.",
            "file_pattern": "*",
            "content_match": "config:sonar",
            "match_type": "contains",
            "target_branch": "*",
            "mr_state": "opened",
            "poll_interval_seconds": 600,
            "action_type": "sonar_issues",
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
    _seed_review_project_profile(conn)
    conn.commit()
    conn.close()


def _seed_review_project_profile(conn: sqlite3.Connection):
    from services.review_project_context import default_adinsure_profile_json

    settings = conn.execute(
        """
        SELECT review_project_root, review_project_config_path, review_sql_target,
               review_graph_context_enabled, review_graph_context_max_files,
               active_project_profile_id
        FROM review_settings WHERE id = 1
        """
    ).fetchone()
    project_root = (settings["review_project_root"] if settings else "") or ""
    config_path = (settings["review_project_config_path"] if settings else "") or "configuration/@config-rgsl"
    sql_target = (settings["review_sql_target"] if settings else "") or "PostgreSQL 17.5+"
    graph_enabled = int(settings["review_graph_context_enabled"] if settings else 1)
    max_files = int((settings["review_graph_context_max_files"] if settings else 12) or 12)
    profile_json = json.dumps(default_adinsure_profile_json(), ensure_ascii=False)

    existing = conn.execute(
        "SELECT id FROM review_project_profiles WHERE seed_key = ?",
        ("adinsure_implementation",),
    ).fetchone()
    if not existing:
        cur = conn.execute(
            """
            INSERT INTO review_project_profiles (
                seed_key, name, description, enabled, is_default,
                project_root, config_path, sql_target, graph_context_enabled,
                graph_context_max_files, profile_json
            )
            VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                "adinsure_implementation",
                "AdInsure implementation",
                "Seed profile for AdInsure configuration-constructor repositories.",
                project_root,
                config_path,
                sql_target,
                graph_enabled,
                max_files,
                profile_json,
            ),
        )
        profile_id = cur.lastrowid
    else:
        profile_id = existing["id"]

    if settings and not settings["active_project_profile_id"]:
        conn.execute(
            "UPDATE review_settings SET active_project_profile_id = ? WHERE id = 1",
            (profile_id,),
        )


SEED_UPDATABLE_FIELDS = (
    "name", "description", "file_pattern", "content_match", "content_exclude",
    "match_type", "target_branch", "mr_state", "project_keys", "title_exclude",
    "file_check_enabled", "file_check_path_prefix", "file_check_mode",
)


def _seed_rule_if_missing(conn: sqlite3.Connection, rule: dict):
    rule = dict(rule)
    seed_key = rule.pop("seed_key", None) or rule.get("name", "")
    if not seed_key:
        rule_id = upsert_rule_aggregate(conn, rule)
        conn.commit()
        return rule_id
    rule["seed_key"] = seed_key
    cols = ", ".join(rule.keys())
    placeholders = ", ".join("?" for _ in rule)
    updatable = {k: v for k, v in rule.items() if k in SEED_UPDATABLE_FIELDS}
    update_clause = ", ".join(f"{k} = excluded.{k}" for k in updatable)
    conn.execute(
        f"INSERT INTO notification_rules ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(seed_key) DO UPDATE SET {update_clause}",
        list(rule.values()),
    )
    row = conn.execute(
        "SELECT id FROM notification_rules WHERE seed_key = ?", (seed_key,)
    ).fetchone()
    if row:
        upsert_rule_aggregate(conn, rule, row["id"])
    conn.commit()
    return row["id"] if row else None
