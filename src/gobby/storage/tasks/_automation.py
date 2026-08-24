"""Task automation candidate and stale-claim helpers."""

import logging
from datetime import timedelta
from typing import Any

from gobby.sessions.compact_markers import (
    COMPACT_SELF_CONTINUE_FRESH_SECONDS,
    COMPACT_SELF_CONTINUE_VARIABLE,
)
from gobby.sessions.contested_expiry import (
    CONTESTED_EXPIRY_CAUSES,
    CONTESTED_TERMINAL_EXPIRY_VARIABLE,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.sql_dialect import json_array_contains_condition
from gobby.storage.tasks._ancestor_gate import find_child_development_ancestor_gate
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._epic_gate import find_epic_descendant_gate
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


def _is_unattended(task: Any) -> bool:
    """Return whether dispatch should avoid human escalation for a task."""
    return bool(getattr(task, "unattended", False))


def is_blocked_by_deps(task: object) -> bool:
    """Return whether a task has unresolved blocking dependencies."""
    active_blocked_by = getattr(task, "active_blocked_by", None)
    if active_blocked_by is not None:
        return bool(active_blocked_by)
    blocked_by = getattr(task, "blocked_by", None)
    return bool(blocked_by)


def list_automation_candidates(
    db: HubDatabase,
    *,
    project_id: str | None = None,
    explicit_task_ids: tuple[str, ...] | None = None,
) -> list[Task]:
    """List unclaimed, unleased, dependency-ready tasks eligible for dispatch."""
    if explicit_task_ids == ():
        return []

    now = utc_now()
    params: list[Any] = []
    if explicit_task_ids is None:
        candidate_filter = "tasks.allow_automation IS TRUE"
    else:
        candidate_filter = "tasks.id = ANY(%s)"
        params.append(list(explicit_task_ids))
    params.append(now)
    project_filter = ""
    if project_id is not None:
        project_filter = "AND tasks.project_id = %s"
        params.append(project_id)

    rows = db.fetchall(
        f"""
        SELECT tasks.*
        FROM tasks
        JOIN task_stage_states current_stage
          ON current_stage.task_id = tasks.id
         AND current_stage.state != 'done'
         AND current_stage.position = (
             SELECT MIN(stage_scan.position)
               FROM task_stage_states stage_scan
              WHERE stage_scan.task_id = tasks.id
                AND stage_scan.state != 'done'
        )
        LEFT JOIN task_dispatch_mutex mutex ON mutex.task_id = tasks.id
        WHERE {candidate_filter}
          AND tasks.claimed_by_session_id IS NULL
          AND tasks.closed_at IS NULL
          AND tasks.escalated_at IS NULL
          AND COALESCE(tasks.is_escalated, FALSE) IS FALSE
          AND current_stage.state IN ('ready', 'in_progress', 'needs_review', 'review_approved')
          AND (
              mutex.task_id IS NULL
              OR mutex.lease_until IS NULL
              OR mutex.lease_until < %s
          )
          {project_filter}
        ORDER BY tasks.priority ASC, tasks.seq_num ASC, tasks.created_at ASC
        """,  # nosec B608 # filters are static SQL selected above.
        tuple(params),
    )
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    ready_with_gate = [
        (task, find_epic_descendant_gate(db, task))
        for task in tasks
        if not is_blocked_by_deps(task) and find_child_development_ancestor_gate(db, task) is None
    ]
    return [
        task
        for task, _gate in sorted(
            ready_with_gate,
            key=lambda item: (
                item[1] is None,
                item[0].priority,
                item[0].seq_num if item[0].seq_num is not None else 2**31,
                item[0].created_at,
            ),
        )
    ]


def release_task_claim(
    db: HubDatabase,
    task_id: str,
    *,
    expected_owner_session_id: str,
    actor: str,
    reason: str,
) -> bool:
    """Release one stale claim if its owner remains ineligible."""
    now = utc_now()
    live_session_clause, live_session_params = json_array_contains_condition(
        db,
        "tasks.labels",
        "live-session",
    )
    compact_cutoff = now - timedelta(seconds=COMPACT_SELF_CONTINUE_FRESH_SECONDS)
    revival_cutoff = now - timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS)
    params: list[Any] = [now, task_id, expected_owner_session_id]
    params.extend(live_session_params)
    params.extend(
        (
            COMPACT_SELF_CONTINUE_VARIABLE,
            COMPACT_SELF_CONTINUE_VARIABLE,
            COMPACT_SELF_CONTINUE_VARIABLE,
            compact_cutoff.isoformat(),
            COMPACT_SELF_CONTINUE_VARIABLE,
            now.isoformat(),
            CONTESTED_TERMINAL_EXPIRY_VARIABLE,
            CONTESTED_TERMINAL_EXPIRY_VARIABLE,
            sorted(CONTESTED_EXPIRY_CAUSES),
            CONTESTED_TERMINAL_EXPIRY_VARIABLE,
            CONTESTED_TERMINAL_EXPIRY_VARIABLE,
            revival_cutoff.isoformat(),
            CONTESTED_TERMINAL_EXPIRY_VARIABLE,
            now.isoformat(),
        )
    )

    with db.transaction() as conn:
        cursor = conn.execute(
            f"""
            UPDATE tasks
               SET claimed_by_session_id = NULL,
                   updated_at = %s
             WHERE id = %s
               AND claimed_by_session_id = %s
               AND closed_at IS NULL
               AND escalated_at IS NULL
               AND COALESCE(is_escalated, FALSE) IS FALSE
               AND NOT COALESCE(({live_session_clause}), FALSE)
               AND NOT EXISTS (
                   SELECT 1 FROM sessions s
                    WHERE s.id = tasks.claimed_by_session_id
                      AND s.status IN ('active', 'paused', 'handoff_ready')
               )
               AND NOT EXISTS (
                   -- A fresh compact-continue marker means the owner is
                   -- mid-compaction and will resume; its lifecycle status can be
                   -- transiently stale, so marker age (not claim age) decides
                   -- eligibility here.
                   SELECT 1
                     FROM session_variables sv
                    WHERE sv.session_id = tasks.claimed_by_session_id
                      AND jsonb_typeof(
                          sv.variables -> %s
                      ) = 'object'
                      AND jsonb_typeof(
                          sv.variables -> %s -> 'created_at'
                      ) = 'string'
                      AND sv.variables -> %s ->> 'created_at' >= %s
                      AND sv.variables -> %s ->> 'created_at' <= %s
               )
               AND NOT EXISTS (
                   -- SessionStart expires every terminal session sharing a
                   -- reused terminal context before anything validates who owns
                   -- the terminal; revive_expired_terminal_session settles that
                   -- afterwards and routinely reverses it. That writer records
                   -- the cause on the way out, so the owner's status is
                   -- transiently stale in the same way a mid-compaction owner's
                   -- is, and the claim outlives it. An expiry that left no
                   -- marker -- inactivity, a killed tmux server, an explicit
                   -- close -- is final and keeps the ordinary schedule, which
                   -- is what stops this from shadowing the marker grace above
                   -- for every other expired terminal session. session_variables
                   -- is a shared store, so the cause has to name one of the two
                   -- speculative writers: a fresh created_at left under this key
                   -- by anything else is not a contest.
                   SELECT 1
                     FROM sessions s
                     JOIN session_variables sv ON sv.session_id = s.id
                    WHERE s.id = tasks.claimed_by_session_id
                      AND s.session_type = 'terminal'
                      AND s.status = 'expired'
                      AND jsonb_typeof(sv.variables -> %s) = 'object'
                      AND sv.variables -> %s ->> 'cause' = ANY(%s)
                      AND jsonb_typeof(
                          sv.variables -> %s -> 'created_at'
                      ) = 'string'
                      AND sv.variables -> %s ->> 'created_at' >= %s
                      AND sv.variables -> %s ->> 'created_at' <= %s
               )
            """,
            tuple(params),
        )

    if cursor.rowcount != 1:
        return False

    logger.info(
        "Released task claim task_id=%s owner_session_id=%s actor=%s reason=%s",
        task_id,
        expected_owner_session_id,
        actor,
        reason,
    )
    return True


