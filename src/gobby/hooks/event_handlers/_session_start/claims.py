"""Task-claim reassignment for a clear-session successor."""

from __future__ import annotations

import logging
from typing import Any

from gobby.storage.tasks import TaskAlreadyClaimedError, TaskClosedError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable
from gobby.workflows.found_work_gate import (
    FOUND_WORK_GATE_ARMED_AT_VARIABLE,
    arm_found_work_gate,
    arm_found_work_gate_from_task_links,
    found_work_gate_task_link_armed_at,
)


def rehydrate_found_work_gate_arm(handler: Any, session_id: str) -> None:
    """Restore found-work duty from persisted claim/close session-task links."""
    session_manager = getattr(handler, "_session_manager", None)
    session_task_manager = getattr(handler, "_session_task_manager", None)
    if session_manager is None or session_task_manager is None:
        return
    try:
        from gobby.workflows.state_manager import SessionVariableManager

        variable_manager = SessionVariableManager(session_manager.db)
        variables = dict(variable_manager.get_variables(session_id) or {})
        links = session_task_manager.get_session_tasks(session_id)
        if arm_found_work_gate_from_task_links(variables, links):
            variable_manager.merge_variables(
                session_id,
                {FOUND_WORK_GATE_ARMED_AT_VARIABLE: variables[FOUND_WORK_GATE_ARMED_AT_VARIABLE]},
            )
        elif (
            FOUND_WORK_GATE_ARMED_AT_VARIABLE in variables
            and found_work_gate_task_link_armed_at(links) is None
        ):
            variable_manager.merge_variables(session_id, {FOUND_WORK_GATE_ARMED_AT_VARIABLE: None})
    except Exception as exc:
        _log(handler).debug(
            "Could not rehydrate found-work gate arm for session=%s: %s",
            session_id,
            exc,
            exc_info=True,
        )


logger = logging.getLogger(__name__)

MCP_PROXY_READY_VARIABLE = "_mcp_proxy_ready"


def inherited_mcp_proxy_ready(predecessor_vars: dict[str, Any]) -> dict[str, Any]:
    """Carry the predecessor's Gobby MCP proxy readiness onto a clear successor.

    The successor runs in the predecessor's CLI process, whose stdio bridge
    reported readiness once, to the predecessor session.
    """
    if predecessor_vars.get(MCP_PROXY_READY_VARIABLE) is True:
        return {MCP_PROXY_READY_VARIABLE: True}
    return {}


def preserve_task_claim_state(
    handler: Any,
    sv_mgr: Any,
    successor_id: str,
    predecessor_id: str,
    predecessor_vars: dict[str, Any],
) -> None:
    """Reassign predecessor claims onto the successor after a winning take."""
    task_claim_keys = ("task_claimed", "claimed_tasks")
    task_handoff = {
        key: predecessor_vars[key] for key in task_claim_keys if predecessor_vars.get(key)
    }
    if not task_handoff:
        return

    claimed_tasks = _as_claimed_tasks(task_handoff.get("claimed_tasks"))
    merged_claims: dict[str, Any] = {}

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
        arm_found_work_gate(
            merged_claims,
            occurred_at=predecessor_vars.get(FOUND_WORK_GATE_ARMED_AT_VARIABLE),
        )
    if merged_claims and sv_mgr is not None:
        try:
            sv_mgr.merge_variables(successor_id, merged_claims, reconcile_claims=True)
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

    # claim_task records the successor's 'claimed' session_tasks link in its own transaction.
    return True


def _log(handler: Any) -> logging.Logger:
    maybe = getattr(handler, "logger", None)
    if isinstance(maybe, logging.Logger):
        return maybe
    return logger
