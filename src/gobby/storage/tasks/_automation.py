"""Task hold labels and stale-claim helpers."""

import json
import logging
from collections.abc import Mapping
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.storage.sql_dialect import json_array_contains_condition
from gobby.storage.task_close_reviews import ACTIVE_TASK_CLOSE_REVIEW_STATUSES
from gobby.storage.tasks._owner_shield import (
    OWNER_MAY_RESUME_GUARD_SQL,
    owner_may_resume_guard_params,
)
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

# Labels that hold a task out of automatic closure until a person acts on it:
# a decision to make, a clean window to
# wait for, or a spec still to be written for work a plan deferred. The task
# stays visible and claimable by hand.
HOLD_LABELS = frozenset({"needs-decision", "clean-window", "needs-planning"})


def has_hold_label(task: object) -> bool:
    """Return whether a task carries a label that needs a person's action first.

    Accepts Task objects and raw task rows: rows keep labels as the JSON text
    the storage layer writes, so decode that form before matching.
    """
    labels = getattr(task, "labels", None)
    if labels is None and isinstance(task, Mapping):
        labels = task.get("labels")
    if isinstance(labels, str):
        labels = json.loads(labels) if labels else ()
    return bool(HOLD_LABELS.intersection(labels or ()))


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
    params: list[Any] = [now, task_id, expected_owner_session_id]
    params.extend(live_session_params)
    params.extend(owner_may_resume_guard_params(now))

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
               AND {OWNER_MAY_RESUME_GUARD_SQL}
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
    """Release task claims held by sessions that are no longer active.

    A task whose close review is still active keeps its claim: the close
    fingerprint keys on the claim owner, so releasing it would turn the
    pending verdict stale even though the headless caller already exited.
    """
    live_session_clause, live_session_params = json_array_contains_condition(
        db,
        "tasks.labels",
        "live-session",
    )
    params: list[Any] = list(live_session_params)
    params.append(list(LIVE_SESSION_STATUS_ORDER))
    params.append(list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES))
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
              AND s.status = ANY(%s)
           )
           AND NOT EXISTS (
               SELECT 1 FROM task_close_reviews review
                WHERE review.task_id = tasks.id
                  AND review.status = ANY(%s)
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
