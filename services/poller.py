import asyncio
import json
import logging
import os
from datetime import datetime

from db import get_db, get_global_setting, set_global_setting
from services.gitlab_client import (
    get_project_id,
    get_merge_requests,
    get_mr_changes,
    get_file_content,
)
from services.rules_engine import evaluate_rules_for_mr, should_skip_by_global_title
from services.notification_dispatcher import dispatch_notifications
from services.xlsx_review_service import review_xlsx_mr
from services.review_service import review_mr
from services.review_comment_formatter import format_gitlab_review_comment
from services.gitlab_notes import (
    post_merge_request_note,
    post_merge_request_discussion,
    resolve_merge_request_discussion,
)
from services.title_check import is_title_valid
from services.pipeline_check import (
    PipelineCheckResult,
    check_pipeline_job_failed,
    parse_retry_job_names,
    retry_failed_config_jobs,
)
from services.sonar_publish import (
    parse_sonar_job_name,
    publish_sonar_issues_after_job,
)
from services.rule_store import load_enabled_runtime_rules, rule_matches_mr_project

logger = logging.getLogger(__name__)

_project_id: int | None = None
MERGED_MR_POLL_CURSORS_KEY = "merged_mr_poll_cursors"


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


def _get_merged_mr_poll_cursors() -> dict[str, str]:
    raw = get_global_setting(MERGED_MR_POLL_CURSORS_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(branch): str(value)
        for branch, value in data.items()
        if str(branch) and str(value)
    }


def _get_merged_mr_poll_cursor(branch: str) -> str:
    return _get_merged_mr_poll_cursors().get(branch, "")


def _set_merged_mr_poll_cursor(branch: str, merged_at: str):
    if not branch or not merged_at:
        return
    cursors = _get_merged_mr_poll_cursors()
    current = cursors.get(branch, "")
    if current and current >= merged_at:
        return
    cursors[branch] = merged_at
    set_global_setting(
        MERGED_MR_POLL_CURSORS_KEY,
        json.dumps(cursors, sort_keys=True),
    )


def _latest_merged_at(mrs: list[dict]) -> str:
    return max((mr.get("merged_at") or "" for mr in mrs), default="")


def _filter_mrs_after_merged_cursor(mrs: list[dict], cursor: str) -> list[dict]:
    if not cursor:
        return mrs
    return [
        mr
        for mr in mrs
        if (mr.get("merged_at") or "") > cursor
    ]


def _is_title_check_notified(rule_id: int, mr_iid: int, mr_title: str) -> bool:
    conn = get_db()
    row = conn.execute(
        """SELECT 1 FROM notification_log
           WHERE rule_id = ? AND mr_iid = ? AND file_path = ?
             AND mr_title = ?
             AND gitlab_sent = 1""",
        (rule_id, mr_iid, "title_check", mr_title),
    ).fetchone()
    conn.close()
    return row is not None


