from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import Any


DEFAULT_TITLE_CHECK_CONFIG = {
    "require_jira_prefix": True,
    "forbid_cyrillic": True,
    "require_release_suffix": True,
    "mention_assignees": True,
    "resolve_discussion_when_fixed": True,
}

DEFAULT_PIPELINE_RETRY_MARKER = "[5/5] Building fresh packages..."
DEFAULT_PIPELINE_RETRY_MATCHER = "TLS socket disconnected|ECONNRESET|connection reset"
DEFAULT_PIPELINE_RETRY_SUFFIX = ""
DEFAULT_PIPELINE_CHECK_TEMPLATE = (
    "Changelog не прошёл валидацию\n\n[Ссылка на job]({job_web_url})"
)
TITLE_PROJECT_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+\b", re.IGNORECASE)


def create_rule_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rule_conditions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            condition_type TEXT NOT NULL,
            operator TEXT DEFAULT '',
            value TEXT DEFAULT '',
            extra_json TEXT DEFAULT '{}',
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rule_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rule_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            channel_type TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            settings_json TEXT DEFAULT '{}',
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
            UNIQUE(rule_id, channel_type)
        );

        CREATE TABLE IF NOT EXISTS rule_email_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
            UNIQUE(rule_id, email)
        );

        CREATE TABLE IF NOT EXISTS rule_title_check_config (
            rule_id INTEGER PRIMARY KEY,
            require_jira_prefix INTEGER DEFAULT 1,
            forbid_cyrillic INTEGER DEFAULT 1,
            require_release_suffix INTEGER DEFAULT 1,
            mention_assignees INTEGER DEFAULT 1,
            resolve_discussion_when_fixed INTEGER DEFAULT 1,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rule_pipeline_check_config (
            rule_id INTEGER PRIMARY KEY,
            job_name TEXT DEFAULT 'changelog:validate',
            discussion_template TEXT DEFAULT '',
            mention_assignees INTEGER DEFAULT 1,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rule_pipeline_retry_config (
            rule_id INTEGER PRIMARY KEY,
            trace_marker TEXT DEFAULT '',
            trace_matcher_regex TEXT DEFAULT '',
            gitlab_boilerplate_suffix TEXT DEFAULT '',
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rule_pipeline_retry_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            job_name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE,
            UNIQUE(rule_id, job_name)
        );

        CREATE TABLE IF NOT EXISTS rule_review_config (
            rule_id INTEGER PRIMARY KEY,
            review_type TEXT NOT NULL,
            file_pattern TEXT DEFAULT '*',
            base_ref TEXT DEFAULT '',
            custom_prompt TEXT DEFAULT '',
            FOREIGN KEY (rule_id) REFERENCES notification_rules(id) ON DELETE CASCADE
        );
        """
    )


def backfill_rule_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM notification_rules").fetchall()
    for row in rows:
        if not _has_v2_children(conn, row["id"]):
            upsert_rule_aggregate(conn, row_to_aggregate(conn, row, prefer_legacy=True))


def load_enabled_runtime_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM notification_rules WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [runtime_rule_from_row(conn, row) for row in rows]


def load_runtime_rule(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        return None
    return runtime_rule_from_row(conn, row)


def load_runtime_rules(
    conn: sqlite3.Connection, rule_ids: list[int], enabled_only: bool = False
) -> list[dict[str, Any]]:
    if not rule_ids:
        return []
    placeholders = ",".join("?" for _ in rule_ids)
    enabled_sql = " AND enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"SELECT * FROM notification_rules WHERE id IN ({placeholders}){enabled_sql}",
        rule_ids,
    ).fetchall()
    by_id = {row["id"]: runtime_rule_from_row(conn, row) for row in rows}
    return [by_id[rule_id] for rule_id in rule_ids if rule_id in by_id]


def list_rule_aggregates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM notification_rules ORDER BY created_at DESC"
    ).fetchall()
    rules = [row_to_aggregate(conn, row) for row in rows]
    signatures = Counter(rule_behavior_signature(rule) for rule in rules)
    for rule in rules:
        rule["is_duplicate_behavior"] = signatures[rule_behavior_signature(rule)] > 1
    return rules


def get_rule_aggregate(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        return None
    rule = row_to_aggregate(conn, row)
    signature = rule_behavior_signature(rule)
    duplicate = conn.execute(
        "SELECT * FROM notification_rules WHERE id != ?", (rule_id,)
    ).fetchall()
    rule["is_duplicate_behavior"] = any(
        rule_behavior_signature(row_to_aggregate(conn, other)) == signature
        for other in duplicate
    )
    return rule


def row_to_aggregate(
    conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any], prefer_legacy: bool = False
) -> dict[str, Any]:
    root = dict(row)
    if prefer_legacy or not _has_v2_children(conn, root["id"]):
        return legacy_row_to_aggregate(conn, root)

    conditions = [
        {
            "id": r["id"],
            "type": r["condition_type"],
            "operator": r["operator"] or "",
            "value": r["value"] or "",
            "extra": _loads_json(r["extra_json"], {}),
        }
        for r in conn.execute(
            "SELECT * FROM rule_conditions WHERE rule_id = ? ORDER BY sort_order, id",
            (root["id"],),
        ).fetchall()
    ]
    actions = [
        {"type": r["action_type"], "enabled": bool(r["enabled"])}
        for r in conn.execute(
            "SELECT * FROM rule_actions WHERE rule_id = ? ORDER BY sort_order, id",
            (root["id"],),
        ).fetchall()
    ]
    channels = {
        r["channel_type"]: {
            "enabled": bool(r["enabled"]),
            "settings": _loads_json(r["settings_json"], {}),
        }
        for r in conn.execute(
            "SELECT * FROM rule_channels WHERE rule_id = ?", (root["id"],)
        ).fetchall()
    }
    recipients = [
        r["email"]
        for r in conn.execute(
            "SELECT email FROM rule_email_recipients WHERE rule_id = ? ORDER BY id",
            (root["id"],),
        ).fetchall()
    ]
    if not recipients:
        recipients = [
            r["email"]
            for r in conn.execute(
                "SELECT email FROM email_recipients WHERE rule_id = ? ORDER BY id",
                (root["id"],),
            ).fetchall()
        ]

    action_type = _primary_action(actions)
    configs = {
        "title_check": _load_title_check_config(conn, root["id"]),
        "pipeline_check": _load_pipeline_check_config(conn, root["id"]),
        "pipeline_job_retry": _load_pipeline_retry_config(conn, root["id"]),
        "review": _load_review_config(conn, root["id"], action_type),
    }
    return _compose_aggregate(root, conditions, actions, channels, recipients, configs)


def legacy_row_to_aggregate(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    action_type = row.get("action_type") or "notify"
    file_pattern = row.get("file_pattern") or ("*.xlsx" if action_type == "xlsx_review" else "*")
    conditions = []
    if file_pattern:
        conditions.append({
            "type": "changed_file_glob",
            "operator": "glob",
            "value": file_pattern,
            "extra": {},
        })
    if row.get("content_match") or row.get("match_type") == "json_additional_props":
        conditions.append({
            "type": "file_content",
            "operator": row.get("match_type") or "contains",
            "value": row.get("content_match") or "",
            "extra": {},
        })
    if row.get("content_exclude"):
        conditions.append({
            "type": "content_exclude_regex",
            "operator": "regex",
            "value": row.get("content_exclude") or "",
            "extra": {},
        })
    if row.get("title_exclude"):
        conditions.append({
            "type": "title_exclude_regex",
            "operator": "regex",
            "value": row.get("title_exclude") or "",
            "extra": {},
        })
    if row.get("file_check_enabled") and row.get("file_check_path_prefix"):
        mode = row.get("file_check_mode") or "present"
        condition_type = (
            "changed_file_under_prefix"
            if mode in {"present_any", "absent_any"}
            else "referenced_file_check"
        )
        conditions.append({
            "type": condition_type,
            "operator": mode,
            "value": row.get("file_check_path_prefix") or "",
            "extra": {},
        })

    channels = {
        "teams": {
            "enabled": bool(row.get("send_teams", 1)),
            "settings": {"webhook_url": row.get("teams_webhook_url") or ""},
        },
        "email": {"enabled": bool(row.get("send_email", 0)), "settings": {}},
        "gitlab": {"enabled": bool(row.get("send_gitlab", 0)), "settings": {}},
    }
    recipients = [
        r["email"]
        for r in conn.execute(
            "SELECT email FROM email_recipients WHERE rule_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
    ]
    actions = [{"type": action_type, "enabled": True}]
    configs = {
        "title_check": DEFAULT_TITLE_CHECK_CONFIG.copy(),
        "pipeline_check": {
            "job_name": row.get("content_match") or "changelog:validate",
            "discussion_template": DEFAULT_PIPELINE_CHECK_TEMPLATE,
            "mention_assignees": True,
        },
        "pipeline_job_retry": {
            "trace_marker": DEFAULT_PIPELINE_RETRY_MARKER,
            "trace_matcher_regex": DEFAULT_PIPELINE_RETRY_MATCHER,
            "gitlab_boilerplate_suffix": DEFAULT_PIPELINE_RETRY_SUFFIX,
            "jobs": _split_csv(row.get("content_match") or ""),
        },
        "review": {
            "review_type": action_type if action_type in {"xlsx_review", "code_review"} else "",
            "file_pattern": file_pattern,
            "base_ref": "",
            "custom_prompt": "",
        },
    }
    return _compose_aggregate(row, conditions, actions, channels, recipients, configs)


def runtime_rule_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    aggregate = row_to_aggregate(conn, row)
    runtime = dict(row)
    runtime.update(flat_fields_from_aggregate(aggregate))
    runtime["aggregate"] = aggregate
    runtime["emails"] = aggregate["recipients"]
    return runtime


def flat_fields_from_aggregate(rule: dict[str, Any]) -> dict[str, Any]:
    conditions = rule.get("conditions", [])
    channels = rule.get("channels", {})
    action_type = _primary_action(rule.get("actions", []))
    file_condition = _first_condition(conditions, "changed_file_glob")
    content_condition = _first_condition(conditions, "file_content")
    content_exclude = _first_condition(conditions, "content_exclude_regex")
    title_exclude = _first_condition(conditions, "title_exclude_regex")
    referenced = _first_condition(conditions, "referenced_file_check")
    prefix = _first_condition(conditions, "changed_file_under_prefix")
    file_check = referenced or prefix

    retry_config = rule.get("configs", {}).get("pipeline_job_retry", {})
    pipeline_config = rule.get("configs", {}).get("pipeline_check", {})
    review_config = rule.get("configs", {}).get("review", {})

    content_match = content_condition.get("value", "") if content_condition else ""
    if action_type == "pipeline_check":
        content_match = pipeline_config.get("job_name") or content_match or "changelog:validate"
    elif action_type == "pipeline_job_retry":
        content_match = ",".join(retry_config.get("jobs") or _split_csv(content_match))

    file_pattern = file_condition.get("value", "") if file_condition else ""
    if action_type in {"xlsx_review", "code_review"}:
        file_pattern = review_config.get("file_pattern") or file_pattern or (
            "*.xlsx" if action_type == "xlsx_review" else "*"
        )

    teams = channels.get("teams", {})
    email = channels.get("email", {})
    gitlab = channels.get("gitlab", {})
    return {
        "project_keys": project_keys_to_string(rule.get("scope", {}).get("project_keys", ["*"])),
        "file_pattern": file_pattern or "*",
        "content_match": content_match,
        "content_exclude": content_exclude.get("value", "") if content_exclude else "",
        "match_type": content_condition.get("operator", "contains") if content_condition else "contains",
        "file_check_enabled": 1 if file_check else 0,
        "file_check_path_prefix": file_check.get("value", "") if file_check else "",
        "file_check_mode": file_check.get("operator", "present") if file_check else "present",
        "title_exclude": title_exclude.get("value", "") if title_exclude else "",
        "action_type": action_type,
        "send_teams": 1 if teams.get("enabled", False) else 0,
        "teams_webhook_url": teams.get("settings", {}).get("webhook_url", ""),
        "send_email": 1 if email.get("enabled", False) else 0,
        "send_gitlab": 1 if gitlab.get("enabled", False) else 0,
    }


def upsert_rule_aggregate(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    rule_id: int | None = None,
    force_disabled: bool = False,
) -> int:
    aggregate = normalize_rule_payload(data)
    flat = flat_fields_from_aggregate(aggregate)
    enabled = 0 if force_disabled else int(bool(aggregate.get("enabled", True)))
    root_values = (
        aggregate["name"],
        aggregate.get("description", ""),
        enabled,
        aggregate["scope"].get("target_branch", "master"),
        aggregate["scope"].get("mr_state", "merged"),
        int(aggregate["scope"].get("poll_interval_seconds", 0) or 0),
        project_keys_to_string(aggregate["scope"].get("project_keys", ["*"])),
        flat["file_pattern"],
        flat["content_match"],
        flat["content_exclude"],
        flat["match_type"],
        flat["file_check_enabled"],
        flat["file_check_path_prefix"],
        flat["file_check_mode"],
        flat["title_exclude"],
        flat["action_type"],
        flat["send_teams"],
        flat["teams_webhook_url"],
        flat["send_email"],
        flat["send_gitlab"],
    )
    if rule_id is None:
        cur = conn.execute(
            """
            INSERT INTO notification_rules (
                name, description, enabled, target_branch, mr_state,
                poll_interval_seconds, project_keys, file_pattern, content_match, content_exclude,
                match_type, file_check_enabled, file_check_path_prefix, file_check_mode,
                title_exclude, action_type, send_teams, teams_webhook_url, send_email, send_gitlab
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            root_values,
        )
        rule_id = cur.lastrowid
    else:
        conn.execute(
            """
            UPDATE notification_rules SET
                name=?, description=?, enabled=?, target_branch=?, mr_state=?,
                poll_interval_seconds=?, project_keys=?, file_pattern=?, content_match=?, content_exclude=?,
                match_type=?, file_check_enabled=?, file_check_path_prefix=?,
                file_check_mode=?, title_exclude=?, action_type=?, send_teams=?,
                teams_webhook_url=?, send_email=?, send_gitlab=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            root_values + (rule_id,),
        )

    replace_rule_children(conn, rule_id, aggregate)
    return int(rule_id)


def replace_rule_children(
    conn: sqlite3.Connection, rule_id: int, aggregate: dict[str, Any]
) -> None:
    for table in (
        "rule_conditions",
        "rule_actions",
        "rule_channels",
        "rule_email_recipients",
        "rule_title_check_config",
        "rule_pipeline_check_config",
        "rule_pipeline_retry_config",
        "rule_pipeline_retry_jobs",
        "rule_review_config",
        "email_recipients",
    ):
        conn.execute(f"DELETE FROM {table} WHERE rule_id = ?", (rule_id,))

    for idx, condition in enumerate(aggregate.get("conditions", []), start=1):
        conn.execute(
            """
            INSERT INTO rule_conditions
                (rule_id, condition_type, operator, value, extra_json, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                condition.get("type", ""),
                condition.get("operator", ""),
                condition.get("value", ""),
                json.dumps(condition.get("extra", {}) or {}, ensure_ascii=False),
                idx,
            ),
        )

    for idx, action in enumerate(aggregate.get("actions", []), start=1):
        conn.execute(
            """
            INSERT INTO rule_actions (rule_id, action_type, enabled, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            (rule_id, action.get("type", "notify"), int(action.get("enabled", True)), idx),
        )

    for channel_type, channel in aggregate.get("channels", {}).items():
        conn.execute(
            """
            INSERT INTO rule_channels (rule_id, channel_type, enabled, settings_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                rule_id,
                channel_type,
                int(channel.get("enabled", False)),
                json.dumps(channel.get("settings", {}) or {}, ensure_ascii=False),
            ),
        )

    for email in aggregate.get("recipients", []):
        clean = str(email).strip()
        if not clean:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO rule_email_recipients (rule_id, email) VALUES (?, ?)",
            (rule_id, clean),
        )
        conn.execute(
            "INSERT OR IGNORE INTO email_recipients (rule_id, email) VALUES (?, ?)",
            (rule_id, clean),
        )

    configs = aggregate.get("configs", {})
    title = configs.get("title_check", {})
    if any(a.get("type") == "title_check" for a in aggregate.get("actions", [])):
        conn.execute(
            """
            INSERT INTO rule_title_check_config (
                rule_id, require_jira_prefix, forbid_cyrillic,
                require_release_suffix, mention_assignees, resolve_discussion_when_fixed
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                int(title.get("require_jira_prefix", True)),
                int(title.get("forbid_cyrillic", True)),
                int(title.get("require_release_suffix", True)),
                int(title.get("mention_assignees", True)),
                int(title.get("resolve_discussion_when_fixed", True)),
            ),
        )

    pipeline = configs.get("pipeline_check", {})
    if any(a.get("type") == "pipeline_check" for a in aggregate.get("actions", [])):
        conn.execute(
            """
            INSERT INTO rule_pipeline_check_config
                (rule_id, job_name, discussion_template, mention_assignees)
            VALUES (?, ?, ?, ?)
            """,
            (
                rule_id,
                pipeline.get("job_name", "changelog:validate"),
                pipeline.get("discussion_template", DEFAULT_PIPELINE_CHECK_TEMPLATE),
                int(pipeline.get("mention_assignees", True)),
            ),
        )

    retry = configs.get("pipeline_job_retry", {})
    if any(a.get("type") == "pipeline_job_retry" for a in aggregate.get("actions", [])):
        conn.execute(
            """
            INSERT INTO rule_pipeline_retry_config
                (rule_id, trace_marker, trace_matcher_regex, gitlab_boilerplate_suffix)
            VALUES (?, ?, ?, ?)
            """,
            (
                rule_id,
                retry.get("trace_marker", DEFAULT_PIPELINE_RETRY_MARKER),
                retry.get("trace_matcher_regex", DEFAULT_PIPELINE_RETRY_MATCHER),
                retry.get("gitlab_boilerplate_suffix", DEFAULT_PIPELINE_RETRY_SUFFIX),
            ),
        )
        for idx, job_name in enumerate(retry.get("jobs", []), start=1):
            conn.execute(
                """
                INSERT OR IGNORE INTO rule_pipeline_retry_jobs
                    (rule_id, job_name, sort_order)
                VALUES (?, ?, ?)
                """,
                (rule_id, job_name, idx),
            )

    review = configs.get("review", {})
    action_type = _primary_action(aggregate.get("actions", []))
    if action_type in {"xlsx_review", "code_review"}:
        conn.execute(
            """
            INSERT INTO rule_review_config
                (rule_id, review_type, file_pattern, base_ref, custom_prompt)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                action_type,
                review.get("file_pattern", "*.xlsx" if action_type == "xlsx_review" else "*"),
                review.get("base_ref", ""),
                review.get("custom_prompt", ""),
            ),
        )


