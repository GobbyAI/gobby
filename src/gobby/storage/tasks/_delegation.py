"""Durable task delegation to another live session."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES, TERMINAL_SESSION_STATUSES
from gobby.storage.tasks._models import Task


def _session_ref(seq_num: int | None, session_id: str) -> str:
    """Audit name for a session: its #seq, or its stored UUID when it has no seq."""
    return f"#{seq_num}" if seq_num is not None else session_id


def delegate_task(
    db: HubDatabase,
    task_id: str,
    *,
    delegated_by_session_id: str,
    delegated_to_session_id: str,
    reason: str,
) -> Task:
    """Record a filer or ended-filer receiver delegation under the task row lock."""
    reason = reason.strip()
    if not reason:
        raise ValueError("Delegation reason is required")
    if delegated_by_session_id == delegated_to_session_id:
        raise ValueError("Cannot delegate a task to self")

    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT t.created_in_session_id, t.project_id, t.claimed_by_session_id,
                   t.closed_at, t.delegated_to_session_id,
                   filer.status AS filer_status,
                   actor.status AS actor_status, actor.project_id AS actor_project_id,
                   target.status AS target_status, target.project_id AS target_project_id
            FROM tasks t
            LEFT JOIN sessions filer ON filer.id = t.created_in_session_id
            LEFT JOIN sessions actor ON actor.id = %s
            LEFT JOIN sessions target ON target.id = %s
            WHERE t.id = %s
            FOR UPDATE OF t
            """,
            (delegated_by_session_id, delegated_to_session_id, task_id),
        ).fetchone()
        if row is None:
            raise ValueError("Task not found")
        is_filer = str(row["created_in_session_id"]) == delegated_by_session_id
        is_current_receiver_after_filer_ended = (
            str(row["delegated_to_session_id"]) == delegated_by_session_id
            and (
                row["filer_status"] in TERMINAL_SESSION_STATUSES or row["filer_status"] == "closed"
            )
            and row["actor_status"] in LIVE_SESSION_STATUSES
            and row["actor_project_id"] == row["project_id"]
        )
        if not (is_filer or is_current_receiver_after_filer_ended):
            raise ValueError(
                "Only the session that filed the task or its current receiver after the filer "
                "ended may delegate it"
            )
        if row["closed_at"] is not None or row["claimed_by_session_id"] is not None:
            raise ValueError("Only an open, unclaimed task may be delegated")
        if row["target_status"] not in LIVE_SESSION_STATUSES:
            raise ValueError("Delegation requires a live receiving session")
        if row["target_project_id"] != row["project_id"]:
            raise ValueError("The receiving session must belong to the task project")

        updated = conn.execute(
            """
            UPDATE tasks
            SET delegated_to_session_id = %s, delegated_by_session_id = %s,
                delegation_reason = %s, delegated_at = now(), updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (delegated_to_session_id, delegated_by_session_id, reason, task_id),
        ).fetchone()
        if updated is None:
            raise ValueError("Task not found")
        return Task.from_row(updated)


def transfer_task_authority(
    db: HubDatabase,
    task_id: str,
    *,
    caller_session_id: str,
    reason: str,
) -> Task:
    """Take over activation-receipt authority from an expired creator/delegator (#23157).

    A coordinator that filed or delegated a task can expire before the task
    closes, leaving no live session able to record the activation receipt the
    close reviewer needs. This is the one supported recovery: a live session in
    the task's project takes the delegator slot the recorded authority held, so
    the existing creator-or-delegator check accepts it without a receipt-rule
    change. The task's claimant and its close reviewers stay excluded.
    """
    from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT

    reason = reason.strip()
    if not reason:
        raise ValueError("Authority transfer reason is required")

    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT t.created_in_session_id::text AS created_in_session_id,
                   t.delegated_by_session_id::text AS delegated_by_session_id,
                   t.claimed_by_session_id::text AS claimed_by_session_id,
                   t.closed_at, t.delegation_reason, t.project_id,
                   creator.status AS creator_status, creator.seq_num AS creator_seq,
                   delegator.status AS delegator_status, delegator.seq_num AS delegator_seq,
                   actor.status AS actor_status, actor.project_id AS actor_project_id,
                   actor.seq_num AS actor_seq
            FROM tasks t
            LEFT JOIN sessions creator ON creator.id = t.created_in_session_id
            LEFT JOIN sessions delegator ON delegator.id = t.delegated_by_session_id
            LEFT JOIN sessions actor ON actor.id = %s
            WHERE t.id = %s
            FOR UPDATE OF t
            """,
            (caller_session_id, task_id),
        ).fetchone()
        if row is None:
            raise ValueError("Task not found")
        if row["closed_at"] is not None:
            raise ValueError("A closed task's authority cannot be transferred")
        if str(row["claimed_by_session_id"]) == caller_session_id:
            raise ValueError("the task's claimant cannot take over receipt authority")
        reviewer = conn.execute(
            """
            SELECT 1 FROM agent_runs
            WHERE agent_name = %s
              AND (child_session_id = %s
                   OR id = (SELECT agent_run_id FROM sessions WHERE id = %s))
            LIMIT 1
            """,
            (TASK_CLOSE_REVIEWER_AGENT, caller_session_id, caller_session_id),
        ).fetchone()
        if reviewer is not None:
            raise ValueError("a task-close reviewer cannot take over receipt authority")
        if (
            row["actor_status"] not in LIVE_SESSION_STATUSES
            or row["actor_project_id"] != row["project_id"]
        ):
            raise ValueError("only a live session in the task's project may take over authority")

        if str(row["delegated_by_session_id"]) == caller_session_id:
            existing = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
            if existing is None:
                raise ValueError("Task not found")
            return Task.from_row(existing)

        if (
            row["creator_status"] in LIVE_SESSION_STATUSES
            or row["delegator_status"] in LIVE_SESSION_STATUSES
        ):
            raise ValueError(
                "the task's creator or delegator is still live and keeps its authority"
            )

        slot = "delegator" if row["delegated_by_session_id"] is not None else "creator"
        expired_id = row["delegated_by_session_id"] or row["created_in_session_id"]
        if expired_id is None:
            expired = "no recorded session"
        else:
            expired = (
                f"{_session_ref(row[f'{slot}_seq'], expired_id)} (status={row[f'{slot}_status']})"
            )
        transfer_note = (
            f"authority transferred from {expired} "
            f"to {_session_ref(row['actor_seq'], caller_session_id)}: {reason}"
        )
        prior_reason = row["delegation_reason"]
        combined_reason = f"{prior_reason} | {transfer_note}" if prior_reason else transfer_note

        updated = conn.execute(
            """
            UPDATE tasks
            SET delegated_by_session_id = %s, delegation_reason = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (caller_session_id, combined_reason, task_id),
        ).fetchone()
        if updated is None:
            raise ValueError("Task not found")
        return Task.from_row(updated)
