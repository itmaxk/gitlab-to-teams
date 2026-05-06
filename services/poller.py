import asyncio
import logging
import os
from datetime import datetime

from db import get_db
from services.gitlab_client import (
    get_project_id,
    get_merge_requests,
    get_mr_changes,
    get_file_content,
)
from services.rules_engine import evaluate_rules_for_mr
from services.notification_dispatcher import dispatch_notifications
from services.xlsx_review_service import review_xlsx_mr
from services.review_service import review_mr
from services.review_comment_formatter import format_gitlab_review_comment
from services.gitlab_notes import post_merge_request_note, post_merge_request_discussion
from services.title_check import is_title_valid

logger = logging.getLogger(__name__)

_project_id: int | None = None


async def _resolve_project_id() -> int:
    global _project_id
    if _project_id is None:
        _project_id = await get_project_id()
        logger.info("Resolved project ID: %s", _project_id)
    return _project_id


async def _get_mr_file_content(
    project_id: int,
    mr_iid: int,
    file_path: str,
    source_branch: str,
    target_branch: str,
) -> str:
    if source_branch:
        try:
            return await get_file_content(project_id, file_path, source_branch)
        except Exception as e:
            if source_branch == target_branch:
                raise
            logger.warning(
                "Failed to get %s from source branch %s for MR !%s, "
                "falling back to target branch %s: %s",
                file_path,
                source_branch,
                mr_iid,
                target_branch,
                e,
            )
    return await get_file_content(project_id, file_path, target_branch)


def _is_mr_processed(rule_id: int, mr_iid: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM processed_mrs WHERE rule_id = ? AND mr_iid = ?",
        (rule_id, mr_iid),
    ).fetchone()
    conn.close()
    return row is not None


def _mark_mr_processed(rule_id: int, mr_iid: int):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO processed_mrs (rule_id, mr_iid) VALUES (?, ?)",
        (rule_id, mr_iid),
    )
    conn.commit()
    conn.close()