def normalize_rule_payload(data: dict[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = data.model_dump()
    if any(key in data for key in ("scope", "conditions", "actions", "channels", "recipients")):
        return _normalize_aggregate(data)
    return _normalize_flat(data)


def rule_behavior_signature(rule: dict[str, Any]) -> str:
    data = {
        "scope": rule.get("scope", {}),
        "conditions": rule.get("conditions", []),
        "actions": rule.get("actions", []),
        "channels": rule.get("channels", {}),
        "recipients": sorted(rule.get("recipients", [])),
        "configs": rule.get("configs", {}),
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _normalize_flat(data: dict[str, Any]) -> dict[str, Any]:
    action_type = data.get("action_type") or "notify"
    file_pattern = data.get("file_pattern")
    if not file_pattern:
        file_pattern = "*.xlsx" if action_type == "xlsx_review" else "*"
    conditions = []
    if file_pattern:
        conditions.append({
            "type": "changed_file_glob",
            "operator": "glob",
            "value": file_pattern,
            "extra": {},
        })
    content_match = data.get("content_match") or ""
    match_type = data.get("match_type") or "contains"
    if content_match or match_type == "json_additional_props":
        conditions.append({
            "type": "file_content",
            "operator": match_type,
            "value": content_match,
            "extra": {},
        })
    if data.get("content_exclude"):
        conditions.append({
            "type": "content_exclude_regex",
            "operator": "regex",
            "value": data.get("content_exclude") or "",
            "extra": {},
        })
    if data.get("title_exclude"):
        conditions.append({
            "type": "title_exclude_regex",
            "operator": "regex",
            "value": data.get("title_exclude") or "",
            "extra": {},
        })
    if data.get("file_check_enabled") and data.get("file_check_path_prefix"):
        mode = data.get("file_check_mode") or "present"
        conditions.append({
            "type": "changed_file_under_prefix" if mode in {"present_any", "absent_any"} else "referenced_file_check",
            "operator": mode,
            "value": data.get("file_check_path_prefix") or "",
            "extra": {},
        })

    jobs = data.get("pipeline_retry_jobs")
    if jobs is None:
        jobs = _split_csv(content_match) if action_type == "pipeline_job_retry" else []
    return _normalize_aggregate({
        "id": data.get("id"),
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "enabled": data.get("enabled", True),
        "scope": {
            "target_branch": data.get("target_branch", "master"),
            "mr_state": data.get("mr_state", "merged"),
            "poll_interval_seconds": data.get("poll_interval_seconds", 0),
            "project_keys": parse_project_keys(data.get("project_keys", "*")),
            "skip_draft": data.get("skip_draft", True),
        },
        "conditions": conditions,
        "actions": [{"type": action_type, "enabled": True}],
        "channels": {
            "teams": {
                "enabled": bool(data.get("send_teams", True)),
                "settings": {"webhook_url": data.get("teams_webhook_url", "") or ""},
            },
            "email": {"enabled": bool(data.get("send_email", False)), "settings": {}},
            "gitlab": {"enabled": bool(data.get("send_gitlab", False)), "settings": {}},
        },
        "recipients": data.get("emails", []) or data.get("recipients", []),
        "configs": {
            "title_check": DEFAULT_TITLE_CHECK_CONFIG.copy(),
            "pipeline_check": {
                "job_name": data.get("pipeline_job_name") or content_match or "changelog:validate",
                "discussion_template": data.get("pipeline_discussion_template") or DEFAULT_PIPELINE_CHECK_TEMPLATE,
                "mention_assignees": bool(data.get("pipeline_mention_assignees", True)),
            },
            "pipeline_job_retry": {
                "trace_marker": data.get("retry_trace_marker") or DEFAULT_PIPELINE_RETRY_MARKER,
                "trace_matcher_regex": data.get("retry_trace_matcher_regex") or DEFAULT_PIPELINE_RETRY_MATCHER,
                "gitlab_boilerplate_suffix": data.get("retry_gitlab_boilerplate_suffix") or DEFAULT_PIPELINE_RETRY_SUFFIX,
                "jobs": jobs,
            },
            "review": {
                "review_type": action_type if action_type in {"xlsx_review", "code_review"} else "",
                "file_pattern": file_pattern,
                "base_ref": data.get("review_base_ref", ""),
                "custom_prompt": data.get("review_custom_prompt", ""),
            },
        },
    })


def _normalize_aggregate(data: dict[str, Any]) -> dict[str, Any]:
    actions = data.get("actions") or [{"type": data.get("action_type", "notify"), "enabled": True}]
    normalized_actions = []
    for action in actions:
        if isinstance(action, str):
            normalized_actions.append({"type": action, "enabled": True})
        else:
            normalized_actions.append({
                "type": action.get("type") or action.get("action_type") or "notify",
                "enabled": bool(action.get("enabled", True)),
            })
    if not normalized_actions:
        normalized_actions = [{"type": "notify", "enabled": True}]

    conditions = []
    for condition in data.get("conditions", []):
        conditions.append({
            "type": condition.get("type") or condition.get("condition_type", ""),
            "operator": condition.get("operator", ""),
            "value": condition.get("value", ""),
            "extra": condition.get("extra", {}) or {},
        })

    channels = data.get("channels") or {}
    normalized_channels = {
        "teams": {"enabled": False, "settings": {"webhook_url": ""}},
        "email": {"enabled": False, "settings": {}},
        "gitlab": {"enabled": False, "settings": {}},
    }
    for channel_type, channel in channels.items():
        if isinstance(channel, bool):
            normalized_channels[channel_type] = {"enabled": channel, "settings": {}}
        else:
            normalized_channels[channel_type] = {
                "enabled": bool(channel.get("enabled", False)),
                "settings": channel.get("settings", {}) or {},
            }

    scope = data.get("scope") or {}
    configs = data.get("configs") or {}
    action_type = _primary_action(normalized_actions)
    configs.setdefault("title_check", DEFAULT_TITLE_CHECK_CONFIG.copy())
    configs.setdefault("pipeline_check", {
        "job_name": "changelog:validate",
        "discussion_template": DEFAULT_PIPELINE_CHECK_TEMPLATE,
        "mention_assignees": True,
    })
    configs.setdefault("pipeline_job_retry", {
        "trace_marker": DEFAULT_PIPELINE_RETRY_MARKER,
        "trace_matcher_regex": DEFAULT_PIPELINE_RETRY_MATCHER,
        "gitlab_boilerplate_suffix": DEFAULT_PIPELINE_RETRY_SUFFIX,
        "jobs": [],
    })
    configs["pipeline_job_retry"]["jobs"] = _split_csv(configs["pipeline_job_retry"].get("jobs", []))
    configs.setdefault("review", {
        "review_type": action_type if action_type in {"xlsx_review", "code_review"} else "",
        "file_pattern": "*.xlsx" if action_type == "xlsx_review" else "*",
        "base_ref": "",
        "custom_prompt": "",
    })

    return {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "enabled": bool(data.get("enabled", True)),
        "scope": {
            "target_branch": scope.get("target_branch", data.get("target_branch", "master")) or "master",
            "mr_state": scope.get("mr_state", data.get("mr_state", "merged")) or "merged",
            "poll_interval_seconds": int(scope.get("poll_interval_seconds", data.get("poll_interval_seconds", 0)) or 0),
            "project_keys": parse_project_keys(scope.get("project_keys", data.get("project_keys", "*"))),
            "skip_draft": bool(scope.get("skip_draft", True)),
        },
        "conditions": conditions,
        "actions": normalized_actions,
        "channels": normalized_channels,
        "recipients": [str(e).strip() for e in data.get("recipients", data.get("emails", [])) if str(e).strip()],
        "configs": configs,
    }


def _compose_aggregate(
    root: dict[str, Any],
    conditions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    channels: dict[str, Any],
    recipients: list[str],
    configs: dict[str, Any],
) -> dict[str, Any]:
    aggregate = _normalize_aggregate({
        "id": root.get("id"),
        "name": root.get("name", ""),
        "description": root.get("description", ""),
        "enabled": bool(root.get("enabled", True)),
        "scope": {
            "target_branch": root.get("target_branch", "master"),
            "mr_state": root.get("mr_state", "merged"),
            "poll_interval_seconds": root.get("poll_interval_seconds", 0),
            "project_keys": parse_project_keys(root.get("project_keys", "*")),
            "skip_draft": True,
        },
        "conditions": conditions,
        "actions": actions,
        "channels": channels,
        "recipients": recipients,
        "configs": configs,
    })
    aggregate["created_at"] = root.get("created_at", "")
    aggregate["updated_at"] = root.get("updated_at", "")
    aggregate.update(flat_fields_from_aggregate(aggregate))
    aggregate["emails"] = aggregate["recipients"]
    aggregate["summary"] = build_rule_summary(aggregate)
    return aggregate


def build_rule_summary(rule: dict[str, Any]) -> str:
    parts = [
        f"{rule.get('action_type', 'notify')} on {rule.get('mr_state', 'merged')} MR",
        f"branch {rule.get('target_branch', 'master')}",
        f"projects {rule.get('project_keys', '*')}",
    ]
    if rule.get("file_pattern"):
        parts.append(f"files {rule['file_pattern']}")
    if rule.get("content_match"):
        parts.append(f"{rule.get('match_type', 'contains')} {rule['content_match']}")
    if rule.get("content_exclude"):
        parts.append(f"exclude {rule['content_exclude']}")
    if rule.get("file_check_enabled"):
        parts.append(
            f"{rule.get('file_check_mode', 'present')} {rule.get('file_check_path_prefix', '')}"
        )
    channels = []
    if rule.get("send_teams"):
        channels.append("Teams")
    if rule.get("send_email"):
        channels.append("Email")
    if rule.get("send_gitlab"):
        channels.append("GitLab")
    if channels:
        parts.append("via " + ", ".join(channels))
    return " | ".join(part for part in parts if part)


def _load_title_check_config(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM rule_title_check_config WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    if not row:
        return DEFAULT_TITLE_CHECK_CONFIG.copy()
    return {
        "require_jira_prefix": bool(row["require_jira_prefix"]),
        "forbid_cyrillic": bool(row["forbid_cyrillic"]),
        "require_release_suffix": bool(row["require_release_suffix"]),
        "mention_assignees": bool(row["mention_assignees"]),
        "resolve_discussion_when_fixed": bool(row["resolve_discussion_when_fixed"]),
    }


def _load_pipeline_check_config(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM rule_pipeline_check_config WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    if not row:
        return {
            "job_name": "changelog:validate",
            "discussion_template": DEFAULT_PIPELINE_CHECK_TEMPLATE,
            "mention_assignees": True,
        }
    return {
        "job_name": row["job_name"] or "changelog:validate",
        "discussion_template": row["discussion_template"] or DEFAULT_PIPELINE_CHECK_TEMPLATE,
        "mention_assignees": bool(row["mention_assignees"]),
    }


def _load_pipeline_retry_config(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM rule_pipeline_retry_config WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    jobs = [
        r["job_name"]
        for r in conn.execute(
            "SELECT job_name FROM rule_pipeline_retry_jobs WHERE rule_id = ? ORDER BY sort_order, id",
            (rule_id,),
        ).fetchall()
    ]
    return {
        "trace_marker": (row["trace_marker"] if row else "") or DEFAULT_PIPELINE_RETRY_MARKER,
        "trace_matcher_regex": (row["trace_matcher_regex"] if row else "") or DEFAULT_PIPELINE_RETRY_MATCHER,
        "gitlab_boilerplate_suffix": (row["gitlab_boilerplate_suffix"] if row else "") or DEFAULT_PIPELINE_RETRY_SUFFIX,
        "jobs": jobs,
    }


def _load_review_config(
    conn: sqlite3.Connection, rule_id: int, action_type: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM rule_review_config WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    if not row:
        return {
            "review_type": action_type if action_type in {"xlsx_review", "code_review"} else "",
            "file_pattern": "*.xlsx" if action_type == "xlsx_review" else "*",
            "base_ref": "",
            "custom_prompt": "",
        }
    return {
        "review_type": row["review_type"],
        "file_pattern": row["file_pattern"] or "*",
        "base_ref": row["base_ref"] or "",
        "custom_prompt": row["custom_prompt"] or "",
    }


def _has_v2_children(conn: sqlite3.Connection, rule_id: int) -> bool:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM rule_conditions WHERE rule_id = ?) +
            (SELECT COUNT(*) FROM rule_actions WHERE rule_id = ?) +
            (SELECT COUNT(*) FROM rule_channels WHERE rule_id = ?) AS child_count
        """,
        (rule_id, rule_id, rule_id),
    ).fetchone()
    return bool(row and row["child_count"])


def _primary_action(actions: list[dict[str, Any]]) -> str:
    for action in actions:
        if action.get("enabled", True):
            return action.get("type", "notify") or "notify"
    return actions[0].get("type", "notify") if actions else "notify"


def _first_condition(
    conditions: list[dict[str, Any]], condition_type: str
) -> dict[str, Any] | None:
    return next((c for c in conditions if c.get("type") == condition_type), None)


def extract_project_key_from_title(mr_title: str) -> str:
    match = TITLE_PROJECT_RE.search(mr_title or "")
    return match.group(1).upper() if match else ""


def parse_project_keys(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return ["*"]
    if isinstance(raw, (list, tuple)):
        parts = raw
    else:
        parts = str(raw).replace("\n", ",").split(",")
    keys = []
    for part in parts:
        key = str(part).strip().upper()
        if not key:
            continue
        if key == "*":
            return ["*"]
        keys.append(key)
    return sorted(dict.fromkeys(keys)) or ["*"]


def project_keys_to_string(keys: str | list[str] | tuple[str, ...] | None) -> str:
    parsed = parse_project_keys(keys)
    return "*" if parsed == ["*"] else ",".join(parsed)


def rule_matches_mr_project(rule: dict[str, Any], mr_title: str) -> bool:
    keys = parse_project_keys(rule.get("project_keys") or rule.get("scope", {}).get("project_keys", "*"))
    if keys == ["*"]:
        return True
    title_project = extract_project_key_from_title(mr_title)
    return bool(title_project and title_project in keys)


def _loads_json(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return default


def _split_csv(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        parts = raw
    else:
        parts = str(raw or "").split(",")
    return [str(part).strip() for part in parts if str(part).strip()]
