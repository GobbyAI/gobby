"""Transactional storage primitives for durable daemon-stop agent resume."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.agents.resume_metadata import dump_resume_metadata, normalize_resume_metadata
from gobby.storage.daemon_resume_keys import (
    CONSUMED_AT_KEY as _CONSUMED_AT_KEY,
)
from gobby.storage.daemon_resume_keys import (
    CONSUMED_BY_KEY as _CONSUMED_BY_KEY,
)
from gobby.storage.daemon_resume_keys import (
    RESUME_PHASE_KEY as _RESUME_PHASE_KEY,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now


@dataclass(frozen=True)
class FinalizeDaemonResumeResult:
    """Durable result of an original-to-successor ownership handoff."""

    original_run_id: str
    successor_run_id: str
    child_session_id: str
    subscriber_session_ids: tuple[str, ...]
    already_finalized: bool


@dataclass(frozen=True)
class DaemonResumeWaitTarget:
    """Authoritative run selected while holding the resume-chain fence."""

    run_id: str
    status: str
    recovery_pending: bool
    subscriber_inserted: bool


def _resume_fence_name(run_id: str) -> str:
    return f"daemon-resume:{run_id}"


def _lock_resume_fence(conn: Any, run_id: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (_resume_fence_name(run_id),),
    )


def _metadata(value: Any) -> dict[str, Any]:
    return normalize_resume_metadata(value) or {}


def finalize_daemon_resume(
    db: HubDatabase,
    *,
    original_run_id: str,
    successor_run_id: str,
    child_session_id: str,
) -> FinalizeDaemonResumeResult:
    """Consume an original run and transfer its durable ownership atomically."""
    now = utc_now()
    with db.transaction() as conn:
        _lock_resume_fence(conn, original_run_id)
        original = conn.execute(
            """
            SELECT id, status, terminal_reason, resume_metadata_json
            FROM agent_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (original_run_id,),
        ).fetchone()
        successor = conn.execute(
            """
            SELECT id, status, child_session_id, resume_metadata_json
            FROM agent_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (successor_run_id,),
        ).fetchone()
        session = conn.execute(
            """
            SELECT id, status, agent_run_id
            FROM sessions
            WHERE id = %s
            FOR UPDATE
            """,
            (child_session_id,),
        ).fetchone()
        if original is None or successor is None:
            raise ValueError("Daemon resume finalization requires existing run rows")

        original_metadata = _metadata(original["resume_metadata_json"])
        successor_metadata = _metadata(successor["resume_metadata_json"])
        consumed_by = original_metadata.get(_CONSUMED_BY_KEY)
        phase = successor_metadata.get(_RESUME_PHASE_KEY)
        if phase == "finalized" and consumed_by == successor_run_id:
            # Idempotent repeat: the handoff already committed. Later state
            # drift (expired session, reaper ownership) must not turn a
            # completed finalization into an error.
            subscriber_rows = conn.execute(
                """
                SELECT session_id
                FROM completion_subscribers
                WHERE completion_id = %s
                ORDER BY session_id
                """,
                (successor_run_id,),
            ).fetchall()
            return FinalizeDaemonResumeResult(
                original_run_id=original_run_id,
                successor_run_id=successor_run_id,
                child_session_id=child_session_id,
                subscriber_session_ids=tuple(str(row["session_id"]) for row in subscriber_rows),
                already_finalized=True,
            )

        if session is None:
            raise ValueError("Daemon resume finalization requires an existing session row")
        if original["status"] != "cancelled" or original["terminal_reason"] != "daemon_stop":
            raise ValueError("Daemon resume original is not parked")
        if str(successor["child_session_id"]) != child_session_id:
            raise ValueError("Daemon resume successor belongs to another session")
        if str(session["agent_run_id"]) != successor_run_id:
            raise ValueError("Daemon resume successor does not own the durable session")
        if session["status"] in {"expired", "deleted"}:
            raise ValueError("Daemon resume cannot activate a terminal session")
        if original_metadata.get("daemon_stop_orphan_reap_started_at"):
            raise ValueError("Daemon resume original is owned by the orphan reaper")
        resumed_from = successor_metadata.get("resumed_from_run_id")
        if resumed_from != original_run_id:
            raise ValueError("Daemon resume successor does not reference the original run")
        if consumed_by not in {None, successor_run_id}:
            raise ValueError("Daemon resume original was consumed by another successor")
        if phase not in {"launch_requested", "runtime_persisted", "finalized"}:
            raise ValueError(f"Daemon resume successor phase cannot finalize: {phase!r}")

        original_patch = {
            _CONSUMED_AT_KEY: original_metadata.get(_CONSUMED_AT_KEY) or now.isoformat(),
            _CONSUMED_BY_KEY: successor_run_id,
        }
        conn.execute(
            """
            UPDATE agent_runs
            SET resume_metadata_json =
                    COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb,
                updated_at = %s
            WHERE id = %s
              AND (
                    NOT (COALESCE(resume_metadata_json, '{}'::jsonb) ? %s)
                    OR resume_metadata_json ->> %s = %s
              )
            """,
            (
                dump_resume_metadata(original_patch),
                now,
                original_run_id,
                _CONSUMED_AT_KEY,
                _CONSUMED_BY_KEY,
                successor_run_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO completion_subscribers (completion_id, session_id)
            SELECT %s, session_id
            FROM completion_subscribers
            WHERE completion_id = %s
            ON CONFLICT (completion_id, session_id) DO NOTHING
            """,
            (successor_run_id, original_run_id),
        )
        conn.execute(
            "DELETE FROM completion_subscribers WHERE completion_id = %s",
            (original_run_id,),
        )
        conn.execute(
            """
            UPDATE sessions
            SET status = CASE WHEN status = 'paused' THEN 'active' ELSE status END,
                updated_at = %s
            WHERE id = %s
              AND agent_run_id = %s
            """,
            (now, child_session_id, successor_run_id),
        )
        successor_patch = {
            _RESUME_PHASE_KEY: "finalized",
            "daemon_stop_resume_finalized_at": successor_metadata.get(
                "daemon_stop_resume_finalized_at"
            )
            or now.isoformat(),
        }
        conn.execute(
            """
            UPDATE agent_runs
            SET resume_metadata_json =
                    COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb,
                updated_at = %s
            WHERE id = %s
              AND resume_metadata_json ->> %s
                    IN ('launch_requested', 'runtime_persisted', 'finalized')
            """,
            (
                dump_resume_metadata(successor_patch),
                now,
                successor_run_id,
                _RESUME_PHASE_KEY,
            ),
        )
        subscriber_rows = conn.execute(
            """
            SELECT session_id
            FROM completion_subscribers
            WHERE completion_id = %s
            ORDER BY session_id
            """,
            (successor_run_id,),
        ).fetchall()

    return FinalizeDaemonResumeResult(
        original_run_id=original_run_id,
        successor_run_id=successor_run_id,
        child_session_id=child_session_id,
        subscriber_session_ids=tuple(str(row["session_id"]) for row in subscriber_rows),
        already_finalized=False,
    )