def _log_polled_mr(
    mr: dict,
    changed_files_count: int,
    rules_checked: int,
    rules_matched: int,
    success: bool,
    error: str = "",
):
    conn = get_db()
    conn.execute(
        """INSERT INTO polled_mrs
           (mr_iid, mr_title, mr_url, mr_state, mr_author,
            source_branch, target_branch, mr_created_at,
            changed_files_count, rules_checked, rules_matched,
            success, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mr["iid"],
            mr.get("title", ""),
            mr.get("web_url", ""),
            mr.get("state", ""),
            mr.get("author", {}).get("name", "")
            if isinstance(mr.get("author"), dict)
            else "",
            mr.get("source_branch", ""),
            mr.get("target_branch", ""),
            mr.get("created_at", ""),
            changed_files_count,
            rules_checked,
            rules_matched,
            1 if success else 0,
            error,
        ),
    )
    conn.commit()
    conn.close()


def _get_rules_grouped_by_schedule() -> dict[int, list[dict]]:
    """
    Группирует правила по интервалу опроса.
    Возвращает {interval_seconds: [rule, ...]}
    """
    default_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    conn = get_db()
    rows = conn.execute("SELECT * FROM notification_rules WHERE enabled = 1").fetchall()
    conn.close()

    groups: dict[int, list[dict]] = {}
    for row in rows:
        interval = row["poll_interval_seconds"] or default_interval
        groups.setdefault(interval, []).append(dict(row))
    return groups


async def poll_once(rules: list[dict]):
    """Один цикл опроса для набора правил."""
    try:
        project_id = await _resolve_project_id()
    except Exception as e:
        logger.error("Failed to resolve project ID: %s", e)
        return

    branch_state_rules: dict[tuple[str, str], list[dict]] = {}
    for rule in rules:
        key = (rule["target_branch"], rule["mr_state"])
        branch_state_rules.setdefault(key, []).append(rule)

    for (branch, state), group_rules in branch_state_rules.items():
        try:
            if branch == "*":
                mrs = await get_merge_requests(project_id, state=state)
            else:
                mrs = await get_merge_requests(
                    project_id, state=state, target_branch=branch
                )
        except Exception as e:
            logger.error(
                "Failed to get MRs (branch=%s, state=%s): %s", branch, state, e
            )
            continue

        for mr in mrs:
            mr_iid = mr["iid"]
            mr_title = mr.get("title", "")
            mr_url = mr.get("web_url", "")

            if mr_title.strip().lower().startswith("draft"):
                continue

            pending_rule_ids = [
                r["id"] for r in group_rules if not _is_mr_processed(r["id"], mr_iid)
            ]
            if not pending_rule_ids:
                continue

            xlsx_rules = [
                r
                for r in group_rules
                if r["id"] in pending_rule_ids and r.get("action_type") == "xlsx_review"
            ]
            code_review_rules = [
                r
                for r in group_rules
                if r["id"] in pending_rule_ids and r.get("action_type") == "code_review"
            ]
            title_check_rules = [
                r
                for r in group_rules
                if r["id"] in pending_rule_ids and r.get("action_type") == "title_check"
            ]
            notify_rules = [
                r
                for r in group_rules
                if r["id"] in pending_rule_ids
                and r.get("action_type", "notify") == "notify"
            ]

            total_matched = 0
            mr_target_branch = mr.get("target_branch", "") or branch

            for tc_rule in title_check_rules:
                valid, error_msg = is_title_valid(mr_title, mr_target_branch)
                if not valid:
                    try:
                        await post_merge_request_discussion(mr_iid, error_msg)
                        total_matched += 1
                    except Exception as e:
                        logger.error(
                            "Title check discussion failed for MR !%s: %s", mr_iid, e
                        )
                _mark_mr_processed(tc_rule["id"], mr_iid)

            rest_rule_ids = [
                rid for rid in pending_rule_ids
                if rid not in {r["id"] for r in title_check_rules}
            ]
            if not rest_rule_ids:
                try:
                    _log_polled_mr(
                        mr, 0, len(pending_rule_ids), total_matched, True
                    )
                except Exception as e:
                    logger.error("Failed to log polled MR !%s: %s", mr_iid, e)
                continue

            changed_files: list[str] = []
            source_branch = mr.get("source_branch") or ""
            target_branch = mr.get("target_branch") or branch

            if notify_rules or (
                xlsx_rules
                and any(r.get("file_pattern", "*.xlsx") != "" for r in xlsx_rules)
            ):
                try:
                    changed_files = await get_mr_changes(project_id, mr_iid)
                except Exception as e:
                    logger.error("Failed to get changes for MR !%s: %s", mr_iid, e)
                    _log_polled_mr(mr, 0, len(pending_rule_ids), total_matched, False, str(e))
                    for rule_id in rest_rule_ids:
                        _mark_mr_processed(rule_id, mr_iid)
                    continue

            async def fetch_content(file_path: str) -> str:
                return await _get_mr_file_content(
                    project_id,
                    mr_iid,
                    file_path,
                    source_branch,
                    target_branch,
                )

            if notify_rules:
                notify_ids = [r["id"] for r in notify_rules]
                matches = await evaluate_rules_for_mr(
                    notify_ids, changed_files, fetch_content, mr_title
                )
                if matches:
                    await dispatch_notifications(matches, mr_iid, mr_title, mr_url)
                total_matched += len(matches)

            for xlsx_rule in xlsx_rules:
                has_xlsx = any(fp.lower().endswith(".xlsx") for fp in changed_files)
                if not has_xlsx:
                    _mark_mr_processed(xlsx_rule["id"], mr_iid)
                    continue

                try:
                    result = await review_xlsx_mr(mr_iid)
                    findings = result.get("findings", [])
                    summary = result.get("summary", {})
                    model_used = result.get("model_used", "")

                    match_data = {
                        "rule": xlsx_rule,
                        "file_path": f"xlsx-review-!{mr_iid}",
                        "file_content": "",
                        "emails": [],
                        "findings": findings,
                        "summary": summary,
                        "model_used": model_used,
                    }
                    await dispatch_notifications([match_data], mr_iid, mr_title, mr_url)
                    total_matched += 1
                except Exception as e:
                    logger.error("XLSX review failed for MR !%s: %s", mr_iid, e)
                _mark_mr_processed(xlsx_rule["id"], mr_iid)

            for cr_rule in code_review_rules:
                try:
                    result = await review_mr(mr_iid)
                    findings = result.get("findings", [])
                    summary = result.get("summary", {})
                    model_used = result.get("model_used", "")

                    match_data = {
                        "rule": cr_rule,
                        "file_path": f"code-review-!{mr_iid}",
                        "file_content": "",
                        "emails": [],
                        "findings": findings,
                        "summary": summary,
                        "model_used": model_used,
                    }
                    await dispatch_notifications([match_data], mr_iid, mr_title, mr_url)
                    total_matched += 1
                except Exception as e:
                    logger.error("Code review failed for MR !%s: %s", mr_iid, e)
                _mark_mr_processed(cr_rule["id"], mr_iid)

            for rule_id in [r["id"] for r in notify_rules]:
                _mark_mr_processed(rule_id, mr_iid)

            try:
                _log_polled_mr(
                    mr, len(changed_files), len(pending_rule_ids), total_matched, True
                )
            except Exception as e:
                logger.error("Failed to log polled MR !%s: %s", mr_iid, e)


async def _run_poll_loop(interval: int, rule_getter):
    """Цикл опроса с заданным интервалом."""
    while True:
        # Перечитываем .env перед каждым циклом
        from env_reload import reload_dotenv

        reload_dotenv()

        rules = rule_getter(interval)
        if rules:
            logger.info(
                "[%s] Polling %d rule(s), interval=%ds",
                datetime.now().strftime("%H:%M:%S"),
                len(rules),
                interval,
            )
            await poll_once(rules)
        await asyncio.sleep(interval)


async def start_polling():
    """Запускает фоновые задачи опроса для каждого уникального интервала."""
    logger.info("Starting GitLab MR poller...")

    default_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

    # Начальный опрос — сразу
    groups = _get_rules_grouped_by_schedule()
    if not groups:
        logger.info("No enabled rules found, will poll with default interval")
        groups = {default_interval: []}

    tasks = []
    seen_intervals = set()

    def get_rules_for_interval(interval: int) -> list[dict]:
        current_groups = _get_rules_grouped_by_schedule()
        return current_groups.get(interval, [])

    for interval in groups:
        if interval not in seen_intervals:
            seen_intervals.add(interval)
            tasks.append(
                asyncio.create_task(_run_poll_loop(interval, get_rules_for_interval))
            )

    # Запускаем дефолтный интервал если нет других
    if default_interval not in seen_intervals:
        tasks.append(
            asyncio.create_task(
                _run_poll_loop(default_interval, get_rules_for_interval)
            )
        )

    # Ждём все таски (бесконечно)
    await asyncio.gather(*tasks)