def sweep_stale_claims(
    db: HubDatabase,
    *,
    project_id: str | None = None,
) -> int:
    """Release task claims held by sessions that are no longer active."""
    live_session_clause, live_session_params = json_array_contains_condition(
        db,
        "tasks.labels",
        "live-session",
    )
    params: list[Any] = list(live_session_params)
    project_filter = ""
    if project_id is not None:
        project_filter = "AND tasks.project_id = %s"
        params.append(project_id)

    rows = db.fetchall(
        f"""
        SELECT tasks.id,
               tasks.claimed_by_session_id,
               owner.status AS owner_status
          FROM tasks
          LEFT JOIN sessions owner ON owner.id = tasks.claimed_by_session_id
         WHERE tasks.closed_at IS NULL
           AND tasks.escalated_at IS NULL
           AND COALESCE(tasks.is_escalated, FALSE) IS FALSE
           AND tasks.claimed_by_session_id IS NOT NULL
           AND NOT COALESCE(({live_session_clause}), FALSE)
           AND NOT EXISTS (
               SELECT 1 FROM sessions s
                WHERE s.id = tasks.claimed_by_session_id
                  AND s.status IN ('active', 'paused', 'handoff_ready')
           )
           {project_filter}
        """,  # nosec B608 # project_filter is static SQL selected above.
        tuple(params),
    )

    released = 0
    for row in rows:
        owner_session_id = row["claimed_by_session_id"]
        if not isinstance(owner_session_id, str):
            continue
        owner_status = row["owner_status"]
        reason = (
            "owner session is missing"
            if owner_status is None
            else f"owner session status is {owner_status}"
        )
        if release_task_claim(
            db,
            str(row["id"]),
            expected_owner_session_id=owner_session_id,
            actor="sweep_stale_claims",
            reason=reason,
        ):
            released += 1
    return released
