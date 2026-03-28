import asyncio
import logging
import os
from datetime import datetime

from db import get_db
from services.gitlab_client import get_project_id, get_merge_requests, get_mr_changes, get_file_content
from services.rules_engine import evaluate_rules_for_mr
from services.notification_dispatcher import dispatch_notifications

logger = logging.getLogger(__name__)

_project_id: int | None = None


async def _resolve_project_id() -> int:
    global _project_id
    if _project_id is None:
        _project_id = await get_project_id()
        logger.info("Resolved project ID: %s", _project_id)
    return _project_id


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


def _get_rules_grouped_by_schedule() -> dict[int, list[dict]]:
    """
    Группирует правила по интервалу опроса.
    Возвращает {interval_seconds: [rule, ...]}
    """
    default_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notification_rules WHERE enabled = 1"
    ).fetchall()
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

    # Группируем правила по (target_branch, mr_state) чтобы не дублировать API-запросы
    branch_state_rules: dict[tuple[str, str], list[dict]] = {}
    for rule in rules:
        key = (rule["target_branch"], rule["mr_state"])
        branch_state_rules.setdefault(key, []).append(rule)

    for (branch, state), group_rules in branch_state_rules.items():
        try:
            mrs = await get_merge_requests(project_id, state=state, target_branch=branch)
        except Exception as e:
            logger.error("Failed to get MRs (branch=%s, state=%s): %s", branch, state, e)
            continue

        for mr in mrs:
            mr_iid = mr["iid"]
            mr_title = mr["title"]
            mr_url = mr["web_url"]
            mr_branch = mr.get("target_branch", branch)

            # Определяем какие правила ещё не обработали этот MR
            pending_rule_ids = [
                r["id"] for r in group_rules
                if not _is_mr_processed(r["id"], mr_iid)
            ]
            if not pending_rule_ids:
                continue

            try:
                changed_files = await get_mr_changes(project_id, mr_iid)
            except Exception as e:
                logger.error("Failed to get changes for MR !%s: %s", mr_iid, e)
                continue

            async def fetch_content(file_path: str) -> str:
                return await get_file_content(project_id, file_path, mr_branch)

            matches = await evaluate_rules_for_mr(pending_rule_ids, changed_files, fetch_content)

            if matches:
                await dispatch_notifications(matches, mr_iid, mr_title, mr_url)

            # Помечаем MR как обработанный для всех правил в группе
            for rule_id in pending_rule_ids:
                _mark_mr_processed(rule_id, mr_iid)


async def _run_poll_loop(interval: int, rule_getter):
    """Цикл опроса с заданным интервалом."""
    while True:
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
            tasks.append(asyncio.create_task(_run_poll_loop(interval, get_rules_for_interval)))

    # Запускаем дефолтный интервал если нет других
    if default_interval not in seen_intervals:
        tasks.append(asyncio.create_task(_run_poll_loop(default_interval, get_rules_for_interval)))

    # Ждём все таски (бесконечно)
    await asyncio.gather(*tasks)