def register_daemon_resume_waiter(
    db: HubDatabase,
    *,
    run_id: str,
    subscriber_session_id: str,
) -> DaemonResumeWaitTarget:
    """Follow a consumed chain and persist a waiter under each handoff fence."""
    current_run_id = run_id
    visited: set[str] = set()
    with db.transaction() as conn:
        while True:
            if current_run_id in visited:
                raise ValueError("Daemon resume successor chain contains a cycle")
            visited.add(current_run_id)
            _lock_resume_fence(conn, current_run_id)
            row = conn.execute(
                """
                SELECT id, status, terminal_reason, resume_metadata_json
                FROM agent_runs
                WHERE id = %s
                FOR UPDATE
                """,
                (current_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Agent run {current_run_id} not found")
            metadata = _metadata(row["resume_metadata_json"])
            successor = metadata.get(_CONSUMED_BY_KEY)
            if (
                row["status"] == "cancelled"
                and row["terminal_reason"] == "daemon_stop"
                and isinstance(successor, str)
                and successor
            ):
                current_run_id = successor
                continue

            recovery_pending = (
                row["status"] == "cancelled"
                and row["terminal_reason"] == "daemon_stop"
                and not metadata.get(_CONSUMED_AT_KEY)
            )
            terminal = row["status"] in {"success", "error", "timeout", "cancelled"}
            inserted = False
            if recovery_pending or not terminal:
                cursor = conn.execute(
                    """
                    INSERT INTO completion_subscribers (completion_id, session_id)
                    VALUES (%s, %s)
                    ON CONFLICT (completion_id, session_id) DO NOTHING
                    """,
                    (current_run_id, subscriber_session_id),
                )
                inserted = bool(cursor.rowcount)
            return DaemonResumeWaitTarget(
                run_id=current_run_id,
                status=str(row["status"]),
                recovery_pending=recovery_pending,
                subscriber_inserted=inserted,
            )


def rollback_prepared_daemon_resume(
    db: HubDatabase,
    *,
    original_run_id: str,
    successor_run_id: str,
    child_session_id: str,
) -> bool:
    """Roll back a prepared-only successor, restoring binding before deletion."""
    with db.transaction() as conn:
        _lock_resume_fence(conn, original_run_id)
        successor = conn.execute(
            """
            SELECT status, resume_metadata_json
            FROM agent_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (successor_run_id,),
        ).fetchone()
        if successor is None:
            return False
        metadata = _metadata(successor["resume_metadata_json"])
        if (
            successor["status"] != "pending"
            or metadata.get(_RESUME_PHASE_KEY) != "prepared"
            or metadata.get("resumed_from_run_id") != original_run_id
        ):
            return False
        restored = conn.execute(
            """
            UPDATE sessions
            SET agent_run_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND agent_run_id = %s
              AND status NOT IN ('expired', 'deleted')
            """,
            (original_run_id, child_session_id, successor_run_id),
        )
        if not restored.rowcount:
            return False
        conn.execute("DELETE FROM agent_runs WHERE id = %s", (successor_run_id,))
        return True


def expire_parked_daemon_session(
    db: HubDatabase,
    *,
    original_run_id: str,
    child_session_id: str,
) -> bool:
    """Expire a parked session only while the unconsumed original still owns it."""
    cursor = db.execute(
        """
        UPDATE sessions
        SET status = 'expired',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND agent_run_id = %s
          AND status IN ('active', 'paused', 'handoff_ready')
        """,
        (child_session_id, original_run_id),
    )
    return bool(cursor.rowcount)


def claim_daemon_stop_orphan_reap(
    db: HubDatabase,
    *,
    original_run_id: str,
    child_session_id: str,
) -> bool:
    """Fence an elapsed parked original against any later resume finalization."""
    now = utc_now()
    with db.transaction() as conn:
        _lock_resume_fence(conn, original_run_id)
        row = conn.execute(
            """
            SELECT ar.resume_metadata_json
            FROM agent_runs ar
            JOIN sessions s ON s.id = ar.child_session_id
            WHERE ar.id = %s
              AND ar.status = 'cancelled'
              AND ar.terminal_reason = 'daemon_stop'
              AND ar.child_session_id = %s
              AND s.agent_run_id = ar.id
            FOR UPDATE OF ar, s
            """,
            (original_run_id, child_session_id),
        ).fetchone()
        if row is None:
            return False
        metadata = _metadata(row["resume_metadata_json"])
        if metadata.get(_CONSUMED_AT_KEY) or metadata.get("daemon_stop_orphan_reaped_at"):
            return False
        conn.execute(
            """
            UPDATE agent_runs
            SET resume_metadata_json =
                    COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb,
                updated_at = %s
            WHERE id = %s
            """,
            (
                dump_resume_metadata(
                    {
                        "daemon_stop_orphan_reap_started_at": metadata.get(
                            "daemon_stop_orphan_reap_started_at"
                        )
                        or now.isoformat()
                    }
                ),
                now,
                original_run_id,
            ),
        )
        return True


def increment_daemon_resume_failure_count(
    db: HubDatabase,
    *,
    run_id: str,
) -> int:
    """Increment and return the durable same-session resume retry count."""
    with db.transaction() as conn:
        _lock_resume_fence(conn, run_id)
        row = conn.execute(
            """
            UPDATE agent_runs
            SET resume_metadata_json = jsonb_set(
                    COALESCE(resume_metadata_json, '{}'::jsonb),
                    '{daemon_stop_resume_failure_count}',
                    to_jsonb(
                        CASE
                            WHEN resume_metadata_json ->> 'daemon_stop_resume_failure_count'
                                 ~ '^[0-9]+$'
                            THEN (
                                resume_metadata_json ->> 'daemon_stop_resume_failure_count'
                            )::int
                            ELSE 0
                        END + 1
                    ),
                    true
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING resume_metadata_json ->> 'daemon_stop_resume_failure_count' AS count
            """,
            (run_id,),
        ).fetchone()
        return int(row["count"]) if row is not None else 0
