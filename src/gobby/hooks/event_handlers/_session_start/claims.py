"""Task-claim reassignment for a clear_self successor."""

from __future__ import annotations

import logging
from typing import Any

from gobby.storage.tasks import TaskAlreadyClaimedError, TaskClosedError
from gobby.storage.tasks._transitions import release_task_claim_if_owned
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable

logger = logging.getLogger(__name__)


def preserve_task_claim_state(
    handler: Any,
    sv_mgr: Any,
    successor_id: str,
    predecessor_id: str,
    predecessor_vars: dict[str, Any],
) -> None:
    """Reassign predecessor claims onto the successor after a winning take."""
    task_claim_keys = ("task_claimed", "claimed_tasks", "session_had_task")
    task_handoff = {
        key: predecessor_vars[key] for key in task_claim_keys if predecessor_vars.get(key)
    }
    if not task_handoff:
        return

    claimed_tasks = _as_claimed_tasks(task_handoff.get("claimed_tasks"))
    merged_claims: dict[str, Any] = {}
    if task_handoff.get("session_had_task"):
        merged_claims["session_had_task"] = True

    filtered_claims: dict[str, str] = {}
    if task_handoff.get("task_claimed") and claimed_tasks:
        filtered_claims = filter_and_reassign_claimed_tasks(
            handler,
            successor_id,
            predecessor_id,
            claimed_tasks,
        )
    if filtered_claims:
        merged_claims["task_claimed"] = True
        merged_claims["claimed_tasks"] = filtered_claims
    if merged_claims and sv_mgr is not None:
        try:
            sv_mgr.merge_variables(successor_id, merged_claims)
        except Exception as e:
            _log(handler).warning(
                "Failed to merge successor claim variables for session=%s: %s",
                successor_id,
                e,
            )


def filter_and_reassign_claimed_tasks(
    handler: Any,
    successor_id: str,
    predecessor_id: str,
    claimed_tasks: dict[str, str],
) -> dict[str, str]:
    """Transfer each predecessor claim with expected-owner CAS; skip failures."""
    if handler._task_manager is None or handler._session_task_manager is None:
        return {}

    filtered_claims: dict[str, str] = {}
    for claimed_id, claimed_ref in claimed_tasks.items():
        if not isinstance(claimed_id, str) or not claimed_id:
            continue
        ref = claimed_ref if isinstance(claimed_ref, str) and claimed_ref else claimed_id
        if _transfer_claimed_task(handler, successor_id, predecessor_id, claimed_id):
            filtered_claims[claimed_id] = ref
    return filtered_claims


def _as_claimed_tasks(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    claimed: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        claimed[key] = value if isinstance(value, str) and value else key
    return claimed


def _transfer_claimed_task(
    handler: Any,
    successor_id: str,
    predecessor_id: str,
    claimed_id: str,
) -> bool:
    log = _log(handler)
    try:
        task_obj = handler._task_manager.get_task(claimed_id)
    except Exception as e:
        log.debug(
            "Best-effort task lookup failed for session=%s task=%s: %s",
            successor_id,
            claimed_id,
            e,
        )
        return False

    if task_obj is None or not is_task_actionable(task_obj):
        return False

    current_owner = get_claimed_session_id(task_obj)
    if current_owner not in (None, predecessor_id, successor_id):
        log.debug(
            "Skipping task handoff for session=%s task=%s; already assigned to %s",
            successor_id,
            claimed_id,
            current_owner,
        )
        return False

    transferred_ownership = current_owner != successor_id
    try:
        handler._task_manager.claim_task(
            claimed_id,
            session_id=successor_id,
            expected_owner=predecessor_id,
        )
    except (TaskAlreadyClaimedError, TaskClosedError) as e:
        log.debug(
            "Skipping task handoff for session=%s task=%s: %s",
            successor_id,
            claimed_id,
            e,
        )
        return False
    except Exception as e:
        log.debug(
            "Best-effort task re-assignment failed for session=%s task=%s: %s",
            successor_id,
            claimed_id,
            e,
        )
        return False

    try:
        handler._session_task_manager.link_task(successor_id, claimed_id, "claimed")
    except Exception as e:
        log.debug(
            "Session-task link failed for session=%s task=%s: %s",
            successor_id,
            claimed_id,
            e,
        )
        if transferred_ownership:
            _compensate_claim(handler, claimed_id, predecessor_id, successor_id)
        return False
    return True


def _compensate_claim(
    handler: Any,
    claimed_id: str,
    predecessor_id: str,
    successor_id: str,
) -> None:
    log = _log(handler)
    try:
        handler._task_manager.claim_task(
            claimed_id,
            session_id=predecessor_id,
            expected_owner=successor_id,
        )
        return
    except Exception as e:
        log.debug(
            "Failed to restore predecessor claim for session=%s task=%s: %s",
            predecessor_id,
            claimed_id,
            e,
        )
    db = getattr(handler._task_manager, "db", None)
    if db is None:
        return
    try:
        release_task_claim_if_owned(db, claimed_id, expected_owner=successor_id)
    except Exception as e:
        log.debug(
            "Failed to drop successor claim after link failure for task=%s: %s",
            claimed_id,
            e,
        )


def _log(handler: Any) -> logging.Logger:
    maybe = getattr(handler, "logger", None)
    if isinstance(maybe, logging.Logger):
        return maybe
    return logger