def _log_title_check(
    rule_id: int,
    mr_iid: int,
    mr_title: str,
    mr_url: str,
    error_msg: str,
    discussion_id: str = "",
):
    conn = get_db()
    conn.execute(
        """INSERT INTO notification_log
           (rule_id, mr_iid, mr_title, mr_url, file_path, file_content,
            teams_sent, email_sent, gitlab_sent, gitlab_discussion_id, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_id,
            mr_iid,
            mr_title,
            mr_url,
            "title_check",
            error_msg,
            0,
            0,
            1,
            discussion_id,
            "",
        ),
    )
    conn.commit()
    conn.close()


def _get_title_check_discussion_ids(rule_id: int, mr_iid: int) -> list[str]:
    conn = get_db()
    rows = conn.execute(
        """SELECT gitlab_discussion_id FROM notification_log
           WHERE rule_id = ? AND mr_iid = ? AND file_path = ?
             AND gitlab_discussion_id != ''""",
        (rule_id, mr_iid, "title_check"),
    ).fetchall()
    conn.close()
    return [row["gitlab_discussion_id"] for row in rows]


def _clear_title_check_log(rule_id: int, mr_iid: int):
    conn = get_db()
    conn.execute(
        "DELETE FROM notification_log WHERE rule_id = ? AND mr_iid = ? AND file_path = ?",
        (rule_id, mr_iid, "title_check"),
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
            source_branch, target_branch, mr_created_at, mr_merged_at,
            changed_files_count, rules_checked, rules_matched,
            success, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            mr.get("merged_at", ""),
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
    rows = load_enabled_runtime_rules(conn)
    conn.close()

    groups: dict[int, list[dict]] = {}
    for row in rows:
        interval = max(1, row["poll_interval_seconds"] or default_interval)
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
        merged_cursor = _get_merged_mr_poll_cursor(branch) if state == "merged" else ""
        try:
            request_kwargs = {}
            if state == "merged":
                request_kwargs = {
                    "updated_after": merged_cursor,
                    "order_by": "merged_at",
                }
            if branch == "*":
                mrs = await get_merge_requests(
                    project_id,
                    state=state,
                    **request_kwargs,
                )
            else:
                mrs = await get_merge_requests(
                    project_id,
                    state=state,
                    target_branch=branch,
                    **request_kwargs,
                )
        except Exception as e:
            logger.error(
                "Failed to get MRs (branch=%s, state=%s): %s", branch, state, e
            )
            continue

        latest_merged_at = _latest_merged_at(mrs) if state == "merged" else ""
        if state == "merged" and not merged_cursor:
            _set_merged_mr_poll_cursor(branch, latest_merged_at)
            if latest_merged_at:
                logger.info(
                    "Initialized merged MR poll cursor for branch=%s at %s",
                    branch,
                    latest_merged_at,
                )
            continue
        if state == "merged":
            mrs = _filter_mrs_after_merged_cursor(mrs, merged_cursor)

        for mr in mrs:
            mr_iid = mr["iid"]
            mr_title = mr.get("title", "")
            mr_url = mr.get("web_url", "")

            if mr_title.strip().lower().startswith("draft"):
                continue

            if should_skip_by_global_title(mr_title):
                continue

            project_rules = [
                rule for rule in group_rules if rule_matches_mr_project(rule, mr_title)
            ]
            if not project_rules:
                continue

            pending_rule_ids = [
                r["id"] for r in project_rules if not _is_mr_processed(r["id"], mr_iid)
            ]
            tc_rule_ids = {
                r["id"] for r in project_rules if r.get("action_type") == "title_check"
            }
            pipeline_retry_rule_ids = {
                r["id"]
                for r in project_rules
                if r.get("action_type") == "pipeline_job_retry"
            }
            sonar_rule_ids = {
                r["id"]
                for r in project_rules
                if r.get("action_type") == "sonar_issues"
            }
            all_rule_ids = list(
                dict.fromkeys(
                    tc_rule_ids
                    | pipeline_retry_rule_ids
                    | sonar_rule_ids
                    | set(pending_rule_ids)
                )
            )
            if not all_rule_ids:
                continue

            xlsx_rules = [
                r
                for r in project_rules
                if r["id"] in pending_rule_ids and r.get("action_type") == "xlsx_review"
            ]
            code_review_rules = [
                r
                for r in project_rules
                if r["id"] in pending_rule_ids and r.get("action_type") == "code_review"
            ]
            title_check_rules = [
                r
                for r in project_rules
                if r["id"] in all_rule_ids and r.get("action_type") == "title_check"
            ]
            pipeline_check_rules = [
                r
                for r in project_rules
                if r["id"] in pending_rule_ids and r.get("action_type") == "pipeline_check"
            ]
            pipeline_retry_rules = [
                r
                for r in project_rules
                if r["id"] in all_rule_ids and r.get("action_type") == "pipeline_job_retry"
            ]
            sonar_rules = [
                r
                for r in project_rules
                if r["id"] in all_rule_ids and r.get("action_type") == "sonar_issues"
            ]
            notify_rules = [
                r
                for r in project_rules
                if r["id"] in pending_rule_ids
                and r.get("action_type", "notify") == "notify"
            ]

            total_matched = 0
            poll_notes: list[str] = []
            mr_target_branch = mr.get("target_branch", "") or branch

            for tc_rule in title_check_rules:
                valid, error_msg = is_title_valid(mr_title, mr_target_branch)
                if not valid:
                    if not _is_title_check_notified(tc_rule["id"], mr_iid, mr_title):
                        assignees = mr.get("assignees") or []
                        if not assignees:
                            assignee = mr.get("assignee")
                            if assignee and isinstance(assignee, dict):
                                assignees = [assignee]
                        mentions = " ".join(
                            f"@{a['username']}" for a in assignees if a.get("username")
                        )
                        comment_body = f"{mentions}\n\n{error_msg}".strip() if mentions else error_msg
                        try:
                            discussion = await post_merge_request_discussion(mr_iid, comment_body)
                            discussion_id = str((discussion or {}).get("id") or "")
                            total_matched += 1
                            _log_title_check(
                                tc_rule["id"],
                                mr_iid,
                                mr_title,
                                mr_url,
                                error_msg,
                                discussion_id,
                            )
                        except Exception as e:
                            logger.error(
                                "Title check discussion failed for MR !%s: %s", mr_iid, e
                            )
                else:
                    for discussion_id in _get_title_check_discussion_ids(tc_rule["id"], mr_iid):
                        try:
                            await resolve_merge_request_discussion(mr_iid, discussion_id)
                        except Exception as e:
                            logger.error(
                                "Title check discussion resolve failed for MR !%s discussion %s: %s",
                                mr_iid,
                                discussion_id,
                                e,
                            )
                    _clear_title_check_log(tc_rule["id"], mr_iid)

            for pc_rule in pipeline_check_rules:
                job_name = pc_rule.get("content_match", "") or "changelog:validate"
                try:
                    result = await check_pipeline_job_failed(
                        project_id, mr_iid, job_name
                    )
                except Exception as e:
                    logger.error(
                        "Pipeline check failed for MR !%s: %s", mr_iid, e
                    )
                    result = PipelineCheckResult()

                if result.failed:
                    assignees = mr.get("assignees") or []
                    if not assignees:
                        assignee = mr.get("assignee")
                        if assignee and isinstance(assignee, dict):
                            assignees = [assignee]
                    mentions = " ".join(
                        f"@{a['username']}" for a in assignees if a.get("username")
                    )
                    body = ""
                    if mentions:
                        body = f"{mentions} "
                    body += "Changelog не прошёл валидацию"
                    if result.job_web_url:
                        body += f"\n\n[Ссылка на job]({result.job_web_url})"
                    try:
                        await post_merge_request_discussion(mr_iid, body)
                        total_matched += 1
                    except Exception as e:
                        logger.error(
                            "Pipeline check discussion failed for MR !%s: %s",
                            mr_iid,
                            e,
                        )

                if result.failed or result.completed:
                    _mark_mr_processed(pc_rule["id"], mr_iid)

            for retry_rule in pipeline_retry_rules:
                job_names = parse_retry_job_names(retry_rule.get("content_match", ""))
                try:
                    retry_result = await retry_failed_config_jobs(
                        project_id,
                        mr_iid,
                        job_names,
                        retry_rule["id"],
                    )
                    total_matched += len(retry_result.retried)
                    if retry_result.retried:
                        retried_jobs = ", ".join(
                            str(item.get("job_name") or item.get("job_id"))
                            for item in retry_result.retried
                        )
                        poll_notes.append(f"retried jobs: {retried_jobs}")
                    if retry_result.skipped:
                        skipped_jobs = ", ".join(
                            f"{item.get('job_name')}:{item.get('reason')}"
                            for item in retry_result.skipped
                        )
                        poll_notes.append(f"skipped jobs: {skipped_jobs}")
                    if retry_result.checked == 0:
                        poll_notes.append(
                            f"no failed target jobs: {', '.join(job_names)}"
                        )
                    if retry_result.errors:
                        poll_notes.append(
                            f"retry errors: {', '.join(retry_result.errors)}"
                        )
                except Exception as e:
                    logger.error(
                        "Pipeline job retry check failed for MR !%s: %s", mr_iid, e
                    )
                    poll_notes.append(f"pipeline job retry error: {e}")

            for sonar_rule in sonar_rules:
                job_name = parse_sonar_job_name(sonar_rule.get("content_match", ""))
                try:
                    sonar_result = await publish_sonar_issues_after_job(
                        project_id,
                        mr_iid,
                        job_name,
                        sonar_rule["id"],
                        mr_title,
                        mr_url,
                    )
                    total_matched += len(sonar_result.published)
                    if sonar_result.published:
                        published_jobs = ", ".join(
                            str(item.get("job_name") or item.get("job_id"))
                            for item in sonar_result.published
                        )
                        poll_notes.append(f"published sonar issues: {published_jobs}")
                    if sonar_result.skipped:
                        skipped_jobs = ", ".join(
                            f"{item.get('job_name')}:{item.get('reason')}"
                            for item in sonar_result.skipped
                        )
                        poll_notes.append(f"skipped sonar issues: {skipped_jobs}")
                    if sonar_result.errors:
                        poll_notes.append(
                            f"sonar publish errors: {', '.join(sonar_result.errors)}"
                        )
                except Exception as e:
                    logger.error(
                        "Sonar issues publish check failed for MR !%s: %s", mr_iid, e
                    )
                    poll_notes.append(f"sonar publish error: {e}")

            rest_rule_ids = [
                rid for rid in pending_rule_ids
                if rid not in {r["id"] for r in title_check_rules}
                and rid not in {r["id"] for r in pipeline_check_rules}
                and rid not in {r["id"] for r in pipeline_retry_rules}
                and rid not in {r["id"] for r in sonar_rules}
            ]
            if not rest_rule_ids:
                try:
                    _log_polled_mr(
                        mr,
                        0,
                        len(all_rule_ids),
                        total_matched,
                        True,
                        "; ".join(poll_notes),
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
                    mr,
                    len(changed_files),
                    len(all_rule_ids),
                    total_matched,
                    True,
                    "; ".join(poll_notes),
                )
            except Exception as e:
                logger.error("Failed to log polled MR !%s: %s", mr_iid, e)

        if state == "merged":
            _set_merged_mr_poll_cursor(branch, latest_merged_at)


async def _run_dynamic_polling():
    """Run poll groups from the current DB schedule so interval edits take effect."""
    last_run: dict[int, float] = {}
    loop = asyncio.get_running_loop()

    while True:
        from env_reload import reload_dotenv

        reload_dotenv()
        default_interval = max(1, int(os.getenv("POLL_INTERVAL_SECONDS", "300")))
        groups = _get_rules_grouped_by_schedule()
        now = loop.time()

        if not groups:
            await asyncio.sleep(default_interval)
            continue

        due_intervals = [
            interval
            for interval in groups
            if interval not in last_run or now - last_run[interval] >= interval
        ]

        for interval in sorted(due_intervals):
            rules = groups.get(interval, [])
            if not rules:
                last_run[interval] = now
                continue
            logger.info(
                "[%s] Polling %d rule(s), interval=%ds",
                datetime.now().strftime("%H:%M:%S"),
                len(rules),
                interval,
            )
            await poll_once(rules)
            last_run[interval] = loop.time()

        active_intervals = set(groups)
        for interval in list(last_run):
            if interval not in active_intervals:
                last_run.pop(interval, None)

        now = loop.time()
        sleep_for = min(
            max(1.0, interval - (now - last_run.get(interval, 0)))
            for interval in groups
        )
        await asyncio.sleep(sleep_for)


async def start_polling():
    """Запускает фоновые задачи опроса для каждого уникального интервала."""
    logger.info("Starting GitLab MR poller...")
    await _run_dynamic_polling()
